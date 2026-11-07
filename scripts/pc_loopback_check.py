"""Which playback device really carries sound right now: a test for the operator's computer.

Listens for three seconds to what each playback device is playing (the same way the audio bridge
does with --capture-loopback) and prints the loudest moment of each. Play something audible
(a video, music, a voice) and run it.

After the playback devices it listens to every "stereo mix" recording device (a Windows feature
that records what the speakers play) in its own format, because on some computers that works when
the loopback of the playback device does not.

Needs: pip install soundcard numpy sounddevice.   Run:  python pc_loopback_check.py
"""

import sys
import time
from array import array


def main():
    try:
        import soundcard
    except ImportError:
        sys.exit("pip install soundcard numpy")
    speakers = soundcard.all_speakers()
    default = soundcard.default_speaker()
    print("The default playback device of Windows:", default.name)
    print("Listening to each device for 3 seconds (keep the sound playing)...\n")
    for speaker in speakers:
        try:
            microphone = soundcard.get_microphone(speaker.id, include_loopback=True)
            with microphone.recorder(samplerate=48000, channels=2) as recorder:
                loudest = 0.0
                end = time.monotonic() + 3.0
                while time.monotonic() < end:
                    block = recorder.record(numframes=4800)
                    loudest = max(loudest, float(abs(block).max()))
            mark = "  <- default" if speaker.name == default.name else ""
            verdict = "SOUND" if loudest > 0.01 else "silence"
            print(f"  {verdict:8} peak {loudest:.3f}  {speaker.name}{mark}")
        except Exception as error:
            print(f"  could not listen to {speaker.name}: {type(error).__name__}: {error}")


def stereo_mix():
    try:
        import sounddevice
    except ImportError:
        print("\n(pip install sounddevice: needed to test the stereo mix)")
        return
    print("\nStereo mix devices (recording what the speakers play), 3 seconds each:")
    found = False
    for index, device in enumerate(sounddevice.query_devices()):
        name = device["name"].lower()
        if device["max_input_channels"] > 0 and ("mix" in name or "микшер" in name):
            found = True
            channels = min(2, device["max_input_channels"])
            rate = int(device["default_samplerate"])
            api = sounddevice.query_hostapis(device["hostapi"])["name"]
            try:
                stream = sounddevice.RawInputStream(
                    device=index, channels=channels, samplerate=rate, dtype="int16"
                )
                stream.start()
                data, _ = stream.read(rate * 3)
                stream.stop()
                stream.close()
                values = array("h")
                values.frombytes(bytes(data))
                peak = max((abs(v) for v in values), default=0) / 32768.0
                verdict = "SOUND" if peak > 0.01 else "silence"
                print(
                    f"  {verdict:8} peak {peak:.3f}  [{index}] {device['name']} ({api}, {rate} Hz)"
                )
            except Exception as error:
                print(f"  [{index}] {device['name']} ({api}): {type(error).__name__}: {error}")
    if not found:
        print("  none found")


if __name__ == "__main__":
    main()
    stereo_mix()
