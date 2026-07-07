import streamlit as st
from ultralytics import YOLO
import cv2
import tempfile
import os
from PIL import Image
import numpy as np
from io import BytesIO
import time

st.set_page_config(page_title="YOLO Object Detection", layout="wide")

MODEL_OPTIONS = {
    "YOLOv8 Nano (fastest, CPU-friendly)": "yolov8n.pt",
    "YOLOv8 Small": "yolov8s.pt",
    "YOLOv8 Medium": "yolov8m.pt",
    "YOLOv8 Large": "yolov8l.pt",
    "YOLOv8 X-Large (most accurate)": "yolov8x.pt",
}

SUPERCATEGORIES = {
    "Person": ["person"],
    "Vehicle": [
        "bicycle", "car", "motorcycle", "airplane", "bus",
        "train", "truck", "boat",
    ],
    "Outdoor": [
        "traffic light", "fire hydrant", "stop sign",
        "parking meter", "bench",
    ],
    "Animal": [
        "bird", "cat", "dog", "horse", "sheep", "cow",
        "elephant", "bear", "zebra", "giraffe",
    ],
    "Accessory": ["backpack", "umbrella", "handbag", "tie", "suitcase"],
    "Sports": [
        "frisbee", "skis", "snowboard", "sports ball", "kite",
        "baseball bat", "baseball glove", "skateboard",
        "surfboard", "tennis racket",
    ],
    "Kitchen": ["bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl"],
    "Food": [
        "banana", "apple", "sandwich", "orange", "broccoli",
        "carrot", "hot dog", "pizza", "donut", "cake",
    ],
    "Furniture": ["chair", "couch", "bed", "dining table", "toilet"],
    "Electronic": ["tv", "laptop", "mouse", "remote", "keyboard", "cell phone"],
    "Appliance": ["microwave", "oven", "toaster", "sink", "refrigerator"],
    "Indoor": [
        "book", "clock", "vase", "scissors", "teddy bear",
        "hair drier", "toothbrush", "potted plant",
    ],
}

COLORS = [
    (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255),
    (0, 255, 255), (128, 0, 0), (0, 128, 0), (0, 0, 128), (128, 128, 0),
    (128, 0, 128), (0, 128, 128), (64, 0, 0), (0, 64, 0), (0, 0, 64),
    (64, 64, 0), (64, 0, 64), (0, 64, 64), (192, 0, 0), (0, 192, 0),
]


@st.cache_resource
def load_model(model_path):
    return YOLO(model_path)


def draw_boxes(image, results, selected_classes, confidence_threshold, class_names):
    annotated = image.copy()
    for result in results:
        boxes = result.boxes
        if boxes is None:
            continue
        for box in boxes:
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])
            class_name = class_names[cls_id]

            if class_name not in selected_classes:
                continue
            if conf < confidence_threshold:
                continue

            x1, y1, x2, y2 = map(int, box.xyxy[0])
            color = COLORS[cls_id % len(COLORS)]
            label = f"{class_name} {conf:.2f}"

            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            label_y1 = y1 - th - 6
            if label_y1 < 0:
                label_y1 = y2 + 2
                text_y = y2 + th + 6
            else:
                text_y = y1 - 4
            label_x2 = x1 + tw + 4
            img_w = annotated.shape[1]
            if label_x2 > img_w:
                x1 = max(0, x1 - (label_x2 - img_w))
                label_x2 = img_w
            cv2.rectangle(annotated, (x1, label_y1), (label_x2, label_y1 + th + 6), color, -1)
            cv2.putText(
                annotated, label, (x1 + 2, text_y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA,
            )
    return annotated


# --- Sidebar ---
st.sidebar.title("Controls")

model_name = st.sidebar.selectbox(
    "Select YOLO model",
    list(MODEL_OPTIONS.keys()),
    index=0,
)
model_path = MODEL_OPTIONS[model_name]

confidence_threshold = st.sidebar.slider(
    "Confidence threshold", 0.0, 1.0, 0.25, 0.05,
)

with st.spinner("Loading model..."):
    model = load_model(model_path)
    class_names = model.names
    all_class_names = set(class_names.values())
st.sidebar.success(f"Model ready ({len(class_names)} classes)")

st.sidebar.markdown("### Upload to Filesystem")
uploads_dir = os.path.join(os.getcwd(), "uploads")
os.makedirs(uploads_dir, exist_ok=True)
st.sidebar.caption(f"Files are saved to `{uploads_dir}/`")

uploaded_for_save = st.sidebar.file_uploader(
    "Drop images or videos here to save to filesystem",
    type=["jpg", "jpeg", "png", "bmp", "webp", "mp4", "avi", "mov", "mkv"],
    accept_multiple_files=True,
    key="save_uploader",
)
if uploaded_for_save:
    save_key = tuple(f.name for f in uploaded_for_save)
    if st.session_state.get("last_saved") != save_key:
        st.session_state["last_saved"] = save_key
        for f in uploaded_for_save:
            path = os.path.join(uploads_dir, f.name)
            with open(path, "wb") as out:
                out.write(f.read())
        st.sidebar.success(f"Saved {len(uploaded_for_save)} file(s) to `uploads/`")

with st.sidebar.expander("Manage saved files", expanded=False):
    saved_files = sorted(os.listdir(uploads_dir)) if os.path.isdir(uploads_dir) else []
    if saved_files:
        files_to_delete = []
        for fname in saved_files:
            if st.checkbox(fname, key=f"del_{fname}"):
                files_to_delete.append(fname)
        if files_to_delete and st.button(f"Delete selected ({len(files_to_delete)})", use_container_width=True):
            for fname in files_to_delete:
                path = os.path.join(uploads_dir, fname)
                try:
                    os.remove(path)
                except Exception as e:
                    st.sidebar.error(f"Failed to delete {fname}: {e}")
            st.rerun()
    else:
        st.sidebar.info("No files in uploads/.")

st.sidebar.markdown("### Process Local File")

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
ALL_MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS

default_samples_dir = os.path.join(os.getcwd(), "default_samples")
source_options = {"uploads": uploads_dir}
if os.path.isdir(default_samples_dir):
    source_options["default_samples"] = default_samples_dir

source_label = st.sidebar.radio("Source folder", list(source_options.keys()))
source_dir = source_options[source_label]

if os.path.isdir(source_dir):
    files = sorted(
        f for f in os.listdir(source_dir)
        if os.path.splitext(f)[1].lower() in ALL_MEDIA_EXTENSIONS
    )
    if files:
        selected_file = st.sidebar.selectbox("Select file", files)
        if st.sidebar.button("Process", type="primary", use_container_width=True):
            st.session_state["local_file_to_process"] = os.path.join(source_dir, selected_file)
            st.rerun()
    else:
        st.sidebar.info(f"No media files in {source_label}/.")

st.sidebar.markdown("### Object categories")
selected_classes = set()

all_available = {}
for category, items in SUPERCATEGORIES.items():
    available = [name for name in items if name in all_class_names]
    if available:
        all_available[category] = available

if all_available:
    col_left, col_right = st.sidebar.columns(2)
    if col_left.button("Select All", use_container_width=True):
        for cat, items in all_available.items():
            st.session_state[f"super_{cat}"] = True
            st.session_state[f"cat_{cat}"] = items
        st.rerun()
    if col_right.button("Clear All", use_container_width=True):
        for cat in all_available:
            st.session_state[f"super_{cat}"] = False
            st.session_state[f"cat_{cat}"] = []
        st.rerun()

for category, items in all_available.items():
    super_key = f"super_{category}"
    if super_key not in st.session_state:
        st.session_state[super_key] = True
    if f"cat_{category}" not in st.session_state:
        st.session_state[f"cat_{category}"] = items

    with st.sidebar.expander(category, expanded=True):
        enabled = st.checkbox(
            f"Enable all {category.lower()}",
            key=super_key,
            label_visibility="collapsed",
        )
        selected = st.multiselect(
            f"Select {category.lower()}",
            items,
            default=items if enabled else [],
            key=f"cat_{category}",
            label_visibility="collapsed",
            disabled=not enabled,
        )
        selected_classes.update(selected if enabled else [])


def process_image_input(img_array, file_name):
    if len(img_array.shape) == 3 and img_array.shape[2] == 3:
        img_bgr = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)
    else:
        img_bgr = img_array

    with st.spinner("Running inference..."):
        results = model(img_bgr, verbose=False)

    annotated_bgr = draw_boxes(
        img_bgr, results, selected_classes, confidence_threshold, class_names
    )
    annotated_rgb = cv2.cvtColor(annotated_bgr, cv2.COLOR_BGR2RGB)

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Original")
        st.image(img_array, width="stretch")
    with col2:
        st.subheader("Predictions")
        st.image(annotated_rgb, channels="RGB", width="stretch")

    detections = []
    for r in results:
        if r.boxes is None:
            continue
        for box in r.boxes:
            cls_id = int(box.cls[0])
            class_name = class_names[cls_id]
            conf = float(box.conf[0])
            if class_name not in selected_classes or conf < confidence_threshold:
                continue
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            detections.append({
                "Class": class_name,
                "Confidence": f"{conf:.2f}",
                "X1": x1,
                "Y1": y1,
                "X2": x2,
                "Y2": y2,
                "Width": x2 - x1,
                "Height": y2 - y1,
            })

    st.subheader(f"Detected Objects ({len(detections)})")
    if detections:
        st.dataframe(detections, hide_index=True, use_container_width=True)
    else:
        st.caption("No objects detected with the current filters.")


def process_video_input(input_path, output_name):
    st.subheader("Video Processing")

    cap = cv2.VideoCapture(input_path)
    fps = int(cap.get(cv2.CAP_PROP_FPS))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    if total_frames == 0:
        st.error("Could not read video file.")
        cap.release()
        return

    output_video_path = tempfile.mktemp(suffix=".mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    out_writer = cv2.VideoWriter(output_video_path, fourcc, fps, (width, height))

    progress_bar = st.progress(0, text="Processing video...")
    frame_placeholder = st.empty()
    status_text = st.empty()

    frame_count = 0
    start_time = time.time()
    annotated_frames = []

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        results = model(frame, verbose=False)
        annotated = draw_boxes(
            frame, results, selected_classes, confidence_threshold, class_names
        )
        out_writer.write(annotated)

        annotated_rgb = cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB)
        frame_placeholder.image(annotated_rgb, channels="RGB", width="stretch")

        frame_count += 1
        progress = frame_count / total_frames
        elapsed = time.time() - start_time
        fps_est = frame_count / elapsed if elapsed > 0 else 0
        progress_bar.progress(
            progress,
            text=f"Frame {frame_count}/{total_frames} — {fps_est:.1f} FPS",
        )
        status_text.text(
            f"Processed {frame_count}/{total_frames} frames ({progress * 100:.1f}%)"
        )

    cap.release()
    out_writer.release()

    progress_bar.progress(1.0, text="Done!")
    status_text.text(
        f"Finished processing {frame_count} frames in {time.time() - start_time:.1f}s"
    )

    with open(output_video_path, "rb") as f:
        video_bytes = f.read()

    st.session_state["annotated_video_bytes"] = video_bytes
    st.session_state["annotated_video_name"] = "annotated_" + output_name
    st.session_state["annotated_video_path"] = output_video_path
    st.session_state["frame_count"] = frame_count


# --- Main area ---
st.title("YOLO Object Detection")
st.caption(f"Model: {model_name} — {len(selected_classes)}/{len(class_names)} categories selected")

if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0

uploaded_for_process = st.file_uploader(
    "Drag & drop images or videos here to process",
    type=["jpg", "jpeg", "png", "bmp", "webp", "mp4", "avi", "mov", "mkv"],
    accept_multiple_files=True,
    key=f"process_uploader_{st.session_state.uploader_key}",
)

if st.button("Clear results"):
    keys = ["annotated_video_bytes", "annotated_video_name", "annotated_video_path",
            "frame_count", "input_path", "output_name", "local_file_to_process",
            "last_processed"]
    for key in keys:
        val = st.session_state.pop(key, None)
        if key == "annotated_video_path" and val and os.path.exists(val):
            os.unlink(val)
    st.session_state.uploader_key += 1
    st.rerun()

if uploaded_for_process:
    processed_key = tuple(f.name for f in uploaded_for_process)
    if st.session_state.get("last_processed") != processed_key:
        st.session_state["last_processed"] = processed_key
        for f in uploaded_for_process:
            file_bytes = f.read()
            ext = os.path.splitext(f.name)[1].lower()

            st.divider()
            st.subheader(f"Processing: {f.name}")

            if ext in VIDEO_EXTENSIONS:
                with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                    tmp.write(file_bytes)
                    tmp_path = tmp.name
                process_video_input(tmp_path, f.name)
                os.unlink(tmp_path)
                st.rerun()
            else:
                image = Image.open(BytesIO(file_bytes))
                process_image_input(np.array(image), f.name)

processed_video = st.session_state.get("annotated_video_bytes")
processed_video_name = st.session_state.get("annotated_video_name")
processed_video_path = st.session_state.get("annotated_video_path")
frame_count = st.session_state.get("frame_count", 0)

local_file_to_process = st.session_state.pop("local_file_to_process", None)

if processed_video is not None:
    st.download_button(
        label="Download annotated video",
        data=processed_video,
        file_name=processed_video_name,
        mime="video/mp4",
        use_container_width=True,
    )

    if processed_video_path and os.path.exists(processed_video_path) and frame_count > 0:
        cap = cv2.VideoCapture(processed_video_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total > 0:
            frame_idx = st.slider("Browse frames", 0, total - 1, 0, key="frame_slider")
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if ret:
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                st.image(frame_rgb, channels="RGB", width="stretch")
        cap.release()

elif local_file_to_process:
    if not os.path.exists(local_file_to_process):
        st.error(f"File not found: {local_file_to_process}")
        st.stop()

    ext = os.path.splitext(local_file_to_process)[1].lower()
    file_name = os.path.basename(local_file_to_process)

    if ext in VIDEO_EXTENSIONS:
        process_video_input(local_file_to_process, file_name)
        st.rerun()
    else:
        img_array = np.array(Image.open(local_file_to_process))
        process_image_input(img_array, file_name)

elif not uploaded_for_process:
    st.info("Drag & drop images or videos above to process, or use the sidebar to browse local files.", icon="📤")
