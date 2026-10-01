#!/usr/bin/env python3
"""Generate a high quality image from a text prompt on the remote ComfyUI.

The model / graph is selected by a workflow id from a registry file
(workflow.json, overridable with --registry). Each registry entry names the API
workflow JSON to use under workflows/, the node ids the script edits, sane
defaults, and the model files the workflow needs (with download URLs). When a
workflow is run, any listed model that is not yet present on the remote ComfyUI
host is downloaded into the correct models/ subdirectory first.

The default workflow is `default_imagegen` (Pony Diffusion V6 XL, an SDXL
finetune); image generation is exported locally as:
    <outdir>/<name>.png      (or an explicit --out path)

Controls: --resolution WxH (alias --width/--height), --seed, --negative,
--steps, --cfg, --sampler, --scheduler, --denoise, --batch, --clip-skip, the
prompt template (--prefix/--style/--no-prompt-template), and a generic
--set NODE.FIELD=VALUE escape hatch for any workflow input.

Upscaling: --upscale runs the decoded image through an upscale model from the
registry's `upscalers` catalog (--upscale-model, default per workflow), then
resizes it with --upscale-scale (final multiplier; default the model's native
scale, e.g. 4x). Missing upscale models are installed on demand like any other.

With --serve the prompt-filled UI workflow (workflows/pony_sdxl.json, derived
from the API JSON by dropping the _api suffix) is uploaded to the remote
ComfyUI's userdata (workflows/) for interactive use in the web UI; nothing is
queued. --serve --upscale un-bypasses the upscale nodes in that graph.

Examples:
    python generate_image.py "a red fox curled up in autumn leaves"
    python generate_image.py "misty mountain valley at sunrise" -r 1344x768
    python generate_image.py "a cozy tavern" --out renders/tavern.png --seed 42
    python generate_image.py "a village at dusk" --upscale
    python generate_image.py "a harbour town" --upscale --upscale-scale 2
    python generate_image.py --list-workflows
    python generate_image.py --list-upscalers
    python generate_image.py "a village" --workflow default_imagegen --serve
"""
import argparse
import json
import os
import random
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REGISTRY = os.path.join(HERE, "workflow.json")
# Remote ComfyUI endpoint; override with COMFY_REMOTE_HOST / COMFY_REMOTE_PORT.
# COMFY_REMOTE_USER / COMFY_REMOTE_HOST are used for passwordless ssh (model
# installs); COMFY_REMOTE_MODELS overrides the remote models directory.
_REMOTE_HOST = os.environ.get("COMFY_REMOTE_HOST", "delphi")
_REMOTE_PORT = os.environ.get("COMFY_REMOTE_PORT", "8188")
_REMOTE_USER = os.environ.get("COMFY_REMOTE_USER", "febret")
DEFAULT_HOST = os.environ.get("COMFY_HOST", f"http://{_REMOTE_HOST}:{_REMOTE_PORT}")
REMOTE_MODELS = os.environ.get("COMFY_REMOTE_MODELS", "$HOME/.local/share/ComfyUI/models")
SSH = os.environ.get("COMFY_SSH", "ssh")


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


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (s[:40] or "image")


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


def _load_registry(path):
    if not os.path.isfile(path):
        raise ValueError(f"registry not found: {path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    workflows = data.get("workflows") if isinstance(data, dict) else None
    if not isinstance(workflows, dict) or not workflows:
        raise ValueError(f"{path}: expected an object with a non-empty 'workflows' map")
    upscalers = data.get("upscalers") or {}
    if not isinstance(upscalers, dict):
        raise ValueError(f"{path}: 'upscalers' must be an object")
    return workflows, upscalers


def _resolve_workflow(workflows, wid):
    if wid not in workflows:
        raise ValueError(f"unknown workflow {wid!r} (available: {', '.join(sorted(workflows))})")
    cfg = workflows[wid]
    for key in ("workflow", "nodes", "models"):
        if key not in cfg:
            raise ValueError(f"workflow {wid!r} is missing {key!r}")
    return cfg


def _ssh_target():
    return f"{_REMOTE_USER}@{_REMOTE_HOST}"


def _remote_exists(remote_path):
    cmd = f'[ -s "{remote_path}" ] && echo yes || echo no'
    try:
        out = subprocess.run(
            [SSH, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
             _ssh_target(), cmd],
            capture_output=True, text=True, timeout=60)
    except Exception as e:
        print(f"[generate_image] ssh check failed: {e}", file=sys.stderr)
        return False
    return out.stdout.strip().endswith("yes")


def _remote_download(url, remote_path):
    cmd = (
        f'mkdir -p "$(dirname "{remote_path}")" && '
        f'curl -fL -C - --retry 10 --retry-all-errors --retry-delay 5 '
        f'--connect-timeout 30 -o "{remote_path}" "{url}"'
    )
    return subprocess.call([SSH, "-o", "BatchMode=yes", _ssh_target(), cmd])


def ensure_models(models, no_download):
    """Install any missing workflow models on the remote host. Returns rc."""
    for m in models:
        dest = m.get("dest")
        url = m.get("url")
        if not (dest and url):
            print(f"[generate_image] bad model entry (need url+dest): {m!r}", file=sys.stderr)
            return 1
        remote = f"{REMOTE_MODELS}/{dest}"
        if _remote_exists(remote):
            print(f"[generate_image] model present: {dest}")
            continue
        if no_download:
            print(f"[generate_image] model missing and --no-download set: {dest}", file=sys.stderr)
            return 1
        label = m.get("name", dest)
        print(f"[generate_image] downloading {label} -> models/{dest} ...")
        if _remote_download(url, remote) != 0:
            print(f"[generate_image] download failed: {url}", file=sys.stderr)
            return 1
    return 0


def _ui_workflow_path(api_path):
    root, ext = os.path.splitext(api_path)
    if root.endswith("_api"):
        return root[: -len("_api")] + ext
    return api_path


def _set_widget(ui, nid, idx, value):
    nodes = {int(n["id"]): n for n in ui.get("nodes", [])}
    node = nodes.get(int(nid))
    if node is None:
        raise KeyError(nid)
    widgets = node.setdefault("widgets_values", [])
    while len(widgets) <= idx:
        widgets.append(None)
    widgets[idx] = value


def _set_mode(ui, nid, mode):
    nodes = {int(n["id"]): n for n in ui.get("nodes", [])}
    node = nodes.get(int(nid))
    if node is None:
        raise KeyError(nid)
    node["mode"] = mode


def _apply_ui(ui, cfg, prompt_text, negative, name, seed, width, height,
              batch, steps, cfg_scale, sampler, scheduler, denoise, clip_skip,
              upscale=None):
    nodes = cfg["nodes"]
    uw = cfg.get("ui_widgets") or {}

    def put(key, value, idx):
        if key in nodes and idx is not None and value is not None:
            _set_widget(ui, nodes[key], idx, value)

    put("prompt", prompt_text, uw.get("prompt"))
    put("negative", negative, uw.get("negative"))
    put("clip_skip", clip_skip, uw.get("clip_skip"))
    put("save", name, uw.get("save"))

    latent = uw.get("latent") or {}
    put("latent", width, latent.get("width"))
    put("latent", height, latent.get("height"))
    put("latent", batch, latent.get("batch_size"))

    ks = uw.get("ksampler") or {}
    put("ksampler", seed, ks.get("seed"))
    put("ksampler", steps, ks.get("steps"))
    put("ksampler", cfg_scale, ks.get("cfg"))
    put("ksampler", sampler, ks.get("sampler_name"))
    put("ksampler", scheduler, ks.get("scheduler"))
    put("ksampler", denoise, ks.get("denoise"))

    if upscale:
        up_nodes = upscale["nodes"]
        up_uw = upscale.get("widgets") or {}
        mode = 0 if upscale["on"] else 4
        for nid in up_nodes.values():
            _set_mode(ui, nid, mode)
        _set_widget(ui, up_nodes["loader"], up_uw.get("loader", 0), upscale["filename"])
        resize = up_uw.get("resize") or {}
        _set_widget(ui, up_nodes["resize"], resize.get("upscale_method", 0),
                    upscale["method"])
        _set_widget(ui, up_nodes["resize"], resize.get("scale_by", 1),
                    upscale["scale_by"])


def _upload_userdata(base, relpath, data):
    url = base + "/api/userdata/" + urllib.parse.quote(relpath, safe="") + "?overwrite=true"
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


def _serve(base, cfg, name, vals, upscale=None):
    api_path = os.path.join(HERE, "workflows", cfg["workflow"])
    ui_path = os.path.join(HERE, "workflows", cfg.get("ui_workflow") or _ui_workflow_path(cfg["workflow"]))
    if not os.path.isfile(ui_path):
        print(f"[generate_image] UI workflow not found: {ui_path}", file=sys.stderr)
        return 1
    with open(ui_path, encoding="utf-8") as f:
        ui = json.load(f)
    try:
        _apply_ui(ui, cfg, vals["prompt"], vals["negative"], name, vals["seed"],
                  vals["width"], vals["height"], vals["batch"], vals["steps"],
                  vals["cfg"], vals["sampler"], vals["scheduler"],
                  vals["denoise"], vals["clip_skip"], upscale)
    except KeyError as e:
        print(f"[generate_image] {ui_path} is not a UI workflow (node {e} missing)",
              file=sys.stderr)
        return 1

    relpath = f"workflows/{name}.json"
    try:
        _upload_userdata(base, relpath, ui)
    except urllib.error.HTTPError as e:
        print("Upload failed:", e.code, e.read().decode()[:2000], file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print("Upload failed:", e, file=sys.stderr)
        return 1

    print(f"[generate_image] loaded workflow '{name}' into ComfyUI userdata ({relpath}); "
          "nothing was queued")
    print(f"[generate_image] open {base}/ and load '{name}.json' from the workflow menu")
    return 0


def _parse_resolution(text):
    m = re.match(r"^\s*(\d+)\s*[xX*]\s*(\d+)\s*$", text or "")
    if not m:
        raise ValueError(f"bad --resolution {text!r} (expected WxH, e.g. 1024x1024)")
    return int(m.group(1)), int(m.group(2))


def _build_prompt(args, defaults):
    if args.no_prompt_template:
        return args.prompt
    prefix = args.prefix if args.prefix is not None else defaults.get("prompt_prefix", "")
    suffix = args.style if args.style is not None else defaults.get("style", "")
    parts = [p.strip() for p in (prefix, args.prompt, suffix) if p and p.strip()]
    return ", ".join(parts)


def _list_workflows(workflows):
    for wid in sorted(workflows):
        cfg = workflows[wid]
        models = cfg.get("models") or []
        print(f"{wid}  ({cfg.get('workflow', '?')})")
        if cfg.get("description"):
            print(f"    {cfg['description']}")
        for m in models:
            print(f"    model: {m.get('name', m.get('dest', '?'))} -> models/{m.get('dest', '?')}")
    return 0


def _list_upscalers(upscalers, workflows):
    if not upscalers:
        print("(no upscalers in the registry)")
        return 0
    defaults = {cfg.get("upscale", {}).get("default") for cfg in workflows.values()}
    for name in sorted(upscalers):
        up = upscalers[name]
        mark = " (default)" if name in defaults else ""
        print(f"{name}  {up.get('scale', '?')}x  -> models/{up.get('dest', '?')}{mark}")
        if up.get("desc"):
            print(f"    {up['desc']}")
    return 0


def _upscaler_entry(upscalers, name, wid):
    if not name:
        raise ValueError(f"workflow {wid!r} has no default upscaler; pass --upscale-model")
    if name not in upscalers:
        avail = ", ".join(sorted(upscalers)) or "none"
        raise ValueError(f"unknown upscaler {name!r} (available: {avail})")
    up = upscalers[name]
    for key in ("url", "dest", "scale"):
        if key not in up:
            raise ValueError(f"upscaler {name!r} is missing {key!r}")
    return up


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prompt", nargs="?", help="text prompt for the image")
    ap.add_argument("--workflow", "-w", default="default_imagegen",
                    help="workflow id from the registry (default: default_imagegen)")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY,
                    help="path to the workflow registry JSON (default: workflow.json)")
    ap.add_argument("--list-workflows", action="store_true",
                    help="list the workflows in the registry and exit")
    ap.add_argument("--name", help="image name (default: slug of prompt + timestamp)")
    ap.add_argument("--out", metavar="PATH",
                    help="output image path or directory (default: <outdir>/<name>.png)")
    ap.add_argument("--outdir", default="images", help="local output directory")
    ap.add_argument("--host", default=DEFAULT_HOST, help="ComfyUI base URL")
    ap.add_argument("--seed", type=int, help="fixed seed (default: random)")
    ap.add_argument("--negative", help="negative prompt (default: workflow's)")
    ap.add_argument("--resolution", "-r", metavar="WxH",
                    help="output resolution, e.g. 1024x1024 "
                         "(default: workflow's, usually 1024x1024)")
    ap.add_argument("--width", type=int, help="output width (overrides --resolution)")
    ap.add_argument("--height", type=int, help="output height (overrides --resolution)")
    ap.add_argument("--batch", type=int, help="number of images to generate")
    ap.add_argument("--upscale", action="store_true",
                    help="run the image through an upscale model (see --list-upscalers)")
    ap.add_argument("--upscale-model", metavar="NAME",
                    help="upscale model from the registry catalog (default: workflow's)")
    ap.add_argument("--upscale-scale", type=float, metavar="F",
                    help="final upscaled size multiplier (default: the model's native scale)")
    ap.add_argument("--upscale-method",
                    choices=("nearest-exact", "bilinear", "area", "bicubic", "lanczos"),
                    help="resize filter when the target scale differs from the model scale")
    ap.add_argument("--list-upscalers", action="store_true",
                    help="list the upscale models in the registry and exit")
    ap.add_argument("--steps", type=int, help="override sampling steps")
    ap.add_argument("--cfg", type=float, help="override CFG (guidance) scale")
    ap.add_argument("--sampler", help="override KSampler sampler_name")
    ap.add_argument("--scheduler", help="override KSampler scheduler")
    ap.add_argument("--denoise", type=float, help="override denoise strength")
    ap.add_argument("--clip-skip", dest="clip_skip", type=int,
                    help="override CLIPSetLastLayer (e.g. -2)")
    ap.add_argument("--prefix", help="override the prompt prefix template")
    ap.add_argument("--style", help="descriptive style suffix appended to the prompt")
    ap.add_argument("--no-prompt-template", action="store_true",
                    help="send the prompt verbatim, without prefix/style")
    ap.add_argument("--set", dest="overrides", action="append", metavar="NODE.FIELD=VALUE",
                    help="override any workflow input (repeatable), e.g. 3.cfg=6.5")
    ap.add_argument("--no-download", action="store_true",
                    help="do not install missing models on the remote host")
    ap.add_argument("--serve", action="store_true",
                    help="load the prompt-filled UI workflow into ComfyUI's userdata "
                         "for interactive use in the web UI; do not run it")
    ap.add_argument("--timeout", type=int, default=900, help="max seconds to wait")
    args = ap.parse_args()

    try:
        workflows, upscalers = _load_registry(args.registry)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"[generate_image] {e}", file=sys.stderr)
        return 2

    if args.list_workflows:
        return _list_workflows(workflows)
    if args.list_upscalers:
        return _list_upscalers(upscalers, workflows)

    if not args.prompt:
        ap.error("a prompt is required (or use --list-workflows)")

    try:
        cfg = _resolve_workflow(workflows, args.workflow)
    except ValueError as e:
        print(f"[generate_image] {e}", file=sys.stderr)
        return 2

    nodes = cfg["nodes"]
    defaults = cfg.get("defaults") or {}
    base = args.host.rstrip("/")
    seed = args.seed if args.seed is not None else random.randint(0, 2 ** 31 - 1)
    name = args.name or f"{_slug(args.prompt)}_{int(time.time())}"

    width = defaults.get("width", 1024)
    height = defaults.get("height", 1024)
    try:
        if args.resolution:
            width, height = _parse_resolution(args.resolution)
    except ValueError as e:
        print(f"[generate_image] {e}", file=sys.stderr)
        return 2
    if args.width is not None:
        width = args.width
    if args.height is not None:
        height = args.height
    if width % 8 or height % 8:
        print(f"[generate_image] width/height must be multiples of 8 (got {width}x{height})",
              file=sys.stderr)
        return 2

    batch = args.batch if args.batch is not None else defaults.get("batch_size", 1)
    steps = args.steps if args.steps is not None else defaults.get("steps", 25)
    cfg_scale = args.cfg if args.cfg is not None else defaults.get("cfg", 7.0)
    sampler = args.sampler or defaults.get("sampler_name", "euler_ancestral")
    scheduler = args.scheduler or defaults.get("scheduler", "normal")
    denoise = args.denoise if args.denoise is not None else defaults.get("denoise", 1.0)
    clip_skip = args.clip_skip if args.clip_skip is not None else defaults.get("clip_skip", -2)
    negative = args.negative if args.negative is not None else defaults.get("negative", "")
    prompt_text = _build_prompt(args, defaults)

    vals = {
        "prompt": prompt_text, "negative": negative, "seed": seed,
        "width": width, "height": height, "batch": batch, "steps": steps,
        "cfg": cfg_scale, "sampler": sampler, "scheduler": scheduler,
        "denoise": denoise, "clip_skip": clip_skip,
    }

    up_cfg = cfg.get("upscale") or {}
    upscale_on = bool(args.upscale or args.upscale_model
                      or args.upscale_scale is not None or args.upscale_method)
    up_apply = None
    if upscale_on:
        if not up_cfg:
            print(f"[generate_image] workflow {args.workflow!r} has no upscale config",
                  file=sys.stderr)
            return 2
        try:
            up = _upscaler_entry(upscalers, args.upscale_model or up_cfg.get("default"),
                                 args.workflow)
        except ValueError as e:
            print(f"[generate_image] {e}", file=sys.stderr)
            return 2
        model_scale = float(up["scale"])
        target = args.upscale_scale if args.upscale_scale is not None else up_cfg.get("target_scale")
        if target is None:
            target = model_scale
        if target <= 0:
            print("[generate_image] --upscale-scale must be > 0", file=sys.stderr)
            return 2
        up_apply = {
            "on": True,
            "nodes": up_cfg["nodes"],
            "widgets": up_cfg.get("ui_widgets") or {},
            "filename": os.path.basename(up["dest"]),
            "scale_by": target / model_scale,
            "target": target,
            "method": args.upscale_method or up_cfg.get("resize_method", "lanczos"),
            "model": up,
        }

    models = list(cfg.get("models") or [])
    if up_apply:
        models.append(up_apply["model"])

    rc = ensure_models(models, args.no_download)
    if rc:
        return rc

    if args.serve:
        return _serve(base, cfg, name, vals, up_apply)

    api_path = os.path.join(HERE, "workflows", cfg["workflow"])
    if not os.path.isfile(api_path):
        print(f"[generate_image] workflow not found: {api_path}", file=sys.stderr)
        return 1
    with open(api_path, encoding="utf-8") as f:
        api = json.load(f)

    api[nodes["prompt"]]["inputs"]["text"] = prompt_text
    api[nodes["negative"]]["inputs"]["text"] = negative
    latent = api[nodes["latent"]]["inputs"]
    latent["width"] = width
    latent["height"] = height
    latent["batch_size"] = batch
    if "clip_skip" in nodes:
        api[nodes["clip_skip"]]["inputs"]["stop_at_clip_layer"] = clip_skip
    ks = api[nodes["ksampler"]]["inputs"]
    ks["seed"] = seed
    ks["steps"] = steps
    ks["cfg"] = cfg_scale
    ks["sampler_name"] = sampler
    ks["scheduler"] = scheduler
    ks["denoise"] = denoise
    api[nodes["save"]]["inputs"]["filename_prefix"] = name

    if up_apply:
        un = up_apply["nodes"]
        api[un["loader"]]["inputs"]["model_name"] = up_apply["filename"]
        api[un["resize"]]["inputs"]["upscale_method"] = up_apply["method"]
        api[un["resize"]]["inputs"]["scale_by"] = up_apply["scale_by"]
        api[nodes["save"]]["inputs"]["images"] = [un["resize"], 0]

    try:
        if args.overrides:
            _apply_overrides(api, args.overrides)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2

    print(f"[generate_image] workflow={args.workflow} host={base} name={name} "
          f"seed={seed} size={width}x{height} steps={steps} cfg={cfg_scale}")
    if up_apply:
        eff_w = int(round(width * up_apply["target"]))
        eff_h = int(round(height * up_apply["target"]))
        print(f"[generate_image] upscale={up_apply['filename']} "
              f"target={up_apply['target']:g}x -> {eff_w}x{eff_h}")
    try:
        pid = _post(base, api)
    except urllib.error.HTTPError as e:
        print("Submit failed:", e.code, e.read().decode()[:2000], file=sys.stderr)
        return 1
    print(f"[generate_image] queued prompt_id={pid} ; waiting (up to {args.timeout}s)...")

    entry = _wait(base, pid, args.timeout)
    if entry is None:
        print("Timed out waiting for the job.", file=sys.stderr)
        return 1
    status = entry.get("status", {})
    if status.get("status_str") != "success" or not status.get("completed"):
        print("Job failed:", json.dumps(status)[:2000], file=sys.stderr)
        return 1

    outs = entry.get("outputs", {})
    items = outs.get(nodes["save"], {}).get("images") or []
    if not items:
        print("No image output found in job result.", file=sys.stderr)
        return 1

    if args.out:
        if args.out.endswith(("/", "\\")) or os.path.isdir(args.out):
            stem = os.path.join(args.out, name)
            out_ext = ".png"
        else:
            root, ext = os.path.splitext(args.out)
            stem = root or args.out
            out_ext = ext or ".png"
    else:
        os.makedirs(args.outdir, exist_ok=True)
        stem = os.path.join(args.outdir, name)
        out_ext = ".png"

    print("[generate_image] exported:")
    for i, item in enumerate(items):
        filename = item.get("filename")
        if not filename:
            continue
        ext = os.path.splitext(filename)[1] or out_ext
        suffix = "" if len(items) == 1 else f"_{i + 1:02d}"
        dst = f"{stem}{suffix}{ext}"
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _download(base, filename, dst, item.get("subfolder", ""))
        print(f"   image: {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
