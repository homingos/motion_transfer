#!/usr/bin/env python3
"""timining.py — run the motion-transfer pipeline and profile where time goes.

Runs one or more generations on the bundled sample (avatar_15) using the warm
in-process runtime, captures the per-phase timing already instrumented in
``pipeline_runtime`` / ``ic_lora`` (see ``ltx_pipelines/utils/timing.py``), then
writes:

  timing_report/timing_report.txt   - human-readable breakdown, phases ranked by
                                       time, with the biggest time-eaters called out
  timing_report/phases_cold.png     - horizontal bar chart (first/cold run)
  timing_report/phases_warm.png     - horizontal bar chart (steady-state run)
  timing_report/category_*.png      - category pie charts (cold/warm)

The FIRST run is "cold" (loads ~70 GB of weights from disk; ~15-20 min, GPU idle).
Every run after that is "warm" (weights cached; tens of seconds). So the default
of 2 runs gives you both a cold and a warm profile in one go.

Usage (use the project venv so ltx_core/ltx_pipelines import):
    ./ltx/bin/python timining.py                 # 2 runs (cold + warm)
    ./ltx/bin/python timining.py --runs 3        # cold + 2 warm
    ./ltx/bin/python timining.py --from-log      # skip running; build report
                                                 # from existing timings.jsonl
    ./ltx/bin/python timining.py --image foo.png --video bar.mp4
"""

import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: write PNGs, never open a window
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
OUTDIR = ROOT / "timing_report"
TIMINGS_LOG = ROOT / "timings.jsonl"

# Category + friendly name per phase (kept in sync with gen_timing_doc.py).
PHASE_CATEGORY = {
    "prompt_encode": "Text encode",
    "build_video_encoder": "Model assembly",
    "encode_conditionings_s1": "Conditioning encode",
    "build_transformer_s1": "Model assembly",
    "denoise_stage1": "Diffusion",
    "upsample_to_stage2": "Upsample",
    "build_transformer_s2": "Model assembly",
    "encode_image_cond_s2": "Conditioning encode",
    "denoise_stage2": "Diffusion",
    "decode_video": "VAE decode",
    "decode_audio": "VAE decode",
    "mp4_encode": "Encode mp4",
}


def _ascii_bar(pct: float, width: int = 32) -> str:
    n = int(round(pct / 100 * width))
    return "#" * n + "." * (width - n)


def run_pipeline(image: str, video: str, runs: int) -> list[dict]:
    """Run generate() `runs` times; return the timing records they appended."""
    import pipeline_runtime as pr

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    image = image or str(pr.SAMPLE_IMAGE)
    video = video or str(pr.SAMPLE_VIDEO)
    if not Path(image).exists():
        sys.exit(f"image not found: {image}")
    if not Path(video).exists():
        sys.exit(f"video not found: {video}")

    before = _read_records()
    start_idx = len(before)
    with tempfile.TemporaryDirectory() as td:
        for i in range(runs):
            tag = "cold" if i == 0 and start_idx == 0 else "warm"
            print(f"\n=== run {i + 1}/{runs} (expected: {tag}) — this writes a record to {TIMINGS_LOG.name} ===")
            out = str(Path(td) / f"timining_run{i}.mp4")
            pr.generate(image_path=image, output_path=out, video_path=video)
    after = _read_records()
    return after[start_idx:]


def _read_records() -> list[dict]:
    if not TIMINGS_LOG.exists():
        return []
    recs = []
    for line in TIMINGS_LOG.read_text().splitlines():
        line = line.strip()
        if line:
            recs.append(json.loads(line))
    return recs


def pick_cold_warm(records: list[dict]) -> tuple[dict | None, dict | None]:
    """Return (last cold record, last warm record) from a list of records."""
    cold = next((r for r in reversed(records) if r.get("kind") == "cold"), None)
    warm = next((r for r in reversed(records) if r.get("kind") == "warm"), None)
    return cold, warm


def categories(rec: dict) -> dict[str, float]:
    cats: dict[str, float] = {}
    for p in rec["phases"]:
        c = PHASE_CATEGORY.get(p["name"], "Other")
        cats[c] = cats.get(c, 0.0) + p["secs"]
    return cats


def write_txt(records: list[dict], path: Path) -> None:
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append(" LTX-2 Motion-Transfer — timing profile")
    lines.append("=" * 70)
    if records:
        r0 = records[-1]
        lines.append(f" config: {r0['num_frames']} frames, {r0['height']}x{r0['width']}, fp8-cast, "
                     "text-encoder on CPU")
    lines.append("")

    for rec in records:
        total = rec["total_s"]
        kind = rec["kind"].upper()
        lines.append("-" * 70)
        lines.append(f" {kind} run — total {total:.1f}s "
                     f"({total/60:.1f} min)" if total >= 90 else f" {kind} run — total {total:.1f}s")
        lines.append("-" * 70)

        ranked = sorted(rec["phases"], key=lambda p: p["secs"], reverse=True)
        lines.append(f"   {'phase':<26}{'secs':>9}{'%':>7}   chart")
        for p in ranked:
            pct = 100 * p["secs"] / total if total else 0
            lines.append(f"   {p['name']:<26}{p['secs']:>9.2f}{pct:>6.1f}%  {_ascii_bar(pct)}")

        lines.append("")
        lines.append("   by category:")
        cats = sorted(categories(rec).items(), key=lambda kv: kv[1], reverse=True)
        for c, s in cats:
            pct = 100 * s / total if total else 0
            lines.append(f"     {c:<24}{s:>9.2f}{pct:>6.1f}%  {_ascii_bar(pct)}")

        lines.append("")
        top = ranked[:3]
        lines.append("   >>> biggest time-eaters: "
                     + ", ".join(f"{p['name']} ({p['secs']:.1f}s, {100*p['secs']/total:.0f}%)" for p in top))
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {path}")


def bar_png(rec: dict, path: Path) -> None:
    ranked = sorted(rec["phases"], key=lambda p: p["secs"])
    names = [p["name"] for p in ranked]
    secs = [p["secs"] for p in ranked]
    total = rec["total_s"]

    fig, ax = plt.subplots(figsize=(10, 6))
    colors = plt.cm.viridis([s / max(secs) for s in secs]) if max(secs) else "tab:blue"
    bars = ax.barh(names, secs, color=colors)
    ax.set_xlabel("seconds")
    ax.set_title(f"{rec['kind'].upper()} run — per-phase time (total {total:.1f}s)")
    for b, s in zip(bars, secs):
        pct = 100 * s / total if total else 0
        ax.text(b.get_width(), b.get_y() + b.get_height() / 2,
                f" {s:.1f}s ({pct:.0f}%)", va="center", fontsize=8)
    ax.margins(x=0.18)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"wrote {path}")


def pie_png(rec: dict, path: Path) -> None:
    cats = sorted(categories(rec).items(), key=lambda kv: kv[1], reverse=True)
    labels = [c for c, _ in cats]
    sizes = [s for _, s in cats]
    fig, ax = plt.subplots(figsize=(7, 7))
    ax.pie(sizes, labels=labels, autopct=lambda p: f"{p:.0f}%", startangle=90,
           colors=plt.cm.tab20.colors)
    ax.set_title(f"{rec['kind'].upper()} run — time by category (total {rec['total_s']:.1f}s)")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"wrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Profile the motion-transfer pipeline.")
    ap.add_argument("--runs", type=int, default=2, help="generations to run (run 1 is cold). Default 2.")
    ap.add_argument("--image", default=None, help="subject image (default: bundled avatar_15).")
    ap.add_argument("--video", default=None, help="reference video (default: bundled sample).")
    ap.add_argument("--from-log", action="store_true",
                    help="do NOT run the pipeline; build the report from existing timings.jsonl.")
    ap.add_argument("--outdir", default=str(OUTDIR), help="output directory for report + graphs.")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if args.from_log:
        records = _read_records()
        if not records:
            sys.exit(f"no records in {TIMINGS_LOG} — run without --from-log first.")
        print(f"using {len(records)} existing record(s) from {TIMINGS_LOG}")
    else:
        records = run_pipeline(args.image, args.video, args.runs)
        if not records:
            sys.exit("no timing records were produced.")

    write_txt(records, outdir / "timing_report.txt")

    cold, warm = pick_cold_warm(records)
    if cold:
        bar_png(cold, outdir / "phases_cold.png")
        pie_png(cold, outdir / "category_cold.png")
    if warm:
        bar_png(warm, outdir / "phases_warm.png")
        pie_png(warm, outdir / "category_warm.png")

    print(f"\nDone. Open {outdir}/ for the txt report and PNG graphs.")


if __name__ == "__main__":
    main()
