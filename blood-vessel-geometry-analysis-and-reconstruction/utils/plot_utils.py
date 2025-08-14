import matplotlib.pyplot as plt
from pathlib import Path
import numpy as np
import open3d as o3d
import os
import plotly.graph_objects as go
from typing import Dict


def mesh_to_plotly(
    mesh_data: Dict[str, np.ndarray], color="gray", name="Mesh", showlegend=True
):
    """Converts mesh data (vertices, triangles) to a Plotly Mesh3d trace."""
    vertices = mesh_data["vertices"]
    triangles = mesh_data["triangles"]

    trace = go.Mesh3d(
        x=vertices[:, 0],
        y=vertices[:, 1],
        z=vertices[:, 2],
        i=triangles[:, 0],
        j=triangles[:, 1],
        k=triangles[:, 2],
        color=color,
        opacity=0.7,
        name=name,
        hoverinfo="name",
        showlegend=showlegend,  # FIX: Explicitly control legend visibility
    )
    return trace


def pcd_to_plotly(pcd_data: Dict[str, np.ndarray], name="Point Cloud", showlegend=True):
    """Converts point cloud data (points, colors) to a Plotly Scatter3d trace."""
    points = pcd_data["points"]
    colors = pcd_data["colors"] * 255

    trace = go.Scatter3d(
        x=points[:, 0],
        y=points[:, 1],
        z=points[:, 2],
        mode="markers",
        marker=dict(size=2, color=colors, opacity=0.8),
        name=name,
        hoverinfo="name",
        showlegend=showlegend,  # FIX: Explicitly control legend visibility
    )
    return trace


def lineset_to_plotly(
    lineset_data: Dict[str, np.ndarray],
    color="yellow",
    name="Centerline",
    showlegend=True,
):
    """Converts lineset data (points, lines) to a Plotly Scatter3d trace with lines."""
    points = lineset_data["points"]
    lines = lineset_data["lines"]

    x_lines, y_lines, z_lines = [], [], []

    for line in lines:
        p1 = points[line[0]]
        p2 = points[line[1]]
        x_lines.extend([p1[0], p2[0], None])
        y_lines.extend([p1[1], p2[1], None])
        z_lines.extend([p1[2], p2[2], None])

    trace = go.Scatter3d(
        x=x_lines,
        y=y_lines,
        z=z_lines,
        mode="lines",
        line=dict(color=color, width=5),
        name=name,
        hoverinfo="name",
        showlegend=showlegend,  # FIX: Explicitly control legend visibility
    )
    return trace


def bin_by_unique_z(points, diameters):
    """
    Identify at each slice, the max diameter value.

    Args:
        points: np.array of x, y, z coordinates of vessel centerline in voxel space
        diameters: np.array of diameter values compute along points

    Returns:
        max_points: np.array of x, y, z coordinates of vessel centerline with unique z coordinates
        max_diams: np.array of diameter values compute along max_points
    """

    assert points.shape[0] == len(diameters), ValueError(
        f"""number of points coordinates and diameter value is inconsistent.
        Input shape is: points: {points.shape}, diameters: {diameters.shape}"""
    )

    z_vals = points[:, 2]
    unique_z = np.unique(z_vals)

    max_points = []
    max_diams = []

    for z in unique_z:
        in_bin = z_vals == z
        bin_points = points[in_bin]
        bin_diams = diameters[in_bin]

        if len(bin_diams) == 0:
            continue

        idx_max = np.argmax(bin_diams)
        max_points.append(bin_points[idx_max])
        max_diams.append(bin_diams[idx_max])

    return np.array(max_points), np.array(max_diams)


def plot_diameter(diameters, output_dir, name, slice_ids=None):
    """
    Plot diameter over vessel's centerline and save it to a file.

    Args:
        diameters: np.array, diameter across the centerline
        output_dir: str folder where to save the plot
        name: str, name of the vessel
        slice_ids: np.array, id for each diameter measure
    """

    max_pts_zbin, max_diam_values = bin_by_unique_z(slice_ids, diameters)

    plt.figure(figsize=(10, 4))
    if slice_ids is None:
        plt.plot(np.arange(len(diameters)), diameters, "-o")
    else:
        plt.plot(max_pts_zbin[:, 2], max_diam_values, "-o")

    if slice_ids is None:
        plt.xlabel("Centerline point index")
    else:
        plt.xlabel("Slice coordinate")
    plt.ylabel("Diameter (mm)")
    plt.title(f"{name} Diameter Along Centerline")
    plt.grid(True)
    plt.tight_layout()

    os.makedirs(output_dir, exist_ok=True)
    entire_file_path = Path(output_dir) / f"{'_'.join(name.split(' '))}.png"
    plt.savefig(entire_file_path, dpi=200)
    # The plot is closed to free up memory and prevent it from being displayed
    # in non-interactive environments.
    plt.close()
    return entire_file_path


def create_flat_disc(center, radius, normal, resolution=60):
    """
    Create a flat disc mesh centered at `center`, perpendicular to `normal`,
    with the specified `radius` and triangle resolution.

    Args:
        center: coordinates where to center the disk
        radius: disc radius
        normal: vector to which the disc should be normal to (i.e. tangent to centerline)
        resolution: the number of segments (or triangles) used to approximate the circular disc.

    Return:
        Open3D disc mesh
    """
    normal = np.asarray(normal, dtype=np.float64)
    if np.linalg.norm(normal) == 0:
        # If the normal is a zero vector, default to Z-axis to avoid errors
        normal = np.array([0, 0, 1], dtype=np.float64)
    else:
        normal /= np.linalg.norm(normal)

    # Step 1: Create disc in XY plane
    angles = np.linspace(0, 2 * np.pi, resolution, endpoint=False)
    circle_points = (
        np.c_[np.cos(angles), np.sin(angles), np.zeros_like(angles)] * radius
    )
    vertices = np.vstack([[0, 0, 0], circle_points])
    triangles = [[0, i, i + 1] for i in range(1, resolution)]
    triangles.append([0, resolution, 1])

    mesh = o3d.geometry.TriangleMesh()
    mesh.vertices = o3d.utility.Vector3dVector(vertices)
    mesh.triangles = o3d.utility.Vector3iVector(triangles)
    mesh.compute_vertex_normals()

    # Step 2: Rotate to align normal with Z-axis
    z_axis = np.array([0, 0, 1], dtype=np.float64)

    if not np.allclose(normal, z_axis):
        axis = np.cross(z_axis, normal)
        # Handle case where normal is parallel to z_axis but opposite
        if np.linalg.norm(axis) == 0:
            if np.allclose(normal, -z_axis):
                axis = np.array([1, 0, 0])  # Rotate 180 degrees around x-axis
            else:
                axis = np.array([0, 1, 0])  # Default axis if already aligned
        else:
            axis /= np.linalg.norm(axis)

        angle = np.arccos(np.clip(np.dot(z_axis, normal), -1.0, 1.0))
        R = o3d.geometry.get_rotation_matrix_from_axis_angle(axis * angle)
        mesh.rotate(R, center=(0, 0, 0))

    # Step 3: Translate to center position
    mesh.translate(center)

    return mesh
