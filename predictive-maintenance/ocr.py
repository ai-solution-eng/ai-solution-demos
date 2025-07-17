import streamlit as st
import pandas as pd
from PIL import Image
import httpx
import base64
import re
import yaml
from config_handler import load_config

### Read Model data from Config.yaml file
config = load_config()

TOKEN = config["ocr_model"]["inference_server_token"]
MODEL_ENDPOINT = config["ocr_model"]["inference_server_url"]
MODEL_NAME = config["ocr_model"]["vlm_model"]

### remove Async part
def call_qwen(prompt: str, encoded_image, url: str):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    payload = {
        "model": MODEL_NAME,  
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}"}}
                ]
            }
        ],
        "max_tokens": 128
    }

    # resp = httpx.post(url,json=payload, headers=headers,verify=False)
    resp = httpx.post(url,json=payload, headers=headers)
    
    return resp.json()["choices"][0]["message"]["content"]


### Convert from String to List
def split_preserve_datetime(raw: str):
    # Match date+time first, then floats, ints, or words
    pattern = r'\d{2}/\d{2}/\d{4} \d{2}:\d{2}|\d+\.\d+|\d+|[A-Za-z]+'
    matches = re.findall(pattern, raw)

    result = []
    for m in matches:
        try:
            result.append(float(m) if '.' in m else int(m))
        except ValueError:
            result.append(m)
    return result

def extract_text_from_image():
    st.markdown("### Image Text Extraction")
    st.markdown("Upload a network speed test screenshot to extract performance metrics.")
    uploaded_file = st.file_uploader("Choose an image...", type=["jpg", "png"], help="Supported formats: JPG, PNG")
    if uploaded_file is not None:
        image = Image.open(uploaded_file)
        st.image(image, caption='Uploaded Image', use_column_width=True)   

        if st.button("Analyze Image", help="Extract metrics from the uploaded image"):
            ### NEW - CALL MODEL ENDPOINT
            # Encode image as base64
            image_bytes = uploaded_file.getvalue()
            ENCODED_IMG = base64.b64encode(image_bytes).decode("utf-8")
            ### Change default prompt
            default_prompt = """
Save only the number shown right below 'Download' and right above 'Mbps', nothing else, as the 1st element in an output list. 
Save only the number shown right below 'Upload' and right above 'Mbps', nothing else, as the second element in the output list. 
Save only the number shown right below 'Latency' and right above 'ms', nothing else, as the third element in the output list.
Save only the location shown right below 'Wifi', only the Wifi network name shown right below 'Wifi' and right next to the wifi icon to the right, nothing else, as the fourth element in the output list. 
Save BOTH the date and time shown right above 'Latency', shown right next to the calendar icon to the right, following the DD/MM/YYYY HH:MM format, nothing else, as the fifth element in the output list.
Finally return the list of all elements in a string format with square brackets and commas separating elements. Make sure the list has 5 elements
            """
            USER_PROMPT= st.text_area("Custom Prompt (Advanced)", value=default_prompt, height=200, help="Modify the prompt to extract different information")
            # print(USER_PROMPT)
            ### Remove Await
            OUTPUT = call_qwen(str(USER_PROMPT), ENCODED_IMG, MODEL_ENDPOINT)
            PARSED = split_preserve_datetime(OUTPUT)
            ### 
            data = {
                'Upload': [PARSED[0]],
                'Download': [PARSED[1]],
                'Latency': [PARSED[2]],
                'SSID': [PARSED[3]],
                'Date': [PARSED[4]]
            }
            df = pd.DataFrame(data)
            # print(df)
            return df