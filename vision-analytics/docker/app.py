import gradio as gr
import cv2
import time
import numpy as np
from fastapi import FastAPI
import uvicorn
from openai import OpenAI
import io
import base64
from PIL import Image
import logging
from datetime import datetime
import json
import os
import pathlib
from typing import Optional, Any, Dict

# ==================================
# Logging
# ==================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ==================================
# Global LLM client config (shared)
# ==================================
active_openai_api_key = os.getenv("OPENAI_API_KEY", "YOUR_API_KEY")
active_openai_api_base = os.getenv("OPENAI_API_BASE", "YOUR_API_BASE_URL")
client: Optional[OpenAI] = None
model_id: Optional[str] = None
current_config_status_message = "Config not yet initialized."


def initialize_or_update_openai_client(api_key_to_use: str, api_base_to_use: str, preferred_model_id: str = "") -> str:
    """Initialize OpenAI-compatible client using provided base URL and API key.
    If preferred_model_id is provided and exists, use it; else default to the first listed model.
    """
    global client, model_id, active_openai_api_key, active_openai_api_base, current_config_status_message

    _old_active_key, _old_active_base, _old_client, _old_model = (
        active_openai_api_key,
        active_openai_api_base,
        client,
        model_id,
    )

    try:
        if not api_base_to_use or not api_key_to_use:
            current_config_status_message = "API Base URL and API Key must be provided."
            logging.warning(current_config_status_message)
            return current_config_status_message

        temp_client = OpenAI(api_key=api_key_to_use, base_url=api_base_to_use)
        models_list = temp_client.models.list()
        if not models_list.data:
            current_config_status_message = f"No models found at base {api_base_to_use}."
            logging.warning(current_config_status_message)
            return current_config_status_message

        chosen = None
        if preferred_model_id:
            for m in models_list.data:
                if m.id == preferred_model_id:
                    chosen = m.id
                    break
        if not chosen:
            chosen = models_list.data[0].id

        client = temp_client
        model_id = chosen
        active_openai_api_key = api_key_to_use
        active_openai_api_base = api_base_to_use
        current_config_status_message = f"Configured. Using model: {model_id} (base: {active_openai_api_base})"
        logging.info(current_config_status_message)
        return current_config_status_message

    except Exception as e:
        client, model_id = _old_client, _old_model
        active_openai_api_key, active_openai_api_base = _old_active_key, _old_active_base
        current_config_status_message = f"Error updating configuration (URL: {api_base_to_use}): {e}."
        logging.error(current_config_status_message)
        return current_config_status_message


# Initialize once at import
current_config_status_message = initialize_or_update_openai_client(active_openai_api_key, active_openai_api_base)

# ==================================
# Shared helpers (Image + RTSP)
# ==================================

def encode_image_array_to_base64(image_array: np.ndarray) -> str:
    img = Image.fromarray(image_array)
    buff = io.BytesIO()
    img.save(buff, format="PNG")
    return base64.b64encode(buff.getvalue()).decode("utf-8")


def call_vision_language_model(image_base64: str, prompt: str = "Describe this image.") -> str:
    global client, model_id, current_config_status_message
    if not client or not model_id:
        return f"Vision model client not configured. Status: {current_config_status_message}"
    try:
        resp = client.chat.completions.create(
            model=model_id,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}},
                ],
            }],
            temperature=0.2,
        )
        return resp.choices[0].message.content
    except Exception as e:
        logging.exception("call_vision_language_model failed")
        return f"Error during API call: {e}"


# ==================================
# Tab 1: Image Understanding (UNCHANGED)
# ==================================
DEFAULT_IMAGE_PROMPT = "Describe this image in detail. Include key objects, colors, text, and notable actions."

def load_image_from_explorer(file_list):
    """Load an image from a file path selected in FileExplorer."""
    if not file_list:
        return None
    # Gradio FileExplorer returns a list of paths (even for single selection)
    path = file_list[0] if isinstance(file_list, list) else file_list
    try:
        # Open and convert to RGB (numpy array)
        img = Image.open(path).convert('RGB')
        return np.array(img)
    except Exception as e:
        logging.error(f"Error loading file from explorer {path}: {e}")
        return None

def analyze_uploaded_image(image_np: Optional[np.ndarray], prompt: str) -> str:
    if image_np is None:
        return "Please upload an image first."
    try:
        image_base64 = encode_image_array_to_base64(image_np)
        return call_vision_language_model(image_base64, prompt)
    except Exception as e:
        logging.error(f"analyze_uploaded_image error: {e}")
        return f"Error analyzing image: {e}"


# ==================================
# Tab 2: Video Understanding (UPDATED per request)
# ==================================
# Implements the provided snippet semantics: send a data: URL for the uploaded video to chat.completions
# and expose num_frames, fps, max_duration controls via extra_body.media_io_kwargs.

DEFAULT_VIDEO_PROMPT = "Did a car run a redlight in this video?"


def _normalize_video_path(video_input: Any) -> Optional[str]:
    if isinstance(video_input, str):
        return video_input
    if isinstance(video_input, dict) and "path" in video_input:
        return video_input["path"]
    return None


def video_to_data_url(path: str) -> str:
    # naive mime guess based on extension
    ext = pathlib.Path(path).suffix.lower()
    mime = "video/mp4"
    if ext in {".webm"}: mime = "video/webm"
    elif ext in {".mov"}: mime = "video/quicktime"
    elif ext in {".mkv"}: mime = "video/x-matroska"
    b64 = base64.b64encode(pathlib.Path(path).read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def analyze_video_with_vllm(
    video_input: Any,
    prompt: str,
    model_override: str,
    num_frames: int,
    fps: int,
    max_duration: int,
) -> str:
    global client, model_id
    if not client:
        return "Client not configured. Set API Base and Key in the configuration panel."

    vid_path = _normalize_video_path(video_input)
    if not vid_path or not os.path.exists(vid_path):
        return "Please upload a valid video file."

    try:
        data_url = video_to_data_url(vid_path)
        chosen_model = model_override.strip() or (model_id or "")
        if not chosen_model:
            return "No model selected. Provide a Model ID in the configuration or in this tab."

        resp = client.chat.completions.create(
            model=chosen_model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "video_url", "video_url": {"url": data_url}},
                ],
            }],
            temperature=0.1,
            extra_body={
                "media_io_kwargs": {
                    "video": {
                        "num_frames": int(num_frames),
                        "fps": int(fps),
                        "max_duration": int(max_duration),
                    }
                }
            },
        )
        return resp.choices[0].message.content
    except Exception as e:
        logging.exception("analyze_video_with_vllm failed")
        return f"Error during video analysis: {e}"


# ==================================
# Tab 3: RTSP Stream (UNCHANGED)
# ==================================
streaming_active: Dict[str, bool] = {}
DEFAULT_FRAME_PROMPT = (
    "You are a helpful assistant. Answer what is the vehicle type, color, and is the hood opened or closed. The type of vehicle you can select is: car, van, bus, minibus, tractor-trailer, dump truck, motorcycle. Answer in this format:\n<Type>car</Type>\n<Color>red</Color>\n<hoodOpen>yes</hoodOpen>"
)

def stop_streaming_generator_rtsp(url_key: str) -> str:
    if not url_key:
        return "Cannot stop: URL is empty."
    if url_key in streaming_active:
        streaming_active[url_key] = False
        logging.info(f"Stop signal sent for stream: {url_key}")
        return f"Stop signal sent for {url_key}."
    return f"Stream {url_key} was not active."


def rtsp_stream_generator(rtsp_url: str):
    if not rtsp_url:
        placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
        placeholder[:] = [100, 100, 100]
        cv2.putText(placeholder, "No RTSP URL Provided", (40, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 0), 2)
        yield placeholder
        return

    url_key = rtsp_url
    streaming_active[url_key] = True
    cap = None
    logging.info(f"Starting RTSP stream: {rtsp_url}")

    try:
        cap = cv2.VideoCapture(rtsp_url)
        if not cap.isOpened():
            logging.warning("RTSP offline or URL incorrect.")
            placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
            placeholder[:] = [128, 128, 128]
            cv2.putText(placeholder, "Stream Offline / Error", (50, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 0, 0), 2)
            streaming_active[url_key] = False
            yield placeholder
            return

        while streaming_active.get(url_key, False):
            ret, frame = cap.read()
            if not ret:
                logging.warning("Failed to read frame; retrying...")
                time.sleep(0.5)
                continue
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            yield frame_rgb
            time.sleep(0.03)

    except Exception as e:
        logging.error(f"RTSP generator error: {e}")
        placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
        placeholder[:] = [0, 0, 128]
        cv2.putText(placeholder, "Streaming Error", (80, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        yield placeholder

    finally:
        if cap and cap.isOpened():
            cap.release()
        streaming_active.pop(url_key, None)
        logging.info(f"RTSP stream ended: {rtsp_url}")
        tail = np.zeros((100, 100, 3), dtype=np.uint8)
        cv2.putText(tail, "Stopped", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        yield tail


def analyze_current_rtsp_frame(rtsp_url: str, prompt: str) -> str:
    if not rtsp_url:
        return "Please enter an RTSP URL."
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        return "Error: Could not open RTSP stream."
    try:
        ret, frame = cap.read()
        if not ret:
            return "Error: Could not read frame from RTSP stream."
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        b64 = encode_image_array_to_base64(frame_rgb)
        return call_vision_language_model(b64, prompt)
    finally:
        cap.release()


# ==================================
# Gradio UI
# ==================================
DEFAULT_SUMMARY_PROMPT = (
    "Summarize notable events succinctly."
)

def build_ui():
    with gr.Blocks(title="Visual Analytics Studio • Powered by HPE Private Cloud AI (PCAI)", analytics_enabled=False) as demo:
        gr.Markdown(
            """
# Visual Analytics Studio — Powered by HPE Private Cloud AI (PCAI)

Deliver rich, real‑time visual understanding across **Images**, **Videos**, and **RTSP live streams**—all running on your enterprise‑grade **[HPE Private Cloud AI](https://www.hpe.com/us/en/private-cloud-ai.html)** platform.

**Recommended model:** `Qwen/Qwen2.5-VL-32B-Instruct-AWQ` deployed via PCAI for the best experience.

**What you can do here**
- **Image Understanding:** Select/Upload an image, preview it, and analyze with your prompt.
- **Video Understanding:** Select/Upload a video and control extraction knobs (**num_frames**, **fps**, **max_duration**). The app passes your video to PCAI's served endpoint using a secure data URL and vLLM media I/O hints.
- **RTSP Stream:** View and analyze live network streams; capture a frame and ask questions in natural language.

**Getting started**
1. Open **Vision Model Configuration** and paste your PCAI endpoint and API token.
2. (Optional) Set **Preferred Model ID** to `Qwen/Qwen2.5-VL-32B-Instruct-AWQ`.
3. Use the tabs below to run analyses.
            """
        )

        with gr.Accordion("Vision Model Configuration", open=False):
            api_key_input = gr.Textbox(label="API Key", value=active_openai_api_key, type="password", interactive=True)
            api_base_input = gr.Textbox(label="API Base URL", value=active_openai_api_base, interactive=True)
            preferred_model_input = gr.Textbox(label="Preferred Model ID (optional)")
            config_status_out = gr.Textbox(label="Config Status", value=current_config_status_message, interactive=False, lines=2)
            apply_config_btn = gr.Button("Apply Configuration")

            apply_config_btn.click(
                fn=initialize_or_update_openai_client,
                inputs=[api_key_input, api_base_input, preferred_model_input],
                outputs=[config_status_out],
            )

        gr.Markdown("---")

        with gr.Tabs(selected="imageUnderstanding"):
            # ---------------- Tab 1: Image Understanding (UPDATED)
            with gr.TabItem("Image Understanding", id="imageUnderstanding"):
                # State to hold the currently selected image (numpy array)
                current_image_state = gr.State()

                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("### 1. Select Input")
                        image_upload = gr.Image(label="Upload Image", type="numpy", sources=["upload", "clipboard", "webcam"])
                        file_explorer = gr.FileExplorer(
                            glob="**/*.[pj][np]g",  # matches .png, .jpg, .jpeg
                            root_dir="./assets",
                            file_count="single",
                            label="Or Select An Image File"
                        )

                    with gr.Column(scale=2):
                        gr.Markdown("### 2. Preview")
                        img_preview = gr.Image(label="Selected Image", interactive=False)

                image_prompt = gr.Textbox(label="Prompt", value=DEFAULT_IMAGE_PROMPT, lines=3)
                analyze_image_btn = gr.Button("Analyze Image", variant="primary")
                image_llm_output = gr.Textbox(label="LLM Response", lines=10)

                # Event Wiring
                
                # 1. Handle Image Upload
                image_upload.change(
                    fn=lambda x: x,
                    inputs=[image_upload],
                    outputs=[current_image_state]
                ).then(
                    fn=lambda x: x,
                    inputs=[current_image_state],
                    outputs=[img_preview]
                )

                # 2. Handle File Explorer Selection
                file_explorer.change(
                    fn=load_image_from_explorer,
                    inputs=[file_explorer],
                    outputs=[current_image_state]
                ).then(
                    fn=lambda x: x,
                    inputs=[current_image_state],
                    outputs=[img_preview]
                )

                # 3. Handle Analysis
                analyze_image_btn.click(
                    fn=analyze_uploaded_image,
                    inputs=[current_image_state, image_prompt],
                    outputs=[image_llm_output],
                )

            # ---------------- Tab 2: Video Understanding (UPDATED)
            with gr.TabItem("Video Understanding"):
                gr.Markdown("Upload a video, tweak *num_frames / fps / max_duration*, and run analysis. The video will be passed to the model as a data: URL.")
                video_in = gr.Video(label="Upload Video (mp4/webm/mov/mkv)",height=480)
                video_prompt = gr.Textbox(label="Prompt", value=DEFAULT_VIDEO_PROMPT, lines=3)
                with gr.Row():
                    model_override = gr.Textbox(label="Model ID (override, optional)", placeholder="e.g. Qwen/Qwen2.5-VL-32B-Instruct-AWQ")
                with gr.Row():
                    num_frames = gr.Slider(1, 10000, value=7860, step=1, label="num_frames (hard cap on extracted frames)")
                with gr.Row():
                    fps = gr.Slider(1, 60, value=1, step=1, label="fps (target extraction FPS)")
                    max_duration = gr.Slider(1, 100000, value=10000, step=1, label="max_duration (seconds cap)")
                run_video_btn = gr.Button("Analyze Video")
                video_llm_output = gr.Textbox(label="LLM Response", lines=12)

                run_video_btn.click(
                    fn=analyze_video_with_vllm,
                    inputs=[video_in, video_prompt, model_override, num_frames, fps, max_duration],
                    outputs=[video_llm_output],
                )

            # ---------------- Tab 3: RTSP Stream (UNCHANGED)
            with gr.TabItem("RTSP Stream"):
                rtsp_url_input = gr.Textbox(label="RTSP URL", value="http://monumentcam.kdhnc.com/mjpg/video.mjpg?timestamp=1717171717",placeholder="http://monumentcam.kdhnc.com/mjpg/video.mjpg?timestamp=1717171717")
                with gr.Row():
                    start_btn = gr.Button("Start Stream")
                    stop_btn = gr.Button("Stop Stream")
                rtsp_image = gr.Image(label="Live RTSP Stream", interactive=False, type="numpy", height=480)
                rtsp_status = gr.Textbox(label="Status", value="Stream not started.", interactive=False)
                rtsp_prompt = gr.Textbox(label="Prompt for Frame Analysis", value=DEFAULT_FRAME_PROMPT, lines=4)
                analyze_rtsp_btn = gr.Button("Analyze Current Frame")
                rtsp_llm_out = gr.Textbox(label="LLM Response", lines=8)

                start_btn.click(
                    fn=rtsp_stream_generator,
                    inputs=[rtsp_url_input],
                    outputs=[rtsp_image],
                ).then(fn=lambda: "Streaming started (or attempting)...", outputs=[rtsp_status])

                stop_btn.click(
                    fn=stop_streaming_generator_rtsp,
                    inputs=[rtsp_url_input],
                    outputs=[rtsp_status],
                )

                analyze_rtsp_btn.click(
                    fn=analyze_current_rtsp_frame,
                    inputs=[rtsp_url_input, rtsp_prompt],
                    outputs=[rtsp_llm_out],
                )

        demo.queue(default_concurrency_limit=8)
    return demo


# ==================================
# Main execution
# ==================================
demo = build_ui()

if __name__ == "__main__":
    if active_openai_api_key == "YOUR_API_KEY" or active_openai_api_base == "YOUR_API_BASE_URL":
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("!!! IMPORTANT: Set OPENAI_API_KEY and OPENAI_API_BASE (or use UI config). !!!")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    
    demo.launch(server_name="0.0.0.0", server_port=7860)