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
import ltx_pipelines.ic_lora as _ic_lora
from ltx_pipelines.utils import timing as T
from ltx_pipelines.utils.args import ImageConditioningInput, resolve_path
from ltx_pipelines.utils.media_io import encode_video

import json

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
TIMINGS_LOG = ROOT / "timings.jsonl"
# Storage location for model weights. Defaults to ROOT/models locally; on Modal
# it's set (via LTX_MODELS_DIR) to the mounted `motion-transfer-models` volume.
MODELS = Path(os.environ.get("LTX_MODELS_DIR", str(ROOT / "models")))
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
    "minimal facial expression, no eyebrow movement, closed mouth, natural blinking, "
    "professional headshot lighting, neutral background, photorealistic"
)

# Defaults mirror main.py's argparse defaults.
DEFAULT_SEED = 42
DEFAULT_HEIGHT = 768
DEFAULT_WIDTH = 768
DEFAULT_NUM_FRAMES = 121
DEFAULT_FRAME_RATE = 25.0
DEFAULT_LORA_STRENGTH = 0.8
DEFAULT_VIDEO_STRENGTH = 0.95
DEFAULT_IMAGE_STRENGTH = 1.0
IMAGE_CONDITIONING_FRAME = 0
IMAGE_CONDITIONING_CRF = 33

# Built lazily on first generate(); reused forever after. The GPU is pinned for
# the duration of a generation, so we both build and run under this lock.
_LOCK = threading.Lock()
_PIPELINE: ICLoraPipeline | None = None
_REGISTRY: StateDictRegistry | None = None

# Prompt embedding cache: prompt_text -> EmbeddingsProcessorOutput
# Populated during prewarm as a side effect. Never touched in snap=True phase.
_PROMPT_CACHE: dict[str, object] = {}

# Resident transformer cache: "s1"/"s2" -> live X0Model on GPU VRAM
# Populated during prewarm. Eliminates ~12-13s of CPU->GPU transfer per request.
_RESIDENT_TRANSFORMERS: dict[str, object] = {}


# --- Hardened prompt cache: always active, survives skipped prewarm ---
def _encode_cached_prompt(prompts, model_ledger, **kw):
    """Cached wrapper for encode_prompts. Hits cache on matching prompts."""
    from ltx_pipelines.utils.helpers import encode_prompts as _orig_encode
    if len(prompts) == 1 and not kw.get("enhance_first_prompt") and prompts[0] in _PROMPT_CACHE:
        logger.info("[cache] encode_prompts HIT")
        return [_PROMPT_CACHE[prompts[0]]]
    results = _orig_encode(prompts, model_ledger, **kw)
    if len(prompts) == 1 and not kw.get("enhance_first_prompt"):
        _PROMPT_CACHE[prompts[0]] = results[0]
        logger.info("[cache] encode_prompts: cached prompt (%d chars)", len(prompts[0]))
    return results

_ic_lora.encode_prompts = _encode_cached_prompt


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


def bind_pipeline_to_gpu() -> None:
    """Repoint the snapshot-built pipeline from CPU to GPU after a memory-snapshot restore.

    The pipeline is constructed during Modal's CPU-only ``@modal.enter(snap=True)`` phase, so
    ``get_device()`` returned CPU and that was baked into ``pipeline.device``, both ledgers'
    ``.device``, and ``pipeline_components.device`` — and captured in the snapshot. After restore
    the GPU exists, but those handles still say CPU, so generation runs on CPU (GPU idle, looks
    hung at 0/8). Reset every device handle to cuda. Idempotent; no-op without CUDA.

    Note: the text encoder still goes to CPU when LTX_TEXT_ENCODER_CPU=1 — that is decided inside
    ``ModelLedger.text_encoder()`` by the env var, independent of ``ledger.device``.
    """
    import torch

    if not torch.cuda.is_available():
        return
    cuda = torch.device("cuda")
    with _LOCK:
        pipeline = _build_pipeline()
        pipeline.device = cuda
        for name in ("stage_1_model_ledger", "stage_2_model_ledger"):
            ledger = getattr(pipeline, name, None)
            if ledger is not None:
                ledger.device = cuda
        comps = getattr(pipeline, "pipeline_components", None)
        if comps is not None and hasattr(comps, "device"):
            comps.device = cuda
    logger.info("[runtime] pipeline re-bound to GPU (cuda) after snapshot restore")


def prewarm_weights() -> None:
    """Run one optimized generation to load weights and populate caches.

    Installs two caches during prewarm so subsequent requests are fast:
    - Prompt embedding cache: DEFAULT_PROMPT cached, eliminates ~10-14s per call
    - Resident transformer cache: s1 & s2 kept in GPU VRAM, eliminates ~12-13s per call

    Prewarm uses skip_stage_2=True + short clip (49 frames) to finish in ~29s.
    After caches are installed, warm requests complete in ~14-17s instead of ~38-42s.
    """
    if not SAMPLE_IMAGE.exists() or not SAMPLE_VIDEO.exists():
        logger.warning("[runtime] pre-warm skipped: sample assets missing (%s / %s); "
                       "building pipeline object only", SAMPLE_IMAGE, SAMPLE_VIDEO)
        warmup()
        return

    t0 = time.perf_counter()
    logger.info("[runtime] pre-warming (optimised: skip_stage_2, short clip, cache install)...")

    with _LOCK:
        pipeline = _build_pipeline()

    # --- Patch: saving shim for stage_1 transformer ---
    _s1_ledger = pipeline.stage_1_model_ledger
    _orig_s1_transformer = _s1_ledger.__class__.transformer

    def _save_s1(self):
        model = _orig_s1_transformer(self)
        _RESIDENT_TRANSFORMERS["s1"] = model
        logger.info("[cache] s1 transformer saved to resident cache")
        return model
    _s1_ledger.transformer = lambda: _save_s1(_s1_ledger)

    # --- Run a fast prewarm: skip stage 2, short clip ---
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "prewarm.mp4")
        generate(
            image_path=str(SAMPLE_IMAGE),
            output_path=out,
            video_path=str(SAMPLE_VIDEO),
            num_frames=49,          # shorter clip: fewer denoise steps
            skip_stage_2=True,      # skip ~8.5s of stage-2 work
        )

    # --- Build + cache s2 transformer (weights in registry from snapshot) ---
    with _LOCK:
        t2 = time.perf_counter()
        logger.info("[cache] building resident s2 transformer...")
        _RESIDENT_TRANSFORMERS["s2"] = pipeline.stage_2_model_ledger.transformer()
        logger.info("[cache] s2 transformer resident in %.1fs", time.perf_counter() - t2)

        # --- Cache video encoder ---
        logger.info("[cache] building resident video encoder...")
        _RESIDENT_TRANSFORMERS["video_encoder"] = pipeline.stage_1_model_ledger.video_encoder()
        pipeline.stage_1_model_ledger.video_encoder = lambda: _RESIDENT_TRANSFORMERS["video_encoder"]
        logger.info("[cache] video encoder resident in GPU cache")

        # --- Rebind both ledgers to return cached instances ---
        pipeline.stage_1_model_ledger.transformer = lambda: _RESIDENT_TRANSFORMERS["s1"]
        pipeline.stage_2_model_ledger.transformer = lambda: _RESIDENT_TRANSFORMERS["s2"]
        logger.info("[cache] both ledger.transformer() rebound to resident GPU cache")

    logger.info("[runtime] pre-warm complete in %.1fs; prompt_cache=%d, transformers=%s",
                time.perf_counter() - t0, len(_PROMPT_CACHE), list(_RESIDENT_TRANSFORMERS))


# Accessors used by a real generation; calling each populates the shared registry.
# (audio_encoder is intentionally excluded — generate() never uses it.)
_PRELOAD_ACCESSORS = (
    "text_encoder", "gemma_embeddings_processor", "video_encoder", "video_decoder",
    "audio_decoder", "vocoder", "spatial_upsampler", "transformer",
)


def preload_weights_cpu() -> None:
    """Populate the shared StateDictRegistry on CPU only — for Modal memory snapshots.

    Builds every model the pipeline uses via the ledger accessors with the ledger's device
    forced to CPU, so the registry is filled with the exact (fp8-chained) keys ``generate()``
    later looks up — a later GPU request then hits the cache instead of re-reading ~67 GB.

    MUST stay CUDA-free: this runs inside Modal's ``@modal.enter(snap=True)`` phase where no GPU
    exists; any ``torch.cuda.*`` call (e.g. ``cleanup_memory``) would init CUDA with zero devices
    and corrupt the snapshot. Forcing ledger.device=cpu makes each accessor's trailing
    ``.to(self.device)`` a no-op, and we never call cleanup_memory here.
    """
    t0 = time.perf_counter()
    logger.info("[snapshot] preloading all weights to CPU (no GPU)...")
    with _LOCK:
        pipeline = _build_pipeline()
        ledgers = [
            getattr(pipeline, "stage_1_model_ledger", None),
            getattr(pipeline, "stage_2_model_ledger", None),
        ]
        for li, ledger in enumerate(ledgers):
            if ledger is None:
                continue
            saved_device = ledger.device
            ledger.device = torch.device("cpu")  # make the accessors' .to(device) a CPU no-op
            try:
                for name in _PRELOAD_ACCESSORS:
                    accessor = getattr(ledger, name, None)
                    if accessor is None:
                        continue
                    ts = time.perf_counter()
                    try:
                        model = accessor()  # side effect: registry populated on CPU
                        del model
                        cached = 0 if _REGISTRY is None else len(_REGISTRY._state_dicts)
                        logger.info("[snapshot] ledger%d.%s OK in %.1fs (registry=%d)",
                                    li + 1, name, time.perf_counter() - ts, cached)
                    except Exception:  # noqa: BLE001 - log the REAL error so the snap phase is diagnosable
                        # Do NOT swallow silently: a missing builder, CUDA touch, OOM precursor,
                        # or dtype error here is exactly what we need to see in the Modal logs.
                        logger.exception("[snapshot] ledger%d.%s FAILED after %.1fs",
                                         li + 1, name, time.perf_counter() - ts)
            finally:
                ledger.device = saved_device
    n = 0 if _REGISTRY is None else len(_REGISTRY._state_dicts)
    logger.info("[snapshot] CPU preload complete in %.1fs; %d state dicts cached.",
                time.perf_counter() - t0, n)


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

        T.begin()
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
        spans = T.report()

        # audio=None -> silent output, equivalent to main.py's post-hoc audio strip.
        t_enc = time.perf_counter()
        encode_video(
            video=video,
            fps=int(frame_rate),
            audio=None,
            output_path=out_abs,
            video_chunks_number=video_chunks_number,
        )
        spans.append(("mp4_encode", time.perf_counter() - t_enc))

        total = time.perf_counter() - t0
        logger.info("[runtime] done in %.1fs (cached state dicts: %d) -> %s",
                    total, 0 if _REGISTRY is None else len(_REGISTRY._state_dicts), out_abs)
        _write_timing_report(spans, total, warm, num_frames, height, width, out_abs)

    return out_abs


def _write_timing_report(spans: list[tuple[str, float]], total: float, warm: bool,
                         num_frames: int, height: int, width: int, out_abs: str) -> None:
    """Append a structured per-phase timing record to timings.jsonl and log a table.

    ``warm`` reflects whether weights were already cached when the run started, so a
    cold record shows the one-time model-load cost (the build_* phases dominate) and a
    warm record shows steady-state per-request compute.
    """
    measured = sum(dt for _, dt in spans)
    record = {
        "kind": "warm" if warm else "cold",
        "total_s": round(total, 3),
        "measured_s": round(measured, 3),
        "unattributed_s": round(total - measured, 3),
        "num_frames": num_frames,
        "height": height,
        "width": width,
        "output": Path(out_abs).name,
        "phases": [{"name": n, "secs": round(dt, 3)} for n, dt in spans],
    }
    try:
        with TIMINGS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:  # pragma: no cover - logging must never break a run
        logger.warning("[timing] could not write %s: %r", TIMINGS_LOG, e)

    lines = [f"[timing] ===== {record['kind'].upper()} run breakdown ({total:.1f}s total) ====="]
    for n, dt in spans:
        pct = 100.0 * dt / total if total else 0.0
        bar = "#" * int(round(pct / 2))
        lines.append(f"[timing] {n:<24} {dt:8.2f}s {pct:5.1f}% {bar}")
    logger.info("\n".join(lines))
