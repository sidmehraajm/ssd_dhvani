"""10-second test take. Shows the peak level of each channel."""
import sys
import time
import queue

import numpy as np
import sounddevice as sd
import soundfile as sf

dev = int(sys.argv[1])
ch = int(sys.argv[2])
secs = int(sys.argv[3]) if len(sys.argv) > 3 else 10
RATE = 48000

q = queue.Queue()


def callback(indata, frames, time_info, status):
    if status:
        print("status:", status)        # dropouts/overflows show up here
    q.put(indata.copy())                # hand the block off, don't write here


peaks = np.zeros(ch)
name = f"test_dev{dev}_{ch}ch.wav"
with sf.SoundFile(name, "w", samplerate=RATE, channels=ch,
                  subtype="PCM_24") as f, \
     sd.InputStream(device=dev, channels=ch, samplerate=RATE,
                    dtype="int32", callback=callback):
    print("Recording... speak now")
    end = time.time() + secs
    while time.time() < end:
        try:
            block = q.get(timeout=0.5)
        except queue.Empty:
            continue
        f.write(block)
        peaks = np.maximum(peaks, np.abs(block).max(axis=0))

for i, p in enumerate(peaks):
    db = 20 * np.log10(p / 2**31) if p else float("-inf")
    print(f"channel {i + 1}: peak {db:.1f} dBFS")
print("saved", name)