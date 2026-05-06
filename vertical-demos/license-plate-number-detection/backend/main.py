import os
from uvicorn import Server, Config
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from routers.image import vehicle_detection,plate_number_detection
from routers.video import video_vehicle_detection,video_plate_number_detection
from routers.utils import draw_results,upload,model_info,delete

app = FastAPI()

# redirect
@app.get("/", include_in_schema=False)
async def redirect():
    return RedirectResponse("/docs")

app.include_router(vehicle_detection.router)
app.include_router(plate_number_detection.router)
app.include_router(video_vehicle_detection.router)
app.include_router(video_plate_number_detection.router)
app.include_router(draw_results.router)
app.include_router(upload.router)
app.include_router(model_info.router)
app.include_router(delete.router)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    server = Server(Config(app, host="127.0.0.1", port=port, lifespan="on"))
    server.run()