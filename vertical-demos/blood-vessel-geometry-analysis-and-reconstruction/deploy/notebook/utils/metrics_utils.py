from utils.geometric_utils import compute_tangent
import numpy as np
import open3d as o3d
from scipy import ndimage
from scipy.ndimage import distance_transform_edt, label, center_of_mass
from scipy.sparse.csgraph import dijkstra, minimum_spanning_tree
from scipy.spatial import distance
from skimage.morphology import skeletonize, binary_opening
from utils.plot_utils import plot_diameter
from pathlib import Path


def compute_centerline_metrics(
    mask,
    affine,
    name="Vessel",
    destination_folder=Path("outputs"),
    patient_id="dummy_patient_id",
    debug_mode=False,
    series_name="series",
):
    """
    Calculates vessel statistics and identifies the max diameter location.

    - Also calculates tortuosity and a centerline quality metric.

    Args:
        mask: np.array binary vessel segmentation mask
        affine: array, affine matrix from nifti , including acquisition parameters
        name: str, vessel name
        debug_mode: bool
    """
    if not np.any(mask):
        return None

    voxel_spacing = np.linalg.norm(affine[:3, :3], axis=0)

    is_isotropic = np.allclose(voxel_spacing[0], voxel_spacing[1]) and np.allclose(
        voxel_spacing[1], voxel_spacing[2]
    )
    if is_isotropic:
        structure = ndimage.generate_binary_structure(3, 3)
        cleaned_mask = binary_opening(mask, footprint=structure)
    else:
        structure = ndimage.generate_binary_structure(3, 1)
        cleaned_mask = binary_opening(mask, footprint=structure)

    if not np.any(cleaned_mask):
        return None

    skeleton = skeletonize(cleaned_mask)
    if not np.any(skeleton):
        return None

    labeled_skeleton, num_features = label(skeleton)
    if num_features == 0:
        return None

    skeleton_voxels = np.argwhere(skeleton)

    # Transform skeleton points from voxel space to mm space using the affine matrix
    skeleton_voxels_hom = np.hstack(
        [skeleton_voxels, np.ones((skeleton_voxels.shape[0], 1))]
    )
    skeleton_mm = (affine @ skeleton_voxels_hom.T).T[:, :3]

    diameters_mm = (
        distance_transform_edt(mask, sampling=voxel_spacing)[tuple(skeleton_voxels.T)]
        * 2
    )
    if len(diameters_mm) == 0:
        return None

    # This will now just save the plot and return the path
    plot_path = plot_diameter(
        diameters=diameters_mm,
        slice_ids=skeleton_voxels,
        output_dir=Path(destination_folder)
        / "diameter_profiles"
        / patient_id
        / series_name,
        name=f"{name}",
    )

    max_diam_idx = np.argmax(diameters_mm)
    max_diam_location_mm = skeleton_mm[max_diam_idx]

    diameter_stats = {
        "min": np.min(diameters_mm),
        "mean": np.mean(diameters_mm),
        "median": np.median(diameters_mm),
        "max": np.max(diameters_mm),
    }

    # Use skeleton_mm for tangent calculation for accuracy in physical space
    tangent = compute_tangent(skeleton_mm, max_diam_idx)
    vis_data = {"vis_type": None, "points": None, "connections": None}
    metrics = {
        "diameters": diameter_stats,
        "length": np.nan,
        "tortuosity": np.nan,
        "quality": np.nan,
        "max_diameter_location": max_diam_location_mm,
        "tangent_at_max_diameter_location": tangent,
    }

    if num_features > 1:
        fragment_sizes = np.bincount(labeled_skeleton.ravel())
        size_threshold = 1
        significant_fragment_labels = np.where(fragment_sizes > size_threshold)[0]
        significant_fragment_labels = significant_fragment_labels[
            significant_fragment_labels != 0
        ]

        if len(significant_fragment_labels) > 1:
            centroids_vox = np.array(
                center_of_mass(skeleton, labeled_skeleton, significant_fragment_labels)
            )
            centroids_hom = np.hstack(
                [centroids_vox, np.ones((centroids_vox.shape[0], 1))]
            )
            centroids_mm = (affine @ centroids_hom.T).T[:, :3]

            dist_matrix = distance.cdist(centroids_mm, centroids_mm)
            mst = minimum_spanning_tree(dist_matrix)
            total_mst_length = mst.sum()

            distances, _ = dijkstra(
                csgraph=mst, directed=False, indices=0, return_predecessors=True
            )
            first_endpoint_idx = np.argmax(distances)
            distances_from_endpoint, predecessors = dijkstra(
                csgraph=mst,
                directed=False,
                indices=first_endpoint_idx,
                return_predecessors=True,
            )

            max_path_length = np.max(distances_from_endpoint)
            second_endpoint_idx = np.argmax(distances_from_endpoint)

            start_point_coords = centroids_mm[first_endpoint_idx]
            end_point_coords = centroids_mm[second_endpoint_idx]
            straight_line_dist = np.linalg.norm(start_point_coords - end_point_coords)

            if straight_line_dist > 0:
                metrics["tortuosity"] = max_path_length / straight_line_dist
            if total_mst_length > 0:
                metrics["quality"] = max_path_length / total_mst_length
            metrics["length"] = max_path_length

            path = []
            current_node = second_endpoint_idx
            while current_node != -9999:
                path.append(current_node)
                current_node = predecessors[current_node]
            path = path[::-1]

            vis_data["vis_type"] = "lines"
            vis_data["points"] = centroids_mm
            vis_data["connections"] = np.array([path[:-1], path[1:]]).T

    return {"metrics": metrics, "vis_data": vis_data, "plot_path": plot_path}


def compute_mesh_metrics(mesh):
    if not mesh.has_vertices() or not mesh.has_triangles():
        return 0, 0, False
    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_vertices()
    mesh.remove_unreferenced_vertices()
    surface_area = mesh.get_surface_area()
    is_watertight = mesh.is_watertight()
    volume = mesh.get_volume() if is_watertight else 0
    return surface_area, volume, is_watertight


def compute_point_cloud_metrics(points, label_mask):
    if not np.any(label_mask):
        return None
    label_points = points[label_mask]
    if len(label_points) < 4:
        return None
    pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(label_points))
    return pcd.get_oriented_bounding_box()


def compute_reconstruction_quality_metrics(points, mesh):
    if len(points) == 0 or not mesh.has_vertices():
        return {}
    mesh_t = o3d.t.geometry.TriangleMesh.from_legacy(mesh)
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(mesh_t)

    distances_orig_to_mesh = scene.compute_distance(
        o3d.core.Tensor(points, dtype=o3d.core.Dtype.Float32)
    ).numpy()

    pcd_original = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
    pcd_mesh = o3d.geometry.PointCloud(mesh.vertices)
    distances_mesh_to_orig = np.asarray(
        pcd_mesh.compute_point_cloud_distance(pcd_original)
    )

    mesh_goodness = np.mean(distances_orig_to_mesh)
    point_representation_goodness = np.mean(distances_mesh_to_orig)

    return {"chamfer_distance": mesh_goodness + point_representation_goodness}
