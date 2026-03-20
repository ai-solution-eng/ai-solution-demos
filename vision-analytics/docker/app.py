import gradio as gr
import cv2
import time
import numpy as np
from fastapi import FastAPI
import uvicorn
from openai import OpenAI
import httpx
import io
import base64
from PIL import Image
import logging
from datetime import datetime
import json
import os
import pathlib
from typing import Optional, Any, Dict, Tuple
from kubernetes import client as k8s_client, config as k8s_config
import shutil
import tempfile
import hashlib
import subprocess

# ==================================
# RTSP Server State
# ==================================
rtsp_server_process = None
ffmpeg_stream_process = None
LOCAL_RTSP_URL = "rtsp://127.0.0.1:8554/live"
DEFAULT_RTSP_URL = "http://monumentcam.kdhnc.com/mjpg/video.mjpg?timestamp=1717171717"

# ==================================
# Logging
# ==================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ==================================
# Global LLM client config (shared)
# ==================================
CONFIG_DIR = "/app/config"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
active_openai_api_key = os.getenv("OPENAI_API_KEY", "YOUR_API_KEY")
active_openai_api_base = os.getenv("OPENAI_API_BASE", "YOUR_API_BASE_URL")
active_ignore_tls = os.getenv("IGNORE_TLS", "false").lower() == "true"
active_root_dir = os.getenv("ROOT_DIR", "/mnt").rstrip("/")
client: Optional[OpenAI] = None
model_id: Optional[str] = None
current_config_status_message = "Config not yet initialized."

def load_config_from_disk():
    """Load configuration from disk if available, overriding env vars."""
    global active_openai_api_key, active_openai_api_base, model_id, active_ignore_tls, active_root_dir
    # Try local config first for compatibility, then persistent config
    config_paths = ["config.json", CONFIG_FILE]
    
    for path in config_paths:
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    config = json.load(f)
                    active_openai_api_key = config.get("api_key", active_openai_api_key)
                    active_openai_api_base = config.get("api_base", active_openai_api_base)
                    model_id = config.get("model_id", model_id)
                    active_ignore_tls = config.get("ignore_tls", active_ignore_tls)
                    active_root_dir = config.get("root_dir", active_root_dir).rstrip("/")
                    logging.info(f"Loaded config from {path}")
                    break # Stop after first successful load
            except Exception as e:
                logging.error(f"Failed to load config file {path}: {e}")

def save_config_to_disk():
    """Save current configuration to disk."""
    config = {
        "api_key": active_openai_api_key,
        "api_base": active_openai_api_base,
        "model_id": model_id,
        "ignore_tls": active_ignore_tls,
        "root_dir": active_root_dir
    }
    try:
        # Ensure directory exists for persistent storage
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        logging.info(f"Saved config to {CONFIG_FILE}")
    except Exception as e:
        logging.error(f"Failed to save config file: {e}")


def initialize_or_update_openai_client(api_key_to_use: str, api_base_to_use: str, preferred_model_id: str = "", ignore_tls: bool = False, root_dir: str = "/mnt/") -> Tuple[str, str]:
    """
    Initialise OpenAI-compatible client using provided base URL and API key.
    
    Args:
        api_key_to_use: The API key for authentication.
        api_base_to_use: The base URL for the API.
        preferred_model_id: Preferred model to use if available.
        ignore_tls: Whether to ignore TLS certificate verification.
        root_dir: The root directory for file browsing.
        
    Returns:
        A tuple containing the status message and the active root directory.
    """
    global client, model_id, active_openai_api_key, active_openai_api_base, active_ignore_tls, active_root_dir, current_config_status_message

    _old_active_key, _old_active_base, _old_client, _old_model = (
        active_openai_api_key,
        active_openai_api_base,
        client,
        model_id,
    )

    try:
        # Check if URL/Key are configured or still using placeholders
        if not api_base_to_use or not api_key_to_use or \
           api_base_to_use == "YOUR_API_BASE_URL" or api_key_to_use == "YOUR_API_KEY":
            current_config_status_message = "API configuration required. Please set the API Base URL and Key in the Settings tab."
            logging.info(current_config_status_message)
            return current_config_status_message, active_root_dir

        # Adjust API Base URL if /v1 is missing
        if api_base_to_use and not api_base_to_use.rstrip("/").endswith("/v1"):
            api_base_to_use = api_base_to_use.rstrip("/") + "/v1"

        http_client = httpx.Client(verify=not ignore_tls)
        temp_client = OpenAI(api_key=api_key_to_use, base_url=api_base_to_use, http_client=http_client)
        models_list = temp_client.models.list()
        if not models_list.data:
            current_config_status_message = f"No models found at base {api_base_to_use}."
            logging.warning(current_config_status_message)
            return current_config_status_message, active_root_dir

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
        
        # Enforce sandbox: ensure root_dir is within /mnt and normalised
        root_dir = os.path.normpath(root_dir)
        if not root_dir.startswith("/mnt"):
            root_dir = os.path.join("/mnt", root_dir.lstrip("/"))
        root_dir = os.path.normpath(root_dir)
        
        active_openai_api_key = api_key_to_use
        active_openai_api_base = api_base_to_use
        active_ignore_tls = ignore_tls
        active_root_dir = root_dir
        
        logging.info(f"Configuration updated. Root directory set to: {active_root_dir}")
        
        # Save to disk on successful update
        save_config_to_disk()
        
        current_config_status_message = f"Configured. Using model: {model_id} (base: {active_openai_api_base})"
        logging.info(current_config_status_message)
        return current_config_status_message, active_root_dir

    except Exception as e:
        client, model_id = _old_client, _old_model
        active_openai_api_key, active_openai_api_base = _old_active_key, _old_active_base
        current_config_status_message = f"Error updating configuration (URL: {api_base_to_use}): {e}."
        logging.error(current_config_status_message)
        return current_config_status_message, active_root_dir

def get_inference_services_urls():
    """List InferenceServices from Kubernetes and return their API Base URLs."""
    urls = []
    try:
        # Check if running inside Kubernetes
        if os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token"):
            k8s_config.load_incluster_config()
        else:
            k8s_config.load_kube_config()
            
        api = k8s_client.CustomObjectsApi()
        
        # List InferenceServices across all namespaces
        isvcs = api.list_custom_object_for_all_namespaces(
            group="serving.kserve.io",
            version="v1beta1",
            resource_plural="inferenceservices"
        )
        
        for item in isvcs.get("items", []):
            try:
                metadata = item.get("metadata", {})
                name = metadata.get("name", "unknown")
                namespace = metadata.get("namespace", "unknown")
                
                status = item.get("status", {})
                # Try to get the URL from various possible locations in status
                # prioritize 'url' (usually external) then 'address.url' (usually internal)
                url = status.get("url") or status.get("address", {}).get("url")
                
                if url:
                    # Add /v1 if missing
                    full_url = url if url.endswith("/v1") else f"{url.rstrip('/')}/v1"
                    urls.append(full_url)
                    logging.info(f"Found InferenceService: {name}.{namespace} -> {full_url}")
                else:
                    logging.info(f"Skipping InferenceService {name}.{namespace}: No URL found in status.")
            except Exception as item_err:
                logging.warning(f"Error processing InferenceService item: {item_err}")
            
    except Exception as e:
        logging.warning(f"Could not fetch InferenceServices from K8s: {e}")
    
    # Fallback to current active base and env var
    if active_openai_api_base and active_openai_api_base != "YOUR_API_BASE_URL":
        urls.append(active_openai_api_base)
    
    env_base = os.getenv("OPENAI_API_BASE")
    if env_base and env_base not in urls:
        urls.append(env_base)
        
    return sorted(list(set(urls)))


# Initialize once at import
load_config_from_disk()
current_config_status_message, active_root_dir = initialize_or_update_openai_client(active_openai_api_key, active_openai_api_base, model_id, active_ignore_tls, active_root_dir)

# ... (helpers) ...

def get_current_config_ui():
    """Helper to populate UI with current config."""
    urls = get_inference_services_urls()
    is_valid = active_openai_api_base and active_openai_api_base != "YOUR_API_BASE_URL"
    
    alert_html = ""
    if not is_valid:
        alert_html = '<div id="config-alert" style="color: #b91c1c; font-weight: bold; background-color: #fef2f2; padding: 15px; border: 1px solid #f87171; border-radius: 8px; margin-bottom: 20px;">' \
                     '⚠️ <strong>Configuration Required:</strong> The API Base URL is not set. Please go to the <strong>Settings</strong> tab and configure your PCAI endpoint.' \
                     '</div>'
    
    settings_label = "Settings ❌" if not is_valid else "Settings"
    
    return (
        active_openai_api_key, 
        gr.update(value=active_openai_api_base, choices=urls), 
        model_id or "", 
        active_ignore_tls, 
        active_root_dir, 
        current_config_status_message,
        gr.update(value=alert_html, visible=not is_valid),
        gr.update(label=settings_label),
        gr.update(label=f"Image files in {active_root_dir}", root_dir=active_root_dir),
        gr.update(label=f"Video files in {active_root_dir}", root_dir=active_root_dir),
        gr.update(root_dir="/mnt" if os.path.exists("/mnt") else os.getcwd())
    )

def get_ui_validation_updates():
    """Helper for real-time UI validation updates."""
    is_valid = active_openai_api_base and active_openai_api_base != "YOUR_API_BASE_URL"
    alert_html = ""
    if not is_valid:
        alert_html = '<div id="config-alert" style="color: #b91c1c; font-weight: bold; background-color: #fef2f2; padding: 15px; border: 1px solid #f87171; border-radius: 8px; margin-bottom: 20px;">' \
                     '⚠️ <strong>Configuration Required:</strong> The API Base URL is not set. Please go to the <strong>Settings</strong> tab and configure your PCAI endpoint.' \
                     '</div>'
    
    settings_label = "Settings ❌" if not is_valid else "Settings"
    return gr.update(value=alert_html, visible=not is_valid), gr.update(label=settings_label)

#
# Shared helpers
#

def render_llm_response(response_text: str) -> str:
    """
    Helper to render LLM response to correct format
    """
    try:
        json_content = json.loads(response_text)
        return gr.JSON(value=json_content, visible=True), gr.Markdown(value="", visible=False)
    except json.JSONDecodeError:
        return gr.JSON(value=None, visible=False), gr.Markdown(value=response_text, visible=True)
        
def get_prompt_from_file(source_path: str, default_prompt: str) -> str:
    """
    Helper to get prompt from file, if it exists, otherwise return default prompt.
    
    Args:
        source_path: Path to the source file.
        default_prompt: The default prompt to use if no file exists.
        
    Returns:
        The content of the prompt file or the default prompt.
    """
    prompt_path = os.path.splitext(source_path)[0] + ".prompt"
    if os.path.exists(prompt_path):
        try:
            with open(prompt_path, "r", encoding="utf-8") as pf:
                default_prompt = pf.read().strip()
        except Exception as e:
            logging.warning(f"Failed to read prompt {prompt_path}: {e}")
    return default_prompt

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
            messages=[
                {
                    "role": "system",
                    "content": [
                        {"type": "text", "text": """
                        You are a scene-description engine for images.
                        Describe the visible content in 3-6 sentences.
                        Start with the overall setting and main subjects, then mention important objects, actions, and relationships.
                        Include concrete visual details: colours, approximate counts, positions ("left", "centre", "background"), and any visible text.
                        Do not infer backstory, emotions, or intentions unless they are visually obvious.
                        Avoid repeating the same detail with different wording.
                        Do not invent objects that are not clearly visible.
                        """}
                    ]
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}},
                    ] 
                }
            ],
            temperature=0.2,
        )
        return resp.choices[0].message.content
    except Exception as e:
        logging.exception("call_vision_language_model failed")
        return f"Error during API call: {e}"


# -----------------------------------------------------------------------------
# Tab 1: Image Understanding
# -----------------------------------------------------------------------------
DEFAULT_IMAGE_PROMPT = "Describe this image in detail. Include key objects, colours, text, and notable actions."

def load_image_from_explorer(file_list):
    """Load an image from a file path selected in FileExplorer."""
    if not file_list:
        return None, DEFAULT_IMAGE_PROMPT
    # Gradio FileExplorer returns a list of paths (even for single selection)
    path = file_list[0] if isinstance(file_list, list) else file_list
    try:
        # Open and convert to RGB (numpy array)
        img = Image.open(path).convert('RGB')
        return np.array(img), get_prompt_from_file(path, DEFAULT_IMAGE_PROMPT)
    except Exception as e:
        logging.error(f"Error loading file from explorer {path}: {e}")
        return None, DEFAULT_IMAGE_PROMPT


def analyse_uploaded_image(image_np: Optional[np.ndarray], prompt: str) -> str:
    """
    Analyse an uploaded image using the Vision Language Model.
    
    Args:
        image_np: The image as a numpy array.
        prompt: The user prompt for analysis.
        
    Returns:
        The text response from the model.
    """
    if image_np is None:
        return "Please upload an image first."
    try:
        image_base64 = encode_image_array_to_base64(image_np)
        return call_vision_language_model(image_base64, prompt)
    except Exception as e:
        logging.error(f"analyse_uploaded_image error: {e}")
        return f"Error analysing image: {e}"


# -----------------------------------------------------------------------------
# Tab 2: Video Understanding
# -----------------------------------------------------------------------------
DEFAULT_VIDEO_PROMPT = "Did a car run a redlight in this video?"

def load_video_from_explorer(file_list):
    """Return the file path for the selected video."""
    if not file_list:
        return None, DEFAULT_VIDEO_PROMPT
    # Gradio FileExplorer returns a list of paths
    path = file_list[0] if isinstance(file_list, list) else file_list
    
    # If the file is in a read-only directory (like /mnt/shared), and it might need conversion,
    # copy it to a writable temp directory so Gradio's automatic conversion works.
    # Gradio tries to write the converted .mp4 in the same directory as the source path.
    ext = pathlib.Path(path).suffix.lower()
    # We check if the directory is writable. If not, and it's a format Gradio might want to convert, 
    # we copy to /tmp to ensure conversion succeeds.
    if not os.access(os.path.dirname(path), os.W_OK) and ext not in [".mp4", ".webm"]:
        try:
            # Create a unique but stable filename to avoid collisions and redundant copies
            path_hash = hashlib.md5(path.encode()).hexdigest()[:8]
            temp_path = os.path.join(tempfile.gettempdir(), f"{path_hash}_{os.path.basename(path)}")
            if not os.path.exists(temp_path):
                logging.info(f"Copying {path} to {temp_path} for writable conversion path")
                shutil.copy2(path, temp_path)
            path = temp_path
        except Exception as e:
            logging.warning(f"Could not copy video to temp: {e}")

    return path, get_prompt_from_file(path, DEFAULT_VIDEO_PROMPT)


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


def analyse_video_with_vllm(
    video_input: Any,
    prompt: str,
    model_override: str,
    num_frames: int,
    fps: int,
    max_duration: int,
) -> str:
    """
    Analyse a video using the Vision Language Model via vLLM's media interface.
    
    Args:
        video_input: The video source (path or Gradio input).
        prompt: The user prompt for analysis.
        model_override: Optional model ID to override the default.
        num_frames: Maximum number of frames to extract.
        fps: Target frames per second for extraction.
        max_duration: Maximum duration in seconds to consider.
        
    Returns:
        The text response from the model.
    """
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
        logging.exception("analyse_video_with_vllm failed")
        return f"Error during video analysis: {e}"


# ==================================
# Tab 3: RTSP Stream (UNCHANGED)
# ==================================
streaming_active: Dict[str, bool] = {}
DEFAULT_FRAME_PROMPT = (
    "You are a helpful assistant. Answer what is the vehicle type, colour, and is the hood opened or closed. The type of vehicle you can select is: car, van, bus, minibus, tractor-trailer, dump truck, motorcycle. Answer in this format:\n<Type>car</Type>\n<Colour>red</Colour>\n<hoodOpen>yes</hoodOpen>"
)

def stop_streaming_generator_rtsp(url_key: str) -> str:
    if not url_key:
        return "Cannot stop: URL is empty."
    
    # Also stop local RTSP server if it was running
    stop_local_rtsp_server()

    if url_key in streaming_active:
        streaming_active[url_key] = False
        logging.info(f"Stop signal sent for stream: {url_key}")
        return f"Stop signal sent for {url_key}."
    return f"Stream {url_key} was not active."


def start_local_rtsp_server(video_path: str):
    """Start a lightweight RTSP server and stream the selected video."""
    global rtsp_server_process, ffmpeg_stream_process
    stop_local_rtsp_server()
    
    # Verify input video path
    if not video_path or not os.path.exists(video_path):
        error_msg = f"Video file not found: {video_path}"
        logging.error(error_msg)
        return error_msg

    try:
        # Check if mediamtx exists
        mediamtx_path = shutil.which("mediamtx")
        if not mediamtx_path:
            # Fallback for common docker installation path
            if os.path.exists("/usr/local/bin/mediamtx"):
                mediamtx_path = "/usr/local/bin/mediamtx"
            else:
                error_msg = "Error: 'mediamtx' (RTSP server) binary not found. Please ensure it is installed and in your PATH."
                logging.error(error_msg)
                return error_msg
            
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            error_msg = "Error: 'ffmpeg' binary not found. Please ensure it is installed and in your PATH."
            logging.error(error_msg)
            return error_msg

        logging.info(f"Starting {mediamtx_path}...")
        
        # We'll write mediamtx logs to a file so they don't block pipes but are still accessible
        mtx_log_path = "/tmp/mediamtx.log"
        mtx_log_file = open(mtx_log_path, "w")
        
        # Construct the exact FFmpeg command that worked for the user in the CLI
        ffmpeg_cmd_str = (
            f"{ffmpeg_path} -re -stream_loop -1 -i \"{video_path}\" "
            f"-c:v libx264 -preset ultrafast -tune zerolatency "
            f"-profile:v baseline -level 3.0 -pix_fmt yuv420p "
            f"-maxrate 2M -bufsize 4M -g 30 "
            f"-f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/live"
        )
        
        env = {
           "MTX_PATHS_LIVE_SOURCE": "publisher",
           "MTX_PATHS_LIVE_RUNONINIT": ffmpeg_cmd_str,
           "MTX_PROTOCOLS": "tcp",
           "MTX_RTSPADDRESS": ":8554",
           "MTX_LOGLEVEL": "info"
        }
        
        rtsp_server_process = subprocess.Popen(
            [mediamtx_path], 
            stdout=mtx_log_file, 
            stderr=subprocess.STDOUT,
            text=True,
            env=env
        )
        
        # Give mediamtx and its internal pusher a few seconds to initialize
        time.sleep(1)
        
        # Check if mediamtx died immediately
        if rtsp_server_process.poll() is not None:
            mtx_log_file.close()
            with open(mtx_log_path, "r") as f:
                logs = f.read()
            error_msg = f"RTSP server (mediamtx) failed to start. Logs:\n{logs}"
            logging.error(error_msg)
            return error_msg

        logging.info(f"Local RTSP server and internal pusher started. Stream: {LOCAL_RTSP_URL}")
        return "Local RTSP server and stream started."
    except Exception as e:
        logging.exception("Failed to start local RTSP server")
        return f"Error starting RTSP server: {e}"


def stop_local_rtsp_server():
    """Stop the local RTSP server and any associated processes."""
    global rtsp_server_process, ffmpeg_stream_process
    
    for process in [rtsp_server_process, ffmpeg_stream_process]:
        if process:
            try:
                logging.info(f"Terminating process {process.pid}...")
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    logging.warning(f"Process {process.pid} did not terminate, killing...")
                    process.kill()
                    process.wait()
            except Exception as e:
                logging.warning(f"Error terminating process: {e}")
    
    rtsp_server_process = None
    ffmpeg_stream_process = None
    
    # Clean up log file if it exists
    if os.path.exists("/tmp/mediamtx.log"):
        try:
            os.remove("/tmp/mediamtx.log")
        except:
            pass


def rtsp_stream_generator(rtsp_url: str):
    if not rtsp_url:
        placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
        placeholder[:] = [40, 40, 40]
        cv2.putText(placeholder, "No RTSP URL Provided", (140, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (200, 200, 200), 2)
        yield placeholder
        return

    url_key = rtsp_url
    streaming_active[url_key] = True
    cap = None
    logging.info(f"Starting RTSP stream: {rtsp_url}")

    # Explicit check for local server
    if rtsp_url == LOCAL_RTSP_URL and (rtsp_server_process is None or rtsp_server_process.poll() is not None):
        logging.error("Attempted to start local RTSP stream, but server is not running.")
        placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
        placeholder[:] = [40, 40, 40]
        cv2.putText(placeholder, "Local RTSP Server Not Running", (100, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        cv2.putText(placeholder, "Please select a sample and click 'Start Stream'.", (80, 300), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 1)
        streaming_active[url_key] = False
        yield placeholder
        return

    try:
        # Set environment variable for OpenCV to force TCP transport for this connection
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        
        # Retry logic for connecting to the stream
        max_retries = 5
        for i in range(max_retries):
            # Use CAP_FFMPEG explicitly
            cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            if cap.isOpened():
                # Set buffer size to 1 to ensure we get the latest frame (low latency)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                break
            
            logging.warning(f"RTSP offline or URL incorrect (attempt {i+1}/{max_retries}): {rtsp_url}")
            if not streaming_active.get(url_key, False):
                break # User stopped while we were retrying
                
            # Show a "connecting" placeholder
            placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
            placeholder[:] = [50, 50, 50]
            cv2.putText(placeholder, f"Connecting... ({i+1}/{max_retries})", (160, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 0), 2)
            cv2.putText(placeholder, f"URL: {rtsp_url}", (50, 450), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)
            yield placeholder
            time.sleep(1.5)

        if not cap or not cap.isOpened():
            logging.error(f"Failed to connect to RTSP after {max_retries} attempts.")
            placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
            placeholder[:] = [30, 30, 30]
            cv2.putText(placeholder, "Connection Failed", (160, 200), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
            cv2.putText(placeholder, "Stream Offline or URL Incorrect", (120, 250), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 1)
            cv2.putText(placeholder, f"Target: {rtsp_url}", (50, 450), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)
            
            # If it's the local URL, remind user to select a sample
            if rtsp_url == LOCAL_RTSP_URL:
                 cv2.putText(placeholder, "Note: Local server requires a sample file selection.", (80, 300), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 165, 0), 1)
            
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
            time.sleep(0.01) # Slightly faster updates

    except Exception as e:
        logging.exception(f"RTSP generator error for {rtsp_url}")
        placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
        placeholder[:] = [30, 0, 0]
        cv2.putText(placeholder, "Streaming Error", (200, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        cv2.putText(placeholder, str(e)[:50], (50, 300), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        yield placeholder

    finally:
        if cap and cap.isOpened():
            cap.release()
        streaming_active.pop(url_key, None)
        logging.info(f"RTSP stream session ended: {rtsp_url}")
        # Final "Stopped" frame
        final_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        final_frame[:] = [20, 20, 20]
        cv2.putText(final_frame, "Stream Stopped", (210, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (100, 100, 100), 2)
        yield final_frame


def analyse_current_rtsp_frame(rtsp_url: str, prompt: str) -> str:
    """
    Capture the current frame from an RTSP stream and analyse it.
    
    Args:
        rtsp_url: The URL of the RTSP stream.
        prompt: The user prompt for analysis.
        
    Returns:
        The text response from the model.
    """
    if not rtsp_url:
        return "Please enter an RTSP URL."
    
    cap = None
    max_retries = 3
    for i in range(max_retries):
        cap = cv2.VideoCapture(rtsp_url)
        if cap.isOpened():
            break
        logging.warning(f"Analysis failed to open stream (attempt {i+1}/{max_retries})")
        time.sleep(1)
        
    if not cap or not cap.isOpened():
        return f"Error: Could not open RTSP stream at {rtsp_url}. Ensure the stream is active."
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
        # Define FileExplorers up front so they can be referenced by the Settings tab
        file_explorer = gr.FileExplorer(
            glob="**/*.[jJpP][PpNn]*[Gg]",
            root_dir=active_root_dir,
            file_count="single",
            height=200,
            label=f"Image files in {active_root_dir}",
            ignore_glob="**/.*",
            render=False
        )
        file_explorer_video = gr.FileExplorer(
            glob="**/*.[mM]*",
            root_dir=active_root_dir,
            file_count="single",
            label=f"Video files in {active_root_dir}",
            ignore_glob="**/.*",
            render=False
        )
        file_explorer_rtsp = gr.FileExplorer(
            glob="**/*.[mM][pP]4",
            root_dir=active_root_dir,
            file_count="single",
            label=f"RTSP Sample files in {active_root_dir}",
            ignore_glob="**/.*",
            render=False
        )

        config_alert = gr.HTML(visible=False)

        gr.Markdown(
            """
            # Visual Analytics Studio • Powered by HPE Private Cloud AI (PCAI)

            Deliver rich, real-time visual understanding across **Images**, **Videos**, and **RTSP live streams**—all running on your enterprise‑grade **[HPE Private Cloud AI](https://www.hpe.com/us/en/private-cloud-ai.html)** platform.
            **What you can do here**
            - **Image Understanding:** Select/Upload an image, preview it, and analyse with your prompt.
            - **Video Understanding:** Select/Upload a video and control extraction knobs (**num_frames**, **fps**, **max_duration**). The app passes your video to PCAI's served endpoint using a secure data URL and vLLM media I/O hints.
            - **RTSP Stream:** View and analyse live network streams; capture a frame and ask questions in natural language.
            """
        )

        with gr.Accordion("Instructions", open=False):
            gr.Markdown(
            """
                **Getting started**

                Open the `Settings` tab and configure your endpoints.

                1. Copy and paste your PCAI endpoint and API token. Endpoint URL should end with `/v1`.
                2. (Optional) Set **Preferred Model ID** to `Qwen/Qwen2.5-VL-32B-Instruct-AWQ`.
                3. Use the tabs to run analyses.

                **Recommended model:** `Qwen/Qwen2.5-VL-32B-Instruct-AWQ` deployed via PCAI for the best experience.
                """
            )
            gr.Image("assets/SolutionOverview.png", label="Solution Overview")

        gr.Markdown("---")

        with gr.Tabs(selected="imageUnderstanding"):
            # ---------------- Tab 1: Settings (UPDATED)
            with gr.TabItem("Settings") as settings_tab:
                api_base_choices = get_inference_services_urls()
                api_base_input = gr.Dropdown(
                    label="API Base URL (internal cluster endpoints detected)", 
                    choices=api_base_choices,
                    value=active_openai_api_base, 
                    allow_custom_value=True,
                    interactive=True
                )
                api_key_input = gr.Textbox(label="API Key", value=active_openai_api_key, type="password", interactive=True)
                preferred_model_input = gr.Textbox(label="Preferred Model ID (optional)")
                ignore_tls_checkbox = gr.Checkbox(label="Ignore TLS Certificates", value=active_ignore_tls)
                root_dir_input = gr.Textbox(label="Root Directory for File Browsers", value=active_root_dir, interactive=True)
                
                with gr.Accordion("Browse Root Directory", open=False):
                    root_explorer = gr.FileExplorer(
                        root_dir="/mnt" if os.path.exists("/mnt") else os.getcwd(),
                        file_count="single",
                        label="Select Root Directory (Sandboxed to /mnt)",
                        ignore_glob="**/.*"
                    )
                    
                    def update_root_dir_from_explorer(file_list):
                        if not file_list: return active_root_dir
                        path = file_list[0] if isinstance(file_list, list) else file_list
                        # If it's a file, get its parent directory
                        if os.path.isfile(path):
                            return os.path.dirname(path)
                        return path

                    root_explorer.change(
                        fn=update_root_dir_from_explorer,
                        inputs=[root_explorer],
                        outputs=[root_dir_input]
                    )

                config_status_out = gr.Textbox(label="Config Status", value=current_config_status_message, interactive=False, lines=2)
                apply_config_btn = gr.Button("Apply Configuration")

                apply_config_btn.click(
                    fn=initialize_or_update_openai_client,
                    inputs=[api_key_input, api_base_input, preferred_model_input, ignore_tls_checkbox, root_dir_input],
                    outputs=[config_status_out, root_dir_input],
                ).then(
                    fn=lambda r: (gr.update(label=f"Image files in {r}", root_dir=r), gr.update(label=f"Video files in {r}", root_dir=r), gr.update(label=f"RTSP Sample files in {r}", root_dir=r)),
                    inputs=[root_dir_input],
                    outputs=[file_explorer, file_explorer_video, file_explorer_rtsp]
                ).then(
                    fn=get_ui_validation_updates,
                    inputs=None,
                    outputs=[config_alert, settings_tab]
                )
            # ---------------- Tab 2: Image Understanding (UPDATED)
            with gr.TabItem("Image Understanding", id="imageUnderstanding"):
                # Define components first (render=False) so Samples can reference them
                image_in = gr.Image(label="Or Upload Image", type="numpy", sources=["upload", "clipboard", "webcam"], height=400, render=False)
                image_prompt = gr.Textbox(label="Prompt", value=DEFAULT_IMAGE_PROMPT, lines=3, render=False)

                gr.Markdown("### 1. Select Input")

                with gr.Accordion("Samples", open=False):
                    # Load examples from assets directory
                    examples_images = []
                    assets_path = "./assets"
                    if os.path.exists(assets_path):
                        for f in sorted(os.listdir(assets_path)):
                            if f.lower().endswith((".jpg", ".jpeg", ".png")):
                                img_path = os.path.join(assets_path, f)
                                examples_images.append([img_path, get_prompt_from_file(img_path, DEFAULT_IMAGE_PROMPT)])

                    if examples_images:
                        gr.Examples(
                            examples=examples_images,
                            inputs=[image_in, image_prompt],
                            label="Samples"
                        )

                file_explorer.render()
                # Event wiring for File Explorer
                file_explorer.change(
                    fn=load_image_from_explorer,
                    inputs=[file_explorer],
                    outputs=[image_in, image_prompt]
                )
                image_in.render()
                    
                image_prompt.render()

                analyse_image_btn = gr.Button("Analyse Image", variant="primary")
                image_llm_output = gr.Textbox(label="LLM Response", lines=10)
                gr.Markdown("---")
                image_json_output = gr.JSON(visible=False)
                image_markdown_output = gr.Markdown(visible=False)
                
                # Event Wiring
                analyse_image_btn.click(
                    fn=analyse_uploaded_image,
                    inputs=[image_in, image_prompt],
                    outputs=[image_llm_output],
                ).then(
                    fn=render_llm_response,
                    inputs=[image_llm_output],
                    outputs=[image_json_output, image_markdown_output],
                )

            # ---------------- Tab 3: Video Understanding (UPDATED)
            with gr.TabItem("Video Understanding"):
                video_in = gr.Video(label="Or Upload Video (mp4/webm/mov/mkv)", height=400, render=False)
                video_prompt = gr.Textbox(label="Prompt", value=DEFAULT_VIDEO_PROMPT, lines=3, render=False)
                with gr.Accordion("Samples", open=False):
                    # Load examples from assets directory
                    example_videos = []
                    assets_path = "./assets"
                    if os.path.exists(assets_path):
                        for f in sorted(os.listdir(assets_path)):
                            if f.lower().endswith((".mp4", ".webm", ".mov", ".mkv")):
                                video_path = os.path.join(assets_path, f)
                                example_videos.append([video_path, get_prompt_from_file(video_path, DEFAULT_VIDEO_PROMPT)])

                    if example_videos:
                        gr.Examples(
                            examples=example_videos,
                            inputs=[video_in, video_prompt],
                            label="Samples"
                        )

                file_explorer_video.render()
                # Event wiring for File Explorer
                file_explorer_video.change(
                    fn=load_video_from_explorer,
                    inputs=[file_explorer_video],
                    outputs=[video_in, video_prompt]
                )

                gr.Markdown("Upload a video, tweak *num_frames / fps / max_duration*, and run analysis. The video will be passed to the model as a data: URL.")
                video_in.render()                
                video_prompt.render()

                with gr.Row():
                    model_override = gr.Textbox(label="Model ID (override, optional)", placeholder="e.g. Qwen/Qwen2.5-VL-32B-Instruct-AWQ")
                with gr.Row():
                    num_frames = gr.Slider(1, 10000, value=7860, step=1, label="num_frames (hard cap on extracted frames)")
                with gr.Row():
                    fps = gr.Slider(1, 60, value=1, step=1, label="fps (target extraction FPS)")
                    max_duration = gr.Slider(1, 100000, value=10000, step=1, label="max_duration (seconds cap)")

                run_video_btn = gr.Button("Analyse Video")
                video_llm_output = gr.Textbox(label="LLM Response", lines=12)
                gr.Markdown("---")
                video_json_output = gr.JSON(visible=False)
                video_markdown_output = gr.Markdown(visible=False)

                run_video_btn.click(
                    fn=analyse_video_with_vllm,
                    inputs=[video_in, video_prompt, model_override, num_frames, fps, max_duration],
                    outputs=[video_llm_output],
                ).then(
                    fn=render_llm_response,
                    inputs=[video_llm_output],
                    outputs=[video_json_output, video_markdown_output],
                )

            # ---------------- Tab 4: RTSP Stream
            with gr.TabItem("RTSP Stream"):
                rtsp_url_input = gr.Textbox(label="RTSP URL", value=DEFAULT_RTSP_URL, placeholder=DEFAULT_RTSP_URL)
                
                with gr.Row():
                    start_btn = gr.Button("Start Stream", variant="primary")
                    stop_btn = gr.Button("Stop Stream")
                
                with gr.Accordion("Samples", open=False):
                    file_explorer_rtsp.render()

                with gr.Row():
                    restore_btn = gr.Button("Restore Default RTSP URL", size="sm")

                rtsp_image = gr.Image(label="Live RTSP Stream", interactive=False, type="numpy", height=400)
                rtsp_status = gr.Textbox(label="Status", value="Stream not started.", interactive=False)
                rtsp_prompt = gr.Textbox(label="Prompt for Frame Analysis", value=DEFAULT_FRAME_PROMPT, lines=4)
                analyse_rtsp_btn = gr.Button("Analyse Current Frame")
                rtsp_llm_out = gr.Textbox(label="LLM Response", lines=8)

                def handle_start_stream(url, sample_path_list):
                    # Handle both list (older Gradio) and string (newer Gradio) from FileExplorer
                    sample_path = None
                    if sample_path_list:
                        sample_path = sample_path_list[0] if isinstance(sample_path_list, list) else sample_path_list
                    

                    if url == LOCAL_RTSP_URL:
                        if not sample_path:
                            # Try to find a default sample in active_root_dir
                            try:
                                paths = sorted(list(pathlib.Path(active_root_dir).glob("**/*.[mM][pP]4")))
                                if paths:
                                    sample_path = str(paths[0])
                                    gr.Info(f"Auto-selected sample: {os.path.basename(sample_path)}")
                            except Exception:
                                pass

                        if not sample_path:
                            gr.Warning("No sample file selected or found in root directory for local RTSP server.")
                            return "Error: Please browse and select a sample file first."
                        
                        gr.Info(f"Starting local RTSP server with: {os.path.basename(sample_path)}")
                        msg = start_local_rtsp_server(sample_path)
                        if msg.startswith("Error") or "failed" in msg.lower():
                            gr.Error(msg)
                            return f"Status: {msg}"
                        return f"Status: {msg}"
                    
                    gr.Info(f"Connecting to remote RTSP stream: {url}")
                    return f"Connecting to {url}..."

                # When sample is selected, update the URL input to our local server address
                def update_url_from_explorer(file_list):
                    if not file_list:
                        return DEFAULT_RTSP_URL
                    return LOCAL_RTSP_URL

                file_explorer_rtsp.change(
                    fn=update_url_from_explorer,
                    inputs=[file_explorer_rtsp],
                    outputs=[rtsp_url_input]
                )

                restore_btn.click(
                    fn=lambda: (DEFAULT_RTSP_URL, []),
                    inputs=None,
                    outputs=[rtsp_url_input, file_explorer_rtsp]
                )

                start_btn.click(
                    fn=handle_start_stream,
                    inputs=[rtsp_url_input, file_explorer_rtsp],
                    outputs=[rtsp_status]
                ).then(
                    fn=rtsp_stream_generator,
                    inputs=[rtsp_url_input],
                    outputs=[rtsp_image],
                )

                stop_btn.click(
                    fn=stop_streaming_generator_rtsp,
                    inputs=[rtsp_url_input],
                    outputs=[rtsp_status],
                )

                analyse_rtsp_btn.click(
                    fn=analyse_current_rtsp_frame,
                    inputs=[rtsp_url_input, rtsp_prompt],
                    outputs=[rtsp_llm_out],
                )

        demo.queue(default_concurrency_limit=8)
        
        # Load current config into UI on page load
        demo.load(
            fn=get_current_config_ui,
            inputs=None,
            outputs=[api_key_input, api_base_input, preferred_model_input, ignore_tls_checkbox, root_dir_input, config_status_out, config_alert, settings_tab, file_explorer, file_explorer_video, root_explorer]
        )
        
    return demo


# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------
demo = build_ui()

if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0", 
        server_port=7860, 
        allowed_paths=["/"],
    )