from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path
import os

class FilePathRequest(BaseModel):
    video_path: str
    output_path: str

router = APIRouter(
    prefix="/api/utils",
)

@router.delete(
    "/video/delete",
    tags=['Utils'],
    summary="Delete temporarily uploaded video",
    description="Delete temporarily uploaded video"
)
async def delete_video(request: FilePathRequest):
    # Delete temporarily uploaded video
    results = []
    for i, filepath in enumerate([request.video_path,request.output_path]):
        try:    
            file_path = Path(filepath)
            if not file_path.exists():
                raise HTTPException(status_code=404, detail=f"File [{filepath}] not found")
            if not file_path.is_file():
                raise HTTPException(status_code=400, detail=f"Path [{filepath}] is not a file")
            os.remove(file_path)
            results.append(f"File {filepath} deleted successfully")

        except PermissionError:
            raise HTTPException(status_code=403, detail=f"Permission denied to delete {filepath}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error deleting file '{filepath}': {str(e)}")
    
    return { "message": "All files are deleted successfully", "success": results }




