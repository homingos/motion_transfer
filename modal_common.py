"""
FLAM — Motion Transfer · Modal common configuration.

Shared image builder + model-weight volume for the FLAM Motion Transfer Modal
deployment.

This deploys THIS repo's pipeline: the FastAPI app in ``server.py`` (UI + the
async job API: POST /generate -> poll GET /jobs/{id} -> GET /jobs/{id}/result),
backed by ``pipeline_runtime`` and the local ``ltx-core`` / ``ltx-pipelines``
packages.

Run-only image: it contains just what's needed to serve. The ~45-70 GB weights
are NOT baked in — they live in the ``motion-transfer-models`` Volume (uploaded
out-of-band) and are mounted at /app/models at runtime.
"""

from pathlib import Path

import modal

# =============================================================================
# Shared configuration
# =============================================================================

APP_BASENAME = "flam-motion-transfer"

# Match the versions proven to work in the local `ltx` venv.
CUDA_VERSION = "12.8.1"          # base image; torch wheels below are +cu128
PYTHON_VERSION = "3.12"

# Where the weights live inside the container. pipeline_runtime/download_models
# resolve `models/` relative to the app dir (/app), so the volume must mount here.
APP_DIR = "/app"
MODELS_DIR = "/app/models"

# Persistent volume for the model weights (mounted at /app/models; uploaded out-of-band).
MODELS_VOLUME_NAME = "motion-transfer-models"
models_volume = modal.Volume.from_name(MODELS_VOLUME_NAME, create_if_missing=True)

# Secrets for the /idle-motion flow.
# Contents required (env-var key names inside the secret must match integrations.py exactly):
#   mongodb-secret : MONGODB_URI (for job status tracking in MongoDB)
# FLAM Resource API (GCS upload) uses no secrets — it's an internal service.
mongodb_secret = modal.Secret.from_name("mongodb-secret")  # MONGODB_URI


def build_modal_image(script_dir: Path) -> modal.Image:
    """Build the Modal image with this project's dependencies and code."""
    image = modal.Image.from_registry(
        f"nvidia/cuda:{CUDA_VERSION}-cudnn-devel-ubuntu22.04",
        add_python=PYTHON_VERSION,
    )

    # System libs: ffmpeg/AV for video encode/decode, GL for image ops, TLS certs.
    image = image.apt_install(
        "ffmpeg", "libgl1-mesa-glx", "libglib2.0-0", "libsm6", "libxext6",
        "ca-certificates",
    )

    image = image.env({"UV_HTTP_TIMEOUT": "120"})

    # Step 1: PyTorch trio from the cu128 index (kept separate so its bundled
    # pins don't fight the PyPI resolution of everything else).
    image = image.pip_install(
        "torch==2.9.1",
        "torchvision==0.24.1",
        "torchaudio==2.9.1",
        extra_index_url="https://download.pytorch.org/whl/cu128",
    )

    # Step 2: runtime deps for server.py + the pipeline, from PyPI.
    image = image.pip_install(
        "fastapi==0.115.6",
        "uvicorn[standard]==0.41.0",
        "python-multipart==0.0.22",
        "transformers==4.57.1",
        "accelerate==1.10.1",
        "safetensors==0.7.0",
        "einops==0.8.1",
        "huggingface-hub==0.36.2",
        "av==16.1.0",
        "numpy==2.2.6",
        "pillow==11.3.0",
        "sentencepiece==0.2.1",
        # /idle-motion integrations: Mongo status tracking + R2 upload.
        "pymongo[srv]>=4.8",   # [srv] pulls dnspython for the mongodb+srv:// URI
        "boto3>=1.34",         # S3-compatible client for Cloudflare R2
    )

    # Runtime env + workdir. These are build steps, so they MUST come BEFORE any
    # add_local_* call — Modal forbids build steps after local files are added
    # (unless copy=True). Hence env/workdir here, local adds last.
    image = image.env({
        "PYTHONPATH": APP_DIR,
        # Fetch weights from the mounted `motion-transfer-models` volume (single
        # source of truth for the storage path; read by pipeline_runtime).
        "LTX_MODELS_DIR": MODELS_DIR,
        # Text encoder on CPU (matches the snapshot CPU-preload path).
        "LTX_TEXT_ENCODER_CPU": "1",
        "PYTORCH_ALLOC_CONF": "expandable_segments:True",
        # Memory-snapshot safety: avoid CUDA init during the snap phase.
        # NOTE: WARMUP_ON_STARTUP is intentionally NOT set — the heavy weight load
        # happens in the @modal.enter(snap=True) CPU preload, captured by the snapshot.
        "XFORMERS_ENABLE_TRITON": "1",
    })
    image = image.workdir(APP_DIR)

    # Step 3: the local workspace packages. copy=True so the editable install
    # (a build step) is allowed to run after this add and is baked into the image.
    image = image.add_local_dir(str((script_dir / "packages").resolve()), f"{APP_DIR}/packages", copy=True)
    image = image.run_commands(
        f"pip install --no-deps -e {APP_DIR}/packages/ltx-core {APP_DIR}/packages/ltx-pipelines"
    )

    # Step 4: application code + UI + sample assets — added LAST (no build steps
    # after these), so edits don't trigger a full rebuild. Only what the server
    # needs to run; we do NOT add models/ (the Volume), the local `ltx` venv,
    # outputs/uploads, or the unused CLI/download helpers.
    for f in ("server.py", "pipeline_runtime.py", "integrations.py"):
        p = script_dir / f
        if p.exists():
            image = image.add_local_file(str(p.resolve()), f"{APP_DIR}/{f}")
    for d in ("static", "assets"):
        p = script_dir / d
        if p.exists():
            image = image.add_local_dir(str(p.resolve()), f"{APP_DIR}/{d}")

    # The deploy entrypoint (modal_app_*.py) imports this module at container
    # startup, so it must be present in the container too (Modal auto-includes the
    # entrypoint file but not its local sibling imports).
    image = image.add_local_python_source("modal_common")

    return image
