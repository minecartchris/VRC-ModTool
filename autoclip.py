"""Medal auto-clipper.

Listens to your speaker output (what you hear from VRChat / Discord — i.e. other
users' voices), transcribes it locally with Vosk, and presses your Medal clip
hotkey whenever a trigger word/phrase is detected. Medal then saves the last N
seconds, so the moment that triggered it is inside the clip.

Nothing is sent to the internet — speech recognition runs fully offline.

Usage:
    python autoclip.py                     # listen on default speakers
    python autoclip.py --list-devices      # show capturable outputs
    python autoclip.py --device "Steam"    # capture a specific output by name
    python autoclip.py --model small       # use the small/fast model instead

For the graphical interface, run gui.py instead.
"""

import argparse
import ctypes
import json
import re
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio
from scipy.signal import resample_poly
from vosk import Model, KaldiRecognizer, SetLogLevel

# ---------------------------- CONFIG ----------------------------------------
# Trigger words/phrases (lowercase, no punctuation). Multi-word phrases work.
# More triggers can be added to triggers.txt (one per line) next to this file.
TRIGGER_WORDS = [
    "clip that",
    "clipped that",
    "clip it",
    "clip is",      # common mishearing of "clip it"
    "no way",
    # "oh my god" removed from defaults: fired 10x in 45 min of casual chat
]

TRIGGER_FILE_NAME = "triggers.txt"

MEDAL_HOTKEY = "f8"        # must match the clip hotkey set in Medal's settings
COOLDOWN_SECONDS = 15      # ignore triggers for this long after firing once
CHUNK = 4096               # audio frames per read (at device rate)

# Vosk models are trained on 16 kHz audio; we resample the capture to match.
ASR_RATE = 16000
# Check the top-N decodings for triggers, not just the single best guess —
# with overlapping voices the right words are often in hypothesis 2 or 3.
N_ALTERNATIVES = 3

# Gentle automatic gain control: keeps quieter voices from being lost under
# loud ones without amplifying silence into noise.
AGC_TARGET_RMS = 2500.0    # int16 scale
AGC_MIN_GAIN = 0.5
AGC_MAX_GAIN = 8.0
AGC_SILENCE_RMS = 50.0     # below this, leave the gain alone
# ----------------------------------------------------------------------------

MODELS = {
    "small": "vosk-model-small-en-us-0.15",   # 40 MB, fast, less accurate
    "large": "vosk-model-en-us-0.22",         # 1.8 GB, much better in noise
}
MODEL_BASE_URL = "https://alphacephei.com/vosk/models/"
from paths import HERE  # noqa: E402  (single source of truth for the tree root)

# Windows virtual-key codes for hotkey presses (no `keyboard` library — its
# global hook thread can deadlock imports under pythonw).
_VK_MAP = {
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
    **{chr(c): ord(chr(c)) - 32 for c in range(ord("a"), ord("z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12,
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "backspace": 0x08,
    "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E,
    "pageup": 0x21, "pagedown": 0x22, "pause": 0x13, "scrolllock": 0x91,
}
_KEYEVENTF_KEYUP = 0x0002


def parse_hotkey(hotkey: str) -> list[int]:
    """'f8' or 'ctrl+f9' -> list of virtual-key codes. Raises on unknown keys."""
    vks = []
    for part in hotkey.lower().split("+"):
        part = part.strip()
        if part not in _VK_MAP:
            raise RuntimeError(f"Unsupported hotkey: '{part}'")
        vks.append(_VK_MAP[part])
    return vks


def press_hotkey(hotkey: str) -> None:
    """Press a key (or modifier+key combo) system-wide."""
    vks = parse_hotkey(hotkey)
    user32 = ctypes.windll.user32
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def model_downloaded(size: str) -> bool:
    return (HERE / MODELS[size]).exists()


def ensure_model(size: str, emit=None) -> Path:
    status = emit or (lambda kind, payload: log(payload))
    name = MODELS[size]
    model_dir = HERE / name
    if model_dir.exists():
        return model_dir
    zip_path = model_dir.with_suffix(".zip")
    status("status", f"Downloading speech model: {MODEL_BASE_URL}{name}.zip")
    urllib.request.urlretrieve(f"{MODEL_BASE_URL}{name}.zip", zip_path)
    status("status", "Extracting model...")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(HERE)
    zip_path.unlink()
    status("status", "Model ready.")
    return model_dir


def loopback_devices(pa: pyaudio.PyAudio) -> list[dict]:
    return list(pa.get_loopback_device_info_generator())


def loopback_device_names() -> list[str]:
    pa = pyaudio.PyAudio()
    try:
        return [d["name"].replace(" [Loopback]", "") for d in loopback_devices(pa)]
    finally:
        pa.terminate()


def list_devices(pa: pyaudio.PyAudio) -> None:
    print("Capturable outputs (loopback devices):")
    for dev in loopback_devices(pa):
        print(f"  [{dev['index']}] {dev['name']}  "
              f"({int(dev['defaultSampleRate'])} Hz, {dev['maxInputChannels']} ch)")


def find_loopback(pa: pyaudio.PyAudio, name_filter: str | None) -> dict:
    if name_filter:
        for dev in loopback_devices(pa):
            if name_filter.lower() in dev["name"].lower():
                return dev
        raise RuntimeError(f"No loopback device matching '{name_filter}'.")
    try:
        return pa.get_default_wasapi_loopback()
    except OSError:
        raise RuntimeError("Couldn't find the default speaker loopback.")


def load_triggers() -> list[str]:
    """Built-in triggers plus one-per-line entries from triggers.txt."""
    words = [w.lower() for w in TRIGGER_WORDS]
    trigger_file = HERE / TRIGGER_FILE_NAME
    if trigger_file.exists():
        for line in trigger_file.read_text(encoding="utf-8").splitlines():
            line = line.strip().lower()
            if line and not line.startswith("#"):
                words.append(line)
    return words


def compile_triggers(words: list[str]) -> list[tuple[str, re.Pattern]]:
    # \b word boundaries so e.g. a trigger never matches inside a longer
    # innocent word ("raccoon", "suspicious")
    return [(w, re.compile(r"\b" + re.escape(w) + r"\b")) for w in words]


def contains_trigger(texts: list[str],
                     patterns: list[tuple[str, re.Pattern]]) -> str | None:
    for text in texts:
        for phrase, pat in patterns:
            if pat.search(text):
                return phrase
    return None


def decoded_texts(raw: str) -> list[str]:
    """Extract all hypothesis texts from a Vosk result/partial JSON string."""
    j = json.loads(raw)
    if "alternatives" in j:
        return [a.get("text", "") for a in j["alternatives"]]
    for key in ("partial", "text"):
        if key in j:
            return [j[key]]
    return []


def run_engine(*, device_filter=None, model_size: str = "large",
               hotkey: str = MEDAL_HOTKEY, cooldown: float = COOLDOWN_SECONDS,
               on_event=None, stop_event=None) -> None:
    """Capture -> transcribe -> trigger loop.

    device_filter may be None (default speakers), a name substring, or a list
    of name substrings — each matching device gets its own capture stream and
    recognizer, so e.g. Discord voices never smear over game audio.

    on_event(kind, payload) receives:
        "status"  str   informational messages
        "heard"   str   a finalized transcript line ("[source] text" if multi)
        "trigger" str   the trigger phrase that fired (hotkey already pressed)
        "level"   float smoothed input level 0..1 (frequent; for meters)
    Runs until stop_event (threading.Event) is set.
    """
    emit = on_event or (lambda kind, payload: None)
    parse_hotkey(hotkey)  # fail fast on an invalid hotkey
    filters = device_filter if isinstance(device_filter, (list, tuple)) \
        else [device_filter]
    pa = pyaudio.PyAudio()
    sources: list[dict] = []
    try:
        model_dir = ensure_model(model_size, emit)
        SetLogLevel(-1)  # silence Vosk's internal logging
        emit("status", f"Loading model '{model_dir.name}'...")
        model = Model(str(model_dir))

        triggers = load_triggers()
        patterns = compile_triggers(triggers)

        for filt in filters:
            dev = find_loopback(pa, filt)
            rate = int(dev["defaultSampleRate"])
            channels = max(1, dev["maxInputChannels"])
            rec = KaldiRecognizer(model, ASR_RATE)
            rec.SetMaxAlternatives(N_ALTERNATIVES)
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=dev["index"],
                frames_per_buffer=CHUNK,
            )
            label = dev["name"].replace(" [Loopback]", "").split(" (")[0]
            sources.append(dict(rate=rate, channels=channels, rec=rec,
                                stream=stream, gain=1.0, label=label))
            emit("status", f"Listening on: {dev['name']}")

        multi = len(sources) > 1
        emit("status", f"Trigger words: {len(triggers)} loaded "
             f"({len(triggers) - len(TRIGGER_WORDS)} from {TRIGGER_FILE_NAME})")
        emit("status", f"On trigger: press '{hotkey.upper()}' "
             f"(cooldown {cooldown:g}s).")

        last_fire = 0.0
        while not (stop_event is not None and stop_event.is_set()):
            level = 0.0
            for src in sources:
                data = src["stream"].read(CHUNK, exception_on_overflow=False)
                audio = np.frombuffer(data, dtype=np.int16)
                if src["channels"] > 1:
                    audio = audio.reshape(-1, src["channels"]).mean(axis=1)
                audio = audio.astype(np.float32)
                if src["rate"] != ASR_RATE:
                    audio = resample_poly(audio, ASR_RATE, src["rate"])

                rms = float(np.sqrt(np.mean(audio ** 2)))
                if rms > AGC_SILENCE_RMS:
                    desired = float(np.clip(AGC_TARGET_RMS / rms,
                                            AGC_MIN_GAIN, AGC_MAX_GAIN))
                    # smooth to avoid pumping
                    src["gain"] = 0.8 * src["gain"] + 0.2 * desired
                pcm = np.clip(audio * src["gain"], -32768, 32767).astype(np.int16)
                level = max(level, min(1.0, rms / 6000.0))

                rec = src["rec"]
                if rec.AcceptWaveform(pcm.tobytes()):
                    texts = decoded_texts(rec.Result())
                    if texts and texts[0]:
                        prefix = f"[{src['label']}] " if multi else ""
                        emit("heard", prefix + texts[0])
                    hit = contains_trigger(texts, patterns)
                else:
                    # partial results let us fire mid-sentence with low
                    # latency, but single-word triggers only fire from
                    # finalized decodes — partial hypotheses flicker and
                    # cause false positives
                    texts = decoded_texts(rec.PartialResult())
                    hit = contains_trigger(texts, patterns)
                    if hit and " " not in hit:
                        hit = None

                if hit and time.time() - last_fire > cooldown:
                    last_fire = time.time()
                    press_hotkey(hotkey)
                    emit("trigger", hit)
                    # clear the partial so it doesn't refire after cooldown
                    rec.Reset()
            emit("level", level)
    finally:
        for src in sources:
            src["stream"].stop_stream()
            src["stream"].close()
        pa.terminate()


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-clip Medal on trigger words")
    parser.add_argument("--list-devices", action="store_true",
                        help="list capturable output devices and exit")
    parser.add_argument("--device", metavar="NAME", action="append",
                        help="capture the output whose name contains NAME "
                             "(repeatable for multiple devices)")
    parser.add_argument("--model", choices=list(MODELS), default="large",
                        help="speech model size (default: large)")
    args = parser.parse_args()

    if args.list_devices:
        pa = pyaudio.PyAudio()
        try:
            list_devices(pa)
        finally:
            pa.terminate()
        return

    def on_event(kind: str, payload) -> None:
        if kind == "heard":
            log(f"heard: {payload}")
        elif kind == "trigger":
            log(f">>> TRIGGERED by '{payload}' -> pressed {MEDAL_HOTKEY.upper()}, "
                "clip saved")
        elif kind == "status":
            log(payload)

    try:
        run_engine(device_filter=args.device, model_size=args.model,
                   on_event=on_event)
    except KeyboardInterrupt:
        log("Stopped.")
    except RuntimeError as e:
        sys.exit(f"{e} Run with --list-devices to see options.")


if __name__ == "__main__":
    main()
