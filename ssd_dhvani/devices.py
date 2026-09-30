"""Audio input devices -- what exists, and what each one really supports.

Why this module exists
----------------------
PortAudio (the C library under `sounddevice`, and under Audacity) numbers
devices 0, 1, 2 ... but those numbers are NOT stable: they change between
Python installs, after re-plugging a USB receiver, or when Windows adds a
virtual device. So the app never stores an index. It stores the device NAME
plus its HOST API ("Windows WASAPI", "Core Audio", ...) and looks the index
up again every time with `find_device()`.

Host APIs in one line each
--------------------------
Windows (the same mic shows up once per API):
  * WASAPI      -- modern Windows audio. The default here. Shared mode goes
                   through the Windows mixer at the device's own rate.
  * WDM-KS      -- kernel streaming, talks to the driver directly (no mixer).
  * DirectSound / MME -- legacy; Windows silently resamples, avoid.
  * ASIO        -- pro interfaces only, if a driver is installed.
macOS:
  * Core Audio  -- the only one, and already direct/low-latency.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

log = logging.getLogger("ssd_dhvani.devices")

try:
    import sounddevice as sd   # type: ignore
    _HAVE_SD = True
    _SD_ERR = ""
except Exception as _e:        # pragma: no cover - missing PortAudio
    sd = None                  # type: ignore
    _HAVE_SD = False
    _SD_ERR = f"{type(_e).__name__}: {_e}"

#: Sample rates offered in the UI (Hz). 48 kHz is the film/video standard.
RATES = (44100, 48000, 88200, 96000, 192000)

#: Bit depth choices -> libsndfile "subtype" names used by soundfile.
SAMPLE_FORMATS = {
    "PCM_16": "16-bit",
    "PCM_24": "24-bit",
    "FLOAT": "32-bit float",
}

#: Container choices. RF64 = WAV without the 4 GB limit.
FILE_FORMATS = {
    "WAV": ".wav",
    "FLAC": ".flac",
    "RF64": ".wav",
}

#: Order of preference when nothing is configured.
_PREFERRED_API = {
    "win32": ("Windows WASAPI", "Windows WDM-KS", "Windows DirectSound", "MME"),
    "darwin": ("Core Audio",),
}
#: Legacy APIs that resample behind your back -- listed last, flagged in UI.
LEGACY_APIS = ("MME", "Windows DirectSound")


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    hostapi: str
    channels: int
    default_rate: float

    @property
    def label(self) -> str:
        return f"{self.name}  ({self.hostapi}, {self.channels} ch)"


def available() -> bool:
    return _HAVE_SD


def why_unavailable() -> str:
    return _SD_ERR or "sounddevice not installed (pip install sounddevice)"


def rescan() -> None:
    """Make PortAudio re-enumerate devices (after a USB receiver is plugged
    in). Only safe while NO stream is open."""
    if not _HAVE_SD:
        return
    try:
        sd._terminate()
        sd._initialize()
    except Exception as exc:    # pragma: no cover
        log.warning("device rescan failed: %s", exc)


def host_apis() -> list[str]:
    """Host API names that have at least one input device."""
    if not _HAVE_SD:
        return []
    names = []
    for api in sd.query_hostapis():
        has_in = any(sd.query_devices(i)["max_input_channels"] > 0
                     for i in api["devices"])
        if has_in:
            names.append(api["name"])
    return names


def default_host_api() -> str:
    apis = host_apis()
    for pref in _PREFERRED_API.get(sys.platform, ()):
        if pref in apis:
            return pref
    return apis[0] if apis else ""


def input_devices(hostapi: str = "") -> list[InputDevice]:
    """Every input device, optionally only those of one host API."""
    if not _HAVE_SD:
        return []
    out: list[InputDevice] = []
    try:
        for i, d in enumerate(sd.query_devices()):
            if int(d["max_input_channels"]) <= 0:
                continue
            api = sd.query_hostapis(d["hostapi"])["name"]
            if hostapi and api != hostapi:
                continue
            out.append(InputDevice(i, str(d["name"]), api,
                                   int(d["max_input_channels"]),
                                   float(d["default_samplerate"])))
    except Exception as exc:     # pragma: no cover
        log.warning("could not list audio devices: %s", exc)
    return out


def find_device(name: str, hostapi: str) -> InputDevice | None:
    """Look the device up by NAME + host API (never by a stored index)."""
    for dev in input_devices(hostapi):
        if dev.name == name:
            return dev
    return None


def default_device(hostapi: str) -> InputDevice | None:
    """The system default input if it belongs to `hostapi`, else the
    first input of that API."""
    devs = input_devices(hostapi)
    if not devs:
        return None
    try:
        default_idx = sd.query_hostapis(
            sd.query_devices(devs[0].index)["hostapi"])["default_input_device"]
        for d in devs:
            if d.index == default_idx:
                return d
    except Exception:
        pass
    return devs[0]


def supports_exclusive(hostapi: str) -> bool:
    """Only Windows WASAPI has an exclusive mode."""
    return sys.platform == "win32" and "WASAPI" in (hostapi or "")


def _extra(exclusive: bool, hostapi: str):
    """Platform-specific stream settings.

    * Windows WASAPI: 'exclusive' bypasses the Windows mixer.
    * macOS Core Audio: switch the DEVICE to the requested rate instead of
      resampling, and if a conversion is unavoidable use the best converter.
    """
    if exclusive and supports_exclusive(hostapi):
        return sd.WasapiSettings(exclusive=True)
    if hostapi == "Core Audio" and hasattr(sd, "CoreAudioSettings"):
        try:
            return sd.CoreAudioSettings(change_device_parameters=True,
                                        conversion_quality="max")
        except Exception:
            return None
    return None


def check(dev: InputDevice, rate: int, dtype: str = "int32",
          channels: int | None = None, exclusive: bool = False) -> bool:
    """Would PortAudio accept these settings? Opens nothing."""
    if not _HAVE_SD:
        return False
    try:
        sd.check_input_settings(device=dev.index,
                                channels=channels or dev.channels,
                                dtype=dtype, samplerate=rate,
                                extra_settings=_extra(exclusive, dev.hostapi))
        return True
    except Exception:
        return False


def supported_rates(dev: InputDevice, exclusive: bool = False) -> list[int]:
    """Rates the device accepts. On WASAPI shared / WDM-KS that is usually
    ONE rate -- the hardware's real one. Legacy APIs 'accept' everything
    because Windows resamples, which adds nothing but processing."""
    return [r for r in RATES if check(dev, r, exclusive=exclusive)]


def best_rate(dev: InputDevice, wanted: int, exclusive: bool = False) -> int:
    """`wanted` if supported, else the device's default, else 48000."""
    rates = supported_rates(dev, exclusive)
    if wanted in rates:
        return wanted
    native = int(dev.default_rate or 0)
    if native in rates:
        return native
    return rates[0] if rates else (native or 48000)


if __name__ == "__main__":    # python -m ssd_dhvani.devices -> probe report
    for api in host_apis():
        print(f"\n== {api}")
        for d in input_devices(api):
            print(f"  [{d.index}] {d.name}: {d.channels} ch, "
                  f"default {d.default_rate:.0f} Hz, "
                  f"accepts {supported_rates(d)}")
