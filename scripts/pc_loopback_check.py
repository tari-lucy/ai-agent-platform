"""Which playback device really carries sound right now: a test for the operator's computer.

Listens for three seconds to what each playback device is playing (the same way the audio bridge
does with --capture-loopback) and prints the loudest moment of each. Play something audible
(a video, music, a voice) and run it.

Needs: pip install soundcard numpy.   Run:  python pc_loopback_check.py
"""

import sys
import time


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


if __name__ == "__main__":
    main()
