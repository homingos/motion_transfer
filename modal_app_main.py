"""
FLAM — Motion Transfer · Modal PRODUCTION deployment.

Deploys this repo's motion-transfer service (image -> short video that moves
like a reference clip) on a GPU container. Run-only: weights are read from the
``motion-transfer-models`` Volume (uploaded out-of-band), nothing is downloaded.

  Deploy:  modal deploy modal_app_main.py

Endpoints (served by server.py's FastAPI app):
  GET  /                       UI
  POST /generate               submit a job (multipart: image, optional video/prompt)
  GET  /jobs/{id}              poll status
  GET  /jobs/{id}/result       download the .mp4
  GET  /docs                   OpenAPI docs
"""

from pathlib import Path

import modal

from modal_common import (
    APP_BASENAME, MODELS_DIR,
    build_modal_image, models_volume, mongodb_secret, jobs_dict,
)

# =============================================================================
# Production configuration (flam, Starter plan)
# =============================================================================

APP_NAME = APP_BASENAME                                  # "flam-motion-transfer"
# Custom domains need a Team plan + DNS; on Starter we use the default
# auto-generated https://<workspace>--<app>-...modal.run URL (printed on deploy).

GPU = "RTX-PRO-6000"            # 48 GB VRAM — pipeline peaks ~20 GB
CPU = 8
MEMORY = 98304          # 96 GB RAM — weight load holds ~80 GB (40 GB would OOM)
TIMEOUT = 3600          # 1h: first container start loads weights (~15-20 min)
MIN_CONTAINERS = 1      # keep one container warm to avoid GPU provisioning delays
MAX_CONTAINERS = 20     # scale to 20 GPU containers; job state in modal.Dict for distributed polling
SCALEDOWN_WINDOW = 300   # 5 minutes — continuous traffic pattern, scale down quickly
MAX_CONCURRENT_INPUTS = 1  # the pipeline pins the whole GPU; one job per container at a time

SCRIPT_DIR = Path(__file__).parent.resolve()
image = build_modal_image(SCRIPT_DIR)
app = modal.App(APP_NAME, image=image)


# =============================================================================
# Inference service — serves the FastAPI app on a warm GPU
# =============================================================================

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
    enable_memory_snapshot=True,   # snapshot CPU RAM so cold starts skip the ~18-min weight read
)
@modal.concurrent(max_inputs=MAX_CONCURRENT_INPUTS)
class MotionTransferInference:
    """FLAM Motion Transfer — Production."""

    @modal.enter(snap=True)
    def _load_weights_to_cpu(self):
        # Runs during snapshotting (CPU only, no GPU). Loads ~67 GB of weights from the
        # volume into the in-RAM registry; the snapshot captures it, so future cold starts
        # restore from the snapshot instead of re-reading/re-parsing from disk. This turns
        # the first request from a ~20-min cold load into the warm path (~40s).
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

    @modal.asgi_app(label="motion-transfer")
    def fastapi_app(self):
        # server.py's lifespan builds the pipeline object (cheap). Weights are already
        # resident in CPU RAM from the restored memory snapshot, so the first real
        # request hits the warm path instead of paying the cold disk read.
        from server import app as fastapi_app
        return fastapi_app


# =============================================================================
# Local info
# =============================================================================

@app.local_entrypoint()
def main():
    print("=" * 60)
    print("🎬 FLAM — Motion Transfer · Deployment (flam)")
    print("=" * 60)
    print(f"  App name:  {APP_NAME}")
    print(f"  GPU:       {GPU}   CPU: {CPU}   RAM: {MEMORY} MB")
    print(f"  Scaling:   min={MIN_CONTAINERS} max={MAX_CONTAINERS} (scale-to-zero)")
    print()
    print("  URL: https://flam--motion-transfer.modal.run")
    print("  Endpoints once deployed: /  /generate  /jobs/{id}  /jobs/{id}/result  /docs")
    print()
    print("🚀 Deploy:  modal deploy modal_app_main.py")
    print(f"   logs:    modal app logs {APP_NAME}")
    print("=" * 60)
