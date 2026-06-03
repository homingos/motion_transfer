"""FastAPI server that exposes the motion-transfer pipeline.

Run locally:
    python server.py
    # or
    uvicorn server:app --host 0.0.0.0 --port 8000

UI:  open http://localhost:8000/ in a browser.
API: POST /generate (multipart), then GET /jobs/{id} to poll, then GET /jobs/{id}/result for the mp4.
"""

import os
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import pipeline_runtime
import integrations

ROOT = Path(__file__).resolve().parent
UPLOADS = ROOT / "uploads"
OUTPUTS = ROOT / "outputs"
STATIC = ROOT / "static"
DEFAULT_VIDEO = ROOT / "assets" / "idle_avatar_15_reverse.mp4"

UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Build the pipeline object (cheap: reads metadata, wires the weight cache).
    # Model weights load lazily on the first /generate call. Fails fast here if
    # any model file is missing.
    pipeline_runtime.warmup()
    # Optionally absorb the slow cold model-load now, so the first real request is
    # already warm (~42s) instead of paying ~minutes. Runs on a background thread
    # so the server still binds its port immediately. Enable with WARMUP_ON_STARTUP=1.
    if os.environ.get("WARMUP_ON_STARTUP") == "1":
        threading.Thread(target=pipeline_runtime.prewarm_weights, name="prewarm", daemon=True).start()
    yield


app = FastAPI(title="LTX-2 Motion Transfer", lifespan=_lifespan)

# In-memory job registry. Persists only for the server's lifetime.
JOBS: dict[str, dict] = {}
_JOB_LOCK = threading.Lock()
# Serialize generations: the pipeline pins the whole GPU, so we run one at a time.
_GPU_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _set(job_id: str, **fields) -> None:
    with _JOB_LOCK:
        JOBS[job_id].update(fields)


def run_job(job_id: str, image_path: Path, video_path: Path, prompt: str | None) -> None:
    """Run the warm in-process pipeline and capture status.

    The pipeline keeps its weights resident in CPU RAM (shared StateDictRegistry),
    so only the first job pays the cold weight-load cost; later jobs are faster.
    pipeline_runtime serialises GPU access internally via its own lock.
    """
    _set(job_id, status="waiting_for_gpu")
    with _GPU_LOCK:
        _set(job_id, status="running", started_at=_now())
        output_path = OUTPUTS / f"api_{job_id}.mp4"
        try:
            pipeline_runtime.generate(
                image_path=str(image_path),
                output_path=str(output_path),
                video_path=str(video_path),
                prompt=prompt or None,
            )
            if output_path.exists():
                _set(job_id, status="done", finished_at=_now(),
                     result=str(output_path.relative_to(ROOT)))
            else:
                _set(job_id, status="failed", finished_at=_now(),
                     error="generation finished but no output file was produced")
        except Exception as e:
            _set(job_id, status="failed", finished_at=_now(), error=repr(e))


def run_idle_job(job_id: str, avatar_id: str, image_path: Path, video_path: Path, prompt: str | None) -> None:
    """Generate the idle-motion video for an avatar, tracking status in MongoDB and
    uploading the result to R2.

    DB lifecycle on the `Faceshot.idle_motion` doc keyed by `avatarid`:
      processing  ->  done processing (+ link)   on success
      processing  ->  failed                     on any error / no output
    """
    _set(job_id, status="waiting_for_gpu", avatar_id=avatar_id)
    integrations.set_status(avatar_id, "processing")
    with _GPU_LOCK:
        _set(job_id, status="running", started_at=_now())
        output_path = OUTPUTS / f"idle_{avatar_id}_{job_id}.mp4"
        try:
            pipeline_runtime.generate(
                image_path=str(image_path),
                output_path=str(output_path),
                video_path=str(video_path),
                prompt=prompt or None,
            )
            if not output_path.exists():
                raise RuntimeError("generation finished but no output file was produced")
            link = integrations.r2_upload(output_path, key=f"idle_motion/{avatar_id}.mp4")
            _set(job_id, status="done", finished_at=_now(),
                 result=str(output_path.relative_to(ROOT)), link=link)
            integrations.set_status(avatar_id, "done processing", link=link)
        except Exception as e:
            _set(job_id, status="failed", finished_at=_now(), error=repr(e))
            integrations.set_status(avatar_id, "failed")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.post("/generate")
async def generate(
    image: UploadFile = File(..., description="Subject image (PNG or JPG)"),
    video: UploadFile | None = File(None, description="Reference motion video (optional; defaults to assets/idle_avatar_15_reverse.mp4)"),
    prompt: str | None = Form(None, description="Text prompt (optional; main.py has a sensible default)"),
):
    job_id = uuid.uuid4().hex[:8]

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(400, "empty image upload")
    image_path = UPLOADS / f"{job_id}_image_{image.filename}"
    image_path.write_bytes(image_bytes)

    if video is not None and video.filename:
        video_bytes = await video.read()
        if not video_bytes:
            raise HTTPException(400, "empty video upload")
        video_path = UPLOADS / f"{job_id}_video_{video.filename}"
        video_path.write_bytes(video_bytes)
        used_default = False
    else:
        video_path = DEFAULT_VIDEO
        used_default = True

    with _JOB_LOCK:
        JOBS[job_id] = {
            "status": "pending",
            "image": image.filename,
            "video": video_path.name,
            "used_default_video": used_default,
            "prompt": prompt,
            "submitted_at": _now(),
        }

    threading.Thread(target=run_job, args=(job_id, image_path, video_path, prompt), daemon=True).start()
    return {"job_id": job_id, **JOBS[job_id]}


@app.post("/idle-motion")
async def idle_motion(
    image: UploadFile = File(..., description="Subject image (PNG or JPG)"),
    avatar_id: str = Form(..., description="Avatar id — the key written to Faceshot.idle_motion"),
):
    """Generate an idle-motion video for an avatar using the default reference clip.

    Async: returns a job_id immediately. Status is tracked in MongoDB keyed by `avatar_id`
    (processing -> done processing/failed); on success the R2 link is written to the doc.
    """
    job_id = uuid.uuid4().hex[:8]

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(400, "empty image upload")
    image_path = UPLOADS / f"{job_id}_{avatar_id}_{image.filename}"
    image_path.write_bytes(image_bytes)

    video_path = DEFAULT_VIDEO  # idle-motion always uses the bundled reference clip

    with _JOB_LOCK:
        JOBS[job_id] = {
            "status": "pending",
            "avatar_id": avatar_id,
            "image": image.filename,
            "video": video_path.name,
            "submitted_at": _now(),
        }

    threading.Thread(target=run_idle_job, args=(job_id, avatar_id, image_path, video_path, None),
                     daemon=True).start()
    return {"job_id": job_id, **JOBS[job_id]}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")
    return JOBS[job_id]


@app.get("/jobs/{job_id}/result")
def get_result(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")
    if JOBS[job_id].get("status") != "done":
        raise HTTPException(409, f"job not done (status={JOBS[job_id].get('status')})")
    return FileResponse(ROOT / JOBS[job_id]["result"], media_type="video/mp4", filename=f"motion_transfer_{job_id}.mp4")


@app.get("/jobs")
def list_jobs():
    return JOBS


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))

    bar = "=" * 60
    display_host = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    warm = "on (first request will be warm)" if os.environ.get("WARMUP_ON_STARTUP") == "1" \
        else "off (first request loads the model, ~minutes)"
    print(f"\n{bar}\n  LTX-2 Motion Transfer server\n"
          f"  ➜ UI:  http://{display_host}:{port}/\n"
          f"  ➜ API: http://{display_host}:{port}/generate (POST multipart)\n"
          f"  Pre-warm on startup: {warm}\n"
          f"  Bound to {host}:{port}.  Ctrl-C to quit.\n{bar}\n", flush=True)

    uvicorn.run(app, host=host, port=port, log_level="info")
