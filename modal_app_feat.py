"""
FLAM — Motion Transfer · Modal FEAT deployment.

Feature-branch environment for testing startup-time optimizations (prompt embedding
cache + resident transformer GPU residency) before promoting to dev/main.

Deploys to the `feat` Modal environment (isolated from dev and main).
Give this env its own volume copy if you need fresh weights, or point it at the
dev volume if the feat environment shares the dev workspace.

    modal environment create feat                        # once
    modal volume create motion-transfer-models -e feat   # or reuse dev volume
    modal deploy modal_app_feat.py -e feat

App name is distinct ("flam-motion-transfer-feat") so it can coexist with
the dev-environment app of the same base name without collision.
"""

from pathlib import Path
import os

import modal

from modal_common import (
    APP_BASENAME, MODELS_DIR,
    build_modal_image, models_volume, mongodb_secret, r2_secret,
)

# Set API mode for this environment
os.environ["API_MODE"] = "full"

APP_NAME = APP_BASENAME + "-feat"   # "flam-motion-transfer-feat" — distinct from dev/main

GPU = "RTX-PRO-6000"    # 96 GB Blackwell — same card as dev/prod
CPU = 8
MEMORY = 98304          # 96 GB RAM
TIMEOUT = 3600
MIN_CONTAINERS = 0
MAX_CONTAINERS = 1
SCALEDOWN_WINDOW = 120
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
    secrets=[mongodb_secret, r2_secret],  # feat: both image (no R2) and avatar_id (with R2) modes
    enable_memory_snapshot=True,
)
@modal.concurrent(max_inputs=MAX_CONCURRENT_INPUTS)
class MotionTransferInferenceFeat:
    """FLAM Motion Transfer — Feat (deploy with `-e feat`)."""

    @modal.enter(snap=True)
    def _load_weights_to_cpu(self):
        # Runs during snapshotting (CPU only, no GPU). Loads ~67 GB of weights from the
        # volume into the in-RAM registry; the snapshot captures it, so future cold starts
        # restore from the snapshot instead of re-reading/re-parsing from disk.
        import pipeline_runtime
        pipeline_runtime.preload_weights_cpu()

    @modal.enter(snap=False)
    def _init_cuda_after_restore(self):
        # Runs AFTER snapshot restore, with GPU attached, on the container's MAIN thread.
        # Initialises CUDA here to avoid SIGABRT on daemon threads.
        import torch
        if torch.cuda.is_available():
            torch.zeros(1, device="cuda")
            torch.cuda.synchronize()
        import pipeline_runtime
        pipeline_runtime.bind_pipeline_to_gpu()
        # prewarm_weights() now installs both the prompt embedding cache and the
        # resident transformer cache after the throwaway generation completes.
        pipeline_runtime.prewarm_weights()

    @modal.asgi_app(label="motion-transfer-feat")
    def fastapi_app(self):
        from server import app as fastapi_app
        return fastapi_app


@app.local_entrypoint()
def main():
    print("🎬 FLAM — Motion Transfer · FEAT")
    print(f"  App:    {APP_NAME}  (deploy into the `feat` environment)")
    print(f"  GPU:    {GPU}  RAM: {MEMORY} MB  scaling: min={MIN_CONTAINERS} max={MAX_CONTAINERS}")
    print("  URL:    https://ai-team-flam-feat--motion-transfer-feat.modal.run")
    print("  Deploy: modal deploy modal_app_feat.py -e feat")
