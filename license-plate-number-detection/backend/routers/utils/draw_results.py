from fastapi import APIRouter
from fastapi import UploadFile, Form
from fastapi.responses import StreamingResponse,FileResponse
from fastapi import WebSocket,WebSocketDisconnect,websockets
from websockets.exceptions import InvalidState

import json
import numpy as np
import pandas as pd
import cv2 as cv

from utils.detector import get_bytes_from_prediction
from utils.logging import logger
from pydantic import BaseModel
from typing import Dict

import asyncio
import os


class DrawRequest(BaseModel):
    video_path: str
    output_path: str
    detection_result: list[Dict] = []

router = APIRouter(
    prefix="/api/utils",
)

@router.post(
    "/draw/annotatedVideo",
    tags=["Utils"],
    response_class=StreamingResponse,
    summary="Draw Annotated Video",
    description="Get Image and detection info -> Draw and Return Annotated Video",
)
def get_info_return_annotatedVideo(drawrequest: DrawRequest):
    """
    Get video and detection info -> Draw and Return Annotated Video
    
    Args:
        Video : Traffic Video
        info : Detection Info
        
    Returns:
        Image (BytesIO): Annotated Image with license number detection by Yolo
    """
    video_path = drawrequest.video_path
    output_path = drawrequest.output_path
    detection_results = drawrequest.detection_result
    
    results_df = pd.DataFrame(detection_results)
    max_frame_num = results_df["frame_number"].max()
    cap = cv.VideoCapture(video_path)
    fps = cap.get(cv.CAP_PROP_FPS)
    total_frame = cap.get(cv.CAP_PROP_FRAME_COUNT)

    # output video 
    fourcc = cv.VideoWriter_fourcc(*'avc1')  # Specify the codec
    width = int(cap.get(cv.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv.CAP_PROP_FRAME_HEIGHT))
    out = cv.VideoWriter(output_path, fourcc, fps, (width, height))

    frame_num = -1

    while cap.isOpened():
        frame_num += 1
        ret, frame = cap.read()
        ### limit 10 frame for test purpose
        if not ret or frame_num > max_frame_num:
            break
        df_ = results_df[results_df['frame_number'] == frame_num]
        for index in range(len(df_)):
            # if str(df_.iloc[index]['lp_number']) != 'nan':
                # draw vehicle bbox
                logger.info(f"lp_number : {df_.iloc[index]['lp_number']}")
                vhcl_x1, vhcl_y1, vhcl_x2, vhcl_y2 = df_.iloc[index]['vehicle_bbox']
                cv.rectangle(frame, (int(vhcl_x1), int(vhcl_y1)),(int(vhcl_x2), int(vhcl_y2)), (0, 255, 0), 8)
                # draw license plate bbox
                plate_x1, plate_y1, plate_x2, plate_y2 = df_.iloc[index]['lp_bbox']

                # region of interest
                roi = frame[int(vhcl_y1):int(vhcl_y2), int(vhcl_x1):int(vhcl_x2)]
                cv.rectangle(roi, (int(plate_x1), int(plate_y1)), (int(plate_x2), int(plate_y2)), (0, 0, 255), 6)

                # write detected number
                (text_width, text_height), _ = cv.getTextSize(str(df_.iloc[index]['lp_number']),cv.FONT_HERSHEY_DUPLEX,2,6)
                cv.putText(
                        roi,
                        str(df_.iloc[index]['lp_number']),
                        (int((plate_x2 + plate_x1 - text_width)/2), int(plate_y1 - text_height)),
                        cv.FONT_HERSHEY_DUPLEX,
                        2,
                        (0, 255, 0),
                        6
                    )
                return_bytes = get_bytes_from_prediction(frame,quality=65)
                image_np = np.frombuffer(return_bytes.read(), np.uint8)
                input_image = cv.imdecode(image_np, cv.IMREAD_COLOR) 
        out.write(input_image)
    out.release()
    cap.release()

    return FileResponse(output_path,media_type="video/mp4")

@router.post(
    "/draw/annotatedImage",
    tags=["Utils"],
    response_class=StreamingResponse,
    summary="Draw Annotated Image",
    description="Get Image and detection info -> Draw and Return Annotated Image",
)
def get_info_return_annotatedImage(image: UploadFile,item_json: str = Form()):
    file = image.file.read()
    image_np = np.frombuffer(file, np.uint8)
    input_image = cv.imdecode(image_np, cv.IMREAD_COLOR) 

    info = json.loads(item_json)
    
    for object in range(len(info)):
        vhcl_x1, vhcl_y1, vhcl_x2, vhcl_y2 = info[object]['vehicle_bbox']
        
        cv.rectangle(input_image, (int(vhcl_x1), int(vhcl_y1)),(int(vhcl_x2), int(vhcl_y2)), (0, 255, 0), 8)
        plate_x1, plate_y1, plate_x2, plate_y2 = info[object]['lp_bbox']
        roi = input_image[int(vhcl_y1):int(vhcl_y2), int(vhcl_x1):int(vhcl_x2)]
        cv.rectangle(roi, (int(plate_x1), int(plate_y1)), (int(plate_x2), int(plate_y2)), (0, 0, 255), 6)

        # write detected number
        (text_width, text_height), _ = cv.getTextSize(info[object]["lp_number"],cv.FONT_HERSHEY_DUPLEX,2,6)
        cv.putText(
                roi,
                info[object]["lp_number"],
                (int((plate_x2 + plate_x1 - text_width)/2), int(plate_y1 - text_height)),
                cv.FONT_HERSHEY_DUPLEX,
                2,
                (0, 255, 0),
                6
            )   
    
    return_bytes = get_bytes_from_prediction(input_image,quality=65)
    # return_bytes.seek(0)
    return StreamingResponse(content=return_bytes,media_type="image/jpeg")


@router.websocket("/ws/draw/annotated/{session_id}")
async def get_info_return_annotatedFrame_ws(websocket: WebSocket):
    """
    Get video and detection info -> Draw and Return Annotated Image
    
    Args:
        Video : Traffic Video
        info : Detection Info
        
    Returns:
        Image (BytesIO): Annotated Image with license number detection by Yolo
    """
    await websocket.accept()

    data = await websocket.receive_json()
    video_path = data["video_path"]
    detection_results = data["detection_results"]
    results_df = pd.DataFrame(detection_results)
    max_frame_num = results_df["frame_number"].max()
    cap = cv.VideoCapture(video_path)
    fps = cap.get(cv.CAP_PROP_FPS)
    total_frame = cap.get(cv.CAP_PROP_FRAME_COUNT)
    frame_num = -1

    try:
        while cap.isOpened():
            frame_num += 1
            ret, frame = cap.read()
            ### limit 10 frame for test purpose
            if not ret or frame_num > max_frame_num:
            # if not ret :
                break
            df_ = results_df[results_df['frame_number'] == frame_num]
            for index in range(len(df_)):
                # if str(df_.iloc[index]['lp_number']) != 'nan':
                    # draw vehicle bbox
                    logger.info(f"lp_number : {df_.iloc[index]['lp_number']}")
                    vhcl_x1, vhcl_y1, vhcl_x2, vhcl_y2 = df_.iloc[index]['vehicle_bbox']
                    cv.rectangle(frame, (int(vhcl_x1), int(vhcl_y1)),(int(vhcl_x2), int(vhcl_y2)), (0, 255, 0), 8)
                    # draw license plate bbox
                    plate_x1, plate_y1, plate_x2, plate_y2 = df_.iloc[index]['lp_bbox']

                    # region of interest
                    roi = frame[int(vhcl_y1):int(vhcl_y2), int(vhcl_x1):int(vhcl_x2)]
                    cv.rectangle(roi, (int(plate_x1), int(plate_y1)), (int(plate_x2), int(plate_y2)), (0, 0, 255), 6)

                    # write detected number
                    (text_width, text_height), _ = cv.getTextSize(str(df_.iloc[index]['lp_number']),cv.FONT_HERSHEY_DUPLEX,2,6)
                    cv.putText(
                            roi,
                            str(df_.iloc[index]['lp_number']),
                            (int((plate_x2 + plate_x1 - text_width)/2), int(plate_y1 - text_height)),
                            cv.FONT_HERSHEY_DUPLEX,
                            2,
                            (0, 255, 0),
                            6
                        )
                    return_bytes = get_bytes_from_prediction(frame,quality=65)
                    if websocket.application_state != websockets.WebSocketState.DISCONNECTED:
                        await websocket.send_bytes(return_bytes)
    
                    # Control processing rate to not overwhelm the connection
                    # await asyncio.sleep(1/fps)
                    await asyncio.sleep(0.1)
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