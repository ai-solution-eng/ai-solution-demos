import os
import yaml
from ultralytics import YOLO
import easyocr
import cv2 as cv

from fastapi import APIRouter
from fastapi import WebSocket,WebSocketDisconnect,websockets
from pydantic import BaseModel, Field
from typing import Optional
from websockets.exceptions import InvalidState
import asyncio

from utils.detector import crop_vehicle_license_then_read
from utils.logging import logger

with open("./config.yaml", "r") as f:
    config = yaml.safe_load(f)

# TOKEN = config["detectors"]["server_token"]
MODEL_ENDPOINT = config["detectors"]["server_url"]
VEHICLE_MODEL_NAME = config["detectors"]["vehicle_detector"]
LICENSE_MODEL_NAME = config["detectors"]["license_detector"]

class inferenceRequest(BaseModel):
    video_path: str
    vehicle_conf: float = 0.25
    license_conf: float = 0.25
    ratio: float = 0.25

class DetectionResult(BaseModel):
    """Single vehicle and license plate detection result"""
    frame_number: int = Field(..., description="Frame number in sequence")
    track_id: int = Field(..., description="Unique tracking ID for the vehicle")
    vehicle_bbox: list[float] = Field(..., description="Vehicle bounding box [x1, y1, x2, y2]")
    vehicle_bbox_score: float = Field(..., ge=0.0, le=1.0, description="Vehicle detection confidence score")
    lp_bbox: list[float] = Field(..., description="License plate bounding box [x1, y1, x2, y2]")
    lp_bbox_score: float = Field(..., ge=0.0, le=1.0, description="License plate detection confidence score")
    lp_number: Optional[str] = Field(default=None, description="Detected license plate text")
    lp_text_score: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="OCR confidence score")
    class Config:
        json_schema_extra = {
            "example": {
                "frame_number": 1,
                "track_id": 123,
                "vehicle_bbox": [100.5, 200.3, 350.7, 450.2],
                "vehicle_bbox_score": 0.95,
                "lp_bbox": [150.1, 220.4, 280.8, 250.6],
                "lp_bbox_score": 0.88,
                "lp_number": "ABC123",
                "lp_text_score": 0.92
            }
        }

router = APIRouter(
    prefix="/api/video",
)   

@router.post(
    "/plate_number/crop/detect/info",
    tags=["Plate Number Detection"],
    response_model=list[DetectionResult],
    summary="Detect license Numbers in Video",
    description="Get vehicle Video -> Crop vehicle -> Crop license plate -> Read Plate Number ( OCR ) -> Return Dict",
)
def get_video_crop_return_info(inferencerequest : inferenceRequest):
    '''
    Use Tracker
    return info ( not image )
    '''
    vehicle_tracker = YOLO(MODEL_ENDPOINT + '/' + VEHICLE_MODEL_NAME, task='detect')
    license_detector = YOLO(MODEL_ENDPOINT + '/' + LICENSE_MODEL_NAME, task='detect')
    plate_reader = easyocr.Reader(['en'],gpu=False)

    results = []
    video_path = inferencerequest.video_path
    vehicle_conf = inferencerequest.vehicle_conf
    license_conf = inferencerequest.license_conf
    ratio = inferencerequest.ratio

    # Process video
    cap = cv.VideoCapture(video_path)
    fps = cap.get(cv.CAP_PROP_FPS)
    total_frame = cap.get(cv.CAP_PROP_FRAME_COUNT)
    thres_frame = total_frame*ratio
    frame_num = -1

    try:
        while cap.isOpened():
            frame_num += 1
            ret, frame = cap.read()
            ### limit num of frames for test purpose
            if not ret or frame_num > thres_frame:
                break
            
            # Process frame - detect license plates
            detection_results = crop_vehicle_license_then_read(vehicle_tracker,license_detector,plate_reader,input_image=frame,vehicle_conf=vehicle_conf,license_conf=license_conf,frame_number=frame_num)
            # Convert frame to bytes and send
            results.extend(detection_results)
    finally:    
        logger.info(f"Inferenced # of Frame : {frame_num} / {total_frame}")
            
        # Clean up input video file
        cap.release()
        if os.path.exists(video_path):
            os.remove(video_path)

        return results


@router.websocket("/ws/license_number/{session_id}")
async def get_video_return_license_number_info_ws(websocket: WebSocket, session_id: str):
    '''
    Does not Use Tracker
    return info ( not image )
    '''
    await websocket.accept()
    
    # Get video path from session data (simplified here)
    data = await websocket.receive_json()
    video_path = data["video_path"]
    vehicle_conf = data["vehicle_conf"]
    license_conf = data["license_conf"]
    ratio = data["ratio"]

    
    # Process video
    cap = cv.VideoCapture(video_path)
    fps = cap.get(cv.CAP_PROP_FPS)
    total_frame = cap.get(cv.CAP_PROP_FRAME_COUNT)
    thres_frame = total_frame*ratio
    frame_num = -1

    try:
        while cap.isOpened():
            frame_num += 1
            ret, frame = cap.read()
            ### limit 10 frame for test purpose
            if not ret or frame_num > thres_frame:
            # if not ret :
                break
            
            # Process frame - detect license plates
            detection_results = crop_vehicle_license_then_read(input_image=frame,vehicle_conf=vehicle_conf,license_conf=license_conf,frame_number=frame_num)
            # Convert frame to bytes and send
            if websocket.application_state != websockets.WebSocketState.DISCONNECTED:
                await websocket.send_json(detection_results)
    
            # # Control processing rate to not overwhelm the connection ( without this sleep, dataframe visualize and interrupt inference won't work)
            await asyncio.sleep(1/fps)

    except (InvalidState,WebSocketDisconnect) as e:
        logger.error(f"{e}")
    finally:
        logger.info(f"websocket client state : {websocket.client_state}. websocket application state : {websocket.application_state}")
        logger.info(f"Inferenced # of Frame : {frame_num} / {total_frame}")
        
        if websocket.application_state != websockets.WebSocketState.DISCONNECTED:
            await websocket.close(reason="Normal closure")
        else:
            logger.info("Connection is already closed!")
        cap.release()
        # Clean up temp file
        if os.path.exists(video_path):
            os.remove(video_path)