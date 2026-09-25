# delphi-comfyui

Game-asset generation toolkit that drives a **remote ComfyUI instance on the
AMD `delphi` machine (ROCm)**. Three text-driven pipelines:

- **3D assets** — text prompt → 2D concept image → textured low-poly model (GLB)
- **Sound effects** — text prompt → sound-effect / short-audio sample (FLAC/WAV)
- **Speech (TTS)** — text → spoken dialogue with prosody control (FLAC/WAV)

```
text prompt
   │  Z-Image-Turbo (text -> image, 1024², ~8 steps)
   ▼
2D concept image ──► BiRefNet background removal ──► alpha matte
   │
   ▼  TRELLIS.2 (GGUF Q8_0) image -> 3D
sparse structure -> shape -> texture -> PBR bake -> low-poly GLB

text prompt ──► Stable Audio Open 1.0  (or Stable Audio 3 Small-SFX) ──► .flac
text        ──► Chatterbox TTS (prosody / emotion, voice cloning)    ──► .flac
```

ComfyUI runs inside the vendor `ryai-comfyui` podman container on the remote host
(`http://${COMFY_REMOTE_HOST:-delphi}:${COMFY_REMOTE_PORT:-8188}`), managed by the
systemd user unit `comfyui@<port>.service`. See **Configuration** below to point
the tooling at a different host/user/port.

## Contents

| Path | Purpose |
|------|---------|
| `generate_asset.py` | Entry point: prompt -> `*_2d.png` + `*.glb` (downloads locally) |
| `generate_sound.py` | Entry point: prompt -> sound effect `*.flac` (+ optional `*.wav`) |
| `generate_speech.py` | Entry point: text -> speech `*.flac` (prosody, voice cloning) |
| `bake.py` | Batch runner: JSON task list -> many assets/sounds at target paths |
| `bake-skill.md` | How to drive `bake.py` for batch asset generation |
| `setup.sh` | First-time provisioning of the remote ComfyUI (software + models) |
| `workflows/zimage_trellis2gguf_game_asset{,_api}.json` | 3D: UI + API workflow for `generate_asset.py` |
| `workflows/stableaudio_sfx{,_api}.json` | Sound: Stable Audio Open 1.0 (`--model sao`) UI + API |
| `workflows/stableaudio_sfx_sa3{,_api}.json` | Sound: Stable Audio 3 Small-SFX (`--model sa3`) UI + API |
| `workflows/chatterbox_speech{,_api}.json` | Speech: Chatterbox TTS (prosody) UI + API |
| `workflows/chatterbox_speech_turbo{,_api}.json` | Speech: Chatterbox Turbo (emotion tags) UI + API |
| `render_glb.py` | Headless Blender render of a GLB (for visual checks) |
| `compare_images.py` | Reference-vs-render colour comparison + side-by-side montage |
| `samples/` | Example outputs (2D image, GLB, render, comparison montage) |
| `demo_sounds/` | `sao` vs `sa3` comparison outputs + `COMMENTARY.md` |
| `demo-speech/` | `chatterbox` vs `chatterbox-turbo` samples + `COMMENTARY.md` |

## Quick start

```bash
./setup.sh                       # one-time: provision remote ComfyUI + models
python generate_asset.py "low poly game model of a treasure chest, 16 color palette, vibrant colors, isometric view, neutral background, game asset" --name chest --render
```

Output (in `assets/` by default):

```
assets/chest_2d.png        generated 2D image
assets/chest.glb           textured low-poly 3D model
assets/render_chest.png    optional Blender render
```

Options: `--outdir`, `--host`, `--workflow`, `--seed`, `--timeout`, `--render`,
`--blender`. Use `BLENDER=/path/to/blender` or `--blender` if Blender is not on
`PATH`.

### Batch generation (`bake.py`)

`bake.py` runs a JSON array of tasks in order, driving `generate_asset.py` or
`generate_sound.py` for each and copying the final artifact to a target path:

```json
[
  {"label": "chest", "tool": "asset",
   "prompt": "low poly treasure chest, 16 color palette",
   "output": "build/models/chest.glb", "args": {"render": true}},
  {"label": "chest_open", "tool": "sound",
   "prompt": "wooden chest lid creaking open, foley",
   "output": "build/sfx/chest_open.flac", "args": {"duration": 3}}
]
```

```bash
python bake.py assets.json                       # run all (skips existing outputs)
python bake.py assets.json --list                # show task indices/labels
python bake.py assets.json --dry-run             # print the generator commands
python bake.py assets.json --only 'chest*'       # re-run by label glob
python bake.py assets.json --index 1,3-5 --force # re-run by index, overwrite
```

Each task needs `tool`, `prompt` and `output`; `label` and `args` are optional.
`output` ends in `.glb` for assets and `.flac`/`.wav` for sounds (`.wav` is
transcoded and needs `soundfile` or `ffmpeg`). `args` values become CLI flags
(`{"no_style": true}` -> `--no-style`). Existing outputs are skipped unless
`--force` is given; failures stop the run unless `--keep-going`. See
`bake-skill.md` for the full reference.

### Configuration

The remote endpoint and ssh target are configurable through environment
variables (a leading `--host` still overrides the server URL):

| Variable | Default | Meaning |
|----------|---------|---------|
| `COMFY_REMOTE_HOST` | `delphi` | ComfyUI server host (also the ssh host) |
| `COMFY_REMOTE_PORT` | `8188` | ComfyUI server port |
| `COMFY_REMOTE_USER` | `febret` | passwordless-ssh user on the remote host (used by `setup.sh`) |

```bash
COMFY_REMOTE_HOST=myhost COMFY_REMOTE_PORT=8188 COMFY_REMOTE_USER=me ./setup.sh
COMFY_REMOTE_HOST=myhost COMFY_REMOTE_PORT=8188 python generate_asset.py "..." --name x
```

To run inside ComfyUI's web UI instead, open `http://delphi:8188`, drag
`workflows/zimage_trellis2gguf_game_asset.json` onto the canvas and edit the
`CLIPTextEncode` prompt. (It is also copied to the server's user workflows dir by
`setup.sh`, under `user/default/workflows/`.)

## Pipeline details

- **Text to image** – Z-Image-Turbo (`z_image_turbo_bf16` + `qwen_3_4b` +
  `ae`), 1024², 8 steps, ~20 s.
- **Matting** – core `RemoveBackground` (BiRefNet) produces the subject mask,
  `InvertMask` + `JoinImageWithAlpha` build an RGBA image, then
  `Trellis2PreProcessImage_GGUF` crops/pads it. (Note: `JoinImageWithAlpha`
  inverts the mask it receives — the `InvertMask` node is required.)
- **Image to 3D** – community `ComfyUI-Trellis2-GGUF` nodes:
  `Trellis2MeshWithVoxelAdvancedGenerator_GGUF` (structure + shape),
  `Trellis2Remesh_GGUF`, `Trellis2SimplifyMesh_GGUF`,
  `Trellis2MeshWithVoxelToTrimesh_GGUF`, `Trellis2MeshTexturing_GGUF`,
  `Trellis2ExportMesh_GGUF`.
- **Default quality/speed settings** – model format `GGUF Q8_0`, texture
  resolution 512, `texture_steps=12`, `sparse_structure_resolution=16`,
  simplify target 5000 faces, `low_vram=True`. Typical runtime 50-140 s per
  asset on `delphi`.

## Hardware / environment notes (AMD ROCm)

- `delphi` is a Strix Halo APU (`gfx1151`, ~120 GB unified memory) running
  PyTorch `2.12.0+rocm7.14.1`.
- The `ryai-comfyui` container is **ephemeral**: it is recreated on every
  restart, so installs are persisted either in the bind-mounted base dir
  (`~/.local/share/ComfyUI`) or in the locally committed image tag
  `oci-registry.ryai.dev/ryai-comfyui:trellis2`, wired up via the quadlet
  drop-in `~/.config/containers/systemd/comfyui@.container.d/override.conf`.
- ComfyUI is pinned to `v0.37.0` (host bind mount at `~/.local/share/ComfyUI/code`)
  because TRELLIS.2 support landed in core after the vendor image was built.
- **Do not use `GGUF Q4_K_M`** for TRELLIS.2 texturing: its dequantisation is
  corrupted on this stack and yields noise textures. Use `GGUF Q8_0`
  (or BF16).
- Headless nvdiffrast/EGL aborts the whole process at the texturing step; a GL
  context is created at the very top of `main.py` (see `setup.sh`) to avoid it.
- `flash_attn`/`xformers` are not installed; the sparse attention path falls
  back to PyTorch SDPA automatically.

## Troubleshooting

- `pip`/models: run `./setup.sh` again (idempotent).
- Check the API: `curl "http://${COMFY_REMOTE_HOST:-delphi}:${COMFY_REMOTE_PORT:-8188}/system_stats"`.
- Logs: `ssh "${COMFY_REMOTE_USER:-febret}@${COMFY_REMOTE_HOST:-delphi}" "podman logs --tail 100 comfyui-${COMFY_REMOTE_PORT:-8188}"`.
- Service: `ssh "${COMFY_REMOTE_USER:-febret}@${COMFY_REMOTE_HOST:-delphi}" "systemctl --user status comfyui@${COMFY_REMOTE_PORT:-8188}.service"`.
- TRELLIS.2 GGUF weights download automatically on the first generation into
  `models/Trellis2/` (~4.5 GB for Q8_0).
