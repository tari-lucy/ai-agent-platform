"""Which virtual cable feeds which input: a short test for the operator's computer.

Plays a quiet tone into every playback device that looks like a virtual cable, one after the
other, and listens on every recording device that looks like one; then prints which recording
device heard which playback device. From that you can tell the cables apart (the names alone do
not: two cables may both call their recording side "CABLE Output"), and pick one cable for the
persona's voice and another for the voices of the people she listens to.

Needs: pip install sounddevice.   Run:  python pc_cable_probe.py
It uses only the MME devices, plays for about half a second per device, and writes nothing.
"""

import math
import sys
import threading
import time
from array import array

WORDS = ("cable", "vb-audio", "voicemeeter", "virtual")
TONE_HZ = 440
SECONDS = 0.7
LEVEL = 0.2  # of full scale: quiet


def looks_virtual(name):
    return any(word in name.lower() for word in WORDS)


def main():
    try:
        import sounddevice
    except ImportError:
        sys.exit("pip install sounddevice")

    mme = next(
        (i for i, api in enumerate(sounddevice.query_hostapis()) if api["name"] == "MME"), None
    )
    if mme is None:
        sys.exit("No MME audio host found.")
    devices = [
        (index, device)
        for index, device in enumerate(sounddevice.query_devices())
        if device["hostapi"] == mme and looks_virtual(device["name"])
    ]
    outputs = [(i, d) for i, d in devices if d["max_output_channels"] > 0]
    inputs = [(i, d) for i, d in devices if d["max_input_channels"] > 0]
    print("Playback devices that look like cables:")
    for index, device in outputs:
        print(f"  [{index}] {device['name']}")
    print("Recording devices that look like cables:")
    for index, device in inputs:
        print(f"  [{index}] {device['name']}")
    if not outputs or not inputs:
        sys.exit("Nothing to test: no cable playback or recording devices found.")

    print("\nTesting (a quiet tone in each playback device, about half a second each)...")
    results = {}
    for out_index, out_device in outputs:
        peaks = {}

        def listen(in_index, in_device, peaks=peaks):
            channels = min(2, in_device["max_input_channels"])
            rate = int(in_device["default_samplerate"])
            try:
                stream = sounddevice.RawInputStream(
                    device=in_index, channels=channels, samplerate=rate, dtype="int16"
                )
                stream.start()
                data, _ = stream.read(int(rate * (SECONDS + 0.6)))
                stream.stop()
                stream.close()
                samples = array("h")
                samples.frombytes(bytes(data))
                peaks[in_index] = max((abs(v) for v in samples), default=0) / 32768.0
            except Exception:  # a device that will not open is simply not heard
                peaks[in_index] = -1.0

        threads = [threading.Thread(target=listen, args=item) for item in inputs]
        for thread in threads:
            thread.start()
        time.sleep(0.4)
        rate = int(out_device["default_samplerate"])
        channels = min(2, out_device["max_output_channels"])
        try:
            count = int(rate * SECONDS)
            tone = array(
                "h",
                (
                    int(32767 * LEVEL * math.sin(2 * math.pi * TONE_HZ * n / rate))
                    for n in range(count)
                ),
            )
            frames = array("h")
            for value in tone:
                frames.extend([value] * channels)
            stream = sounddevice.RawOutputStream(
                device=out_index, samplerate=rate, channels=channels, dtype="int16"
            )
            stream.start()
            stream.write(frames.tobytes())
            stream.stop()
            stream.close()
        except Exception as error:
            print(f"  [{out_index}] could not play: {type(error).__name__}")
        for thread in threads:
            thread.join()
        results[out_index] = peaks

    print("\nResult: which recording device heard which playback device")
    for out_index, out_device in outputs:
        heard = [
            f"[{i}] {d['name']}" for i, d in inputs if results.get(out_index, {}).get(i, 0.0) > 0.02
        ]
        line = ", ".join(heard) if heard else "nobody (or it is silent)"
        print(f"  play into [{out_index}] {out_device['name']}  ->  heard on: {line}")


if __name__ == "__main__":
    main()
