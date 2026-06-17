#!/usr/bin/env python3
"""
Lightweight dev server for UI testing without loading models.
Serves the UI and mocks the API endpoints with realistic responses.
"""

import os
import json
import uuid
import threading
import time
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
OUTPUTS = ROOT / "outputs"

app = FastAPI(title="Motion Transfer Dev UI")

# In-memory job registry
JOBS: dict[str, dict] = {}
_JOB_LOCK = threading.Lock()

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _update_job(request_id: str, **fields) -> None:
    with _JOB_LOCK:
        if request_id in JOBS:
            JOBS[request_id].update(fields)

def mock_generate(request_id: str, image_name: str, video_name: str, prompt: str | None,
                  lora_strength: float | None = None, video_strength: float | None = None) -> None:
    """Simulate generation by progressing status and creating a dummy output."""
    # Log the prompt and conditioning strengths
    from pipeline_runtime import DEFAULT_PROMPT, DEFAULT_LORA_STRENGTH, DEFAULT_VIDEO_STRENGTH
    used_prompt = prompt or DEFAULT_PROMPT
    used_lora = lora_strength if lora_strength is not None else DEFAULT_LORA_STRENGTH
    used_video = video_strength if video_strength is not None else DEFAULT_VIDEO_STRENGTH
    print(f"\n[{request_id}] Prompt: {used_prompt}")
    print(f"[{request_id}] lora_strength={used_lora}, video_strength={used_video}\n")

    _update_job(request_id, status="waiting_for_gpu")
    time.sleep(0.5)

    _update_job(request_id, status="running", started_at=_now())
    # Simulate work
    time.sleep(2)

    # Create a dummy output (empty mp4-like file for demo)
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUTS / f"mock_{request_id}.mp4"
    output_path.write_bytes(b"MOCK_VIDEO_DATA")

    _update_job(request_id, status="done", finished_at=_now(), result=f"outputs/{output_path.name}")

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")

@app.post("/generate")
async def generate(
    image: UploadFile = File(...),
    video: UploadFile | None = File(None),
    prompt: str | None = Form(None),
    lora_strength: float | None = Form(None),
    video_strength: float | None = Form(None),
):
    request_id = uuid.uuid4().hex[:12]

    # Read image (just to validate)
    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(400, "empty image upload")

    video_name = video.filename if (video and video.filename) else "default"
    used_default = not (video and video.filename)

    with _JOB_LOCK:
        JOBS[request_id] = {
            "status": "pending",
            "image": image.filename,
            "video": video_name,
            "used_default_video": used_default,
            "prompt": prompt,
            "submitted_at": _now(),
        }

    # Start mock generation in background
    threading.Thread(target=mock_generate, args=(request_id, image.filename, video_name, prompt, lora_strength, video_strength), daemon=True).start()

    return {"request_id": request_id, **JOBS[request_id]}

@app.get("/jobs/{request_id}")
def get_job(request_id: str):
    with _JOB_LOCK:
        if request_id not in JOBS:
            raise HTTPException(404, "job not found")
        return JOBS[request_id]

@app.get("/jobs/{request_id}/result")
def get_result(request_id: str):
    with _JOB_LOCK:
        if request_id not in JOBS:
            raise HTTPException(404, "job not found")
        result = JOBS[request_id].get("result")
        if not result:
            raise HTTPException(404, "result not ready")

    result_path = ROOT / result
    if not result_path.exists():
        raise HTTPException(404, "result file not found")

    return FileResponse(result_path, media_type="video/mp4")

if __name__ == "__main__":
    print("🎬 Motion Transfer Dev Server (UI only, no models)")
    print(f"   URL: http://localhost:8000")
    print(f"   Serving: {STATIC / 'index.html'}")
    uvicorn.run(app, host="0.0.0.0", port=8000)
