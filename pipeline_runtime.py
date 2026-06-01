"""Warm, in-process motion-transfer runtime.

Unlike ``main.py`` (which spawns a fresh subprocess per generation and therefore
re-reads ~70 GB of weights from the S3-backed model store every single time),
this module builds the :class:`ICLoraPipeline` **once** and reuses it across
calls. A shared :class:`StateDictRegistry` caches model weights in CPU RAM, so
the slow disk/S3 read happens once per process lifetime instead of twice per
request.

Cost model:
  - First ``generate()`` call: cold — reads weights from disk (~7-10 min).
  - Every later call: warm — weights served from the in-RAM registry; only the
    CPU->GPU copy, model assembly, diffusion and decode run.

The whole GPU is pinned during a generation, so calls are serialised by a lock.
Output is a silent MP4 (audio is dropped at encode time), matching ``main.py``.
"""

import os

# Must be set before torch / ltx import. Mirrors main.py's subprocess env.
os.environ.setdefault("LTX_TEXT_ENCODER_CPU", "1")
os.environ.setdefault("PYTORCH_ALLOC_CONF", "expandable_segments:True")

import logging
import tempfile
import threading
import time
from pathlib import Path

import torch

from ltx_core.loader import LTXV_LORA_COMFY_RENAMING_MAP, LoraPathStrengthAndSDOps
from ltx_core.loader.registry import StateDictRegistry
from ltx_core.model.video_vae import TilingConfig, get_video_chunks_number
from ltx_core.quantization import QuantizationPolicy
from ltx_pipelines.ic_lora import ICLoraPipeline
from ltx_pipelines.utils.args import ImageConditioningInput, resolve_path
from ltx_pipelines.utils.media_io import encode_video

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models"
ASSETS = ROOT / "assets"

# Bundled sample used to pre-warm the weight cache at startup.
SAMPLE_IMAGE = ASSETS / "images" / "avatar_15.png"
SAMPLE_VIDEO = ASSETS / "idle_avatar_15_reverse.mp4"

DISTILLED = MODELS / "distilled" / "ltx-2.3-22b-distilled.safetensors"
UPSAMPLER = MODELS / "upscaler" / "ltx-2.3-spatial-upscaler-x2-1.0.safetensors"
GEMMA = MODELS / "gemma"
LORA = MODELS / "ic-lora" / "ltx-2.3-22b-ic-lora-motion-track-control-ref0.5.safetensors"

DEFAULT_PROMPT = (
    "A person facing the camera with subtle head and shoulder movement, "
    "professional headshot lighting, neutral background, photorealistic"
)

# Defaults mirror main.py's argparse defaults.
DEFAULT_SEED = 42
DEFAULT_HEIGHT = 768
DEFAULT_WIDTH = 768
DEFAULT_NUM_FRAMES = 121
DEFAULT_FRAME_RATE = 25.0
DEFAULT_LORA_STRENGTH = 0.8
DEFAULT_VIDEO_STRENGTH = 1.0
DEFAULT_IMAGE_STRENGTH = 1.0
IMAGE_CONDITIONING_FRAME = 0
IMAGE_CONDITIONING_CRF = 33

# Built lazily on first generate(); reused forever after. The GPU is pinned for
# the duration of a generation, so we both build and run under this lock.
_LOCK = threading.Lock()
_PIPELINE: ICLoraPipeline | None = None
_REGISTRY: StateDictRegistry | None = None


def _build_pipeline() -> ICLoraPipeline:
    """Construct the pipeline once, wiring a shared in-RAM weight cache."""
    global _PIPELINE, _REGISTRY
    if _PIPELINE is not None:
        return _PIPELINE

    for path, label in [
        (DISTILLED, "distilled checkpoint"),
        (UPSAMPLER, "spatial upsampler"),
        (GEMMA, "Gemma encoder dir"),
        (LORA, "IC-LoRA weights"),
    ]:
        if not path.exists():
            raise FileNotFoundError(f"missing {label}: {path} (see BUILD.md / download_models.py)")

    t0 = time.perf_counter()
    logger.info("[runtime] building warm pipeline (one-time)...")
    _REGISTRY = StateDictRegistry()
    loras = [LoraPathStrengthAndSDOps(resolve_path(str(LORA)), DEFAULT_LORA_STRENGTH, LTXV_LORA_COMFY_RENAMING_MAP)]
    _PIPELINE = ICLoraPipeline(
        distilled_checkpoint_path=resolve_path(str(DISTILLED)),
        spatial_upsampler_path=resolve_path(str(UPSAMPLER)),
        gemma_root=resolve_path(str(GEMMA)),
        loras=loras,
        quantization=QuantizationPolicy.fp8_cast(),
        registry=_REGISTRY,
    )
    logger.info("[runtime] pipeline object built in %.1fs (weights load lazily on first generation)",
                time.perf_counter() - t0)
    return _PIPELINE


def warmup() -> None:
    """Pre-build the pipeline object. Does not load weights (that happens on the
    first generation). Safe to call at server startup."""
    with _LOCK:
        _build_pipeline()


def prewarm_weights() -> None:
    """Run one throwaway generation on the bundled sample so all model weights are
    loaded into the shared registry up front. After this returns, real requests are
    warm (~42s) instead of paying the cold model-load (~minutes) on the first one.

    Blocking and slow (roughly the cold-start time) — call it on a background thread
    so the server can still bind its port immediately.
    """
    if not SAMPLE_IMAGE.exists() or not SAMPLE_VIDEO.exists():
        logger.warning("[runtime] pre-warm skipped: sample assets missing (%s / %s); "
                       "building pipeline object only", SAMPLE_IMAGE, SAMPLE_VIDEO)
        warmup()
        return

    t0 = time.perf_counter()
    logger.info("[runtime] pre-warming: running one sample generation to load weights "
                "(this is the slow cold load, done once)...")
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "prewarm.mp4")
        generate(image_path=str(SAMPLE_IMAGE), output_path=out, video_path=str(SAMPLE_VIDEO))
    logger.info("[runtime] pre-warm complete in %.1fs; %d state dicts cached. Requests are now warm.",
                time.perf_counter() - t0, 0 if _REGISTRY is None else len(_REGISTRY._state_dicts))


@torch.inference_mode()
def generate(
    image_path: str,
    output_path: str,
    video_path: str,
    prompt: str | None = None,
    seed: int = DEFAULT_SEED,
    height: int = DEFAULT_HEIGHT,
    width: int = DEFAULT_WIDTH,
    num_frames: int = DEFAULT_NUM_FRAMES,
    frame_rate: float = DEFAULT_FRAME_RATE,
    lora_strength: float = DEFAULT_LORA_STRENGTH,
    video_strength: float = DEFAULT_VIDEO_STRENGTH,
    image_strength: float = DEFAULT_IMAGE_STRENGTH,
    skip_stage_2: bool = False,
) -> str:
    """Generate a silent motion-transferred MP4 at ``output_path`` and return it.

    Serialised on the GPU lock. The first call is cold (loads weights from disk);
    subsequent calls reuse the cached weights.
    """
    prompt = prompt or DEFAULT_PROMPT
    image_abs = resolve_path(image_path)
    video_abs = resolve_path(video_path)
    out_abs = resolve_path(output_path)
    Path(out_abs).parent.mkdir(parents=True, exist_ok=True)

    images = [
        ImageConditioningInput(
            path=image_abs,
            frame_idx=IMAGE_CONDITIONING_FRAME,
            strength=image_strength,
            crf=IMAGE_CONDITIONING_CRF,
        )
    ]
    video_conditioning = [(video_abs, video_strength)]
    tiling_config = TilingConfig.default()
    video_chunks_number = get_video_chunks_number(num_frames, tiling_config)

    with _LOCK:
        pipeline = _build_pipeline()
        warm = _REGISTRY is not None and len(_REGISTRY._state_dicts) > 0
        t0 = time.perf_counter()
        logger.info("[runtime] generating (%s start): %s -> %s",
                    "warm" if warm else "cold", Path(image_abs).name, Path(out_abs).name)

        video, _audio = pipeline(
            prompt=prompt,
            seed=seed,
            height=height,
            width=width,
            num_frames=num_frames,
            frame_rate=frame_rate,
            images=images,
            video_conditioning=video_conditioning,
            tiling_config=tiling_config,
            conditioning_attention_strength=1.0,
            skip_stage_2=skip_stage_2,
            conditioning_attention_mask=None,
        )

        # audio=None -> silent output, equivalent to main.py's post-hoc audio strip.
        encode_video(
            video=video,
            fps=int(frame_rate),
            audio=None,
            output_path=out_abs,
            video_chunks_number=video_chunks_number,
        )
        logger.info("[runtime] done in %.1fs (cached state dicts: %d) -> %s",
                    time.perf_counter() - t0,
                    0 if _REGISTRY is None else len(_REGISTRY._state_dicts), out_abs)

    return out_abs
