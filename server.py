"""FastAPI server that exposes the motion-transfer pipeline.

Run locally:
    python server.py
    # or
    uvicorn server:app --host 0.0.0.0 --port 8000

UI:  open http://localhost:8000/ in a browser.
API: POST /generate (multipart), then GET /jobs/{id} to poll, then GET /jobs/{id}/result for the mp4.
"""

import logging
import os
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import pipeline_runtime
import integrations

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
UPLOADS = ROOT / "uploads"
OUTPUTS = ROOT / "outputs"
STATIC = ROOT / "static"
DEFAULT_VIDEO = ROOT / "assets" / "idle_avatar_15_reverse.mp4"

UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

# Environment-specific API mode: "full" (both image + avatar_id) or "r2_only" (avatar_id only)
# Set by modal_app_dev.py (r2_only) or modal_app_feat.py (full) before import
API_MODE = os.environ.get("API_MODE", "full")


def _public_api_key_guard(request: Request) -> None:
    """Check PUBLIC_API_KEY header if PUBLIC_API_KEY env var is set."""
    key = os.environ.get("PUBLIC_API_KEY")
    if key and request.headers.get("X-API-Key") != key:
        raise HTTPException(401, "invalid or missing X-API-Key")


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

# In-memory job registry for this server instance.
JOBS: dict[str, dict] = {}
_JOB_LOCK = threading.Lock()
# Serialize generations: the pipeline pins the whole GPU, so we run one at a time.
_GPU_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _update_job(request_id: str, **fields) -> None:
    with _JOB_LOCK:
        if request_id in JOBS:
            JOBS[request_id].update(fields)


def run_job(request_id: str, image_path: Path, video_path: Path, prompt: str | None,
            lora_strength: float | None = None, video_strength: float | None = None) -> None:
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
            kwargs = {
                "image_path": str(image_path),
                "output_path": str(output_path),
                "video_path": str(video_path),
                "prompt": prompt or None,
            }
            if lora_strength is not None:
                kwargs["lora_strength"] = lora_strength
            if video_strength is not None:
                kwargs["video_strength"] = video_strength
            pipeline_runtime.generate(**kwargs)
            if output_path.exists():
                _update_job(request_id, status="done", finished_at=_now(),
                     result=str(output_path.relative_to(ROOT)))
            else:
                _update_job(request_id, status="failed", finished_at=_now(),
                     error="generation finished but no output file was produced")
        except Exception as e:
            _update_job(request_id, status="failed", finished_at=_now(), error=repr(e))


def run_idle_job(request_id: str, avatar_id: str, video_path: Path, prompt: str | None,
                 lora_strength: float | None = None, video_strength: float | None = None) -> None:
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
            kwargs = {
                "image_path": str(image_path),
                "output_path": str(output_path),
                "video_path": str(video_path),
                "prompt": prompt or None,
            }
            if lora_strength is not None:
                kwargs["lora_strength"] = lora_strength
            if video_strength is not None:
                kwargs["video_strength"] = video_strength
            pipeline_runtime.generate(**kwargs)
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


def run_idle_job_with_url(request_id: str, image_url: str, video_path: Path, prompt: str | None,
                          lora_strength: float | None = None, video_strength: float | None = None) -> None:
    """Generate the idle-motion video from a GCS image URL.

    Downloads the image from the provided GCS URL, generates the motion video,
    and uploads the result to R2.
    """
    _update_job(request_id, status="fetching_source", image_url=image_url)

    # Download the subject image from the GCS URL
    try:
        import requests
        image_ext = ".png"
        if "." in image_url.split("/")[-1]:
            image_ext = "." + image_url.split(".")[-1]
        image_path = UPLOADS / f"{request_id}_gcs_source{image_ext}"

        response = requests.get(image_url, timeout=30)
        response.raise_for_status()
        image_path.write_bytes(response.content)
    except Exception as e:
        _update_job(request_id, status="failed", finished_at=_now(), error=repr(e))
        return

    _update_job(request_id, status="waiting_for_gpu")
    with _GPU_LOCK:
        _update_job(request_id, status="running", started_at=_now())
        output_path = OUTPUTS / f"idle_{request_id}.mp4"
        try:
            kwargs = {
                "image_path": str(image_path),
                "output_path": str(output_path),
                "video_path": str(video_path),
                "prompt": prompt or None,
            }
            if lora_strength is not None:
                kwargs["lora_strength"] = lora_strength
            if video_strength is not None:
                kwargs["video_strength"] = video_strength
            pipeline_runtime.generate(**kwargs)
            if not output_path.exists():
                raise RuntimeError("generation finished but no output file was produced")
            # Upload to R2 with a generic key
            animation_key = f"motion/{request_id}/idle"
            link = integrations.r2_upload(output_path, key=animation_key)
            _update_job(request_id, status="done", finished_at=_now(),
                 result=str(output_path.relative_to(ROOT)), animation_key=animation_key, link=link)
        except Exception as e:
            _update_job(request_id, status="failed", finished_at=_now(), error=repr(e))


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.post("/generate")
async def generate(
    image: UploadFile = File(..., description="Subject image (PNG or JPG)"),
    video: UploadFile | None = File(None, description="Reference motion video (optional; defaults to assets/idle_avatar_15_reverse.mp4)"),
    prompt: str | None = Form(None, description="Text prompt (optional; main.py has a sensible default)"),
    lora_strength: float | None = Form(None, description="LoRA strength (optional, 0.0-1.0; default 0.8)"),
    video_strength: float | None = Form(None, description="Video conditioning strength (optional, 0.0-1.0; default 0.95)"),
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

    with _JOB_LOCK:
        JOBS[request_id] = {
            "status": "pending",
            "image": image.filename,
            "video": video_path.name,
            "used_default_video": used_default,
            "prompt": prompt,
            "submitted_at": _now(),
        }

    threading.Thread(target=run_job, args=(request_id, image_path, video_path, prompt, lora_strength, video_strength), daemon=True).start()
    return {"request_id": request_id, **JOBS[request_id]}


@app.post("/idle-motion")
async def idle_motion(
    image_url: str = Form(..., description="GCS image URL — direct URL to the image to process."),
    lora_strength: float | None = Form(None, description="LoRA strength (optional, 0.0-1.0; default 0.8)"),
    video_strength: float | None = Form(None, description="Video conditioning strength (optional, 0.0-1.0; default 0.95)"),
):
    """Generate an idle-motion video from a GCS image URL.

    Async: returns a request_id immediately. The image is downloaded from the provided GCS URL;
    generation uses the bundled reference clip. Status is tracked in memory; on success the
    generated idle video is uploaded to R2.
    """
    image_url = image_url.strip()
    if not image_url:
        raise HTTPException(400, "image_url is required")

    request_id = uuid.uuid4().hex[:12]
    video_path = DEFAULT_VIDEO  # idle-motion always uses the bundled reference clip

    with _JOB_LOCK:
        JOBS[request_id] = {
            "status": "pending",
            "image_url": image_url,
            "video": video_path.name,
            "submitted_at": _now(),
        }

    threading.Thread(target=run_idle_job_with_url, args=(request_id, image_url, video_path, None, lora_strength, video_strength),
                     daemon=True).start()
    return {"request_id": request_id, **JOBS[request_id]}


@app.get("/avatar/{avatar_id}/info")
def avatar_info(avatar_id: str):
    """Get avatar metadata: status, image_key, idle_animation_key, and R2 status.

    Returns the template doc's status and asset keys without generating, confirming
    what exists in MongoDB and R2.
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
    idle_animation_key = summary.get("idle_animation_key")

    image_exists = False
    animation_exists = False

    if image_key:
        try:
            image_exists = integrations.r2_object_exists(image_key)
        except Exception as e:
            raise HTTPException(502, f"r2 head_object failed for {image_key!r}: {e!r}")

    if idle_animation_key:
        try:
            animation_exists = integrations.r2_object_exists(idle_animation_key)
        except Exception as e:
            logger.warning("failed to check animation key in R2: %r", e)

    return {
        "avatar_id": avatar_id,
        "status": summary.get("status"),
        "image_key": image_key,
        "image_exists_in_r2": image_exists,
        "idle_animation_key": idle_animation_key,
        "idle_animation_exists_in_r2": animation_exists,
    }


@app.post("/avatar/{avatar_id}/status")
def update_avatar_status(avatar_id: str, status: str, failure_reason: str | None = Form(None)):
    """Update avatar status (and optionally failure_reason).

    Valid status values: "processing", "ready", "failed".
    """
    avatar_id = avatar_id.strip()
    if not avatar_id:
        raise HTTPException(400, "avatar_id is required")
    if status not in ("processing", "ready", "failed"):
        raise HTTPException(400, "status must be one of: processing, ready, failed")

    try:
        integrations.set_status(avatar_id, status, failure_reason=failure_reason)
        return {"avatar_id": avatar_id, "status": status, "updated": True}
    except Exception as e:
        raise HTTPException(502, f"failed to update status: {e!r}")


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


@app.post("/animate")
async def animate(
    image: UploadFile | None = File(None, description="Subject image (PNG or JPG). If provided, returns MP4 directly."),
    avatar_id: str | None = Form(None, description="Avatar id (ObjectId). If provided, returns request_id for async polling (R2 lookup required)."),
    prompt: str | None = Form(None, description="Text prompt (optional)"),
    lora_strength: float | None = Form(None, description="LoRA strength (optional, 0.0-1.0; default 0.8)"),
    video_strength: float | None = Form(None, description="Video conditioning strength (optional, 0.0-1.0; default 0.95)"),
    _: None = Depends(_public_api_key_guard),
):
    """Unified endpoint: image → MP4 directly; avatar_id → request_id for polling.

    Auth: X-API-Key header (only required if PUBLIC_API_KEY env var is set).

    **Image mode (synchronous):**
      Supply `image` file → generates and returns MP4 directly.
      Takes 2-5 minutes, returns binary MP4 file.

    **Avatar ID mode (asynchronous):**
      Supply `avatar_id` → returns request_id immediately.
      System fetches image from R2, generates, and uploads result.
      Poll via GET /jobs/{request_id}, download via GET /jobs/{request_id}/result.
    """
    if not image and not avatar_id:
        raise HTTPException(400, "either 'image' or 'avatar_id' is required")
    if image and avatar_id:
        raise HTTPException(400, "provide only one of 'image' or 'avatar_id'")

    # Environment-specific API mode check: dev only allows avatar_id
    if API_MODE == "r2_only" and image:
        raise HTTPException(400, "image upload not allowed in this environment; provide avatar_id instead")

    # ============= IMAGE MODE (SYNCHRONOUS) =============
    if image:
        request_id = uuid.uuid4().hex[:12]

        image_bytes = await image.read()
        if not image_bytes:
            raise HTTPException(400, "empty image upload")
        image_path = UPLOADS / f"{request_id}_image_{image.filename}"
        image_path.write_bytes(image_bytes)

        video_path = DEFAULT_VIDEO
        output_path = OUTPUTS / f"sync_{request_id}.mp4"

        # Run synchronously (blocking) and return the MP4 file directly
        try:
            kwargs = {
                "image_path": str(image_path),
                "output_path": str(output_path),
                "video_path": str(video_path),
                "prompt": prompt or None,
            }
            if lora_strength is not None:
                kwargs["lora_strength"] = lora_strength
            if video_strength is not None:
                kwargs["video_strength"] = video_strength

            with _GPU_LOCK:
                pipeline_runtime.generate(**kwargs)

            if not output_path.exists():
                raise HTTPException(500, "generation completed but no output file was produced")

            return FileResponse(
                output_path,
                media_type="video/mp4",
                filename=f"idle_animation_{request_id}.mp4",
            )
        except FileNotFoundError:
            raise HTTPException(500, "output file not found after generation")
        except Exception as e:
            raise HTTPException(500, f"generation failed: {repr(e)}")

    # ============= AVATAR ID MODE (ASYNCHRONOUS) =============
    else:
        avatar_id = avatar_id.strip()
        if not avatar_id:
            raise HTTPException(400, "avatar_id is required and cannot be empty")

        request_id = uuid.uuid4().hex[:12]
        video_path = DEFAULT_VIDEO

        with _JOB_LOCK:
            JOBS[request_id] = {
                "status": "pending",
                "avatar_id": avatar_id,
                "video": video_path.name,
                "submitted_at": _now(),
            }

        threading.Thread(
            target=run_idle_job,
            args=(request_id, avatar_id, video_path, None, lora_strength, video_strength),
            daemon=True,
        ).start()
        return {"request_id": request_id, **JOBS[request_id]}


@app.get("/jobs/{request_id}")
def get_job(request_id: str):
    if request_id not in JOBS:
        raise HTTPException(404, "request not found")
    return JOBS[request_id]


@app.get("/jobs/{request_id}/result")
def get_result(request_id: str):
    if request_id not in JOBS:
        raise HTTPException(404, "request not found")
    if JOBS[request_id].get("status") != "done":
        raise HTTPException(409, f"job not done (status={JOBS[request_id].get('status')})")
    return FileResponse(ROOT / JOBS[request_id]["result"], media_type="video/mp4", filename=f"motion_transfer_{request_id}.mp4")


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
