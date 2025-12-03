import os
import shutil
import uuid
import cv2
import base64
import json
import pathlib
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Body
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from . import database, models, schemas
from .core import get_logger
from .routers_ai import call_llm, get_available_models
from .utils import get_llm_config

logger = get_logger(__name__)

router = APIRouter(
    prefix="/cases",
    tags=["videos"],
)

UPLOAD_DIR = "uploads/videos"
os.makedirs(UPLOAD_DIR, exist_ok=True)

def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

def video_to_data_url(path: str) -> str:
    """Convert video file to base64 data URL"""
    # Determine mime type based on extension
    ext = pathlib.Path(path).suffix.lower()
    mime = "video/mp4"
    if ext in {".webm"}:
        mime = "video/webm"
    elif ext in {".mov"}:
        mime = "video/quicktime"
    elif ext in {".mkv"}:
        mime = "video/x-matroska"
    
    # Read and encode video
    b64 = base64.b64encode(pathlib.Path(path).read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{b64}"



@router.post("/{case_id}/videos", response_model=schemas.CaseVideo)
async def upload_video(
    case_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    case = db.query(models.Case).filter(models.Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    # Validate extension
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ['.mp4', '.mkv', '.mov', '.webm']:
        raise HTTPException(status_code=400, detail="Invalid video format. Allowed: mp4, mkv, mov, webm")

    # Generate safe filename
    safe_filename = f"{uuid.uuid4()}{ext}"
    file_path = os.path.join(UPLOAD_DIR, safe_filename)

    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        logger.error(f"Error saving video: {e}")
        raise HTTPException(status_code=500, detail="Failed to save video file")

    video = models.CaseVideo(
        filename=file.filename,
        file_path=file_path,
        case_id=case_id
    )
    db.add(video)
    db.commit()
    db.refresh(video)
    return video

@router.get("/{case_id}/videos", response_model=List[schemas.CaseVideo])
def get_videos(case_id: int, db: Session = Depends(get_db)):
    videos = db.query(models.CaseVideo).filter(models.CaseVideo.case_id == case_id).all()
    return videos

@router.delete("/{case_id}/videos/{video_id}")
def delete_video(case_id: int, video_id: int, db: Session = Depends(get_db)):
    video = db.query(models.CaseVideo).filter(models.CaseVideo.id == video_id, models.CaseVideo.case_id == case_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    
    # Delete file
    if video.file_path and os.path.exists(video.file_path):
        try:
            os.remove(video.file_path)
        except Exception as e:
            logger.error(f"Error deleting video file: {e}")

    db.delete(video)
    db.commit()
    return {"message": "Video deleted"}

class VideoChatRequest(BaseModel):
    message: str
    history: List[dict] = []
    num_frames: int = 16  # Number of frames to sample
    fps: int = 1  # Frames per second
    max_duration: int = 30  # Maximum duration in seconds

@router.post("/{case_id}/videos/{video_id}/chat")
async def chat_with_video(
    case_id: int,
    video_id: int,
    request: VideoChatRequest,
    db: Session = Depends(get_db)
):
    video = db.query(models.CaseVideo).filter(models.CaseVideo.id == video_id, models.CaseVideo.case_id == case_id).first()
    logger.info(f"Video chat request for video_id={video_id}, case_id={case_id}")
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    llm_endpoint, api_key = get_llm_config(db)
    if not llm_endpoint:
        raise HTTPException(status_code=500, detail="LLM not configured")
    
    logger.info(f"Using LLM endpoint: {llm_endpoint}")

    # Convert video to base64 data URL
    logger.info(f"Converting video to data URL: {video.file_path}")
    try:
        video_data_url = video_to_data_url(video.file_path)
        logger.info(f"Video converted successfully (size: {len(video_data_url)} bytes)")
    except Exception as e:
        logger.error(f"Error converting video: {e}")
        raise HTTPException(status_code=400, detail=f"Could not process video: {str(e)}")

    # Prepare messages for vision model
    messages = []
    
    # Add history (text only)
    for msg in request.history:
        if isinstance(msg.get("content"), str):
            messages.append({"role": msg["role"], "content": msg["content"]})
        
    # Add system message
    messages.append({
        "role": "system", 
        "content": "You are a helpful legal assistant analyzing video evidence. Answer the user's questions based on the video content."
    })
    
    # Add current message with video
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": request.message},
            {"type": "video_url", "video_url": {"url": video_data_url}},
        ],
    })

    try:
        # Try to get available models, but don't fail if we can't
        available_models = []
        try:
            available_models = await get_available_models(llm_endpoint, api_key)
            logger.info(f"Available models: {available_models}")
        except Exception as e:
            logger.warning(f"Could not fetch available models: {e}")
        
        # Use first available model, or try common vision model names
        if available_models:
            model = available_models[0]
        else:
            # Fallback to common vision model names
            model = "Qwen/Qwen2.5-VL-32B-Instruct-AWQ"  # Try qwen-2.5-vl-32b-instruct-awq first (supports vision)
        
        logger.info(f"Using model: {model}")
        logger.info(f"Sending request to LLM with {len(messages)} messages")
        logger.info(f"Message structure: {json.dumps([{'role': m['role'], 'content_type': type(m['content']).__name__} for m in messages])}")
        
        # Make the LLM call
        import httpx
        async with httpx.AsyncClient(timeout=300.0) as client:  # 5 minute timeout for video processing
            base_url = llm_endpoint.rstrip('/').rstrip('/v1')
            
            payload = {
                "model": model,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 2000,
                "extra_body": {
                    "media_io_kwargs": {
                        "video": {
                            "num_frames": request.num_frames,
                            "fps": request.fps,
                            "max_duration": request.max_duration,
                        }
                    }
                }
            }
            
            logger.info(f"Sending to: {base_url}/v1/chat/completions")
            
            response = await client.post(
                f"{base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json=payload
            )
            
            logger.info(f"Response status: {response.status_code}")
            
            if response.status_code != 200:
                logger.error(f"LLM Error response: {response.text}")
                raise Exception(f"LLM Error ({response.status_code}): {response.text}")
            
            data = response.json()
            logger.info(f"Response keys: {data.keys()}")
            
            if "choices" not in data or not data["choices"]:
                logger.error(f"Empty choices in response: {json.dumps(data)}")
                raise Exception("Empty response from LLM")
            
            choice = data["choices"][0]
            logger.info(f"Choice keys: {choice.keys()}")
            
            if "message" not in choice:
                logger.error(f"No message in choice: {json.dumps(choice)}")
                raise Exception("No message in LLM response")
            
            message = choice["message"]
            content = message.get("content", "")
            
            # Get finish and stop reasons
            finish_reason = choice.get("finish_reason", "unknown")
            stop_reason = choice.get("stop_reason", None)
            
            logger.info(f"Finish reason: {finish_reason}, Stop reason: {stop_reason}")
            
            if not content:
                logger.warning(f"Empty content in message: {json.dumps(message)}")
                # Check if there's a refusal or other field
                if "refusal" in message:
                    logger.error(f"LLM refused: {message['refusal']}")
                    # Try fallback to text-only mode
                    logger.info("Attempting text-only fallback...")
                    text_messages = [
                        {"role": "system", "content": "You are a helpful legal assistant."},
                        {"role": "user", "content": f"I have a video file named '{video.filename}' but cannot show you the frames. The user asks: {request.message}. Please explain that you cannot analyze the video content without vision capabilities, but offer to help with other aspects of the case."}
                    ]
                    
                    fallback_response = await client.post(
                        f"{base_url}/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "model": model,
                            "messages": text_messages,
                            "temperature": 0.7,
                            "max_tokens": 500
                        }
                    )
                    
                    if fallback_response.status_code == 200:
                        fallback_data = fallback_response.json()
                        if fallback_data.get("choices"):
                            fallback_content = fallback_data["choices"][0]["message"].get("content", "")
                            if fallback_content:
                                return {"response": f"⚠️ Vision model unavailable. {fallback_content}"}
                    
                    raise Exception(f"LLM refused: {message.get('refusal', 'Unknown reason')}")
                raise Exception("LLM returned empty content")
            
            logger.info(f"Successfully received response from LLM (length: {len(content)})")
            return {
                "response": content,
                "finish_reason": finish_reason,
                "stop_reason": stop_reason
            }
            
    except Exception as e:
        logger.error(f"Error in video chat: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"LLM Error: {str(e)}")
