import open3d as o3d
import trimesh
import numpy as np
import io
import zipfile


def o3d_to_trimesh(data):
    return trimesh.Trimesh(
        vertices=data["vertices"], faces=data["triangles"], process=False
    )


def export_mesh_to_stl_bytes(o3d_mesh):
    tmesh = o3d_to_trimesh(o3d_mesh)
    return tmesh.export(file_type="stl")  # returns bytes


# Create a zip file in memory
def create_zip_of_mesh_in_memory(combined_meshes):
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        for nv, mesh in combined_meshes.items():
            stl_data = export_mesh_to_stl_bytes(mesh)
            zipf.writestr(f"{nv}.stl", stl_data)
    zip_buffer.seek(0)
    return zip_buffer
