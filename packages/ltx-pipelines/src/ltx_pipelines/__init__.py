"""LTX-2 motion transfer pipeline.

Exposes the single supported pipeline (image + reference video -> video):

- ICLoraPipeline: motion transfer via IC-LoRA conditioning on a reference video.
- ModelLedger:   central coordinator for loading and building model components.
"""

# Modified from Lightricks LTX-2 by Flam, 2026-05-25 (LTX-2 Community License §3(c)):
# - Trimmed public exports to ICLoraPipeline + ModelLedger only; the other
#   pipelines (TI2Vid* / Distilled / KeyframeInterpolation / A2Vid / Retake)
#   were removed in this fork because they are not used by main.py.

from ltx_pipelines.ic_lora import ICLoraPipeline
from ltx_pipelines.utils.model_ledger import ModelLedger

__all__ = [
    "ICLoraPipeline",
    "ModelLedger",
]
