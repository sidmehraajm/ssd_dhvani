"""Waveform overviews for the Review tab.

A waveform thumbnail is just, for each pixel column, the lowest and highest
sample in that slice of the file. We read the file in blocks (never all at
once -- an hour at 48 kHz is 170 M samples) and keep the result in memory,
keyed by path + size + mtime so a re-recorded file is recomputed.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger("ssd_dhvani.waveform")

_CACHE: dict = {}
_LOCK = threading.Lock()


def _key(path: Path, columns: int):
    st = path.stat()
    return (str(path), st.st_size, int(st.st_mtime), columns)


def peaks(path, columns: int = 600) -> tuple[np.ndarray, np.ndarray] | None:
    """(mins, maxs) arrays of length `columns`, values -1..1. None if the
    file can't be read."""
    path = Path(path)
    try:
        import soundfile as sf   # type: ignore
        key = _key(path, columns)
    except Exception:
        return None
    with _LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit
    try:
        info = sf.info(str(path))
        total = max(1, info.frames)
        mins = np.zeros(columns, dtype=np.float32)
        maxs = np.zeros(columns, dtype=np.float32)
        # Column index of every sample, block by block.
        pos = 0
        for block in sf.blocks(str(path), blocksize=65536, dtype="float32",
                               always_2d=True):
            mono = block.max(axis=1) if block.shape[1] > 1 else block[:, 0]
            lo = block.min(axis=1) if block.shape[1] > 1 else mono
            cols = ((np.arange(pos, pos + len(mono)) * columns) // total
                    ).clip(0, columns - 1)
            np.minimum.at(mins, cols, lo)
            np.maximum.at(maxs, cols, mono)
            pos += len(mono)
        result = (mins, maxs)
    except Exception as exc:
        log.debug("waveform failed for %s: %s", path, exc)
        return None
    with _LOCK:
        _CACHE[key] = result
    return result


def describe(path) -> dict:
    """samplerate / subtype / duration / channels of an audio file."""
    try:
        import soundfile as sf   # type: ignore
        i = sf.info(str(path))
        return {"samplerate": i.samplerate, "subtype": i.subtype,
                "duration": i.duration, "channels": i.channels,
                "format": i.format}
    except Exception:
        return {}
