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

Options: `--outdir`, `--host`, `--workflow`, `--seed`, `--faces`, `--timeout`,
`--serve`, `--render`, `--blender`. Use `BLENDER=/path/to/blender` or `--blender`
if Blender is not on `PATH`.

`--faces N` (alias `--vertices N`) sets the mesh simplification target — the
output poly/vertex budget (default 5000, the workflow's Target Face Number).

With `--serve` the workflow is **loaded, not run**: the UI workflow
(`workflows/zimage_trellis2gguf_game_asset.json`, derived from `--workflow`) is
filled in with the same prompt/style/name/seed and uploaded to the remote
ComfyUI's userdata, so it appears in the web UI's workflow menu. Open
`http://delphi:8188`, load the named workflow, edit it, and run it there. Nothing
is queued and no local files are written:

```bash
python generate_asset.py "a rusty medieval lantern" --name lantern --serve
```

### Sound effects (`generate_sound.py`)

Generate sound effects / short audio samples from a text prompt:

| `--model` | Model | Notes |
|-----------|-------|-------|
| `sao` (default) | Stable Audio Open 1.0 | best all-round foley/SFX; band-limited to ~16 kHz |
| `sa3` | Stable Audio 3 Small-SFX | smaller/faster SFX specialist; can be quiet/dull on diffuse sounds |

```bash
python generate_sound.py "kitty meowing, close-up, foley sound effect" --name cat_meow --duration 5 --wav
python generate_sound.py "rain hitting a window" --model sa3 --duration 12
python generate_sound.py "ui click, single" --duration 0.2   # sub-1s (generated at 1s, then trimmed)
```

Output (in `sounds/` by default): `sounds/<name>.flac` (+ `<name>.wav` with
`--wav`). Options: `--model`, `--duration` (sub-second supported), `--seed`,
`--steps`, `--cfg`, `--sampler`, `--scheduler`, `--negative`, `--style`,
`--batch`, `--wav`, `--set NODE.FIELD=VALUE` (override any workflow input),
`--list-models`. The negative prompt is empty by default: Stable Audio Open is
trained on literal descriptions, so keep prompts short and concrete.

### Speech / TTS (`generate_speech.py`)

Generate spoken dialogue with prosody control:

| `--model` | Model | Notes |
|-----------|-------|-------|
| `chatterbox` (default) | Chatterbox | `--exaggeration` (0.25-2.0) emotion/prosody dial; higher quality |
| `chatterbox-turbo` | Chatterbox Turbo | faster; inline emotion tags `[laugh] [sigh] [gasp] ...`; quieter output |

```bash
python generate_speech.py "The gate is locked. Find another way around." --name guard --exaggeration 0.5
python generate_speech.py "Watch out! Behind you!" --exaggeration 1.0 --name warn
python generate_speech.py "You'll never catch me now. [laugh]" --model chatterbox-turbo
python generate_speech.py "Welcome, traveller." --voice narrator.wav   # voice cloning
```

Output (in `speech/` by default): `speech/<name>.flac` (+ `.wav` with `--wav`).
Options: `--model`, `--exaggeration`, `--cfg-weight` (pace), `--temperature`,
`--top-k`, `--top-p`, `--repetition-penalty`, `--voice <file>`, `--seed`,
`--wav`, `--set NODE.FIELD=VALUE`, `--list-models`. Prosody tips are in the
tool's `--help`.

### Batch generation (`bake.py`)

`bake.py` runs a JSON list of tasks in order, driving `generate_asset.py` or
`generate_sound.py` for each and copying the final artifact to a target path.
The file is either a bare task array or an object with named style prompts:

```json
{
  "asset_styles": {"voxel": "voxel art. 16 color palette. vibrant."},
  "sound_styles": {"foley": "close-up foley, dry recording"},
  "tasks": [
    {"label": "chest", "tool": "asset",
     "prompt": "low poly treasure chest, 16 color palette",
     "output": "build/models/chest.glb", "args": {"render": true}},
    {"label": "chest_open", "tool": "sound",
     "prompt": "wooden chest lid creaking open, foley",
     "output": "build/sfx/chest_open.flac", "args": {"duration": 3}}
  ]
}
```

```bash
python bake.py assets.json                       # run all (skips existing outputs)
python bake.py assets.json --list                # show task indices/labels
python bake.py assets.json --list-styles         # show named style prompts
python bake.py assets.json --dry-run             # print the generator commands
python bake.py assets.json --only 'chest*'       # re-run by label glob
python bake.py assets.json --index 1,3-5 --force # re-run by index, overwrite
python bake.py assets.json --style voxel --force # re-render all in one style
```

Each task needs `tool`, `prompt` and `output`; `label` and `args` are optional.
`output` ends in `.glb` for assets and `.flac`/`.wav` for sounds (`.wav` is
transcoded and needs `soundfile` or `ffmpeg`). `args` values become CLI flags
(`{"no_style": true}` -> `--no-style`). `--style NAME` applies a named prompt
from `asset_styles`/`sound_styles` to every selected task whose section defines
it, overriding per-task styles. Existing outputs are skipped unless `--force` is
given; failures stop the run unless `--keep-going`. See `bake-skill.md` for the
full reference. Only the `asset` and `sound` tools are batched; run
`generate_speech.py` directly for TTS.

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

To run inside ComfyUI's web UI instead, open `http://delphi:8188` and drag any
UI workflow from `workflows/` onto the canvas — `zimage_trellis2gguf_game_asset.json`
(3D), `stableaudio_sfx.json` / `stableaudio_sfx_sa3.json` (sound), or
`chatterbox_speech.json` / `chatterbox_speech_turbo.json` (speech) — then edit
the prompt node and run. Each pipeline also ships an `*_api.json` graph (same
graph, API format) that the generators submit. For the 3D pipeline,
`generate_asset.py --serve` automates the load step: it fills in the prompt and
uploads the UI workflow into your ComfyUI workflow list instead of running it.

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
  simplify target 5000 faces (`--faces`), `low_vram=True`. Typical runtime 50-140
  s per asset on `delphi`.
- **Sound effects** – core ComfyUI audio nodes only (`CLIPLoader` type
  `stable_audio`, `EmptyLatentAudio`, `KSampler`, `VAEDecodeAudio`,
  `TrimAudioDuration`, `SaveAudioAdvanced`). Default `sao` = Stable Audio Open
  1.0 (T5 text encoder); `sa3` = Stable Audio 3 Small-SFX base (T5Gemma
  encoder, `lcm`/`simple` 8 steps). Output is 44.1 kHz stereo FLAC; clips under
  1 s are generated at the 1 s latent minimum and trimmed.
- **Speech (TTS)** – Chatterbox (Resemble AI) via the `ComfyUI_Fill-ChatterBox`
  custom node pack (`FL_ChatterboxTTS` / `FL_ChatterboxTurboTTS`), installed and
  model-prefetched by `setup.sh`. `exaggeration` is the prosody/emotion control;
  the Turbo model adds inline tags (`[laugh]`, `[sigh]`, ...). Optional
  zero-shot voice cloning from a short reference clip.

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
- The audio pipelines need no extra system packages: sound uses core audio
  nodes with the bundled PyAV codec, and speech uses the Chatterbox pack whose
  Python deps (`librosa`, `s3tokenizer`, ...) `setup.sh` installs into the
  bind-mounted venv. Model weights are pre-fetched under
  `~/.local/share/ComfyUI/models/` (`checkpoints/`, `text_encoders/`,
  `chatterbox/{chatterbox,chatterbox_turbo}`).

## Troubleshooting

- `pip`/models: run `./setup.sh` again (idempotent).
- Check the API: `curl "http://${COMFY_REMOTE_HOST:-delphi}:${COMFY_REMOTE_PORT:-8188}/system_stats"`.
- Logs: `ssh "${COMFY_REMOTE_USER:-febret}@${COMFY_REMOTE_HOST:-delphi}" "podman logs --tail 100 comfyui-${COMFY_REMOTE_PORT:-8188}"`.
- Service: `ssh "${COMFY_REMOTE_USER:-febret}@${COMFY_REMOTE_HOST:-delphi}" "systemctl --user status comfyui@${COMFY_REMOTE_PORT:-8188}.service"`.
- TRELLIS.2 GGUF weights download automatically on the first generation into
  `models/Trellis2/` (~4.5 GB for Q8_0).
- Sound/speech models are downloaded by `./setup.sh` (~5.8 GB Stable Audio
  Open 1.0 + ~3.5 GB Stable Audio 3 Small-SFX + ~5.6 GB Chatterbox standard and
  Turbo). If a Chatterbox model is missing it auto-downloads from
  `ResembleAI/chatterbox[-turbo]` on first use.
- Chatterbox nodes missing from the API? Re-run `./setup.sh` (it clones the
  pack and installs its deps in step **3b**) and check
  `/object_info` for `FL_ChatterboxTTS`.
