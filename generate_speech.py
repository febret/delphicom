#!/usr/bin/env python3
"""Generate speech / spoken dialogue from text (TTS) on the remote ComfyUI.

Two prosody-capable Chatterbox (Resemble AI) models are wired and selectable
with --model:

    chatterbox         expressive TTS with an `exaggeration` prosody control
    chatterbox-turbo   faster GPT2-based model; inline emotion tags in the text
                       ([laugh] [sigh] [gasp] [chuckle] [cough] [sniff] [groan]
                       [clear throat])

Speech is exported locally as:
    <outdir>/<name>.flac     the generated speech
    <outdir>/<name>.wav      (optional) PCM WAV, if --wav

Prosody tips:
  - `--exaggeration` (0.25-2.0) is the main emotion/prosody dial: 0.5 is neutral,
    higher is more expressive. `--cfg-weight` (0.2-1.0) controls pace/guidance.
  - Punctuation and wording shape delivery: "Watch out! Behind you!" reads
    differently from "The gate is locked."
  - For chatterbox-turbo, insert tags like "[laugh]" or "[sigh]" inline.
  - Optional voice cloning: `--voice reference.wav` (5-10 s clean speech).

Examples:
    python generate_speech.py "The gate is locked. Find another way around."
    python generate_speech.py "Watch out! Behind you!" --exaggeration 1.0 --name warn
    python generate_speech.py "You'll never catch me now. [laugh]" --model chatterbox-turbo
    python generate_speech.py "Welcome, traveller." --voice narrator.wav
"""
import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
_REMOTE_HOST = os.environ.get("COMFY_REMOTE_HOST", "delphi")
_REMOTE_PORT = os.environ.get("COMFY_REMOTE_PORT", "8188")
DEFAULT_HOST = os.environ.get("COMFY_HOST", f"http://{_REMOTE_HOST}:{_REMOTE_PORT}")

# Per-model workflow + node ids + the generation params each node accepts.
# The node ids must stay in sync with the matching workflows/<file>.
MODELS = {
    "chatterbox": {
        "desc": "Chatterbox - expressive TTS, exaggeration/prosody control",
        "workflow": "chatterbox_speech_api.json",
        "tts": "4", "save": "5",
        "params": ("exaggeration", "cfg_weight", "temperature"),
        "tags": False,
    },
    "chatterbox-turbo": {
        "desc": "Chatterbox Turbo - faster, inline emotion tags in the text",
        "workflow": "chatterbox_speech_turbo_api.json",
        "tts": "4", "save": "5",
        "params": ("temperature", "top_k", "top_p", "repetition_penalty"),
        "tags": True,
    },
}


def _post(base, api):
    body = json.dumps({"prompt": api, "client_id": str(random.random())}).encode()
    req = urllib.request.Request(base + "/prompt", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["prompt_id"]


def _wait(base, pid, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(3)
        try:
            with urllib.request.urlopen(f"{base}/history/{pid}", timeout=30) as r:
                h = json.load(r)
        except Exception:
            continue
        if pid in h:
            return h[pid]
    return None


def _upload(base, path):
    """Upload a reference audio file into ComfyUI's input dir; return its name."""
    filename = os.path.basename(path)
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        data = f.read()
    body = b"".join([
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
        f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(),
        data, b"\r\n",
        f'--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\ninput\r\n'.encode(),
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        base + "/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)["name"]


def _download(base, filename, dst, subfolder=""):
    q = {"filename": filename, "type": "output"}
    if subfolder:
        q["subfolder"] = subfolder
    url = base + "/view?" + urllib.parse.urlencode(q)
    with urllib.request.urlopen(url, timeout=300) as r, open(dst, "wb") as f:
        f.write(r.read())


def _to_wav(src, dst):
    try:
        import soundfile as sf
        data, rate = sf.read(src)
        sf.write(dst, data, rate, subtype="PCM_16")
        return dst
    except Exception:
        pass
    import shutil
    import subprocess
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", src, dst],
                           check=True)
            return dst
        except Exception:
            return None
    return None


def _parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _apply_overrides(api, overrides):
    for item in overrides:
        key, _, value = item.partition("=")
        node, _, field = key.partition(".")
        if not (node and field and value != ""):
            raise ValueError(f"--set expects NODE.FIELD=VALUE, got {item!r}")
        if node not in api:
            raise ValueError(f"--set: no node {node!r} in this workflow")
        api[node]["inputs"][field] = _parse_value(value)


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (s[:40] or "speech")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="?", help="text to speak")
    ap.add_argument("--model", default="chatterbox", choices=sorted(MODELS),
                    help="TTS model to use (default: chatterbox)")
    ap.add_argument("--list-models", action="store_true",
                    help="list available models and exit")
    ap.add_argument("--name", help="output name (default: slug of text + timestamp)")
    ap.add_argument("--outdir", default="speech", help="local output directory")
    ap.add_argument("--host", default=DEFAULT_HOST, help="ComfyUI base URL")
    ap.add_argument("--workflow", help="override the API workflow JSON")
    ap.add_argument("--seed", type=int, help="fixed seed (default: random)")
    ap.add_argument("--voice", metavar="FILE",
                    help="reference audio for voice cloning (5-10 s clean speech)")
    ap.add_argument("--exaggeration", type=float,
                    help="prosody/emotion intensity 0.25-2.0 (chatterbox)")
    ap.add_argument("--cfg-weight", type=float, help="pace/guidance 0.2-1.0 (chatterbox)")
    ap.add_argument("--temperature", type=float, help="sampling randomness")
    ap.add_argument("--top-k", type=int, help="top-k sampling (chatterbox-turbo)")
    ap.add_argument("--top-p", type=float, help="nucleus sampling (chatterbox-turbo)")
    ap.add_argument("--repetition-penalty", type=float,
                    help="repetition penalty (chatterbox-turbo)")
    ap.add_argument("--set", dest="overrides", action="append", metavar="NODE.FIELD=VALUE",
                    help="override any workflow input (repeatable)")
    ap.add_argument("--wav", action="store_true",
                    help="also transcode the FLAC to WAV (needs soundfile or ffmpeg)")
    ap.add_argument("--timeout", type=int, default=600, help="max seconds to wait")
    args = ap.parse_args()

    if args.list_models:
        for key in sorted(MODELS):
            print(f"{key:16s} {MODELS[key]['desc']}")
        return 0

    if not args.text:
        ap.error("text is required (or use --list-models)")

    mcfg = MODELS[args.model]
    base = args.host.rstrip("/")
    seed = args.seed if args.seed is not None else random.randint(0, 2 ** 31 - 1)
    name = args.name or f"{_slug(args.text)}_{int(time.time())}"
    workflow = args.workflow or os.path.join(HERE, "workflows", mcfg["workflow"])
    os.makedirs(args.outdir, exist_ok=True)

    api = json.load(open(workflow, encoding="utf-8"))
    tts = api[mcfg["tts"]]["inputs"]
    tts["text"] = args.text
    tts["seed"] = seed

    wanted = {
        "exaggeration": args.exaggeration,
        "cfg_weight": args.cfg_weight,
        "temperature": args.temperature,
        "top_k": args.top_k,
        "top_p": args.top_p,
        "repetition_penalty": args.repetition_penalty,
    }
    for key, value in wanted.items():
        if value is None:
            continue
        if key not in tts:
            print(f"--{key.replace('_', '-')} is not supported by {args.model}",
                  file=sys.stderr)
            return 2
        tts[key] = value
    tts["keep_model_loaded"] = True

    if args.voice:
        if not os.path.exists(args.voice):
            print(f"voice reference not found: {args.voice}", file=sys.stderr)
            return 2
        try:
            uploaded = _upload(base, args.voice)
        except Exception as e:
            print(f"voice upload failed: {e}", file=sys.stderr)
            return 1
        api["6"] = {"class_type": "LoadAudio", "inputs": {"audio": uploaded}}
        tts["audio_prompt"] = ["6", 0]

    api[mcfg["save"]]["inputs"]["filename_prefix"] = name

    try:
        if args.overrides:
            _apply_overrides(api, args.overrides)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2

    print(f"[generate_speech] model={args.model} host={base} name={name} seed={seed}"
          + (f" voice={args.voice}" if args.voice else ""))
    try:
        pid = _post(base, api)
    except urllib.error.HTTPError as e:
        print("Submit failed:", e.code, e.read().decode()[:2000], file=sys.stderr)
        return 1
    print(f"[generate_speech] queued prompt_id={pid} ; waiting (up to {args.timeout}s)...")

    entry = _wait(base, pid, args.timeout)
    if entry is None:
        print("Timed out waiting for the job.", file=sys.stderr)
        return 1
    status = entry.get("status", {})
    if status.get("status_str") != "success" or not status.get("completed"):
        print("Job failed:", json.dumps(status)[:2000], file=sys.stderr)
        return 1

    outs = entry.get("outputs", {})
    items = outs.get(mcfg["save"], {}).get("audio") or []
    if not items:
        print("No audio output found in job result.", file=sys.stderr)
        return 1

    print("[generate_speech] exported:")
    wav_jobs = []
    for i, item in enumerate(items):
        filename = item.get("filename")
        if not filename:
            continue
        ext = os.path.splitext(filename)[1] or ".flac"
        suffix = "" if len(items) == 1 else f"_{i + 1:02d}"
        dst = os.path.join(args.outdir, f"{name}{suffix}{ext}")
        _download(base, filename, dst, item.get("subfolder", ""))
        print(f"   audio: {dst}")
        wav_jobs.append((dst, os.path.join(args.outdir, f"{name}{suffix}.wav")))

    if args.wav:
        for src, wav in wav_jobs:
            if _to_wav(src, wav):
                print(f"   wav:   {wav}")
            else:
                print("   wav skipped (install 'soundfile' or 'ffmpeg')", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
