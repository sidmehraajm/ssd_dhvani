"""List input devices and what each one actually supports."""
import sys
import sounddevice as sd

RATES = (44100, 48000, 88200, 96000, 192000)
DTYPES = ("int16", "int32", "float32")


def supported(dev, rate, dtype, exclusive=False):
    """Ask PortAudio: would this device accept these settings?"""
    extra = None
    if exclusive and sys.platform == "win32":
        extra = sd.WasapiSettings(exclusive=True)   # skip the Windows mixer
    try:
        sd.check_input_settings(device=dev, channels=1, dtype=dtype,
                                samplerate=rate, extra_settings=extra)
        return True
    except Exception:
        return False


def inputs():
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            yield i, d["name"], sd.query_hostapis(d["hostapi"])["name"], d


if __name__ == "__main__":
    for i, name, api, d in inputs():
        print(f"\n[{i}] {name} ({api}) max channels: {d['max_input_channels']}")
        modes = [False]
        if sys.platform == "win32" and "WASAPI" in api:
            modes.append(True)                      # also test exclusive mode
        for excl in modes:
            tag = "exclusive" if excl else "shared   "
            rates = [r for r in RATES if supported(i, r, "int32", excl)]
            fmts = [t for t in DTYPES if supported(i, 48000, t, excl)]
            print(f"   {tag} rates: {rates}  formats@48k: {fmts}")