from fastapi import APIRouter
from fastapi import UploadFile
from pydantic import BaseModel,Field
import uuid

class UploadInfo(BaseModel):
    session_id : str = Field(..., description="Session uuid of upload")
    video_path : str = Field(..., description="Uploaded Video File path")
    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "session_id": "d9707320-6125-4dae-a53f-46f8e7503a6b",
                    "video_path": "temp_6e736403-9acb-4fd4-9ec7-2ce810131b4a.mp4"
                },
            ]
        }

router = APIRouter(
    prefix="/api/utils",
)

@router.post(
    "/video/upload",
    tags=['Utils'],
    response_model=UploadInfo,
    summary="Save uploaded video temporarily",
    description="Save uploaded video temporarily and Return session ID for WebSocket connection"
)
async def upload_video(file: UploadFile):
    # Save uploaded video temporarily
    temp_path = f"temp_{uuid.uuid4()}.mp4"
    with open(temp_path, "wb") as buffer:
        buffer.write(await file.read())
    
    # Return session ID for WebSocket connection
    session_id = str(uuid.uuid4())
    
    return UploadInfo(session_id=session_id, video_path=temp_path)