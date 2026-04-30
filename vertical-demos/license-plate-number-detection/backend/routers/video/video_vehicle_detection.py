import os
from fastapi import APIRouter
from fastapi.responses import StreamingResponse, FileResponse
from fastapi import Query
from pydantic import BaseModel

from fastapi import WebSocket,WebSocketDisconnect,websockets
from websockets.exceptions import InvalidState
import asyncio

import numpy as np
import cv2 as cv

from utils.detector import vehicle_detect_bytes,license_detect_bytes
from utils.logging import logger


class inferenceRequest(BaseModel):
    video_path: str
    output_path: str
    conf: float = 0.25
    ratio: float = 0.25

router = APIRouter(
    prefix="/api/video",
)   


@router.post(
    "/vehicles/detect/annotatedVideo",
    tags=["Vehicles Detection"],
    response_class=StreamingResponse,
    summary="Detect Vehicles in Video",
    description="Get Traffic Video, Return Annotated Video",
)
def get_video_return_vehicle_annotatedVideo(inferencerequest : inferenceRequest,classes: list[int] = Query()):
    '''
    Does not Use Tracker
    '''
    video_path = inferencerequest.video_path
    output_path = inferencerequest.output_path
    conf = inferencerequest.conf
    ratio = inferencerequest.ratio
    # Process video
    cap = cv.VideoCapture(video_path)
    fps = cap.get(cv.CAP_PROP_FPS)
    
    fourcc = cv.VideoWriter_fourcc(*'avc1')  # Specify the codec
    width = int(cap.get(cv.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv.CAP_PROP_FRAME_HEIGHT))
    output_path = output_path
    out = cv.VideoWriter(output_path, fourcc, fps, (width, height))


    total_frame = cap.get(cv.CAP_PROP_FRAME_COUNT)
    thres_frame = total_frame*ratio
    frame_num = -1

    while cap.isOpened():
        frame_num += 1
        ret, frame = cap.read()
        ### limit 10 frame for test purpose
        if not ret or frame_num > thres_frame:
        # if not ret :
            break

        return_bytes = vehicle_detect_bytes(frame,conf,classes)
        # Read bytes -> convert to cv image -> write
        image_np = np.frombuffer(return_bytes.read(), np.uint8)
        input_image = cv.imdecode(image_np, cv.IMREAD_COLOR) 
        out.write(input_image)

    out.release()
    cap.release()

    return FileResponse(output_path,media_type="video/mp4")

@router.post(
    "/plates/detect/annotatedVideo",
    tags=["Plates Detection"],
    response_class=StreamingResponse,
    summary="Detect license plates in Video",
    description="Get Traffic Video, Return Annotated Video",
)
def get_video_return_plate_annotatedVideo(inferencerequest : inferenceRequest,classes: list[int] = Query()):
    '''
    Does not Use Tracker
    '''
    video_path = inferencerequest.video_path
    output_path = inferencerequest.output_path
    conf = inferencerequest.conf
    ratio = inferencerequest.ratio

    # Process video
    cap = cv.VideoCapture(video_path)
    fps = cap.get(cv.CAP_PROP_FPS)
    
    fourcc = cv.VideoWriter_fourcc(*'avc1')  # Specify the codec
    width = int(cap.get(cv.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv.CAP_PROP_FRAME_HEIGHT))
    out = cv.VideoWriter(output_path, fourcc, fps, (width, height))


    total_frame = cap.get(cv.CAP_PROP_FRAME_COUNT)
    thres_frame = total_frame * ratio
    frame_num = -1

    while cap.isOpened():
        frame_num += 1
        ret, frame = cap.read()
        ### limit 10 frame for test purpose
        if not ret or frame_num > thres_frame:
        # if not ret :
            break

        return_bytes = license_detect_bytes(frame,conf)
        # Read bytes -> convert to cv image -> write
        image_np = np.frombuffer(return_bytes.read(), np.uint8)
        input_image = cv.imdecode(image_np, cv.IMREAD_COLOR) 
        out.write(input_image)

    out.release()
    cap.release()

    return FileResponse(output_path,media_type="video/mp4")


@router.websocket("/ws/vehicle/{session_id}")
async def get_video_return_vehicle_annotatedFrame_ws(websocket: WebSocket, session_id: str):
    '''
    Does not Use Tracker
    '''
    await websocket.accept()
    
    # Get video path from session data (simplified here)
    data = await websocket.receive_json()
    video_path = data["video_path"]
    conf = data["conf"]
    classes = data["classes"]
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

            return_bytes = vehicle_detect_bytes(frame,conf,classes)
            # Send bytes 
            if websocket.application_state != websockets.WebSocketState.DISCONNECTED:
                await websocket.send_bytes(return_bytes)
    
            # # Control processing rate to not overwhelm the connection
            # await asyncio.sleep(1/fps)
            await asyncio.sleep(0.05)
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

@router.websocket("/ws/license_plate/{session_id}")
async def get_video_return_plate_annotatedFrame_ws(websocket: WebSocket, session_id: str):
    '''
    Does not Use Tracker
    '''
    await websocket.accept()
    
    # Get video path from session data (simplified here)
    data = await websocket.receive_json()
    video_path = data["video_path"]
    conf = data["conf"]
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
            return_bytes = license_detect_bytes(frame,conf)
            # Convert frame to bytes and send
            if websocket.application_state != websockets.WebSocketState.DISCONNECTED:
                await websocket.send_bytes(return_bytes)
    
            # # Control processing rate to not overwhelm the connection
            # await asyncio.sleep(1/fps)
            await asyncio.sleep(0.05)
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