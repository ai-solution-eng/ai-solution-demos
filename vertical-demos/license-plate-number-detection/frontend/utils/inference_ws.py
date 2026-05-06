import json
import websocket
import streamlit as st
import pandas as pd
from utils.logging import logger  
import time

def display_frame(frame_bytes,ann_frame,results_frame):
    logger.info("display frame function called")
    results_frame.append(frame_bytes)
    ann_frame.image(frame_bytes) # Update the placeholder
    logger.info("display frame Done!")

def display_dataframe(detection_results,results_list,result_df):
    logger.info("display dataframe function called")
    results_json = json.loads(detection_results)
    result_df.add_rows(results_json)
    logger.info(f"{results_json}")
    results_list.extend(results_json) # update list
    logger.info("display dataframe Done!")


def vehicle_detection_video_ws(api_host,sess_id,video_path,selected_classes,vehicle_conf,selected_ind,ann_frame,video_inference_ratio):
    # import websocket
    if selected_classes[0] == 'License_Plate':
        ws_url = f"ws://{api_host}/api/video/ws/license_plate/"
    else:
        ws_url = f"ws://{api_host}/api/video/ws/vehicle/"
    ws_url = ws_url + sess_id
    results_frame = []
    ws = websocket.WebSocketApp(
        ws_url,
        on_message=lambda ws, msg: display_frame(msg,ann_frame,results_frame),
        on_error=lambda ws, err: st.error(f"Error: {err}"),
        on_close=lambda ws,status,msg: st.info(f"Processing complete. code {status} : {msg}")
    )

    # Send video path to backend
    ws.on_open = lambda ws: ws.send(json.dumps({"video_path": video_path,"conf":vehicle_conf,"classes":selected_ind,"ratio":video_inference_ratio}))
    with st.spinner("Wait for Inferencing...", show_time=True):
        ws.run_forever()
    for frame in results_frame:
        ann_frame.image(frame)
        time.sleep(0.1)



def license_number_video_infer_ws(api_host,sess_id,video_path,vehicle_conf,license_conf,video_inference_ratio):
    results_list = []
    result_df = st.dataframe(pd.DataFrame(columns=("frame_number","track_id","vehicle_bbox","vehicle_bbox_score","lp_bbox","lp_bbox_score","lp_number","lp_text_score")),hide_index=True)
    # streaming_dataframe(results_list)
    ws = websocket.WebSocketApp(
        f"ws://{api_host}/api/video/ws/license_number/{sess_id}",
        on_message=lambda ws, msg: display_dataframe(msg,results_list,result_df),
        # on_message=lambda ws, msg: display_dataframe(msg,results_list),
        on_error=lambda ws, err: st.error(f"Error: {err}"),
        on_close=lambda ws,status,msg: st.info(f"Processing complete. code {status} : {msg}")
    )
    # Send video path to backend
    ws.on_open = lambda ws: ws.send(json.dumps({"video_path": video_path,"vehicle_conf": vehicle_conf,"license_conf":license_conf,"ratio":video_inference_ratio}))
    ws.run_forever()
    return results_list


def license_number_video_visualize_ws(api_host,sess_id,video_path,detection_result,ann_frame):
    results_frame = []
    ws = websocket.WebSocketApp(
        f"ws://{api_host}/api/utils/ws/draw/annotated/{sess_id}",
        on_message=lambda ws,msg: display_frame(msg,ann_frame,results_frame),
        on_error=lambda ws,err: st.error(f"Error: {err}"),
        on_close=lambda ws,status,msg: st.info(f"Processing complete. code {status} : {msg}")
    )

    # Send video path to backend
    ws.on_open = lambda ws: ws.send(json.dumps({"video_path": video_path,"detection_results":detection_result}))
    with st.spinner("Wait for Visualization...", show_time=True):
        ws.run_forever()

    for frame in results_frame:
        ann_frame.image(frame)
        time.sleep(0.1)

