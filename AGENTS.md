# AGENTS.md

Guidance for automated agents working in `delphi-comfyui`.

## What this repo is

Drives a **remote ComfyUI** instance on the AMD `delphi` machine (ROCm) to
generate game assets: **text prompt -> 2D image -> textured low-poly 3D model
(GLB)**, **text prompt -> high quality image (PNG)**, **text prompt -> sound
effect (FLAC)**, and **text -> speech / spoken dialogue (TTS, FLAC)**. There is
no local ML stack; everything executes on `delphi:8188`.

## Golden rules

- Do not commit or create new artifacts; only the files listed below are
  intentional. Do not add experimental scripts or extra workflow JSONs.
- Prefer editing the existing files. The pipeline is validated end-to-end.
- Never use `GGUF Q4_K_M` for TRELLIS.2 (corrupt dequant -> noise textures).
  Use `Q8_0` / BF16.
- Any change to a workflow must be reflected in **both** its UI and API JSONs,
  and the node-id constants in the corresponding generator must stay in sync
  (for the 3D workflow also `UI_WIDGETS`, the `widgets_values` indices that
  `generate_asset.py --serve` edits in the interactive graph):
  `workflows/zimage_trellis2gguf_game_asset{,_api}.json` <->
  `generate_asset.py`; `workflows/stableaudio_sfx{,_api}.json` and
  `workflows/stableaudio_sfx_sa3{,_api}.json` <-> `generate_sound.py`;
  `workflows/chatterbox_speech{,_api}.json` and
  `workflows/chatterbox_speech_turbo{,_api}.json` <-> `generate_speech.py`;
  `workflows/pony_sdxl{,_api}.json` and
  `workflows/noobai_xl_vpred{,_api}.json` <-> `generate_image.py` (node ids live
  in the `workflow.json` registry, not hardcoded).
- Image generation is driven by a **registry** (`workflow.json` at the repo
  root): each workflow id names the API/UI JSON under `workflows/`, the node ids
  the script edits, defaults, and the models it needs (URL + `models/`
  destination). `generate_image.py` installs missing models on the remote host
  via ssh + curl before running. Keep the registry, the workflow JSONs, and the
  script in sync.
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
                      --image: skip text-to-image and feed a supplied image into
                      the 3D stage instead (prompt then optional)
                      --serve: load the prompt-filled UI workflow into ComfyUI's
                      userdata (workflows/) for interactive use; do not run it
generate_image.py     prompt -> images/<name>.png using a diffusion model
                      selected from workflow.json (registry); installs missing
                      models on delphi via ssh + curl; --upscale (4x model),
                      --serve (load prompt-filled UI workflow; do not run)
generate_sound.py     prompt -> sounds/<name>.flac (+ --wav via local soundfile/ffmpeg)
generate_speech.py    text -> speech/<name>.flac (TTS; prosody + voice cloning)
bake.py               batch runner: JSON task list -> many assets/images/sounds at
                       target paths (+ named styles via --style, + TEMPLATE entries)
mesh_simplify.py       GLB complexity analyzer + optional bake postprocessing API/CLI
mesh_simplify_blender.py Blender worker: decimation or retopo/UV/texture rebake
bake_image_demo.json  example bake file: one character, many expression/pose
                      variants, built from reusable templates
workflow.json         image workflow registry: workflow ids -> API/UI JSON, node
                      ids, defaults, and model URLs (checkpoints + upscalers)
bake-skill.md         usage reference for bake.py (batch generation)
setup.sh              idempotent provisioning of remote ComfyUI (software + models)
render_glb.py         Blender headless GLB renderer
compare_images.py     HSV-histogram comparison + montage
workflows/            the workflow JSONs (UI + API) for all pipelines (3D, image, sound, speech)
samples/              example outputs (do not regenerate unless asked)
demo_sounds/          sao vs sa3 model comparison outputs + COMMENTARY.md
demo-speech/          chatterbox vs turbo TTS samples + COMMENTARY.md
```

## Run / verify

```bash
bash -n setup.sh                          # syntax check
python -m py_compile generate_asset.py generate_image.py generate_sound.py generate_speech.py bake.py
./setup.sh                                # (re)provision remote; safe to re-run
python generate_asset.py "<prompt>" --name x --render
python generate_asset.py "<prompt>" --name x --faces 2000  # mesh simplify target (poly budget)
python generate_asset.py "<prompt>" --name x --serve  # load UI workflow into ComfyUI; do not run
python generate_asset.py --image concept.png --name x --render  # skip text-to-image, use image
python generate_asset.py --image concept.png --name x --serve   # load image-wired UI workflow
python generate_image.py "a red fox in autumn leaves" --name fox
python generate_image.py "a harbour town" --resolution 1344x768 --upscale   # 4x upscale model
python generate_image.py "a village" --workflow default_imagegen --serve    # load UI workflow; do not run
python generate_image.py --list-workflows   # registry ids; --list-upscalers for upscale models
python generate_sound.py "kitty meowing, foley" --name cat_meow --duration 5 --wav
python generate_sound.py "rain on a window" --model sa3 --duration 12   # alternative model
python generate_sound.py "ui click, single" --duration 0.2             # sub-1s (trimmed)
python generate_speech.py "The gate is locked. Find another way around." --name guard
python generate_speech.py "You'll never catch me now. [laugh]" --model chatterbox-turbo
python bake.py tasks.json                 # batch: run all, skip existing outputs
python bake.py tasks.json --list --dry-run # inspect tasks + commands without running
python bake.py bake_image_demo.json --list-templates   # reusable prompt/args templates
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
`generate_asset.py`, `generate_image.py`, `generate_sound.py`,
`generate_speech.py` and `setup.sh` read these:
`COMFY_REMOTE_HOST` (`delphi`), `COMFY_REMOTE_PORT` (`8188`),
`COMFY_REMOTE_USER` (`febret`). `setup.sh` derives the ssh target
`${COMFY_REMOTE_USER}@${COMFY_REMOTE_HOST}` and the systemd unit
`comfyui@${COMFY_REMOTE_PORT}.service`. `generate_image.py` also reads
`COMFY_REMOTE_MODELS` (remote models dir; default
`$HOME/.local/share/ComfyUI/models`) for on-demand model installs.

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
`171` PrimitiveInt (mesh simplify target face count; `--faces`/`--vertices`) ·
`10` Preview3D (exposes the exported GLB filename).

With `--image`, `generate_asset.py` drops the Z-Image chain + its output nodes
(`401`-`411`), injects a `LoadImage` node (`416`; runtime-only, not in the
JSONs, like `generate_speech.py`'s `LoadAudio`), and points the existing
background-removal/matte stage (`413` RemoveBackground, `414` JoinImageWithAlpha)
at it. `--serve --image` mirrors this in the UI graph: it injects `416`, rewires
links into `413`/`414`/`410` PreviewImage/`411` SaveImage, and mutes `401`-`409`.

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

`pony_sdxl_api.json` (used by `generate_image.py`, workflow id
`default_imagegen` in the `workflow.json` registry):

`4` CheckpointLoaderSimple (`ponyDiffusionV6XL_v6StartWithThisOne.safetensors`) ·
`10` CLIPSetLastLayer (`stop_at_clip_layer: -2`, required by Pony) ·
`6` CLIPTextEncode (positive prompt) · `7` CLIPTextEncode (negative) ·
`5` EmptyLatentImage (width/height/batch) · `3` KSampler (seed/steps/cfg/
sampler/scheduler/denoise) · `8` VAEDecode · `9` SaveImage (PNG prefix).
Optional upscale branch: `20` UpscaleModelLoader · `21` ImageUpscaleWithModel ·
`22` ImageScaleBy (final resize). The API JSON leaves `9` wired to `8` (the
upscale nodes are present but unreachable) until `--upscale`, when the script
wires `9.images` through `22`; the UI JSON has `20/21/22` in bypass (`mode: 4`)
by default and `--serve --upscale` un-bypasses them. Upscale models come from
the registry's `upscalers` catalog (`--list-upscalers`; default `4x-UltraSharp`,
native 4x, `--upscale-scale` sets a final multiplier). `generate_image.py`
config: `--workflow`, `--resolution WxH`/`--width`/`--height`, `--seed`,
`--negative`, `--steps`, `--cfg`, `--sampler`, `--scheduler`, `--denoise`,
`--batch`, `--clip-skip`, `--prefix`/`--style`/`--no-prompt-template`,
`--upscale*`, `--out`, `--no-download`, and `--set NODE.FIELD=VALUE`.

`noobai_xl_vpred_api.json` (used by `generate_image.py`, workflow id
`noobai_xl_vpred` in the registry):

`4` CheckpointLoaderSimple (`NoobAI-XL-Vpred-v1.0.safetensors`) · `2`
ModelSamplingDiscrete (**`sampling: v_prediction`, `zsnr: true`** — this is a
v-prediction/ZSNR checkpoint; do not remove it or use eps sampling) · `10`
CLIPSetLastLayer (`-2`) · `6`/`7` CLIPTextEncode (prompt / negative) · `5`
EmptyLatentImage · `3` KSampler (`euler` + `normal`, CFG 4-5, 28-35 steps) · `8`
VAEDecode · `9` SaveImage · upscale branch `20`/`21`/`22` as above. The UI JSON
mirrors it (node `2` present, KSampler wired `4 -> 2 -> 3`). Prompt prefix:
`masterpiece, best quality, newest, absurdres, highres, safe`; a negative prompt
is set by default. Model is ~7.1 GB (repo `Laxhar/noobai-XL-Vpred-1.0`), NSFW
flagged but not gated.

## Typical timings

Z-Image ~20 s; structure+shape ~10-30 s; texture 512 @12 steps ~60-70 s;
total ~50-140 s per asset once models are warm. First run also downloads the
TRELLIS.2 GGUF weights to `models/Trellis2/`.

Image: Pony Diffusion V6 XL (SDXL), 25-28 steps at 1024px, ~15-30 s per image
once the ~6.9 GB checkpoint is warm (first run downloads it). A 4x upscale adds
a few seconds (tiled, memory-safe); native 4x turns a 1024px image into 4096px.
First upscaled run also downloads the ~67 MB upscale model. NoobAI XL V-Pred 1.0
is another ~7.1 GB SDXL checkpoint with the same per-image cost (Euler, 28 steps).

Sound: Stable Audio Open 1.0, 50 steps, ~5-15 s for an 8 s clip once the model
is warm (first run loads the ~4.9 GB checkpoint + ~0.9 GB t5-base). Stable
Audio 3 Small-SFX, 8 steps, faster (~3.5 GB total). SAO is the better/more
consistent default for game SFX; SA3 is smaller and occasionally better for
low-frequency sounds, but without its Qwen reprompt stage it is quiet/dull on
diffuse sounds (rain, ambience).

Speech: Chatterbox runs in a few seconds per line on GPU once loaded (first run
loads the model; `keep_model_loaded` is enabled so repeats are fast). Turbo is
the faster variant; standard is higher quality.
