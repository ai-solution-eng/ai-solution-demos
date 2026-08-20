# FILE: docker/gradio_ui/app_ui.py

import gradio as gr
import websockets
import asyncio
import os
import traceback
import struct
import json
import tempfile
from typing import AsyncGenerator

# --- Voice Data for Dropdowns ---
_RAW_VOICE_DATA = {
    "en-US,es-US,fr-FR,de-DE": {
        "voices": [
            "Magpie-Multilingual.EN-US.Sofia", "Magpie-Multilingual.EN-US.Ray", "Magpie-Multilingual.EN-US.Sofia.Calm",
            "Magpie-Multilingual.EN-US.Sofia.Fearful", "Magpie-Multilingual.EN-US.Sofia.Happy", "Magpie-Multilingual.EN-US.Sofia.Angry",
            "Magpie-Multilingual.EN-US.Sofia.Neutral", "Magpie-Multilingual.EN-US.Sofia.Disgisted", "Magpie-Multilingual.EN-US.Sofia.Sad",
            "Magpie-Multilingual.EN-US.Ray.Angry", "Magpie-Multilingual.EN-US.Ray.Calm", "Magpie-Multilingual.EN-US.Ray.Disgusted",
            "Magpie-Multilingual.EN-US.Ray.Fearful", "Magpie-Multilingual.EN-US.Ray.Happy", "Magpie-Multilingual.EN-US.Ray.Neutral",
            "Magpie-Multilingual.EN-US.Ray.Sad", "Magpie-Multilingual.ES-US.Diego", "Magpie-Multilingual.ES-US.Isabela",
            "Magpie-Multilingual.ES-US.Isabela.Neutral", "Magpie-Multilingual.ES-US.Diego.Neutral", "Magpie-Multilingual.ES-US.Isabela.Fearful",
            "Magpie-Multilingual.ES-US.Diego.Fearful", "Magpie-Multilingual.ES-US.Isabela.Happy", "Magpie-Multilingual.ES-US.Diego.Happy",
            "Magpie-Multilingual.ES-US.Isabela.Sad", "Magpie-Multilingual.ES-US.Diego.Sad", "Magpie-Multilingual.ES-US.Isabela.Pleasant_Surprise",
            "Magpie-Multilingual.ES-US.Diego.Pleasant_Surprise", "Magpie-Multilingual.ES-US.Isabela.Angry", "Magpie-Multilingual.ES-US.Diego.Angry",
            "Magpie-Multilingual.ES-US.Isabela.Calm", "Magpie-Multilingual.ES-US.Diego.Calm", "Magpie-Multilingual.ES-US.Isabela.Disgust",
            "Magpie-Multilingual.ES-US.Diego.Disgust", "Magpie-Multilingual.FR-FR.Louise", "Magpie-Multilingual.FR-FR.Pascal",
            "Magpie-Multilingual.FR-FR.Louise.Neutral", "Magpie-Multilingual.FR-FR.Pascal.Neutral", "Magpie-Multilingual.FR-FR.Louise.Angry",
            "Magpie-Multilingual.FR-FR.Louise.Calm", "Magpie-Multilingual.FR-FR.Louise.Disgust", "Magpie-Multilingual.FR-FR.Louise.Fearful",
            "Magpie-Multilingual.FR-FR.Louise.Happy", "Magpie-Multilingual.FR-FR.Louise.Pleasant_Surprise", "Magpie-Multilingual.FR-FR.Louise.Sad",
            "Magpie-Multilingual.FR-FR.Pascal.Angry", "Magpie-Multilingual.FR-FR.Pascal.Calm", "Magpie-Multilingual.FR-FR.Pascal.Disgust",
            "Magpie-Multilingual.FR-FR.Pascal.Fearful", "Magpie-Multilingual.FR-FR.Pascal.Happy", "Magpie-Multilingual.FR-FR.Pascal.Pleasant_Surprise",
            "Magpie-Multilingual.FR-FR.Pascal.Sad", "Magpie-Multilingual.EN-US.Mia", "Magpie-Multilingual.DE-DE.Jason",
            "Magpie-Multilingual.DE-DE.Leo", "Magpie-Multilingual.DE-DE.Aria"
        ]
    }
}
# Create flat lists of all available languages and voices
SUPPORTED_LANGUAGES = list(_RAW_VOICE_DATA.keys())[0].split(',')
ALL_SUPPORTED_VOICES = _RAW_VOICE_DATA[list(_RAW_VOICE_DATA.keys())[0]]['voices']


# --- Configuration (Loaded from environment variables set by Helm) ---
WEBSOCKET_URI = os.getenv("WEBSOCKET_URI", "ws://localhost:8765")
DEFAULT_ASR_URL = os.getenv("ASR_SERVER_ADDRESS", "your-asr-server.svc.cluster.local:50051")
DEFAULT_TTS_URL = os.getenv("TTS_SERVER_ADDRESS", "your-tts-server.svc.cluster.local:50051")
DEFAULT_PROMPT = os.getenv("LLM_PROMPT_TEMPLATE", 'Answer the question: "{transcript}"\n\nAnswer concisely.')
DEFAULT_TTS_LANG = os.getenv("TTS_LANGUAGE_CODE", "en-US")
DEFAULT_TTS_VOICE = os.getenv("TTS_VOICE", "Magpie-Multilingual.EN-US.Sofia")
DEFAULT_TTS_RATE_HZ = os.getenv("TTS_SAMPLE_RATE_HZ", "44100")

# Audio properties for WAV header creation
TTS_SAMPLE_RATE_HZ = int(DEFAULT_TTS_RATE_HZ)
TTS_CHANNELS = 1
TTS_SAMPLE_WIDTH_BYTES = 2


# --- WebSocket and Audio Helper Functions (Unchanged) ---
def _add_wav_header(data: bytes, sample_rate: int, channels: int, sample_width: int) -> bytes:
    datasize = len(data)
    chunksize = 36 + datasize
    header = struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', chunksize, b'WAVE', b'fmt ', 16, 1, channels, sample_rate, sample_rate * channels * sample_width, channels * sample_width, sample_width * 8, b'data', datasize)
    return header + data

async def _producer(filepath: str, event_queue: asyncio.Queue, settings: dict):
    try:
        async with websockets.connect(WEBSOCKET_URI, max_size=50 * 1024 * 1024, ping_interval=None) as websocket:
            await event_queue.put(("status", "Connection successful. Sending configuration..."))
            settings_json = json.dumps(settings)
            await websocket.send(settings_json)
            await event_queue.put(("status", f"Configuration sent: {settings_json}"))
            
            await event_queue.put(("status", "Reading audio file..."))
            with open(filepath, "rb") as f:
                audio_bytes = f.read()
            
            await event_queue.put(("status", f"Sending {len(audio_bytes)} bytes to server..."))
            await websocket.send(audio_bytes)
            await event_queue.put(("status", "Audio sent. Receiving stream..."))
            
            while True:
                response = await websocket.recv()
                if response == "__END_OF_STREAM__":
                    await event_queue.put(("status", "End of stream signal received."))
                    await event_queue.put(("done", None))
                    break
                if isinstance(response, str):
                    await event_queue.put(("status", f"Server message: {response}"))
                    continue
                await event_queue.put(("audio", response))
    except Exception as e:
        error_msg = f"ERROR in producer: {e}\n{traceback.format_exc()}"
        await event_queue.put(("status", error_msg))
        await event_queue.put(("done", None))

async def process_audio_and_return_complete_file(
    audio_filepath: str | None,
    prompt_template: str,
    asr_server_address: str,
    tts_server_address: str,
    tts_language_code: str,
    tts_voice: str,
    tts_sample_rate_hz_str: str,
) -> AsyncGenerator[tuple[str | None, str], None]:
    if not audio_filepath:
        yield None, "Please provide an audio file or record your voice."
        return

    yield None, "Initializing..."
    dynamic_settings = {
        "llm_prompt_template": prompt_template, "asr_server_address": asr_server_address,
        "tts_server_address": tts_server_address, "tts_language_code": tts_language_code,
        "tts_voice": tts_voice, "tts_sample_rate_hz": int(tts_sample_rate_hz_str)
    }
    
    full_audio_buffer = bytearray()
    status_log = "Initializing...\n"
    event_queue = asyncio.Queue()
    producer_task = asyncio.create_task(_producer(audio_filepath, event_queue, dynamic_settings))

    try:
        while True:
            event_type, data = await event_queue.get()
            if event_type == "done": break
            if event_type == "status":
                status_log += f"{data}\n"
                yield None, status_log
                continue
            if event_type == "audio":
                full_audio_buffer.extend(data)
                status_log += f"Buffering audio chunk of {len(data)} bytes...\n"
                yield None, status_log

        if not full_audio_buffer:
            status_log += "ERROR: No audio data was received from the server.\n"
            yield None, status_log
            return

        status_log += "Stream finished. Assembling final audio file...\n"
        yield None, status_log
        final_wav_data = _add_wav_header(bytes(full_audio_buffer), dynamic_settings['tts_sample_rate_hz'], TTS_CHANNELS, TTS_SAMPLE_WIDTH_BYTES)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmpf:
            tmpf.write(final_wav_data)
            final_audio_path = tmpf.name
        
        status_log += f"--- Playback Starting ---\nFinal audio file ready: {os.path.basename(final_audio_path)}\n"
        yield final_audio_path, status_log
    except Exception as e:
        error_msg = f"An error occurred in the Gradio handler: {e}\n{traceback.format_exc()}"
        yield None, error_msg
    finally:
        producer_task.cancel()


# --- Gradio UI Layout ---
with gr.Blocks(theme=gr.themes.Soft()) as demo:
    gr.Markdown(
        "# Real-time Audio AI Pipeline with Dynamic Configuration in HPE's Private Cloud AI\n\n"
        "This demonstration showcases an end-to-end audio conversational agent. Record or upload your voice, and the system will transcribe it, generate a response using a Large Language Model, and synthesize the response back to you as a single, complete audio clip. "
        "The entire pipeline is deployed on **HPE's Private Cloud AI**, which handles the complex infrastructure management required to orchestrate and scale these AI models in a production environment."
    )
    
    with gr.Row():
        with gr.Column(scale=1):
            input_audio = gr.Audio(sources=["microphone", "upload"], type="filepath", label="Your Voice Input")
            
            with gr.Accordion("Advanced Settings", open=False):
                gr.Markdown("Override the default server settings for this request. Note: Mismatched Language and Voice settings may cause errors.")
                # --- SIMPLIFIED DROPDOWNS ---
                lang_dropdown = gr.Dropdown(label="Language Code", choices=SUPPORTED_LANGUAGES, value=DEFAULT_TTS_LANG)
                voice_dropdown = gr.Dropdown(label="Voice Name", choices=ALL_SUPPORTED_VOICES, value=DEFAULT_TTS_VOICE)
                prompt_template_input = gr.Textbox(label="LLM Prompt Template", value=DEFAULT_PROMPT, lines=3)
                asr_server_input = gr.Textbox(label="ASR Server Address", value=DEFAULT_ASR_URL)
                tts_server_input = gr.Textbox(label="TTS Server Address", value=DEFAULT_TTS_URL)
                tts_rate_input = gr.Textbox(label="TTS Sample Rate (Hz)", value=DEFAULT_TTS_RATE_HZ)

            submit_btn = gr.Button("Submit", variant="primary")
            
            with gr.Accordion("Model Information", open=True):
                 gr.Markdown(
                    """
                    This demo is powered by state-of-the-art models deployed using:
                    - **[HPE Private Cloud AI](https://www.hpe.com/us/en/private-cloud-ai.html)**
                    - **[HPE Machine Learning Inference Software](https://docs.ai-solutions.ext.hpe.com/products/mlis/)**
                    - **ASR Model:** [parakeet-ctc-1.1b-asr](https://build.nvidia.com/nvidia/parakeet-ctc-1_1b-asr)
                    - **TTS Model:** [magpie-tts-multilingual](https://build.nvidia.com/nvidia/magpie-tts-multilingual)
                    - **LLM Model:** [meta-llama/Llama-3.2-1B-Instruct](https://huggingface.co/meta-llama/Llama-3.2-1B)
                    """
                )

        with gr.Column(scale=2):
            output_audio = gr.Audio(label="Synthesized Voice Output", streaming=False, autoplay=True)
            status_textbox = gr.Textbox(label="Processing Status Log", lines=15, interactive=False, autoscroll=True)

    # --- REMOVED: No longer need the .change() event for dependent dropdowns ---
    
    all_inputs = [
        input_audio, prompt_template_input, asr_server_input,
        tts_server_input, lang_dropdown, voice_dropdown, tts_rate_input
    ]

    submit_btn.click(
        fn=process_audio_and_return_complete_file,
        inputs=all_inputs,
        outputs=[output_audio, status_textbox]
    )

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=8080)