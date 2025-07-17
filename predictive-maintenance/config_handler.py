import os
import yaml

def load_config():
    """Load configuration from config.yaml and environment variables"""
    # Load base config from file
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)
    
    # Override with environment variables if they exist
    if os.environ.get('LLM_INFERENCE_URL'):
        config['resolution_model']['inference_server_url'] = os.environ.get('LLM_INFERENCE_URL')
    
    if os.environ.get('LLM_INFERENCE_TOKEN'):
        config['resolution_model']['inference_server_token'] = os.environ.get('LLM_INFERENCE_TOKEN')
    
    if os.environ.get('OCR_INFERENCE_URL'):
        config['ocr_model']['inference_server_url'] = os.environ.get('OCR_INFERENCE_URL')
    
    if os.environ.get('OCR_INFERENCE_TOKEN'):
        config['ocr_model']['inference_server_token'] = os.environ.get('OCR_INFERENCE_TOKEN')
    
    return config