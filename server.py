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
import time
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
DEFAULT_VIDEO = ROOT / "assets" / "default.mp4"

# Default output duration (seconds) — can be overridden by environment variable
DEFAULT_OUTPUT_SECONDS = float(os.environ.get("TARGET_OUTPUT_SECONDS", "4.0"))

REFERENCE_VIDEOS = {
    "default": DEFAULT_VIDEO,  # default reference video
    "female": ROOT / "assets" / "idle_avatar_15_reverse.mp4",
    "male": ROOT / "assets" / "idle_male.mp4",
    "trimmed": ROOT / "assets" / "10sec_trimmed.mp4",
}

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


def _append_reverse(video_path: Path) -> Path:
    """Append a reversed copy of the video to itself (forward + reverse = natural loop).

    Input: 5s video → Output: 10s video (forward 5s + reversed 5s).
    Overwrites the input file in-place. Uses ffmpeg concat filter.
    Returns the same path on success, raises on failure.
    """
    import subprocess
    tmp = video_path.with_suffix(".tmp.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-filter_complex",
        "[0:v]split=2[fwd][rev];[rev]reverse[rvid];[fwd][rvid]concat=n=2:v=1:a=0[out]",
        "-map", "[out]",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-an",
        str(tmp),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg reverse-append failed: {result.stderr[-500:]}")
    tmp.replace(video_path)
    return video_path


def _is_transient_gpu_error(error: Exception) -> bool:
    """Check if error is transient GPU-related (should retry) vs permanent (should fail)."""
    error_str = str(error).lower()
    transient_patterns = [
        "cuda",
        "gpu",
        "out of memory",
        "oom",
        "device",
        "allocation",
    ]
    return any(pattern in error_str for pattern in transient_patterns)


def run_job(request_id: str, image_path: Path, video_path: Path, prompt: str | None,
            lora_strength: float | None = None, video_strength: float | None = None,
            crf: int | None = None, target_output_seconds: float | None = None) -> None:
    """Run the warm in-process pipeline and capture status.

    The pipeline keeps its weights resident in CPU RAM (shared StateDictRegistry),
    so only the first job pays the cold weight-load cost; later jobs are faster.
    pipeline_runtime serialises GPU access internally via its own lock.

    Retries on transient GPU errors for up to 30 minutes, then fails with timeout.
    """
    _update_job(request_id, status="waiting_for_gpu")
    backoff = 2.0
    gpu_wait_deadline = time.time() + 1800  # 30 minutes
    while True:
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
                    return
                else:
                    _update_job(request_id, status="failed", finished_at=_now(),
                         error="generation finished but no output file was produced")
                    return
            except Exception as e:
                if _is_transient_gpu_error(e):
                    if time.time() >= gpu_wait_deadline:
                        _update_job(request_id, status="failed", finished_at=_now(),
                             error=f"GPU allocation timeout after 30 minutes: {repr(e)}")
                        return
                    logger.warning(f"Transient GPU error in run_job({request_id}): {e!r}. Retrying in {backoff}s...")
                    _update_job(request_id, status="waiting_for_gpu")
                    time.sleep(backoff)
                    backoff = min(backoff * 1.5, 30.0)  # exponential backoff, cap at 30s
                    continue
                else:
                    _update_job(request_id, status="failed", finished_at=_now(), error=repr(e))
                    return


def run_idle_job(request_id: str, avatar_id: str, video_path: Path, prompt: str | None,
                 lora_strength: float | None = None, video_strength: float | None = None,
                 crf: int | None = None, target_output_seconds: float | None = None) -> None:
    """Generate the idle-motion video for an avatar from its avatar_id alone.

    The subject image is resolved from the `fableface.templates` doc (keyed by ObjectId
    `_id` = avatar_id): we read `source_assets.image_key` and download that object from R2.

    DB lifecycle on that doc:
      status: processing                                                  on start
      source_assets.idle_animation_key = R2 key, then status: ready       on success (key first)
      status: failed (+ failure_reason)                                   on error
    (idle_vector_key is a separate field and is left untouched.)

    Retries indefinitely on transient GPU errors while staying in "waiting_for_gpu" state.
    """
    _update_job(request_id, status="fetching_source", avatar_id=avatar_id)
    integrations.set_status(avatar_id, "processing")

    # Download the subject image from the URL stored in source_assets.image_key.
    try:
        import requests
        image_url = integrations.get_source_image_key(avatar_id)
        if not image_url:
            raise RuntimeError(f"no source_assets.image_key on templates doc _id={avatar_id}")
        image_ext = ".png"
        if "." in image_url.split("/")[-1]:
            image_ext = "." + image_url.split(".")[-1]
        image_path = UPLOADS / f"{request_id}_{avatar_id}_source{image_ext}"
        response = requests.get(image_url, timeout=30)
        response.raise_for_status()
        image_path.write_bytes(response.content)
    except Exception as e:
        _update_job(request_id, status="failed", finished_at=_now(), error=repr(e))
        integrations.set_status(avatar_id, "failed", failure_reason=repr(e))
        return

    _update_job(request_id, status="waiting_for_gpu")
    backoff = 2.0
    gpu_wait_deadline = time.time() + 1800  # 30 minutes
    while True:
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
                # Append reversed clip so animation loops naturally (5s forward + 5s reverse)
                _append_reverse(output_path)
                # Upload to FLAM Resource API (GCS-backed)
                link = integrations.flam_upload(output_path)
                animation_key = link
                # Write the animation KEY first, THEN flip status to ready, so a consumer that
                # sees status="ready" is guaranteed to also see source_assets.idle_animation_key.
                integrations.set_idle_animation_key(avatar_id, animation_key)
                integrations.set_status(avatar_id, "ready")
                _update_job(request_id, status="done", finished_at=_now(),
                     result=str(output_path.relative_to(ROOT)), animation_key=animation_key, link=link)
                return
            except Exception as e:
                if _is_transient_gpu_error(e):
                    if time.time() >= gpu_wait_deadline:
                        error_msg = f"GPU allocation timeout after 30 minutes: {repr(e)}"
                        _update_job(request_id, status="failed", finished_at=_now(), error=error_msg)
                        integrations.set_status(avatar_id, "failed", failure_reason=error_msg)
                        return
                    logger.warning(f"Transient GPU error in run_idle_job({request_id}): {e!r}. Retrying in {backoff}s...")
                    _update_job(request_id, status="waiting_for_gpu")
                    time.sleep(backoff)
                    backoff = min(backoff * 1.5, 30.0)  # exponential backoff, cap at 30s
                    continue
                else:
                    _update_job(request_id, status="failed", finished_at=_now(), error=repr(e))
                    integrations.set_status(avatar_id, "failed", failure_reason=repr(e))
                    return


def run_idle_job_with_url(request_id: str, image_url: str, video_path: Path, prompt: str | None,
                          lora_strength: float | None = None, video_strength: float | None = None,
                          crf: int | None = None, target_output_seconds: float | None = None) -> None:
    """Generate the idle-motion video from a GCS image URL.

    Downloads the image from the provided GCS URL, generates the motion video,
    and uploads the result to R2.

    Retries indefinitely on transient GPU errors while staying in "waiting_for_gpu" state.
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
    backoff = 2.0
    gpu_wait_deadline = time.time() + 1800  # 30 minutes
    while True:
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
                # Append reversed clip so animation loops naturally (5s forward + 5s reverse)
                _append_reverse(output_path)
                # Upload to FLAM Resource API (GCS-backed)
                link = integrations.flam_upload(output_path)
                animation_key = link
                _update_job(request_id, status="done", finished_at=_now(),
                     result=str(output_path.relative_to(ROOT)), animation_key=animation_key, link=link)
                return
            except Exception as e:
                if _is_transient_gpu_error(e):
                    if time.time() >= gpu_wait_deadline:
                        _update_job(request_id, status="failed", finished_at=_now(),
                             error=f"GPU allocation timeout after 30 minutes: {repr(e)}")
                        return
                    logger.warning(f"Transient GPU error in run_idle_job_with_url({request_id}): {e!r}. Retrying in {backoff}s...")
                    _update_job(request_id, status="waiting_for_gpu")
                    time.sleep(backoff)
                    backoff = min(backoff * 1.5, 30.0)  # exponential backoff, cap at 30s
                    continue
                else:
                    _update_job(request_id, status="failed", finished_at=_now(), error=repr(e))
                    return


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.post("/generate")
async def generate(
    image: UploadFile = File(..., description="Subject image (PNG or JPG)"),
    video: UploadFile | None = File(None, description="Reference motion video (optional; overrides 'reference' if provided)"),
    prompt: str | None = Form(None, description="Text prompt (optional; main.py has a sensible default)"),
    lora_strength: float | None = Form(None, description="LoRA strength (optional, 0.0-1.0; default 0.8)"),
    video_strength: float | None = Form(None, description="Video conditioning strength (optional, 0.0-1.0; default 0.95)"),
    crf: int | None = Form(None, description="Image CRF compression (optional, 18-28; default 18; higher = more smoothing)"),
    target_output_seconds: float | None = Form(None, description="Target output duration in seconds (optional, default 4.0)"),
    reference: str = Form("default", description="Preset reference video: 'default' (female), 'female', or 'male'"),
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
        if reference not in REFERENCE_VIDEOS:
            raise HTTPException(400, f"invalid reference: {reference}; must be one of {list(REFERENCE_VIDEOS.keys())}")
        video_path = REFERENCE_VIDEOS[reference]
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

    # Use environment default if not provided
    output_seconds = target_output_seconds if target_output_seconds is not None else DEFAULT_OUTPUT_SECONDS
    threading.Thread(target=run_job, args=(request_id, image_path, video_path, prompt, lora_strength, video_strength, crf, output_seconds), daemon=True).start()
    return {"request_id": request_id, **JOBS[request_id]}


@app.post("/idle-motion")
async def idle_motion(
    image_url: str = Form(..., description="GCS image URL — direct URL to the image to process."),
    lora_strength: float | None = Form(None, description="LoRA strength (optional, 0.0-1.0; default 0.8)"),
    video_strength: float | None = Form(None, description="Video conditioning strength (optional, 0.0-1.0; default 0.95)"),
    crf: int | None = Form(None, description="Image CRF compression (optional, 18-28; default 18; higher = more smoothing)"),
    target_output_seconds: float | None = Form(None, description="Target output duration in seconds (optional, default 4.0)"),
    reference: str = Form("default", description="Preset reference video: 'default' (female), 'female', or 'male'"),
):
    """Generate an idle-motion video from a GCS image URL.

    Async: returns a request_id immediately. The image is downloaded from the provided GCS URL;
    generation uses the specified reference clip (default: female). Status is tracked in memory;
    on success the generated idle video is uploaded to R2.
    """
    image_url = image_url.strip()
    if not image_url:
        raise HTTPException(400, "image_url is required")
    if reference not in REFERENCE_VIDEOS:
        raise HTTPException(400, f"invalid reference: {reference}; must be one of {list(REFERENCE_VIDEOS.keys())}")

    request_id = uuid.uuid4().hex[:12]
    video_path = REFERENCE_VIDEOS[reference]

    with _JOB_LOCK:
        JOBS[request_id] = {
            "status": "pending",
            "image_url": image_url,
            "video": video_path.name,
            "submitted_at": _now(),
        }

    # Use environment default if not provided
    output_seconds = target_output_seconds if target_output_seconds is not None else DEFAULT_OUTPUT_SECONDS
    threading.Thread(target=run_idle_job_with_url, args=(request_id, image_url, video_path, None, lora_strength, video_strength, crf, output_seconds),
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
            import requests
            response = requests.head(image_key, timeout=10, allow_redirects=True)
            image_exists = response.status_code < 400
        except Exception as e:
            logger.warning("failed to check image URL: %r", e)

    if idle_animation_key:
        try:
            import requests
            response = requests.head(idle_animation_key, timeout=10, allow_redirects=True)
            animation_exists = response.status_code < 400
        except Exception as e:
            logger.warning("failed to check animation URL: %r", e)

    return {
        "avatar_id": avatar_id,
        "status": summary.get("status"),
        "image_key": image_key,
        "image_exists": image_exists,
        "idle_animation_key": idle_animation_key,
        "idle_animation_exists": animation_exists,
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
        import requests
        response = requests.head(image_key, timeout=10, allow_redirects=True)
        exists = response.status_code < 400
    except Exception as e:
        logger.warning("failed to check image URL: %r", e)
        exists = False

    return {
        "avatar_id": avatar_id,
        "ok": exists,
        "reason": None if exists else f"image_key not accessible: {image_key}",
        **summary,
    }


@app.post("/animate")
async def animate(
    image: UploadFile | None = File(None, description="Subject image (PNG or JPG). If provided, returns MP4 directly."),
    avatar_id: str | None = Form(None, description="Avatar id (ObjectId). If provided, returns request_id for async polling (R2 lookup required)."),
    prompt: str | None = Form(None, description="Text prompt (optional)"),
    lora_strength: float | None = Form(None, description="LoRA strength (optional, 0.0-1.0; default 0.8)"),
    video_strength: float | None = Form(None, description="Video conditioning strength (optional, 0.0-1.0; default 0.95)"),
    crf: int | None = Form(None, description="Image CRF compression (optional, 18-28; default 18; higher = more smoothing)"),
    target_output_seconds: float | None = Form(None, description="Target output duration in seconds (optional, default 4.0)"),
    reference: str = Form("default", description="Preset reference video: 'default' (female), 'female', or 'male'"),
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
        if reference not in REFERENCE_VIDEOS:
            raise HTTPException(400, f"invalid reference: {reference}; must be one of {list(REFERENCE_VIDEOS.keys())}")

        request_id = uuid.uuid4().hex[:12]

        image_bytes = await image.read()
        if not image_bytes:
            raise HTTPException(400, "empty image upload")
        image_path = UPLOADS / f"{request_id}_image_{image.filename}"
        image_path.write_bytes(image_bytes)

        video_path = REFERENCE_VIDEOS[reference]
        output_path = OUTPUTS / f"sync_{request_id}.mp4"

        # Run synchronously (blocking) and return the MP4 file directly
        try:
            # Use environment default if not provided
            output_seconds = target_output_seconds if target_output_seconds is not None else DEFAULT_OUTPUT_SECONDS

            kwargs = {
                "image_path": str(image_path),
                "output_path": str(output_path),
                "video_path": str(video_path),
                "prompt": prompt or None,
                "target_output_seconds": output_seconds,
            }
            if lora_strength is not None:
                kwargs["lora_strength"] = lora_strength
            if video_strength is not None:
                kwargs["video_strength"] = video_strength
            if crf is not None:
                kwargs["crf"] = crf

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
        if reference not in REFERENCE_VIDEOS:
            raise HTTPException(400, f"invalid reference: {reference}; must be one of {list(REFERENCE_VIDEOS.keys())}")

        avatar_id = avatar_id.strip()
        if not avatar_id:
            raise HTTPException(400, "avatar_id is required and cannot be empty")

        request_id = uuid.uuid4().hex[:12]
        video_path = REFERENCE_VIDEOS[reference]

        with _JOB_LOCK:
            JOBS[request_id] = {
                "status": "pending",
                "avatar_id": avatar_id,
                "video": video_path.name,
                "submitted_at": _now(),
            }

        # Use environment default if not provided
        output_seconds = target_output_seconds if target_output_seconds is not None else DEFAULT_OUTPUT_SECONDS
        threading.Thread(
            target=run_idle_job,
            args=(request_id, avatar_id, video_path, None, lora_strength, video_strength, crf, output_seconds),
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
