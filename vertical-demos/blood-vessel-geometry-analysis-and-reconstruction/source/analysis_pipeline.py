import nibabel as nib
import numpy as np
import open3d as o3d
from scipy import ndimage
from skimage import measure
from pathlib import Path
import traceback
from typing import TypedDict, Dict, Any, Callable, Optional

# Import from existing utils
from utils.metrics_utils import (
    compute_centerline_metrics,
    compute_reconstruction_quality_metrics,
)
from utils.plot_utils import create_flat_disc


def _normalize_ct_to_uint8_rgb(ct_volume: np.ndarray) -> np.ndarray:
    """
    Apply volume-wide min-max normalization to a uint8 RGB volume (H, W, D, 3).

    Volume-wide normalization keeps brightness consistent across slices, which
    is what radiologists expect. A per-slice normalization (the previous
    behavior) caused flicker when scrolling because each slice was mapped to
    its own [min, max] range.
    """
    ct_min = float(ct_volume.min())
    ct_max = float(ct_volume.max())
    if ct_max - ct_min < 1e-6:
        ct_max = ct_min + 1.0
    normalized = ((ct_volume - ct_min) / (ct_max - ct_min) * 255).astype(np.uint8)
    # Broadcast grayscale to RGB by repeating the last axis.
    return np.repeat(normalized[..., np.newaxis], 3, axis=-1)


# --- NEW: Define serializable types for geometries ---
class SerializableMesh(TypedDict):
    vertices: np.ndarray
    triangles: np.ndarray


class SerializableLineSet(TypedDict):
    points: np.ndarray
    lines: np.ndarray


class SerializablePointCloud(TypedDict):
    points: np.ndarray
    colors: np.ndarray


# --- Update Type hinting for the results dictionary ---
class VesselData(TypedDict):
    mesh: Optional[SerializableMesh]
    centerline: Optional[SerializableLineSet]
    max_diameter_disc: Optional[SerializableMesh]
    metrics: Dict[str, Any]
    diameter_plot_path: Optional[str]
    diameter_plot_bytes: Optional[bytes]


class PointCloudData(TypedDict):
    geometry: Optional[SerializablePointCloud]


class AnalysisResult(TypedDict):
    ct_rgb_volume: Optional[np.ndarray]  # (H, W, D, 3) uint8 - pre-normalized for fast slice rendering
    segmentation: Optional[np.ndarray]
    point_cloud: Optional[PointCloudData]
    vessels: Dict[str, VesselData]


def enhanced_vessel_reconstruction_analysis(
    scan_nifti_file: str,
    segmentation_file: str,
    pointcloud_file: str,
    destination_folder: Path,
    patient_id: str,
    status_callback: Callable[[str], None],
) -> AnalysisResult:
    """
    Main analysis function adapted for Streamlit. Now returns serializable data.
    CT volume is pre-normalized to a uint8 RGB volume for consistent brightness
    across slices and O(1) slice rendering in the UI.
    """
    results: AnalysisResult = {
        "ct_rgb_volume": None,
        "segmentation": None,
        "vessels": {},
    }

    status_callback("Loading data...")

    try:
        seg_data = np.load(segmentation_file)
        new_seg_scan = seg_data["new_seg_scan"]
    except Exception as e:
        status_callback(f"ERROR: Could not load segmentation file: {e}")
        return results

    try:
        pcd_data = np.load(pointcloud_file)
        points = pcd_data["points"]
    except Exception as e:
        status_callback(
            f"WARNING: Could not load point cloud file: {e}. Quality metrics will be skipped."
        )
        points = None

    try:
        series_name = Path(scan_nifti_file).name.replace(".nii.gz", "")
        scan_data = nib.load(scan_nifti_file)
        affine = scan_data.affine
        voxel_spacing = np.linalg.norm(affine[:3, :3], axis=0)
        inv_affine = np.linalg.inv(affine)
    except Exception as e:
        status_callback(f"ERROR: Could not load NIfTI scan file: {e}")
        return results

    if points is not None:
        status_callback("Mapping point cloud to segmentation labels...")
        points_homogeneous = np.c_[points, np.ones(points.shape[0])]
        voxel_coords = (points_homogeneous @ inv_affine.T)[:, :3]
        voxel_coords = np.round(voxel_coords).astype(int)
        dims = np.array(new_seg_scan.shape)
        voxel_coords = np.clip(voxel_coords, [0, 0, 0], dims - 1)
        point_labels = new_seg_scan[
            voxel_coords[:, 0], voxel_coords[:, 1], voxel_coords[:, 2]
        ]
        status_callback("...mapping complete.")
    else:
        point_labels = None

    results["ct_rgb_volume"] = _normalize_ct_to_uint8_rgb(scan_data.get_fdata())
    results["segmentation"] = new_seg_scan

    colors_rgb = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    names = ["Aorta", "Left Iliac Artery", "Right Iliac Artery"]

    for label_idx, (color, name) in enumerate(zip(colors_rgb, names), start=1):
        status_callback(f"Processing: {name} (Label {label_idx})...")

        mask = new_seg_scan == label_idx
        if not np.any(mask):
            continue

        vessel_results: VesselData = {
            "mesh": None,
            "centerline": None,
            "max_diameter_disc": None,
            "metrics": {},
            "diameter_plot_path": None,
            "diameter_plot_bytes": None,
        }

        try:
            status_callback(f"Reconstructing mesh for {name}...")
            pad_amount = 2
            padded_mask = np.pad(
                mask, pad_width=pad_amount, mode="constant", constant_values=0
            )
            padded_mask = ndimage.binary_fill_holes(padded_mask)
            smoothed = ndimage.gaussian_filter(padded_mask.astype(float), sigma=1)
            # get vertices in voxel coord
            verts, faces, _, _ = measure.marching_cubes(
                smoothed, level=0.2, spacing=voxel_spacing
            )
            # account for padding
            verts -= pad_amount * voxel_spacing

            mesh = o3d.geometry.TriangleMesh(
                o3d.utility.Vector3dVector(verts), o3d.utility.Vector3iVector(faces)
            )
            mesh.compute_vertex_normals()
            # --- Store serializable data instead of o3d object ---
            vessel_results["mesh"] = {
                "vertices": np.asarray(mesh.vertices),
                "triangles": np.asarray(mesh.triangles),
            }

            status_callback(f"Analyzing centerline for {name}...")
            centerline_analysis = compute_centerline_metrics(
                mask,
                affine,
                name=name,
                destination_folder=destination_folder,
                patient_id=patient_id,
                debug_mode=False,
                series_name=series_name,
            )

            if centerline_analysis and centerline_analysis["metrics"]:
                vessel_results["metrics"]["centerline"] = centerline_analysis["metrics"]
                vessel_results["diameter_plot_path"] = str(
                    centerline_analysis["plot_path"]
                )

                vis_data = centerline_analysis.get("vis_data", {})
                if (
                    vis_data
                    and vis_data.get("points") is not None
                    and vis_data.get("connections") is not None
                ):
                    # --- Store serializable data ---
                    vessel_results["centerline"] = {
                        "points": vis_data["points"],
                        "lines": vis_data["connections"],
                    }

                max_diam_loc_mm = centerline_analysis["metrics"].get(
                    "max_diameter_location"
                )
                if max_diam_loc_mm is not None:
                    disc = create_flat_disc(
                        center=max_diam_loc_mm,
                        radius=float(
                            centerline_analysis["metrics"]["diameters"]["max"] / 2
                        ),
                        normal=centerline_analysis["metrics"].get(
                            "tangent_at_max_diameter_location"
                        ),
                    )
                    # --- Store serializable data ---
                    vessel_results["max_diameter_disc"] = {
                        "vertices": np.asarray(disc.vertices),
                        "triangles": np.asarray(disc.triangles),
                    }

            if points is not None and point_labels is not None:
                pc_label_mask = point_labels == label_idx
                if np.any(pc_label_mask):
                    status_callback(f"Calculating reconstruction quality for {name}...")
                    quality_metrics = compute_reconstruction_quality_metrics(
                        points[pc_label_mask], mesh
                    )
                    vessel_results["metrics"]["quality"] = quality_metrics

            results["vessels"][name] = vessel_results

        except Exception as e:
            status_callback(f"ERROR processing {name}: {e}")
            traceback.print_exc()

    if points is not None and point_labels is not None:
        pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
        pcd_colors = np.zeros_like(points, dtype=float)
        for i, color in enumerate(colors_rgb, 1):
            pcd_colors[point_labels == i] = color

        # --- Store serializable data ---
        results["point_cloud"] = {
            "geometry": {"points": np.asarray(pcd.points), "colors": pcd_colors}
        }

    status_callback("Analysis complete!")

    return results
