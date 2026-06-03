#!/usr/bin/env python3
"""Download all model weights required by main.py into ./models/.

Files fetched (~67 GB total):
  - models/distilled/ltx-2.3-22b-distilled.safetensors           (~43 GB)
  - models/upscaler/ltx-2.3-spatial-upscaler-x2-1.0.safetensors  (~1 GB)
  - models/ic-lora/ltx-2.3-22b-ic-lora-motion-track-control-ref0.5.safetensors  (~0.3 GB)
  - models/gemma/                                                (~23 GB, 5 shards)

The Gemma 3 12B text encoder is a GATED Google model. Before running this
script you must:
  1. Have a HuggingFace account.
  2. Visit https://huggingface.co/google/gemma-3-12b-it-qat-q4_0-unquantized
     while logged in, click "Acknowledge license", and submit the form.
  3. Create a read token at https://huggingface.co/settings/tokens.

Provide the token any of these ways (first match wins):
  - `HF_TOKEN` env var
  - `HUGGINGFACE_HUB_TOKEN` env var
  - Cached login from a prior `huggingface-cli login`
  - Interactive prompt from this script
"""

import getpass
import os
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download
from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError

ROOT = Path(__file__).resolve().parent
# Honor LTX_MODELS_DIR (set on Modal to the mounted volume); default ROOT/models.
MODELS = Path(os.environ.get("LTX_MODELS_DIR", str(ROOT / "models")))

LIGHTRICKS_FILES = [
    {
        "repo_id": "Lightricks/LTX-2.3",
        "filename": "ltx-2.3-22b-distilled.safetensors",
        "subdir": "distilled",
        "approx_size_gb": 43,
    },
    {
        "repo_id": "Lightricks/LTX-2.3",
        "filename": "ltx-2.3-spatial-upscaler-x2-1.0.safetensors",
        "subdir": "upscaler",
        "approx_size_gb": 1,
    },
    {
        "repo_id": "Lightricks/LTX-2.3-22b-IC-LoRA-Motion-Track-Control",
        "filename": "ltx-2.3-22b-ic-lora-motion-track-control-ref0.5.safetensors",
        "subdir": "ic-lora",
        "approx_size_gb": 0.3,
    },
]

GEMMA_REPO = "google/gemma-3-12b-it-qat-q4_0-unquantized"


def get_hf_token() -> str | None:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    if token:
        return token
    cached = Path.home() / ".cache" / "huggingface" / "token"
    if cached.is_file():
        return cached.read_text().strip() or None
    return None


def download_lightricks() -> None:
    for spec in LIGHTRICKS_FILES:
        target_dir = MODELS / spec["subdir"]
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / spec["filename"]
        if target_file.exists():
            print(f"[skip] {target_file.relative_to(ROOT)} already present")
            continue
        print(f"[get ] {spec['filename']} (~{spec['approx_size_gb']} GB) from {spec['repo_id']}")
        hf_hub_download(
            repo_id=spec["repo_id"],
            filename=spec["filename"],
            local_dir=str(target_dir),
        )


def gemma_present() -> bool:
    target_dir = MODELS / "gemma"
    return (target_dir / "model.safetensors.index.json").exists() and any(target_dir.glob("model-*.safetensors"))


def download_gemma(token: str | None) -> None:
    target_dir = MODELS / "gemma"
    target_dir.mkdir(parents=True, exist_ok=True)
    if gemma_present():
        print(f"[skip] models/gemma already populated")
        return
    print(f"[get ] {GEMMA_REPO} (~23 GB across 5 shards)")
    try:
        snapshot_download(
            repo_id=GEMMA_REPO,
            local_dir=str(target_dir),
            token=token,
            max_workers=4,
        )
    except GatedRepoError:
        sys.exit(
            "\nERROR: HuggingFace says this account isn't authorized for "
            f"{GEMMA_REPO}.\n"
            "Visit the URL above while logged in, click 'Acknowledge license', "
            "submit the form, then re-run this script."
        )
    except RepositoryNotFoundError:
        sys.exit(f"\nERROR: repo not found or your token is invalid: {GEMMA_REPO}")


def main() -> None:
    MODELS.mkdir(parents=True, exist_ok=True)
    print(f"Downloading models into {MODELS}\n")

    download_lightricks()

    if gemma_present():
        print(f"[skip] models/gemma already populated")
    else:
        token = get_hf_token()
        if token is None:
            print(
                f"\nGemma 3 ({GEMMA_REPO}) is gated. "
                "Paste a HuggingFace read token (input hidden), or press Ctrl-C to abort:"
            )
            token = getpass.getpass("HF token: ").strip() or None
        download_gemma(token)
    print("\nAll downloads complete. You can now run: python main.py <image>")


if __name__ == "__main__":
    main()
