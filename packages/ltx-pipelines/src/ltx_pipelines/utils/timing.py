# Added by Flam, 2026-06-02 (LTX-2 Community License §3(c)):
# Lightweight, thread-local phase profiler used to answer "where does the time go".
# Zero behaviour change to the pipeline: tick() only reads the clock (and syncs CUDA
# so GPU work in the just-finished interval is attributed correctly).
"""Thread-local phase timer.

Usage (single-line inserts, no re-indentation of existing code):

    from ltx_pipelines.utils import timing as T
    T.begin()                 # reset, start the clock
    T.tick("prompt_encode")   # names the work that follows, until the next tick
    ...
    T.tick("denoise_stage1")
    ...
    T.end()                   # flush the final interval
    spans = T.report()        # [(label, seconds), ...] in execution order

Each tick() closes the previously-named interval and opens a new one. The label
passed to tick() describes the code that runs *after* it, up to the next tick.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

_local = threading.local()


def _state() -> threading.local:
    if not hasattr(_local, "spans"):
        _local.spans = []
        _local.last = None
        _local.label = None
    return _local


def _sync() -> None:
    # Make sure queued GPU work in the interval we are about to close has finished,
    # otherwise async CUDA kernels would leak their cost into the *next* interval.
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.synchronize()
    except Exception:  # pragma: no cover - profiling must never break a run
        pass


def begin() -> None:
    """Reset the timeline and start the clock."""
    s = _state()
    s.spans = []
    s.last = time.perf_counter()
    s.label = None


def tick(label: str | None) -> None:
    """Close the current interval (if any) and start a new one named ``label``."""
    s = _state()
    _sync()
    now = time.perf_counter()
    if s.label is not None and s.last is not None:
        dt = now - s.last
        s.spans.append((s.label, dt))
        logger.info("[timing] %-26s %8.2fs", s.label, dt)
    s.last = time.perf_counter()
    s.label = label


def end() -> None:
    """Flush the final interval."""
    tick(None)


def report() -> list[tuple[str, float]]:
    """Return the recorded spans as ``[(label, seconds), ...]``."""
    return list(_state().spans)
