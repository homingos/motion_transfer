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

# Serialize generations: the pipeline pins the whole GPU, so we run one at a time.
_GPU_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _update_job(request_id: str, **fields) -> None:
    integrations.update_job(request_id, **fields)


def run_job(request_id: str, image_path: Path, video_path: Path, prompt: str | None) -> None:
    """Run the warm in-process pipeline and capture status.

    The pipeline keeps its weights resident in CPU RAM (shared StateDictRegistry),
    so only the first job pays the cold weight-load cost; later jobs are faster.
    pipeline_runtime serialises GPU access internally via its own lock.
    """
    _update_job(request_id, status="waiting_for_gpu")
    with _GPU_LOCK:
        _update_job(request_id, status="running", started_at=_now())
        output_path = OUTPUTS / f"api_{request_id}.mp4"
        try:
            pipeline_runtime.generate(
                image_path=str(image_path),
                output_path=str(output_path),
                video_path=str(video_path),
                prompt=prompt or None,
            )
            if output_path.exists():
                _update_job(request_id, status="done", finished_at=_now(),
                     result=str(output_path.relative_to(ROOT)))
            else:
                _update_job(request_id, status="failed", finished_at=_now(),
                     error="generation finished but no output file was produced")
        except Exception as e:
            _update_job(request_id, status="failed", finished_at=_now(), error=repr(e))


def run_idle_job(request_id: str, avatar_id: str, video_path: Path, prompt: str | None) -> None:
    """Generate the idle-motion video for an avatar from its avatar_id alone.

    The subject image is resolved from the `fableface.templates` doc (keyed by ObjectId
    `_id` = avatar_id): we read `source_assets.image_key` and download that object from R2.

    DB lifecycle on that doc:
      status: processing                                                  on start
      source_assets.idle_animation_key = R2 key, then status: ready       on success (key first)
      status: failed (+ failure_reason)                                   on error
    (idle_vector_key is a separate field and is left untouched.)
    """
    _update_job(request_id, status="fetching_source", avatar_id=avatar_id)
    integrations.set_status(avatar_id, "processing")

    # Resolve + download the subject image from R2 using the doc's source_assets.image_key.
    try:
        image_key = integrations.get_source_image_key(avatar_id)
        if not image_key:
            raise RuntimeError(f"no source_assets.image_key on templates doc _id={avatar_id}")
        image_ext = Path(image_key).suffix or ".png"
        image_path = UPLOADS / f"{request_id}_{avatar_id}_source{image_ext}"
        integrations.r2_download(image_key, image_path)
    except Exception as e:
        _update_job(request_id, status="failed", finished_at=_now(), error=repr(e))
        integrations.set_status(avatar_id, "failed", failure_reason=repr(e))
        return

    _update_job(request_id, status="waiting_for_gpu")
    with _GPU_LOCK:
        _update_job(request_id, status="running", started_at=_now())
        output_path = OUTPUTS / f"idle_{avatar_id}_{request_id}.mp4"
        try:
            pipeline_runtime.generate(
                image_path=str(image_path),
                output_path=str(output_path),
                video_path=str(video_path),
                prompt=prompt or None,
            )
            if not output_path.exists():
                raise RuntimeError("generation finished but no output file was produced")
            # Match the doc's key convention (no extension), e.g. "templates/<id>/idle".
            animation_key = f"templates/{avatar_id}/idle"
            link = integrations.r2_upload(output_path, key=animation_key)
            # Write the animation KEY first, THEN flip status to ready, so a consumer that
            # sees status="ready" is guaranteed to also see source_assets.idle_animation_key.
            integrations.set_idle_animation_key(avatar_id, animation_key)
            integrations.set_status(avatar_id, "ready")
            _update_job(request_id, status="done", finished_at=_now(),
                 result=str(output_path.relative_to(ROOT)), animation_key=animation_key, link=link)
        except Exception as e:
            _update_job(request_id, status="failed", finished_at=_now(), error=repr(e))
            integrations.set_status(avatar_id, "failed", failure_reason=repr(e))


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.post("/generate")
async def generate(
    image: UploadFile = File(..., description="Subject image (PNG or JPG)"),
    video: UploadFile | None = File(None, description="Reference motion video (optional; defaults to assets/idle_avatar_15_reverse.mp4)"),
    prompt: str | None = Form(None, description="Text prompt (optional; main.py has a sensible default)"),
):
    request_id = uuid.uuid4().hex[:12]

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(400, "empty image upload")
    image_path = UPLOADS / f"{request_id}_image_{image.filename}"
    image_path.write_bytes(image_bytes)

    if video is not None and video.filename:
        video_bytes = await video.read()
        if not video_bytes:
            raise HTTPException(400, "empty video upload")
        video_path = UPLOADS / f"{request_id}_video_{video.filename}"
        video_path.write_bytes(video_bytes)
        used_default = False
    else:
        video_path = DEFAULT_VIDEO
        used_default = True

    job_data = {
        "image": image.filename,
        "video": video_path.name,
        "used_default_video": used_default,
        "prompt": prompt,
    }
    integrations.create_job(request_id, "generate", **job_data)

    threading.Thread(target=run_job, args=(request_id, image_path, video_path, prompt), daemon=True).start()
    return {
        "request_id": request_id,
        "status": "pending",
        "submitted_at": _now(),
        **job_data
    }


@app.post("/idle-motion")
async def idle_motion(
    avatar_id: str = Form(..., description="Avatar id — ObjectId _id of the fableface.templates doc. "
                                          "The subject image is read from its source_assets.image_key (R2)."),
):
    """Generate an idle-motion video for an avatar from its avatar_id alone.

    Async: returns a request_id immediately. The subject image is fetched from R2 using the
    template doc's source_assets.image_key; generation uses the bundled reference clip.
    Status is tracked in MongoDB keyed by avatar_id (processing -> ready/failed); on
    success the generated idle video is uploaded to R2 and its key is written to
    source_assets.idle_animation_key before status flips to ready.
    """
    avatar_id = avatar_id.strip()
    if not avatar_id:
        raise HTTPException(400, "avatar_id is required")

    request_id = uuid.uuid4().hex[:12]
    video_path = DEFAULT_VIDEO  # idle-motion always uses the bundled reference clip

    job_data = {
        "avatar_id": avatar_id,
        "video": video_path.name,
    }
    integrations.create_job(request_id, "idle-motion", **job_data)

    threading.Thread(target=run_idle_job, args=(request_id, avatar_id, video_path, None),
                     daemon=True).start()
    return {
        "request_id": request_id,
        "status": "pending",
        "submitted_at": _now(),
        **job_data
    }


@app.get("/idle-motion/{avatar_id}/preview")
def idle_motion_preview(avatar_id: str):
    """Dry run: resolve the avatar's source image and confirm it exists in R2 — no GPU.

    Validates the same lookup /idle-motion would do (read source_assets.image_key, then
    HEAD it in R2) without generating, so you can check an avatar_id before spending a run.
    Returns ``ok: true`` only when the source object is present and fetchable.
    """
    avatar_id = avatar_id.strip()
    if not avatar_id:
        raise HTTPException(400, "avatar_id is required")

    try:
        summary = integrations.get_template_summary(avatar_id)
    except Exception as e:
        raise HTTPException(502, f"mongo lookup failed: {e!r}")
    if summary is None:
        raise HTTPException(404, f"no fableface.templates doc with _id={avatar_id}")

    image_key = summary.get("image_key")
    if not image_key:
        return {"avatar_id": avatar_id, "ok": False,
                "reason": "doc has no source_assets.image_key", **summary}

    try:
        exists = integrations.r2_object_exists(image_key)
    except Exception as e:
        raise HTTPException(502, f"r2 head_object failed for {image_key!r}: {e!r}")

    return {
        "avatar_id": avatar_id,
        "ok": exists,
        "reason": None if exists else f"image_key not found in R2: {image_key}",
        "would_write_animation_key": f"templates/{avatar_id}/idle",
        **summary,
    }


@app.get("/jobs/{request_id}")
def get_job(request_id: str):
    job = integrations.get_job(request_id)
    if not job:
        raise HTTPException(404, "request not found")
    # Remove MongoDB _id from response
    job.pop("_id", None)
    return job


@app.get("/jobs/{request_id}/result")
def get_result(request_id: str):
    job = integrations.get_job(request_id)
    if not job:
        raise HTTPException(404, "request not found")
    if job.get("status") != "done":
        raise HTTPException(409, f"job not done (status={job.get('status')})")
    return FileResponse(ROOT / job["result"], media_type="video/mp4", filename=f"motion_transfer_{request_id}.mp4")


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
