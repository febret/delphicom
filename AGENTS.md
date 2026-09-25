# AGENTS.md

Guidance for automated agents working in `delphi-comfyui`.

## What this repo is

Drives a **remote ComfyUI** instance on the AMD `delphi` machine (ROCm) to
generate game assets: **text prompt -> 2D image -> textured low-poly 3D model
(GLB)**, **text prompt -> sound effect (FLAC)**, and **text -> speech / spoken
dialogue (TTS, FLAC)**. There is no local ML stack; everything executes on
`delphi:8188`.

## Golden rules

- Do not commit or create new artifacts; only the files listed below are
  intentional. Do not add experimental scripts or extra workflow JSONs.
- Prefer editing the existing files. The pipeline is validated end-to-end.
- Never use `GGUF Q4_K_M` for TRELLIS.2 (corrupt dequant -> noise textures).
  Use `Q8_0` / BF16.
- Any change to a workflow must be reflected in **both** its UI and API JSONs,
  and the node-id constants in the corresponding generator must stay in sync:
  `workflows/zimage_trellis2gguf_game_asset{,_api}.json` <->
  `generate_asset.py`; `workflows/stableaudio_sfx{,_api}.json` and
  `workflows/stableaudio_sfx_sa3{,_api}.json` <-> `generate_sound.py`;
  `workflows/chatterbox_speech{,_api}.json` and
  `workflows/chatterbox_speech_turbo{,_api}.json` <-> `generate_speech.py`.
- Sound generation uses **core ComfyUI audio nodes only** (`CLIPLoader` type
  `stable_audio`, `EmptyLatentAudio`, `KSampler`, `VAEDecodeAudio`,
  `TrimAudioDuration`, `SaveAudioAdvanced`). Two models are wired:
  **Stable Audio Open 1.0** (`--model sao`, default) and **Stable Audio 3
  Small-SFX base** (`--model sa3`). Keep model files + defaults in sync with
  `setup.sh`; do not switch to GGUF-style quantizations without updating it.
- Speech generation uses the **Chatterbox** custom node pack
  (`ComfyUI_Fill-ChatterBox`; nodes `FL_ChatterboxTTS` /
  `FL_ChatterboxTurboTTS`), installed + model-prefetched by `setup.sh`.
  `exaggeration` is the prosody/emotion control; the Turbo model adds inline
  tags (`[laugh]`, `[sigh]`, ...). Keep models in `setup.sh` in sync.

## Layout

```
generate_asset.py     prompt -> assets/<name>_2d.png + assets/<name>.glb (+ --render)
generate_sound.py     prompt -> sounds/<name>.flac (+ --wav via local soundfile/ffmpeg)
generate_speech.py    text -> speech/<name>.flac (TTS; prosody + voice cloning)
bake.py               batch runner: JSON task array -> many assets/sounds at target paths
bake-skill.md         usage reference for bake.py (batch asset generation)
setup.sh              idempotent provisioning of remote ComfyUI (software + models)
render_glb.py         Blender headless GLB renderer
compare_images.py     HSV-histogram comparison + montage
workflows/            the workflow JSONs (UI + API) for all pipelines (3D, sound, speech)
samples/              example outputs (do not regenerate unless asked)
demo_sounds/          sao vs sa3 model comparison outputs + COMMENTARY.md
demo-speech/          chatterbox vs turbo TTS samples + COMMENTARY.md
```

## Run / verify

```bash
bash -n setup.sh                          # syntax check
python -m py_compile generate_asset.py generate_sound.py generate_speech.py bake.py
./setup.sh                                # (re)provision remote; safe to re-run
python generate_asset.py "<prompt>" --name x --render
python generate_sound.py "kitty meowing, foley" --name cat_meow --duration 5 --wav
python generate_sound.py "rain on a window" --model sa3 --duration 12   # alternative model
python generate_sound.py "ui click, single" --duration 0.2             # sub-1s (trimmed)
python generate_speech.py "The gate is locked. Find another way around." --name guard
python generate_speech.py "You'll never catch me now. [laugh]" --model chatterbox-turbo
python bake.py tasks.json                 # batch: run all, skip existing outputs
python bake.py tasks.json --list --dry-run # inspect tasks + commands without running
python render_glb.py <glb> <png> 512 35 22   # via: blender --background --python ...
python compare_images.py <ref.png> <render.png> <montage.png>
```

Verify remotely (defaults shown; `${COMFY_REMOTE_*}` override):

```bash
curl -s "http://${COMFY_REMOTE_HOST:-delphi}:${COMFY_REMOTE_PORT:-8188}/system_stats"
ssh "${COMFY_REMOTE_USER:-febret}@${COMFY_REMOTE_HOST:-delphi}" "systemctl --user status comfyui@${COMFY_REMOTE_PORT:-8188}.service"
ssh "${COMFY_REMOTE_USER:-febret}@${COMFY_REMOTE_HOST:-delphi}" "podman logs --tail 100 comfyui-${COMFY_REMOTE_PORT:-8188}"
```

## Remote environment facts

Remote endpoint / ssh target are configurable (defaults below).
`generate_asset.py`, `generate_sound.py`, `generate_speech.py` and `setup.sh`
read these:
`COMFY_REMOTE_HOST` (`delphi`), `COMFY_REMOTE_PORT` (`8188`),
`COMFY_REMOTE_USER` (`febret`). `setup.sh` derives the ssh target
`${COMFY_REMOTE_USER}@${COMFY_REMOTE_HOST}` and the systemd unit
`comfyui@${COMFY_REMOTE_PORT}.service`.

- Host `delphi` = Strix Halo APU, gfx1151, PyTorch 2.12.0+rocm7.14.1.
- ComfyUI runs in the podman container `comfyui-8188` from image
  `oci-registry.ryai.dev/ryai-comfyui:trellis2` (a locally committed layer on
  top of the vendor image), via systemd user unit `comfyui@8188.service`.
- The container is **ephemeral** (recreated on restart). Persistent state lives
  in the bind mount `~/.local/share/ComfyUI` (base dir): `models/`, `code/`
  (ComfyUI v0.37.0 checkout), `custom_nodes/`, `venv/`.
- The systemd drop-in is
  `~/.config/containers/systemd/comfyui@.container.d/override.conf`; it pins the
  image, the entrypoint (`/opt/comfyui/comfyui.sh`), the code bind mount and env
  (`TORCH_BLAS_PREFER_HIPBLASLT=0`, `TRITON_CACHE_DIR`).
- Core ComfyUI is updated only via the `code/` bind mount; the vendor container
  also uses systemd **socket activation**, so `server.py` must keep the
  socket-activation patch (applied by `setup.sh`).
- `main.py` starts with an early nvdiffrast/EGL context; removing it makes
  texturing abort the process (`Fatal Python error: Aborted`).

## API workflow node ids

`zimage_trellis2gguf_game_asset_api.json` (used by `generate_asset.py`):

`405` CLIPTextEncode (prompt) · `408` Z-Image KSampler (seed) ·
`45` mesh generator (seed) · `163` texturing (seed) ·
`166` PrimitiveString (export name) · `411` SaveImage (2D prefix) ·
`10` Preview3D (exposes the exported GLB filename).

`stableaudio_sfx_api.json` / `stableaudio_sfx_sa3_api.json` (used by
`generate_sound.py`, selectable via `--model sao|sa3`):

`6` CLIPTextEncode (positive prompt) · `7` CLIPTextEncode (negative) ·
`3` KSampler (seed/steps/cfg/sampler/scheduler) · `11` EmptyLatentAudio
(duration seconds; min 1s) · `21` TrimAudioDuration (final duration; enables
sub-1s output) · `20` SaveAudioAdvanced (FLAC, filename prefix).
Loaders: `4` CheckpointLoaderSimple (`stable-audio-open-1.0.safetensors` for
sao, `stable_audio_3_small_sfx_base.safetensors` for sa3) · `10` CLIPLoader
(`t5-base.safetensors` for sao, `t5gemma_b_b_ul2.safetensors` for sa3, both
type `stable_audio`). The sa3 graph adds `8` ConditioningStableAudio
(seconds_total = duration). `SaveAudioAdvanced.format` is a dynamic combo
stored flat (`"format": "flac"`).

`generate_sound.py` config: `--duration` (sub-second supported), `--steps`,
`--cfg`, `--sampler`, `--scheduler`, `--negative`, `--style`, `--batch`, and a
generic `--set NODE.FIELD=VALUE` override for any workflow input. sa3 defaults:
`lcm`/`simple`, 8 steps, cfg 3 (50 steps with `lcm` corrupts output to a
constant).

`chatterbox_speech_api.json` / `chatterbox_speech_turbo_api.json` (used by
`generate_speech.py`, `--model chatterbox|chatterbox-turbo`):

`4` FL_ChatterboxTTS / FL_ChatterboxTurboTTS (text, `exaggeration`,
`cfg_weight`, `temperature`, `seed`, optional `audio_prompt`) · `5`
SaveAudioAdvanced (FLAC). `generate_speech.py` config: `--exaggeration`,
`--cfg-weight`, `--temperature`, `--top-k`, `--top-p`,
`--repetition-penalty`, `--voice <file>` (voice cloning via an injected
`LoadAudio` node), and `--set NODE.FIELD=VALUE`. Models live in
`models/chatterbox/{chatterbox,chatterbox_turbo}` (auto-downloaded from
`ResembleAI/chatterbox[-turbo]`).

## Typical timings

Z-Image ~20 s; structure+shape ~10-30 s; texture 512 @12 steps ~60-70 s;
total ~50-140 s per asset once models are warm. First run also downloads the
TRELLIS.2 GGUF weights to `models/Trellis2/`.

Sound: Stable Audio Open 1.0, 50 steps, ~5-15 s for an 8 s clip once the model
is warm (first run loads the ~4.9 GB checkpoint + ~0.9 GB t5-base). Stable
Audio 3 Small-SFX, 8 steps, faster (~3.5 GB total). SAO is the better/more
consistent default for game SFX; SA3 is smaller and occasionally better for
low-frequency sounds, but without its Qwen reprompt stage it is quiet/dull on
diffuse sounds (rain, ambience).

Speech: Chatterbox runs in a few seconds per line on GPU once loaded (first run
loads the model; `keep_model_loaded` is enabled so repeats are fast). Turbo is
the faster variant; standard is higher quality.
