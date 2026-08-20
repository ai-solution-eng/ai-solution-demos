"""Vessel Geometry Analysis and 3D Reconstruction - optimized Streamlit app."""
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

# Import your custom modules
from analysis_pipeline import AnalysisResult, enhanced_vessel_reconstruction_analysis
from utils.mesh_utils import create_zip_of_mesh_in_memory
from utils.plot_utils import lineset_to_plotly, mesh_to_plotly, pcd_to_plotly


# ============================================================================
# Page configuration
# ============================================================================
st.set_page_config(
    page_title="Vessel Geometry Analysis",
    page_icon="🩸",
    layout="wide",
    initial_sidebar_state="expanded",
)

BASE_DATA_DIR = Path("./outputs")

# Vessel color palette - hex for CSS, named colors for Plotly traces
VESSEL_HEX_COLORS = {
    "Aorta": "#ef4444",
    "Left Iliac Artery": "#10b981",
    "Right Iliac Artery": "#3b82f6",
}
VESSEL_PLOTLY_COLORS = {
    "Aorta": "red",
    "Left Iliac Artery": "green",
    "Right Iliac Artery": "blue",
}
# Per-label 1/2/3 overlay colors (matches the previous hard-coded values)
SEG_OVERLAY_COLORS = np.array(
    [[255, 100, 100], [100, 255, 100], [100, 100, 255]],
    dtype=np.uint8,
)


# ============================================================================
# Visual styling - clean medical / scientific (light, minimal)
# ============================================================================
CUSTOM_CSS = """
<style>
:root {
  --vessel-bg: #f8fafc;
  --vessel-card: #ffffff;
  --vessel-border: #e2e8f0;
  --vessel-text: #1e293b;
  --vessel-text-muted: #64748b;
  --vessel-accent: #0d9488;
  --vessel-shadow: 0 1px 3px rgba(0,0,0,0.05), 0 1px 2px rgba(0,0,0,0.03);
}

.block-container {
    padding-top: 2.5rem;
    padding-bottom: 3rem;
    max-width: 1400px;
}

h1, h2, h3, h4, h5 {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important;
    letter-spacing: -0.01em !important;
    color: var(--vessel-text) !important;
}
h1 { font-weight: 700 !important; font-size: 1.9rem !important; margin-bottom: 0.25rem !important; }
h2 { font-weight: 600 !important; font-size: 1.35rem !important; }
h3 { font-weight: 600 !important; font-size: 1.05rem !important; }

p { color: var(--vessel-text) !important; }
.stMarkdown p { font-size: 0.92rem; line-height: 1.5; }

[data-testid="stMetric"] {
    background: var(--vessel-card);
    border: 1px solid var(--vessel-border);
    border-left: 3px solid var(--vessel-accent);
    border-radius: 8px;
    padding: 0.85rem 1rem !important;
    box-shadow: var(--vessel-shadow);
}
[data-testid="stMetric"] label {
    font-size: 0.7rem !important;
    color: var(--vessel-text-muted) !important;
    font-weight: 500 !important;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}
[data-testid="stMetric"] [data-testid="stMetricValue"] {
    font-size: 1.35rem !important;
    color: var(--vessel-text) !important;
    font-weight: 600 !important;
}

[data-testid="stSidebar"] {
    background: var(--vessel-card);
    border-right: 1px solid var(--vessel-border);
}
[data-testid="stSidebar"] h2 {
    font-size: 0.78rem !important;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--vessel-text-muted) !important;
    font-weight: 600 !important;
    margin-bottom: 0.5rem !important;
}

.stTabs [data-baseweb="tab-list"] { gap: 6px; }
.stTabs [data-baseweb="tab"] {
    padding: 8px 14px !important;
    background: #f1f5f9 !important;
    border-radius: 8px 8px 0 0 !important;
    border: 1px solid var(--vessel-border) !important;
    border-bottom: none !important;
    font-size: 0.85rem !important;
    font-weight: 500 !important;
    color: var(--vessel-text-muted) !important;
}
.stTabs [data-baseweb="tab"][aria-selected="true"] {
    background: var(--vessel-card) !important;
    border-bottom: 2px solid var(--vessel-accent) !important;
    color: var(--vessel-accent) !important;
    font-weight: 600 !important;
}
.stTabs [data-baseweb="tab-border"] { display: none; }
.stTabs [data-baseweb="tab-highlight"] { background-color: var(--vessel-accent) !important; }

.stButton > button[kind="primary"] {
    background-color: var(--vessel-accent) !important;
    border-color: var(--vessel-accent) !important;
    color: white !important;
    border-radius: 6px !important;
    font-weight: 500 !important;
    padding: 0.45rem 1rem !important;
}
.stButton > button[kind="secondary"] {
    background: var(--vessel-card) !important;
    border: 1px solid var(--vessel-border) !important;
    color: var(--vessel-text) !important;
    border-radius: 6px !important;
}
.stDownloadButton > button {
    background: var(--vessel-card) !important;
    border: 1px solid var(--vessel-accent) !important;
    color: var(--vessel-accent) !important;
    border-radius: 6px !important;
    font-weight: 500 !important;
}

.streamlit-expanderHeader {
    font-weight: 600 !important;
    background: #f8fafc;
    border-radius: 8px !important;
    font-size: 0.95rem !important;
}

.vessel-badge-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
    margin: 0.75rem 0 1.25rem 0;
}
.vessel-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.3rem 0.75rem;
    background: #ecfeff;
    color: #155e75;
    border: 1px solid #a5f3fc;
    border-radius: 9999px;
    font-size: 0.78rem;
    font-weight: 500;
}
.vessel-badge .vessel-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
}
.vessel-card-header {
    border-left: 4px solid var(--vessel-accent);
    padding: 0.15rem 0 0.15rem 0.85rem;
    margin-bottom: 0.6rem;
}
.vessel-card-header h4 {
    margin: 0 !important;
    font-size: 1rem !important;
    font-weight: 600 !important;
}

.vessel-footer {
    margin-top: 3rem;
    padding-top: 1rem;
    border-top: 1px solid var(--vessel-border);
    font-size: 0.72rem;
    color: var(--vessel-text-muted);
    text-align: center;
    letter-spacing: 0.02em;
}
</style>
"""

st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ============================================================================
# Cached data helpers
# ============================================================================
@st.cache_data
def find_patient_scans(base_dir: Path) -> Dict[str, Dict[str, Dict[str, str]]]:
    """
    Scans the outputs tree to find patients and their associated scans.
    For each patient, it finds all available scans by matching filenames across
    nifti, segmentation, and point cloud directories.
    """
    patient_scan_data: Dict[str, Dict[str, Dict[str, str]]] = {}

    nifti_dir = base_dir / "nifti_data"
    seg_dir = base_dir / "processed_segmentations"
    pcd_dir = base_dir / "pointclouds"

    if not all([nifti_dir.is_dir(), seg_dir.is_dir(), pcd_dir.is_dir()]):
        st.sidebar.error(
            f"One or more required base directories not found in '{base_dir}'."
        )
        return {}

    patient_ids = [
        p.name for p in nifti_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    ]

    for pid in sorted(patient_ids):
        patient_scans: Dict[str, Dict[str, str]] = {}
        patient_nifti_dir = nifti_dir / pid
        for nifti_file in patient_nifti_dir.glob("*.nii*"):
            scan_name = nifti_file.name.replace(".nii.gz", "").replace(".nii", "")
            seg_file = seg_dir / pid / f"{scan_name}.npz"
            pcd_file = pcd_dir / pid / f"{scan_name}.npz"
            if seg_file.exists() and pcd_file.exists():
                patient_scans[scan_name] = {
                    "nifti": str(nifti_file),
                    "seg": str(seg_file),
                    "pcd": str(pcd_file),
                }
        if patient_scans:
            patient_scan_data[pid] = patient_scans
        else:
            st.sidebar.warning(f"No complete scan sets found for patient `{pid}`.")
    return patient_scan_data


@st.cache_data(show_spinner=False, max_entries=3)
def run_analysis(
    nifti_path: str,
    seg_path: str,
    pcd_path: str,
    patient_id: str,
    nifti_mtime: float,
    seg_mtime: float,
    pcd_mtime: float,
):
    """
    Cached wrapper around the main analysis function, keyed on file paths and
    their modification times so content changes invalidate the cache.
    """
    with tempfile.TemporaryDirectory() as temp_dir_path:
        status_container = st.empty()

        def status_callback(message):
            status_container.status(f"`{message}`", expanded=True)

        results = enhanced_vessel_reconstruction_analysis(
            scan_nifti_file=nifti_path,
            segmentation_file=seg_path,
            pointcloud_file=pcd_path,
            destination_folder=Path(temp_dir_path),
            patient_id=patient_id,
            status_callback=status_callback,
        )

        for vessel_name, data in results.get("vessels", {}).items():
            if plot_path_str := data.get("diameter_plot_path"):
                plot_path = Path(plot_path_str)
                if plot_path.exists():
                    with open(plot_path, "rb") as f:
                        data["diameter_plot_bytes"] = f.read()
                else:
                    data["diameter_plot_bytes"] = None

        status_container.empty()
        return results


# ============================================================================
# Session-state caches (in-memory only - not pickled by st.cache_data)
# ============================================================================
def get_traces_cache(results: AnalysisResult, analysis_id: str) -> dict:
    """Build Plotly 3D traces once per analysis_id; reuse across reruns."""
    cache = st.session_state.setdefault("traces_cache", {})
    if analysis_id in cache:
        return cache[analysis_id]

    mesh_traces, centerline_traces = [], []
    pcd_trace = None
    combined_meshes: Dict[str, dict] = {}

    pcd_data = (results.get("point_cloud") or {}).get("geometry")
    if pcd_data is not None:
        pcd_trace = pcd_to_plotly(pcd_data, name="Point Cloud", showlegend=True)

    for vessel_name, data in results.get("vessels", {}).items():
        color = VESSEL_PLOTLY_COLORS.get(vessel_name, "gray")
        if data.get("mesh"):
            mesh_traces.append(
                mesh_to_plotly(
                    data["mesh"],
                    color=color,
                    name=f"{vessel_name} Mesh",
                    showlegend=True,
                )
            )
            combined_meshes[vessel_name] = data["mesh"]
        if data.get("centerline"):
            centerline_traces.append(
                lineset_to_plotly(
                    data["centerline"],
                    color="yellow",
                    name=f"{vessel_name} Centerline",
                    showlegend=True,
                )
            )
        if data.get("max_diameter_disc"):
            centerline_traces.append(
                mesh_to_plotly(
                    data["max_diameter_disc"],
                    color="magenta",
                    name=f"{vessel_name} Max Diameter",
                    showlegend=True,
                )
            )

    cache[analysis_id] = {
        "pcd": pcd_trace,
        "meshes": mesh_traces,
        "centerlines": centerline_traces,
        "combined_meshes": combined_meshes,
    }
    return cache[analysis_id]


def get_stl_zip(analysis_id: str, combined_meshes: Dict[str, dict]):
    """Cache the STL zip once per analysis_id (was rebuilt on every rerun before)."""
    cache = st.session_state.setdefault("stl_zip_cache", {})
    if analysis_id not in cache:
        cache[analysis_id] = create_zip_of_mesh_in_memory(combined_meshes)
    return cache[analysis_id]


def get_ct_rgb_volume(results: AnalysisResult) -> Optional[np.ndarray]:
    """
    Return the pre-computed uint8 RGB CT volume, with backward-compat for
    stale caches populated by older pipeline versions that stored ct_scan (float).
    """
    vol = results.get("ct_rgb_volume")
    if vol is not None:
        return vol
    legacy = results.get("ct_scan")
    if legacy is None:
        return None
    # Stale cache from a previous pipeline version - normalize on-the-fly.
    ct_min = float(legacy.min())
    ct_max = float(legacy.max())
    if ct_max - ct_min < 1e-6:
        ct_max = ct_min + 1.0
    normalized = ((legacy - ct_min) / (ct_max - ct_min) * 255).astype(np.uint8)
    vol = np.repeat(normalized[..., np.newaxis], 3, axis=-1)
    results["ct_rgb_volume"] = vol  # stash back so subsequent calls are O(1)
    return vol


# ============================================================================
# CT slice viewer - isolated fragment so slider moves do NOT trigger a full rerun
# ============================================================================
@st.fragment(run_every=None)
def _ct_viewer_fragment(
    ct_rgb_volume: np.ndarray, seg_volume: np.ndarray, analysis_id: str
):
    """Render a single CT slice with optional segmentation overlay.

    Wrapped in @st.fragment so the slider only re-executes this block - the
    3D tabs, STL zip download, and metric cards are not recomputed when the
    user scrolls through slices. Slice rendering uses st.image (raw HxWx3
    array) instead of a Plotly go.Image figure to skip Plotly JSON
    serialization entirely.
    """
    max_slice = int(ct_rgb_volume.shape[2] - 1)
    key_slider = f"ct_slice_idx_{analysis_id}"
    key_mode = f"ct_mode_{analysis_id}"

    controls_col, _, _ = st.columns([3, 1, 4])
    with controls_col:
        slice_idx = st.slider(
            "Slice",
            0,
            max_slice,
            max_slice // 2,
            key=key_slider,
        )
        view_mode = st.radio(
            "View mode",
            ["CT Scan", "Overlay Segmentation"],
            horizontal=True,
            key=key_mode,
        )

    # Pull a single (H, W, 3) uint8 slice and .copy() since we may mutate it.
    ct_rgb_slice = ct_rgb_volume[:, :, slice_idx].copy()
    if view_mode == "Overlay Segmentation":
        seg_slice = seg_volume[:, :, slice_idx]
        for label_idx in (1, 2, 3):
            ct_rgb_slice[seg_slice == label_idx] = SEG_OVERLAY_COLORS[label_idx - 1]

    # Match the orientation used by the previous Plotly go.Image path:
    # np.flipud(np.transpose(ct_rgb, (1, 0, 2)))
    img_display = np.flipud(np.transpose(ct_rgb_slice, (1, 0, 2)))

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.image(img_display, use_container_width=True, clamp=False)


# ============================================================================
# 3D viewer - Plotly via Streamlit iframe (Plotly.js loads once from CDN)
# ============================================================================
def render_3d_view(traces, title: str):
    if not traces:
        st.info(f"No data to display for {title}.")
        return
    fig = go.Figure(data=traces)
    fig.update_layout(
        scene=dict(
            xaxis_title="X (mm)",
            yaxis_title="Y (mm)",
            zaxis_title="Z (mm)",
            aspectmode="data",
        ),
        margin=dict(l=0, r=0, b=0, t=30),
        legend=dict(yanchor="top", y=0.99, xanchor="left", x=0.01),
        height=720,
    )
    components.html(
        fig.to_html(include_plotlyjs="cdn", full_html=False),
        height=740,
    )


def _fmt(value, digits: int = 2, default: str = "—") -> str:
    """Format a numeric value for display, handling None / NaN gracefully."""
    try:
        if value is None:
            return default
        f = float(value)
        if np.isnan(f):
            return default
        return f"{f:.{digits}f}"
    except (TypeError, ValueError):
        return default


# ============================================================================
# Main UI
# ============================================================================
st.title("🔬 Vessel Geometry Analysis and 3D Reconstruction")
st.caption(
    "Automatic segmentation, three-vessel reconstruction and geometrical "
    "biomarkers from CT exams."
)

# Initialize session state
if "results" not in st.session_state:
    st.session_state.results = None
if "selected_patient_id" not in st.session_state:
    st.session_state.selected_patient_id = None
if "selected_scan_name" not in st.session_state:
    st.session_state.selected_scan_name = None
if "selected_analysis_id" not in st.session_state:
    st.session_state.selected_analysis_id = None


# ---- Sidebar ----
with st.sidebar:
    with st.container(border=True):
        st.subheader("Select Data")

        if not BASE_DATA_DIR.exists():
            st.error(
                f"Base data directory not found: '{BASE_DATA_DIR}'. Please ensure it exists."
            )
            st.stop()

        patient_scan_data = find_patient_scans(BASE_DATA_DIR)

        if not patient_scan_data:
            st.error(
                f"No valid patient data found in '{BASE_DATA_DIR}'. Please check the directory structure."
            )
            st.stop()

        patient_ids = list(patient_scan_data.keys())
        selected_patient_id = st.selectbox("Patient", patient_ids, key="sb_patient")

        selected_scan_name = None
        if selected_patient_id:
            available_scans = list(patient_scan_data[selected_patient_id].keys())
            if available_scans:
                selected_scan_name = st.selectbox(
                    "Scan", available_scans, key="sb_scan"
                )
            else:
                st.warning("This patient has no complete scans available.")

        run_clicked = st.button(
            "Run analysis",
            type="primary",
            use_container_width=True,
            disabled=not (selected_patient_id and selected_scan_name),
            key="run_btn",
        )

    with st.container(border=True):
        st.subheader("Maintenance")
        if st.button("Clear cache & reset", use_container_width=True, key="clear_btn"):
            st.cache_data.clear()
            # Keep selectbox selections; drop everything else.
            keys_to_keep = {"sb_patient", "sb_scan"}
            for key in list(st.session_state.keys()):
                if key not in keys_to_keep:
                    del st.session_state[key]
            st.session_state.results = None
            st.session_state.selected_analysis_id = None
            st.success("Cache cleared.")
            st.rerun()


# ---- Run analysis on demand (button click sets state and triggers rerun) ----
if run_clicked and selected_patient_id and selected_scan_name:
    st.session_state.selected_patient_id = selected_patient_id
    st.session_state.selected_scan_name = selected_scan_name

    patient_files = patient_scan_data[selected_patient_id][selected_scan_name]
    nifti_path = patient_files["nifti"]
    seg_path = patient_files["seg"]
    pcd_path = patient_files["pcd"]

    with st.spinner("Starting analysis… This may take a few moments."):
        nifti_mtime = os.path.getmtime(nifti_path)
        seg_mtime = os.path.getmtime(seg_path)
        pcd_mtime = os.path.getmtime(pcd_path)
        analysis_id = f"{selected_patient_id}_{selected_scan_name}"
        st.session_state.selected_analysis_id = analysis_id

        st.session_state.results = run_analysis(
            nifti_path,
            seg_path,
            pcd_path,
            analysis_id,
            nifti_mtime,
            seg_mtime,
            pcd_mtime,
        )
    st.rerun()


# ---- Results area ----
if not st.session_state.results:
    st.info(
        "Select a patient and scan in the sidebar, then click 'Run analysis' to begin."
    )
    st.markdown(
        '<div class="vessel-footer">Vessel Geometry Analysis — HPE AI Solutions Engineering</div>',
        unsafe_allow_html=True,
    )
    st.stop()

results: AnalysisResult = st.session_state.results
display_patient_id = st.session_state.get("selected_patient_id", "N/A")
display_scan_name = st.session_state.get("selected_scan_name", "N/A")
analysis_id = st.session_state.get(
    "selected_analysis_id", f"{display_patient_id}_{display_scan_name}"
)


# ---- Header: badges row (patient / scan / vessels present in this analysis) ----
vessel_badges = "".join(
    f'<span class="vessel-badge"><span class="vessel-dot" style="background: {color};"></span>{name}</span>'
    for name, color in VESSEL_HEX_COLORS.items()
    if name in results.get("vessels", {})
)
st.markdown(
    f"""
    <div class="vessel-badge-row">
        <span class="vessel-badge"><span class="vessel-dot" style="background: #0d9488;"></span>Patient: {display_patient_id}</span>
        <span class="vessel-badge"><span class="vessel-dot" style="background: #64748b;"></span>Scan: {display_scan_name}</span>
        {vessel_badges}
    </div>
    """,
    unsafe_allow_html=True,
)


# ---- Build 3D traces once (cached in session_state) ----
traces_cache = get_traces_cache(results, analysis_id)
mesh_traces = traces_cache["meshes"]
centerline_traces = traces_cache["centerlines"]
pcd_trace = traces_cache["pcd"]
combined_meshes = traces_cache["combined_meshes"]


# ---- Download STLs (cached zip) ----
st.download_button(
    label="⬇  Download 3D STL files",
    data=get_stl_zip(analysis_id, combined_meshes),
    file_name="vessels_meshes.zip",
    mime="application/zip",
)


# ---- Interactive viewer tabs ----
st.subheader("Interactive viewer")
tab_ct, tab_pcd, tab_centerline, tab_mesh = st.tabs(
    ["🩻  CT & Segmentation", "☁  Point Cloud", "〰  Centerline", "🧊  Mesh"]
)

with tab_ct:
    ct_rgb_volume = get_ct_rgb_volume(results)
    seg_volume = results.get("segmentation")
    if ct_rgb_volume is None or seg_volume is None:
        st.warning("CT / segmentation data not available for this scan.")
    else:
        _ct_viewer_fragment(ct_rgb_volume, seg_volume, analysis_id)

with tab_pcd:
    render_3d_view([pcd_trace] if pcd_trace is not None else [], "Point Cloud View")

with tab_centerline:
    render_3d_view(centerline_traces, "Centerline View")

with tab_mesh:
    render_3d_view(mesh_traces, "Mesh View")


# ---- Per-vessel metric cards + diameter plots ----
st.markdown("---")
st.subheader(f"Geometrical biomarkers — {display_patient_id} ({display_scan_name})")

vessels = results.get("vessels", {})
if not vessels:
    st.warning("No vessels were reconstructed in this scan.")
else:
    for vessel_name, data in vessels.items():
        hex_color = VESSEL_HEX_COLORS.get(vessel_name, "#64748b")
        with st.container(border=True):
            st.markdown(
                f'<div class="vessel-card-header" style="border-left-color: {hex_color};">'
                f'<h4 style="color: {hex_color};">{vessel_name}</h4></div>',
                unsafe_allow_html=True,
            )

            metrics = (data.get("metrics") or {}).get("centerline") or {}
            diameters = metrics.get("diameters") or {}

            cols = st.columns(4)
            cols[0].metric("Length (mm)", _fmt(metrics.get("length"), digits=2))
            cols[1].metric(
                "Max diameter (mm)", _fmt(diameters.get("max"), digits=2)
            )
            cols[2].metric(
                "Min diameter (mm)", _fmt(diameters.get("min"), digits=2)
            )
            cols[3].metric("Tortuosity", _fmt(metrics.get("tortuosity"), digits=3))

            if data.get("diameter_plot_bytes"):
                st.image(
                    data["diameter_plot_bytes"],
                    caption=f"{vessel_name} — diameter along centerline",
                    use_column_width=True,
                )
            else:
                st.caption("No diameter profile available for this vessel.")


st.markdown(
    '<div class="vessel-footer">Vessel Geometry Analysis — HPE AI Solutions Engineering · '
    "NVIDIA VISTA-3D segmentation + custom reconstruction</div>",
    unsafe_allow_html=True,
)
