"""
FLAM — Motion Transfer · Modal DEV deployment.

Same app/image as production, intended for the `dev` Modal environment so dev and
prod stay isolated within the flam workspace:

    modal environment create dev                       # once
    modal volume create motion-transfer-models -e dev
    modal volume put motion-transfer-models ./models/distilled /distilled -e dev
    modal volume put motion-transfer-models ./models/gemma     /gemma     -e dev
    modal volume put motion-transfer-models ./models/ic-lora   /ic-lora   -e dev
    modal volume put motion-transfer-models ./models/upscaler  /upscaler  -e dev
    modal deploy modal_app_dev.py -e dev

NOTE: Modal Volumes are environment-scoped, so the dev environment needs its OWN
copy of the weights (the commands above). If you'd rather not re-upload 67 GB,
skip the separate environment and deploy this file to `main` instead — but then
give it a distinct app name so it doesn't collide with prod.
"""

from pathlib import Path
import os

import modal

from modal_common import (
    APP_BASENAME, MODELS_DIR,
    build_modal_image, models_volume, mongodb_secret, jobs_dict,
)

# Set API mode for this environment
os.environ["API_MODE"] = "r2_only"
# Set default output duration to 2 seconds (will be reversed → 4 seconds total looped)
os.environ["TARGET_OUTPUT_SECONDS"] = "2.0"

APP_NAME = APP_BASENAME          # same name; isolated by the `dev` environment

GPU = "RTX-PRO-6000"    # 96 GB Blackwell (~$3.03/hr); same card used on Lightning
CPU = 8
MEMORY = 98304          # 96 GB RAM (weight load holds ~80 GB)
TIMEOUT = 3600
MIN_CONTAINERS = 1      # keep one container warm to avoid GPU provisioning delays
MAX_CONTAINERS = 5      # scale to 5 GPU containers; job state in modal.Dict for distributed polling
SCALEDOWN_WINDOW = 300  # 5 min: scale down containers after 5 minutes of no jobs
MAX_CONCURRENT_INPUTS = 1

SCRIPT_DIR = Path(__file__).parent.resolve()
image = build_modal_image(SCRIPT_DIR)
app = modal.App(APP_NAME, image=image)


@app.cls(
    gpu=GPU,
    cpu=CPU,
    memory=MEMORY,
    timeout=TIMEOUT,
    min_containers=MIN_CONTAINERS,
    max_containers=MAX_CONTAINERS,
    scaledown_window=SCALEDOWN_WINDOW,
    volumes={MODELS_DIR: models_volume},
    secrets=[mongodb_secret],   # /idle-motion: MongoDB status tracking
    enable_memory_snapshot=True,   # snapshot CPU RAM so cold starts skip the ~20-min weight read
)
@modal.concurrent(max_inputs=MAX_CONCURRENT_INPUTS)
class MotionTransferInferenceDev:
    """FLAM Motion Transfer — Dev (deploy with `-e dev`)."""

    @modal.enter(snap=True)
    def _load_weights_to_cpu(self):
        # Runs during snapshotting (CPU only, no GPU). Loads ~67 GB of weights from the
        # volume into the in-RAM registry; the snapshot captures it, so future cold starts
        # restore from the snapshot instead of re-reading/re-parsing from disk.
        import pipeline_runtime
        pipeline_runtime.preload_weights_cpu()

    @modal.enter(snap=False)
    def _init_cuda_after_restore(self):
        # Runs AFTER snapshot restore, with the GPU attached, on the container's MAIN thread.
        # Memory snapshots are CPU-only, so CUDA is uninitialized after restore. If the first
        # CUDA call instead happens lazily inside a request's daemon thread (run_idle_job runs
        # in a threading.Thread), the first kernel aborts with SIGABRT
        # ("terminate called without an active exception"). Initializing CUDA here, then running
        # one warmup generation (compiles fp8/xformers/triton kernels on the main thread), makes
        # request threads reuse a ready context — and makes the first real request fast.
        import torch
        if torch.cuda.is_available():
            torch.zeros(1, device="cuda")
            torch.cuda.synchronize()
        import pipeline_runtime
        # The pipeline was built CPU-only during the snapshot phase; repoint it at the GPU,
        # otherwise generation runs on CPU (GPU idle, hangs at 0/8).
        pipeline_runtime.bind_pipeline_to_gpu()
        pipeline_runtime.prewarm_weights()

    @modal.asgi_app(label="motion-transfer-dev")
    def fastapi_app(self):
        from server import app as fastapi_app
        return fastapi_app


@app.local_entrypoint()
def main():
    print("🎬 FLAM — Motion Transfer · DEV")
    print(f"  App:   {APP_NAME}  (deploy into the `dev` environment)")
    print(f"  GPU:   {GPU}  RAM: {MEMORY} MB  scaling: min={MIN_CONTAINERS} max={MAX_CONTAINERS}")
    print("  URL:   https://flam-dev--motion-transfer-dev.modal.run  (label=motion-transfer-dev)")
    print("         NOTE: the env ('dev') is part of the workspace slug -> 'flam-dev--...'")
    print("  Deploy: modal deploy modal_app_dev.py -e dev")
