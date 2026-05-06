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
import re
from datetime import datetime
import json
import tempfile
import os
from gradio.data_classes import FileData # <--- IMPORT THIS

# --- Configure Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# --- Helper function for filename sanitization ---
def sanitize_filename_component(text: str) -> str:
    if not text:
        return "unknown_stream"
    text = re.sub(r'^https?://', '', text)
    text = re.sub(r'[/:\\?%*&="\'<>| \t\n\r\f\v]', '_', text)
    text = re.sub(r'_+', '_', text)
    text = text.strip('_')
    max_len = 50
    if len(text) > max_len:
        text = text[:max_len]
    return text if text else "sanitized_url"

# --- MJPEG Streaming Logic ---
streaming_active = {}

def stop_streaming_generator(url_key: str) -> str:
    if not url_key:
        return "Cannot stop: URL key is empty."
    if url_key in streaming_active:
        streaming_active[url_key] = False
        logging.info(f"Stop signal sent for stream: {url_key}")
        return f"Streaming stop signal sent for {url_key}. Loop will terminate shortly."
    return f"Stream {url_key} was not active or key not found."


def mjpeg_stream_generator(mjpeg_url: str):
    if not mjpeg_url: # Handle empty URL input
        placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
        placeholder[:] = [100, 100, 100] # Dark gray
        cv2.putText(placeholder, "No URL Provided", (50, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255,255,0), 2)
        yield placeholder
        return

    url_key = mjpeg_url
    streaming_active[url_key] = True
    cap = None
    logging.info(f"Starting stream for {mjpeg_url}")

    try:
        cap = cv2.VideoCapture(mjpeg_url)

        if not cap.isOpened():
            logging.warning(f"Stream {mjpeg_url} offline or URL incorrect.")
            placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
            placeholder[:] = [128, 128, 128] # Gray
            cv2.putText(placeholder, "Stream Offline / Error", (50, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255,0,0), 2)
            streaming_active[url_key] = False
            yield placeholder
            return

        while streaming_active.get(url_key, False):
            ret, frame = cap.read()
            if not ret:
                logging.warning(f"Failed to read frame from {mjpeg_url}, attempting to reconnect.")
                if cap: cap.release()
                time.sleep(2)
                cap = cv2.VideoCapture(mjpeg_url)
                if not cap.isOpened():
                    logging.error(f"Failed to reconnect to {mjpeg_url}. Stopping stream.")
                    streaming_active[url_key] = False
                    placeholder_disconnected = np.zeros((480, 640, 3), dtype=np.uint8)
                    placeholder_disconnected[:] = [128, 0, 0] # Dark Red
                    cv2.putText(placeholder_disconnected, "Stream Disconnected", (50, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255,255,255), 2)
                    yield placeholder_disconnected
                    break
                continue

            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            yield frame_rgb
            time.sleep(0.03)
    except Exception as e:
        logging.error(f"Error in mjpeg_stream_generator for {mjpeg_url}: {e}")
        placeholder_error = np.zeros((480, 640, 3), dtype=np.uint8)
        placeholder_error[:] = [0, 0, 128] # Dark Blue for general error
        cv2.putText(placeholder_error, "Streaming Error", (50, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255,255,255), 2)
        yield placeholder_error
    finally:
        if cap and cap.isOpened():
            cap.release()
        streaming_active.pop(url_key, None)
        logging.info(f"Stream ended for {mjpeg_url}")
        final_frame = np.zeros((100,100,3), dtype=np.uint8)
        cv2.putText(final_frame, "Stopped", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255,255,255), 1)
        yield final_frame

# --- Vision Model Integration
active_openai_api_key = "YOUR_API_KEY" # Replace with your actual key
active_openai_api_base = "YOUR_API_BASE_URL" # Replace with your actual base URL
client = None
model = None
current_config_status_message = "Config not yet initialized."

def initialize_or_update_openai_client(api_key_to_use: str, api_base_to_use: str) -> str:
    global client, model, active_openai_api_key, active_openai_api_base, current_config_status_message
    _old_active_key, _old_active_base, _old_client, _old_model = active_openai_api_key, active_openai_api_base, client, model
    try:
        logging.info(f"Attempting to initialize/update OpenAI client with API Base: {api_base_to_use}")
        if not api_base_to_use or not api_key_to_use:
            current_config_status_message = "API Base URL and API Key must be provided."
            logging.warning(current_config_status_message); return current_config_status_message
        temp_client = OpenAI(api_key=api_key_to_use, base_url=api_base_to_use)
        models_list = temp_client.models.list()
        if models_list.data:
            client, model, active_openai_api_key, active_openai_api_base = temp_client, models_list.data[0].id, api_key_to_use, api_base_to_use
            current_config_status_message = f"OpenAI client configured. Using model: {model}"
            logging.info(current_config_status_message)
        else:
            client, model, active_openai_api_key, active_openai_api_base = _old_client, _old_model, _old_active_key, _old_active_base
            current_config_status_message = f"Update Failed: No models found with new config (URL: {api_base_to_use}). Previous config (URL: {_old_active_base}, Model: {_old_model}) remains active." if _old_client else f"Configuration Problem: No models found with API Base: {api_base_to_use}."
            logging.warning(current_config_status_message)
        return current_config_status_message
    except Exception as e:
        client, model, active_openai_api_key, active_openai_api_base = _old_client, _old_model, _old_active_key, _old_active_base
        current_config_status_message = f"Error updating configuration (URL: {api_base_to_use}): {e}. Previous config (URL: {_old_active_base}, Model: {_old_model}) remains active." if _old_client else f"Configuration Error (URL: {api_base_to_use}): {e}."
        logging.error(current_config_status_message)
        return current_config_status_message
current_config_status_message = initialize_or_update_openai_client(active_openai_api_key, active_openai_api_base)

def encode_image_array_to_base64(image_array: np.ndarray) -> str:
    img = Image.fromarray(image_array); buffered = io.BytesIO(); img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")

def call_vision_language_model(image_base64: str, prompt: str = "Describe this image.") -> str:
    global client, model, current_config_status_message
    if not client or not model:
        error_msg = f"Vision model client not configured. Status: {current_config_status_message}"
        logging.error(error_msg); return error_msg
    try:
        logging.info(f"Calling vision model {model} at {active_openai_api_base} with prompt: '{prompt[:100]}...'")
        response = client.chat.completions.create(
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}}]}],
            model=model, temperature=0.3)
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"Error calling vision model: {e}"); return f"Error during API call: {e}"

# --- Parsing Logic --- ( 그대로 유지 )
def parse_vehicle_info(text: str) -> dict:
    timestamp = datetime.utcnow().isoformat() + "Z"
    type_m = re.search(r"<Type>(.*?)</Type>", text, re.I | re.S)
    color_m = re.search(r"<Color>(.*?)</Color>", text, re.I | re.S)
    hood_m = re.search(r"<hoodOpen>(.*?)</hoodOpen>", text, re.I | re.S)
    e_type = type_m.group(1).strip().lower() if type_m and type_m.group(1) else None
    e_color = color_m.group(1).strip().lower() if color_m and color_m.group(1) else None
    e_hood_raw = hood_m.group(1).strip().lower() if hood_m and hood_m.group(1) else None
    e_hood = e_hood_raw if e_hood_raw in ['yes', 'no'] else None
    if e_hood_raw and e_hood_raw not in ['yes', 'no']: logging.warning(f"Invalid hoodOpen: '{e_hood_raw}'. Setting to None.")
    parsed = {"type": e_type, "color": e_color, "hoodOpen": e_hood, "timestamp": timestamp, "raw_output": text.strip()}
    logging.info(f"Parsed vehicle info: {parsed}"); return parsed

def process_frame_and_parse(mjpeg_url: str, prompt: str) -> tuple[str, dict]:
    if not mjpeg_url: err_msg = "Error: MJPEG URL empty."; logging.error(err_msg); return err_msg, parse_vehicle_info(err_msg)
    logging.info(f"process_frame_and_parse for URL: {mjpeg_url} with prompt: '{prompt[:100]}...'")
    cap = cv2.VideoCapture(mjpeg_url)
    if not cap.isOpened(): err_msg = f"Error: Could not open stream: {mjpeg_url}"; logging.error(err_msg); cap.release(); return err_msg, parse_vehicle_info(err_msg)
    ret, frame = cap.read(); cap.release()
    if not ret: err_msg = f"Error: Could not read frame: {mjpeg_url}"; logging.error(err_msg); return err_msg, parse_vehicle_info(err_msg)
    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB); image_base64 = encode_image_array_to_base64(frame_rgb)
    raw_output = call_vision_language_model(image_base64, prompt); parsed_data = parse_vehicle_info(raw_output)
    return raw_output, parsed_data

# --- MODIFIED Function for DownloadButton ---
def prepare_json_for_download(parsed_data: dict, stream_url: str): # Return type hint
    if not parsed_data or not isinstance(parsed_data, dict) or not parsed_data.get("timestamp"):
        logging.warning("No valid parsed data to save for download.")
        return None

    temp_file_path = None
    try:
        sanitized_url_part = sanitize_filename_component(stream_url)
        timestamp_iso = parsed_data.get("timestamp", datetime.utcnow().isoformat() + "Z")
        dt_object = datetime.fromisoformat(timestamp_iso.replace('Z', '+00:00'))
        time_filename_part = dt_object.strftime('%Y%m%d_%H%M%S')
        
        desired_filename = f"{sanitized_url_part}_{time_filename_part}.json"
        desired_filename = os.path.basename(desired_filename)[:200] # Sanitize and shorten
        
        json_string = json.dumps(parsed_data, indent=4)
        json_bytes = json_string.encode('utf-8')
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="wb") as temp_file_obj:
            temp_file_path = temp_file_obj.name
            temp_file_obj.write(json_bytes)

        logging.info(f"Data written to temporary file: {temp_file_path}. Offering as '{desired_filename}'.")
        
        # Return a FileData object
        return temp_file_path

    except Exception as e:
        logging.error(f"Error preparing JSON for download: {e}")
        if temp_file_path and os.path.exists(temp_file_path):
            try: os.remove(temp_file_path); logging.info(f"Cleaned up temp file: {temp_file_path}")
            except Exception as e_del: logging.error(f"Error deleting temp file {temp_file_path}: {e_del}")
        return None


# --- Gradio UI --- ( 그대로 유지, except ensure outputs=[save_json_btn] is correct )
structured_prompt_template = """You are a helpful assistant. Answer what is the vehicle type, color, and is the hood opened or closed.
The type of vehicle you can select is: car, van, bus, minibus, tractor-trailer, dump truck, motorcycle. Answer in this format:
<Type>car</Type>
<Color>red</Color>
<hoodOpen>yes</hoodOpen>"""

def build_quad_mjpeg_player_ui():
    with gr.Blocks(title="Quad MJPEG Stream Player") as demo:
        gr.Markdown("# Quad MJPEG Stream Player with Vision AI")

        with gr.Accordion("Vision Model Configuration", open=False):
            api_key_input = gr.Textbox(label="API Key", value=active_openai_api_key, type="password", interactive=True)
            api_base_input = gr.Textbox(label="API Base URL", value=active_openai_api_base, interactive=True)
            config_status_out = gr.Textbox(label="Config Status", value=current_config_status_message, interactive=False, lines=2)
            apply_config_btn = gr.Button("Apply Configuration")

            apply_config_btn.click(
                fn=initialize_or_update_openai_client,
                inputs=[api_key_input, api_base_input],
                outputs=[config_status_out]
            )

        gr.Markdown("---")

        with gr.Tabs():
            for i in range(1, 5):
                with gr.TabItem(f"Stream {i}"):
                    url_input = gr.Textbox(
                        label=f"Stream {i} URL (MJPEG)",
                        value=(
                            "http://monumentcam.kdhnc.com/mjpg/video.mjpg?timestamp=1717171717" if i == 1 else
                            "http://pendelcam.kip.uni-heidelberg.de/mjpg/video.mjpg" if i == 2 else ""
                        ),
                        interactive=True,
                    )
                    with gr.Row():
                        start_btn = gr.Button(f"Start Stream {i}")
                        stop_btn = gr.Button(f"Stop Stream {i}")

                    img_display = gr.Image(label=f"Live Stream {i}", interactive=False, type="numpy", height=480)
                    status_out = gr.Textbox(label=f"Stream Status {i}", interactive=False, value="Stream not started.")

                    prompt_input = gr.Textbox(
                        label=f"Prompt for Vision Model {i}", 
                        value=structured_prompt_template, 
                        interactive=True, 
                        lines=7
                    )
                    process_btn = gr.Button(f"Analyze Current Frame {i}")
                    
                    raw_model_output_txt = gr.Textbox(label=f"Raw Model Output {i}", interactive=False, lines=6)
                    
                    with gr.Row():
                        parsed_json_output = gr.JSON(label=f"Parsed JSON Output {i}", scale=3)
                        save_json_btn = gr.DownloadButton(label=f"Save Parsed JSON {i}", scale=1)
                    
                    start_btn.click(
                        fn=mjpeg_stream_generator,
                        inputs=[url_input],
                        outputs=[img_display]
                    ).then(
                        fn=lambda: "Streaming started (or attempting)...", 
                        outputs=[status_out]
                    )
                    
                    stop_btn.click(
                        fn=stop_streaming_generator,
                        inputs=[url_input],
                        outputs=[status_out]
                    )
                    
                    process_btn.click(
                        fn=process_frame_and_parse,
                        inputs=[url_input, prompt_input],
                        outputs=[raw_model_output_txt, parsed_json_output]
                    )

                    save_json_btn.click(
                        fn=prepare_json_for_download,
                        inputs=[parsed_json_output, url_input],
                        outputs=[save_json_btn] # Output updates the DownloadButton's value
                    )

    demo.queue(default_concurrency_limit=10)
    return demo

# --- FastAPI App and Server --- ( 그대로 유지 )
app = FastAPI()

def main_server():
    gradio_ui = build_quad_mjpeg_player_ui()
    global app
    app = gr.mount_gradio_app(app, gradio_ui, path="/")
    uvicorn_config = uvicorn.Config(app, host="0.0.0.0", port=7860, workers=1)
    server = uvicorn.Server(uvicorn_config)
    server.run()

if __name__ == "__main__":
    if active_openai_api_key == "YOUR_API_KEY" or active_openai_api_base == "YOUR_API_BASE_URL":
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("!!! IMPORTANT: Please replace 'YOUR_API_KEY' and 'YOUR_API_BASE_URL'     !!!")
        print("!!! in the script with your actual OpenAI credentials before running.      !!!")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    main_server()
