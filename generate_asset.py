#!/usr/bin/env python3
"""Generate a 3D game asset from a text prompt.

Pipeline (runs on the remote ComfyUI instance, e.g. delphi:8188):
    Z-Image-Turbo text-to-image  ->  BiRefNet alpha  ->  TRELLIS.2 (GGUF Q8_0)
    image -> textured GLB (+ 2D reference image)

Assets are exported locally as:
    <outdir>/<name>_2d.png      the generated 2D reference image
    <outdir>/<name>.glb         the textured 3D model
    <outdir>/render_<name>.png  (optional) Blender render, if --render

Examples:
    python generate_asset.py "low poly game model of a treasure chest, ..." --name chest
    python generate_asset.py "a stylized low poly magic potion" --outdir assets --render
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
DEFAULT_API = os.path.join(HERE, "workflows", "zimage_trellis2gguf_game_asset_api.json")
# Remote ComfyUI endpoint; override with COMFY_REMOTE_HOST / COMFY_REMOTE_PORT
# (or pass --host). COMFY_REMOTE_USER is used by setup.sh for passwordless ssh.
_REMOTE_HOST = os.environ.get("COMFY_REMOTE_HOST", "delphi")
_REMOTE_PORT = os.environ.get("COMFY_REMOTE_PORT", "8188")
DEFAULT_HOST = os.environ.get("COMFY_HOST", f"http://{_REMOTE_HOST}:{_REMOTE_PORT}")

# Node ids inside workflows/zimage_trellis2gguf_game_asset_api.json
NODE_PROMPT = "405"     # CLIPTextEncode (Z-Image prompt)
NODE_Z_KS = "408"       # KSampler (Z-Image)
NODE_GENERATOR = "45"   # TRELLIS.2 mesh generator (seed)
NODE_TEXTURING = "163"  # TRELLIS.2 mesh texturing (seed)
NODE_NAME = "166"       # PrimitiveString: export name prefix
NODE_SAVEIMG = "411"    # SaveImage: 2D reference
NODE_PREVIEW3D = "10"   # Preview3D: exposes the exported GLB filename

STYLE_PROMPT = "3D game asset illustration. low poly. 16 color palette. vibrant colors. isometric view. Neutral white background."


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


def _download(base, filename, dst):
    url = base + "/view?filename=" + urllib.parse.quote(filename) + "&type=output"
    with urllib.request.urlopen(url, timeout=180) as r, open(dst, "wb") as f:
        f.write(r.read())


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (s[:40] or "asset")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prompt", help="text prompt for the 2D image / 3D asset")
    ap.add_argument("--name", help="asset name (default: slug of prompt + timestamp)")
    ap.add_argument("--outdir", default="assets", help="local output directory")
    ap.add_argument("--host", default=DEFAULT_HOST, help="ComfyUI base URL")
    ap.add_argument("--workflow", default=DEFAULT_API, help="API workflow JSON")
    ap.add_argument("--seed", type=int, help="fixed seed (default: random)")
    ap.add_argument("--timeout", type=int, default=900, help="max seconds to wait")
    ap.add_argument("--render", action="store_true",
                    help="render the GLB with Blender (render_glb.py)")
    ap.add_argument("--blender", default=os.environ.get("BLENDER", "blender"),
                    help="path to the Blender executable")
    args = ap.parse_args()

    base = args.host.rstrip("/")
    seed = args.seed if args.seed is not None else random.randint(1, 2 ** 31 - 1)
    name = args.name or f"{_slug(args.prompt)}_{int(time.time())}"
    os.makedirs(args.outdir, exist_ok=True)

    api = json.load(open(args.workflow, encoding="utf-8"))
    api[NODE_PROMPT]["inputs"]["text"] = args.prompt + " " + STYLE_PROMPT
    api[NODE_NAME]["inputs"]["value"] = name
    api[NODE_SAVEIMG]["inputs"]["filename_prefix"] = name
    for nid in (NODE_Z_KS, NODE_GENERATOR, NODE_TEXTURING):
        if "seed" in api.get(nid, {}).get("inputs", {}):
            api[nid]["inputs"]["seed"] = seed

    print(f"[generate_asset] host={base} name={name} seed={seed}")
    try:
        pid = _post(base, api)
    except urllib.error.HTTPError as e:
        print("Submit failed:", e.code, e.read().decode()[:2000], file=sys.stderr)
        return 1
    print(f"[generate_asset] queued prompt_id={pid} ; waiting (up to {args.timeout}s)...")

    entry = _wait(base, pid, args.timeout)
    if entry is None:
        print("Timed out waiting for the job.", file=sys.stderr)
        return 1
    status = entry.get("status", {})
    if status.get("status_str") != "success" or not status.get("completed"):
        print("Job failed:", json.dumps(status)[:2000], file=sys.stderr)
        return 1

    outs = entry.get("outputs", {})
    png = (outs.get(NODE_SAVEIMG, {}).get("images") or [{}])[0].get("filename")
    glb = (outs.get(NODE_PREVIEW3D, {}).get("result") or [None])[0]

    local = {}
    if png:
        local["2d"] = os.path.join(args.outdir, f"{name}_2d.png")
        _download(base, png, local["2d"])
    if glb:
        local["glb"] = os.path.join(args.outdir, f"{name}.glb")
        _download(base, glb, local["glb"])

    print("[generate_asset] exported:")
    for k, v in local.items():
        print(f"   {k}: {v}")

    if args.render and "glb" in local:
        import subprocess
        renderer = os.path.join(HERE, "render_glb.py")
        out_png = os.path.join(args.outdir, f"render_{name}.png")
        try:
            subprocess.run([args.blender, "--background", "--python", renderer, "--",
                            os.path.abspath(local["glb"]), os.path.abspath(out_png),
                            "512", "35", "22"], check=True)
            print(f"   render: {out_png}")
        except Exception as e:
            print(f"   render skipped ({e})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
