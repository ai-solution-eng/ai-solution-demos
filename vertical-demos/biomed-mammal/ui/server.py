import os
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx

BACKEND_URL = os.environ.get("MODEL_URL", "http://localhost:3000")

app = FastAPI(title="BiomedMammal UI")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.post("/api/predict")
async def predict(request: Request):
    body = await request.json()
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(f"{BACKEND_URL}/predict", json=body)
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except httpx.ConnectError:
        return JSONResponse(
            content={"error": f"Model container not reachable at {BACKEND_URL}."},
            status_code=503,
        )
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
