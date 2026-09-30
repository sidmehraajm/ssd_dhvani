"""Auto-sync: line the clean mic recording up with a facial video.

The phone / head-cam and the wireless mic start recording at different
moments, but they hear the SAME performance. So we slide one recording along
the other and find the shift where they match best -- cross-correlation, the
same idea as DaVinci's "auto sync audio" or PluralEyes.

Done with an FFT, which tests every possible shift at once: minutes of audio
take well under a second. Both signals are compared at 8 kHz -- that still
resolves the offset to 0.125 ms (a video frame at 24 fps is 41.7 ms).

Result: `offset` = how many seconds AFTER the video's start the mic's first
sample belongs. Positive -> the mic started later (pad it); negative -> the
mic started earlier (trim its head). `confidence` = how far the best match
beats the runner-up; below MIN_CONFIDENCE we don't trust it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from . import media

log = logging.getLogger("ssd_dhvani.sync")

RATE = 8000
#: The best match must beat the second-best by this factor to be trusted.
MIN_CONFIDENCE = 2.5   # unrelated audio scores <= 1.5, real matches 4+


@dataclass
class SyncResult:
    offset: float = 0.0          # seconds; mic start relative to video start
    confidence: float = 0.0
    ok: bool = False
    reason: str = ""


def _prep(x: np.ndarray) -> np.ndarray:
    """Remove DC, and 'whiten' loudness a little so a hot mic and a quiet
    phone mic compare fairly (we care about WHEN things happen)."""
    x = x.astype(np.float64)
    x -= x.mean()
    s = x.std()
    return x / s if s > 0 else x


def correlate(video_audio: np.ndarray, mic_audio: np.ndarray,
              rate: int = RATE) -> SyncResult:
    """Offset of `mic_audio` relative to `video_audio` (both mono, `rate`)."""
    if video_audio is None or mic_audio is None:
        return SyncResult(reason="missing audio")
    if len(video_audio) < rate or len(mic_audio) < rate:
        return SyncResult(reason="less than 1 s of audio")
    a, b = _prep(video_audio), _prep(mic_audio)
    if not a.any() or not b.any():
        return SyncResult(reason="one of the recordings is silent")
    n = len(a) + len(b) - 1
    size = 1 << (n - 1).bit_length()
    # corr[k] = sum a[i + k] * b[i]  (b slid along a)
    corr = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)),
                        size)
    # lags 0..len(a)-1 then negative lags -(len(b)-1)..-1
    corr = np.concatenate((corr[size - (len(b) - 1):], corr[:len(a)]))
    lags = np.arange(-(len(b) - 1), len(a))
    mag = np.abs(corr)
    best = int(np.argmax(mag))
    peak = mag[best]
    # Confidence = how clearly the best match beats the SECOND-best match
    # (anything more than ±100 ms away). Repetitive sound (a hum, a beat,
    # a steady tone) matches itself at many shifts -> ratio near 1 -> not
    # trusted. A real performance has one clear answer -> ratio well above.
    guard = int(0.1 * rate)
    rest = np.concatenate((mag[:max(0, best - guard)], mag[best + guard:]))
    second = float(rest.max()) if len(rest) else 0.0
    conf = float(peak / second) if second > 0 else 99.0
    # sub-sample refinement (parabola through the peak and its neighbours)
    frac = 0.0
    if 0 < best < len(mag) - 1:
        y0, y1, y2 = mag[best - 1], mag[best], mag[best + 1]
        d = y0 - 2 * y1 + y2
        if d != 0:
            frac = 0.5 * (y0 - y2) / d
    offset = (lags[best] + frac) / float(rate)
    ok = conf >= MIN_CONFIDENCE
    return SyncResult(offset=float(offset), confidence=round(conf, 1), ok=ok,
                      reason="" if ok else
                      f"no clear match (confidence {conf:.1f} < {MIN_CONFIDENCE})")


def sync_video_to_mic(video, mic_wav) -> SyncResult:
    """Decode both with ffmpeg at 8 kHz mono and correlate them."""
    va = media.extract_audio(video, RATE)
    if va is None or not len(va):
        return SyncResult(reason="the video has no audio to sync with")
    ma = media.extract_audio(mic_wav, RATE)
    if ma is None or not len(ma):
        return SyncResult(reason="could not read the mic recording")
    res = correlate(va, ma, RATE)
    log.info("sync %s <-> %s: offset %.4f s, confidence %.1f",
             video, mic_wav, res.offset, res.confidence)
    return res
