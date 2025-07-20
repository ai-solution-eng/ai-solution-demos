import os
import yaml

def load_config():
    """Load configuration from config.yaml and environment variables"""
    # Load base config from file
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)
    
    # Override with environment variables - prioritize env vars over config file
    config['resolution_model']['inference_server_url'] = (
        os.environ.get('LLM_INFERENCE_URL') or 
        config['resolution_model']['inference_server_url']
    )
    
    config['resolution_model']['inference_server_token'] = (
        os.environ.get('LLM_INFERENCE_TOKEN') or 
        config['resolution_model']['inference_server_token']
    )
    
    config['ocr_model']['inference_server_url'] = (
        os.environ.get('OCR_INFERENCE_URL') or 
        config['ocr_model']['inference_server_url']
    )
    
    config['ocr_model']['inference_server_token'] = (
        os.environ.get('OCR_INFERENCE_TOKEN') or 
        config['ocr_model']['inference_server_token']
    )
    
    return config

def validate_config(config):
    """Validate that required configuration values are present"""
    errors = []
    
    if not config['ocr_model']['inference_server_url']:
        errors.append("OCR inference server URL is not configured")
    
    if not config['ocr_model']['inference_server_token']:
        errors.append("OCR inference server token is not configured")
    
    if not config['resolution_model']['inference_server_url']:
        errors.append("LLM inference server URL is not configured")
    
    if not config['resolution_model']['inference_server_token']:
        errors.append("LLM inference server token is not configured")
    
    return errors