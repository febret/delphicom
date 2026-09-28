#!/usr/bin/env python3
"""Generate a 3D game asset from a text prompt.

Pipeline (runs on the remote ComfyUI instance, e.g. delphi:8188):
    Z-Image-Turbo text-to-image  ->  BiRefNet alpha  ->  TRELLIS.2 (GGUF Q8_0)
    image -> textured GLB (+ 2D reference image)

Assets are exported locally as:
    <outdir>/<name>_2d.png      the generated 2D reference image
    <outdir>/<name>.glb         the textured 3D model
    <outdir>/render_<name>.png  (optional) Blender render, if --render

The prompt submitted to Z-Image is the task prompt plus a style suffix: the
built-in STYLE_PROMPT by default, overridable with --style TEXT, or disabled
with --no-style.

The exported mesh is simplified to a target face count, set with --faces N
(alias --vertices N; default: the workflow's Target Face Number, 5000). This is
the main control on the output mesh density / vertex budget.

With --serve the workflow is *loaded, not run*: the UI workflow
(workflows/zimage_trellis2gguf_game_asset.json, derived from --workflow) is
filled in with the same prompt/name/seed and uploaded to the remote ComfyUI's
userdata (workflows/), so it shows up in the web UI's workflow menu. Open
http://<host>:<port>, load the named workflow, tweak it, and run it there.

Examples:
    python generate_asset.py "low poly game model of a treasure chest, ..." --name chest
    python generate_asset.py "a stylized low poly magic potion" --outdir assets --render
    python generate_asset.py "a knight's shield" --style "pixel art, 8-bit palette"
    python generate_asset.py "a low poly treasure chest" --faces 2000
    python generate_asset.py "a rusty medieval lantern" --name lantern --serve
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
NODE_FACE_NUM = "171"   # PrimitiveInt: mesh simplification target (face count)
NODE_SIMPLIFY = "160"   # Trellis2SimplifyMesh_GGUF (target_face_num widget)

# Widget positions (index into "widgets_values") inside the UI workflow
# (workflows/zimage_trellis2gguf_game_asset.json) matching the API node inputs
# above. --serve edits these so the interactive graph matches a normal run.
UI_WIDGETS = {
    NODE_PROMPT: 0,     # CLIPTextEncode.text
    NODE_NAME: 0,       # PrimitiveString.value
    NODE_SAVEIMG: 0,    # SaveImage.filename_prefix
    NODE_Z_KS: 0,       # KSampler.seed
    NODE_GENERATOR: 0,  # mesh generator seed
    NODE_TEXTURING: 0,  # texturing seed
    NODE_FACE_NUM: 0,   # PrimitiveInt.value (simplify target)
    NODE_SIMPLIFY: 0,   # Trellis2SimplifyMesh_GGUF.target_face_num
}

STYLE_PROMPT = "3D game asset illustration. low poly. simple lines. cel shading.vibrant colors. isometric view. Neutral white background."


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


def _ui_workflow_path(api_path):
    """Map an API workflow path to its UI counterpart (drop the _api suffix)."""
    root, ext = os.path.splitext(api_path)
    if root.endswith("_api"):
        return root[: -len("_api")] + ext
    return api_path


def _apply_ui(ui, prompt_text, name, seed, faces=None):
    """Fill the interactive (UI) graph with the same values as an API run."""
    values = {
        NODE_PROMPT: prompt_text,
        NODE_NAME: name,
        NODE_SAVEIMG: name,
        NODE_Z_KS: seed,
        NODE_GENERATOR: seed,
        NODE_TEXTURING: seed,
    }
    if faces is not None:
        values[NODE_FACE_NUM] = faces
        values[NODE_SIMPLIFY] = faces
    nodes = {int(n["id"]): n for n in ui.get("nodes", [])}
    for nid, value in values.items():
        node = nodes.get(int(nid))
        if node is None:
            raise KeyError(nid)
        idx = UI_WIDGETS[nid]
        widgets = node.setdefault("widgets_values", [])
        while len(widgets) <= idx:
            widgets.append(None)
        widgets[idx] = value


def _upload_userdata(base, relpath, data):
    """Store a JSON file under the remote ComfyUI userdata dir (e.g. workflows/)."""
    url = base + "/api/userdata/" + urllib.parse.quote(relpath, safe="") + "?overwrite=true"
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


def _serve(base, api_workflow, name, seed, prompt_text, faces=None):
    """Load the prompt-filled UI workflow into ComfyUI; do not run it."""
    ui_path = _ui_workflow_path(api_workflow)
    if not os.path.isfile(ui_path):
        print(f"[generate_asset] UI workflow not found: {ui_path}", file=sys.stderr)
        return 1
    ui = json.load(open(ui_path, encoding="utf-8"))
    try:
        _apply_ui(ui, prompt_text, name, seed, faces)
    except KeyError as e:
        print(f"[generate_asset] {ui_path} is not a UI workflow (node {e} missing); "
              "pass --workflow pointing at a UI workflow JSON", file=sys.stderr)
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

    print(f"[generate_asset] loaded workflow '{name}' into ComfyUI userdata "
          f"({relpath}); nothing was queued")
    print(f"[generate_asset] open {base}/ and load '{name}.json' from the workflow menu")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prompt", help="text prompt for the 2D image / 3D asset")
    ap.add_argument("--name", help="asset name (default: slug of prompt + timestamp)")
    ap.add_argument("--outdir", default="assets", help="local output directory")
    ap.add_argument("--host", default=DEFAULT_HOST, help="ComfyUI base URL")
    ap.add_argument("--workflow", default=DEFAULT_API, help="API workflow JSON")
    ap.add_argument("--seed", type=int, help="fixed seed (default: random)")
    ap.add_argument("--faces", "--vertices", dest="faces", type=int, metavar="N",
                    help="target face count for mesh simplification, i.e. the output "
                         "poly/vertex budget (default: workflow's 5000; alias --vertices)")
    ap.add_argument("--style", default=None,
                    help="style suffix appended to the prompt (default: built-in "
                         "STYLE_PROMPT; pass an empty string for no suffix)")
    ap.add_argument("--no-style", action="store_true",
                    help="append no style suffix at all")
    ap.add_argument("--timeout", type=int, default=900, help="max seconds to wait")
    ap.add_argument("--serve", action="store_true",
                    help="load the prompt-filled UI workflow into ComfyUI's userdata "
                         "for interactive use in the web UI; do not run it")
    ap.add_argument("--render", action="store_true",
                    help="render the GLB with Blender (render_glb.py)")
    ap.add_argument("--blender", default=os.environ.get("BLENDER", "blender"),
                    help="path to the Blender executable")
    args = ap.parse_args()

    base = args.host.rstrip("/")
    seed = args.seed if args.seed is not None else random.randint(1, 2 ** 31 - 1)
    name = args.name or f"{_slug(args.prompt)}_{int(time.time())}"

    if args.no_style:
        style = ""
    elif args.style is not None:
        style = args.style
    else:
        style = STYLE_PROMPT
    prompt_text = f"{args.prompt} {style}".strip() if style else args.prompt

    if args.serve:
        return _serve(base, args.workflow, name, seed, prompt_text, args.faces)

    os.makedirs(args.outdir, exist_ok=True)

    api = json.load(open(args.workflow, encoding="utf-8"))
    api[NODE_PROMPT]["inputs"]["text"] = prompt_text
    api[NODE_NAME]["inputs"]["value"] = name
    api[NODE_SAVEIMG]["inputs"]["filename_prefix"] = name
    for nid in (NODE_Z_KS, NODE_GENERATOR, NODE_TEXTURING):
        if "seed" in api.get(nid, {}).get("inputs", {}):
            api[nid]["inputs"]["seed"] = seed
    if args.faces is not None:
        api[NODE_FACE_NUM]["inputs"]["value"] = args.faces

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
