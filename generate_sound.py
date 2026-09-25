#!/usr/bin/env python3
"""Generate a sound effect / short audio sample from a text prompt.

Runs on the remote ComfyUI instance (e.g. delphi:8188). Two models are
supported and selectable with --model:

    sao   Stable Audio Open 1.0        (~1.1B, core nodes; all-round foley/SFX)
    sa3   Stable Audio 3 Small-SFX     (~3.5GB, SFX-specialist alternative)

Both output stereo audio; sounds are exported locally as:
    <outdir>/<name>.flac     the generated audio sample
    <outdir>/<name>.wav      (optional) PCM WAV, if --wav

Configuration: --duration (sub-second supported), --steps, --cfg, --sampler,
--scheduler, --negative, --style, --batch and a generic --set NODE.FIELD=VALUE
escape hatch for any workflow input.

Prompting tips (Stable Audio Open 1.0 conditions on literal text):
  - Describe the sound concretely, e.g. "rain falling heavily, droplets hitting
    a glass window pane, close-up". Short, plain descriptions work well.
  - Leave --negative empty (default). A negative like "muffled" pushes the
    model to boost highs and produces a thinner, hissier clip.
  - Vary --seed; results vary a lot per seed.
  - SAO is effectively band-limited to ~16 kHz (it emits a 44.1 kHz container);
    this is inherent to the model, not a rendering bug.

Examples:
    python generate_sound.py "kitty meowing, close-up, foley" --name cat_meow
    python generate_sound.py "rain hitting a window" --model sa3 --duration 12
    python generate_sound.py "ui click, single" --duration 0.2 --wav
    python generate_sound.py "wind and rain" --steps 100 --cfg 7 --set 3.scheduler=karras
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

HERE = os.path.dirname(os.path.abspath(__file__))
# Remote ComfyUI endpoint; override with COMFY_REMOTE_HOST / COMFY_REMOTE_PORT
# (or pass --host). COMFY_REMOTE_USER is used by setup.sh for passwordless ssh.
_REMOTE_HOST = os.environ.get("COMFY_REMOTE_HOST", "delphi")
_REMOTE_PORT = os.environ.get("COMFY_REMOTE_PORT", "8188")
DEFAULT_HOST = os.environ.get("COMFY_HOST", f"http://{_REMOTE_HOST}:{_REMOTE_PORT}")

DEFAULT_NEGATIVE = ""

# Per-model workflow + node ids + defaults. The node ids must stay in sync with
# the matching workflows/<file> (both the UI and API JSONs).
MODELS = {
    "sao": {
        "desc": "Stable Audio Open 1.0 - all-round foley/SFX (~16 kHz bandwidth)",
        "workflow": "stableaudio_sfx_api.json",
        "prompt": "6", "negative": "7", "ks": "3",
        "latent": "11", "seconds_total": None, "trim": "21", "save": "20",
        "steps": 50, "cfg": 4.98,
        "sampler": "dpmpp_3m_sde_gpu", "scheduler": "exponential",
        "min_seconds": 1.0,
    },
    "sa3": {
        "desc": "Stable Audio 3 Small-SFX - SFX specialist alternative",
        "workflow": "stableaudio_sfx_sa3_api.json",
        "prompt": "6", "negative": "7", "ks": "3",
        "latent": "11", "seconds_total": "8", "trim": "21", "save": "20",
        "steps": 8, "cfg": 3.0,
        "sampler": "lcm", "scheduler": "simple",
        "min_seconds": 1.0,
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
        time.sleep(4)
        try:
            with urllib.request.urlopen(f"{base}/history/{pid}", timeout=30) as r:
                h = json.load(r)
        except Exception:
            continue
        if pid in h:
            return h[pid]
    return None


def _download(base, filename, dst, subfolder=""):
    q = {"filename": filename, "type": "output"}
    if subfolder:
        q["subfolder"] = subfolder
    url = base + "/view?" + urllib.parse.urlencode(q)
    with urllib.request.urlopen(url, timeout=300) as r, open(dst, "wb") as f:
        f.write(r.read())


def _to_wav(src, dst):
    """Best-effort FLAC -> WAV. Returns the path on success, else None."""
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
    return (s[:40] or "sound")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prompt", nargs="?", help="text prompt for the sound effect")
    ap.add_argument("--model", default="sao", choices=sorted(MODELS),
                    help="sound model to use (default: sao)")
    ap.add_argument("--list-models", action="store_true",
                    help="list available models and exit")
    ap.add_argument("--name", help="sound name (default: slug of prompt + timestamp)")
    ap.add_argument("--outdir", default="sounds", help="local output directory")
    ap.add_argument("--host", default=DEFAULT_HOST, help="ComfyUI base URL")
    ap.add_argument("--workflow", help="override the API workflow JSON")
    ap.add_argument("--seed", type=int, help="fixed seed (default: random)")
    ap.add_argument("--duration", type=float, default=8.0,
                    help="clip length in seconds; values below 1 are allowed and "
                         "are generated at 1s then trimmed (default: 8)")
    ap.add_argument("--batch", type=int, help="number of samples to generate")
    ap.add_argument("--negative", default=DEFAULT_NEGATIVE,
                    help="negative prompt (default: empty; see prompt tips)")
    ap.add_argument("--style", default="",
                    help="optional descriptive suffix appended to the prompt")
    ap.add_argument("--steps", type=int, help="override sampling steps")
    ap.add_argument("--cfg", type=float, help="override CFG (guidance) scale")
    ap.add_argument("--sampler", help="override KSampler sampler_name")
    ap.add_argument("--scheduler", help="override KSampler scheduler")
    ap.add_argument("--set", dest="overrides", action="append", metavar="NODE.FIELD=VALUE",
                    help="override any workflow input (repeatable), e.g. 3.cfg=6.5")
    ap.add_argument("--wav", action="store_true",
                    help="also transcode the FLAC to WAV (needs soundfile or ffmpeg)")
    ap.add_argument("--timeout", type=int, default=600, help="max seconds to wait")
    args = ap.parse_args()

    if args.list_models:
        for key in sorted(MODELS):
            m = MODELS[key]
            print(f"{key:5s} {m['desc']}  [defaults: {m['steps']} steps, "
                  f"cfg {m['cfg']}, {m['sampler']}/{m['scheduler']}]")
        return 0

    if not args.prompt:
        ap.error("a prompt is required (or use --list-models)")

    mcfg = MODELS[args.model]
    base = args.host.rstrip("/")
    seed = args.seed if args.seed is not None else random.randint(1, 2 ** 31 - 1)
    name = args.name or f"{_slug(args.prompt)}_{int(time.time())}"
    workflow = args.workflow or os.path.join(HERE, "workflows", mcfg["workflow"])
    os.makedirs(args.outdir, exist_ok=True)

    prompt = f"{args.prompt}, {args.style}" if args.style else args.prompt
    # Sub-second clips: the latent node enforces a 1s minimum, so generate at
    # 1s and trim the decoded audio down to the requested length.
    latent_seconds = max(args.duration, mcfg["min_seconds"])

    api = json.load(open(workflow, encoding="utf-8"))
    api[mcfg["prompt"]]["inputs"]["text"] = prompt
    api[mcfg["negative"]]["inputs"]["text"] = args.negative
    api[mcfg["latent"]]["inputs"]["seconds"] = latent_seconds
    if args.batch:
        api[mcfg["latent"]]["inputs"]["batch_size"] = args.batch
    if mcfg.get("seconds_total"):
        api[mcfg["seconds_total"]]["inputs"]["seconds_total"] = latent_seconds
    if mcfg.get("trim"):
        api[mcfg["trim"]]["inputs"]["start_index"] = 0.0
        api[mcfg["trim"]]["inputs"]["duration"] = args.duration

    ks = api[mcfg["ks"]]["inputs"]
    ks["seed"] = seed
    if args.steps is not None:
        ks["steps"] = args.steps
    if args.cfg is not None:
        ks["cfg"] = args.cfg
    if args.sampler:
        ks["sampler_name"] = args.sampler
    if args.scheduler:
        ks["scheduler"] = args.scheduler
    api[mcfg["save"]]["inputs"]["filename_prefix"] = name

    try:
        if args.overrides:
            _apply_overrides(api, args.overrides)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2

    print(f"[generate_sound] model={args.model} host={base} name={name} "
          f"seed={seed} duration={args.duration}s")
    try:
        pid = _post(base, api)
    except urllib.error.HTTPError as e:
        print("Submit failed:", e.code, e.read().decode()[:2000], file=sys.stderr)
        return 1
    print(f"[generate_sound] queued prompt_id={pid} ; waiting (up to {args.timeout}s)...")

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

    print("[generate_sound] exported:")
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
