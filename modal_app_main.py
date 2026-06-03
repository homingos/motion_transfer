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
    build_modal_image, models_volume,
)

# =============================================================================
# Production configuration (ai-team-flam, Starter plan)
# =============================================================================

APP_NAME = APP_BASENAME                                  # "flam-motion-transfer"
# Custom domains need a Team plan + DNS; on Starter we use the default
# auto-generated https://<workspace>--<app>-...modal.run URL (printed on deploy).

GPU = "L40S"            # 48 GB VRAM — pipeline peaks ~20 GB
CPU = 8
MEMORY = 98304          # 96 GB RAM — weight load holds ~80 GB (40 GB would OOM)
TIMEOUT = 3600          # 1h: first container start loads weights (~15-20 min)
MIN_CONTAINERS = 0      # scale to zero — conserve the $30 credits (cold start on first hit)
MAX_CONTAINERS = 1      # single GPU, serialized; in-memory job state (see note below)
SCALEDOWN_WINDOW = 300  # keep warm 5 min after the last request
MAX_CONCURRENT_INPUTS = 1  # the pipeline pins the whole GPU; one job at a time

# NOTE on scaling: server.py keeps job state in an in-process dict, so polling
# /jobs/{id} must hit the same container that created the job. That's why
# MAX_CONTAINERS=1. To scale horizontally, move JOBS into a modal.Dict so any
# container can serve a poll, then raise MAX_CONTAINERS.

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
)
@modal.concurrent(max_inputs=MAX_CONCURRENT_INPUTS)
class MotionTransferInference:
    """FLAM Motion Transfer — Production."""

    @modal.asgi_app()
    def fastapi_app(self):
        # server.py's lifespan builds the pipeline and (WARMUP_ON_STARTUP=1, set
        # in the image) warms the weights in a background thread, so the port
        # binds immediately and the first real request is already warm.
        from server import app as fastapi_app
        return fastapi_app


# =============================================================================
# Local info
# =============================================================================

@app.local_entrypoint()
def main():
    print("=" * 60)
    print("🎬 FLAM — Motion Transfer · Deployment (ai-team-flam)")
    print("=" * 60)
    print(f"  App name:  {APP_NAME}")
    print(f"  GPU:       {GPU}   CPU: {CPU}   RAM: {MEMORY} MB")
    print(f"  Scaling:   min={MIN_CONTAINERS} max={MAX_CONTAINERS} (scale-to-zero)")
    print()
    print("  URL: shown in `modal deploy` output and on the Modal dashboard")
    print("       (auto-generated *.modal.run — custom domains need a Team plan).")
    print("  Endpoints once deployed: /  /generate  /jobs/{id}  /jobs/{id}/result  /docs")
    print()
    print("🚀 Deploy:  modal deploy modal_app_main.py")
    print(f"   logs:    modal app logs {APP_NAME}")
    print("=" * 60)
