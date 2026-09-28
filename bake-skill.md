---
name: bake
description: Use bake.py to batch-generate game assets (2D+3D GLB models and sound-effect FLAC/WAV files) on the remote delphi ComfyUI from a JSON task list, with progress reporting and per-task selection. Trigger when the user wants to generate multiple assets/sounds at once, author or edit a bake task file, or re-run only some tasks.
---

# bake skill

`bake.py` runs a **JSON list of generation tasks** in order against the remote
ComfyUI instance and copies each result to its target path. It is a thin,
deterministic driver over `generate_asset.py` and `generate_sound.py`.

Use it whenever more than one asset/sound is needed, when outputs must land at
stable, known paths (e.g. for a game build), or when a subset of a batch must be
regenerated.

## Task file format

The file is either a **bare JSON array** of task objects (legacy), or an object
holding optional named style prompts plus the task array:

```json
{
  "asset_styles": {"voxel": "voxel art. 16 color palette. vibrant."},
  "sound_styles": {"retro": "chiptune, 8-bit", "foley": "close-up foley"},
  "tasks": [ ... ]
}
```

`asset_styles` are used by asset tasks, `sound_styles` by sound tasks. Each is a
map of `name` -> prompt string. Select one at run time with `--style NAME` to
override the style used by every selected task (see below). A bare array is
still accepted; it simply has no named styles.

Each task object:

| Field | Required | Meaning |
|-------|----------|---------|
| `label` | no | Short name for progress + `--only` selection. Default: output stem. |
| `tool` | yes | `asset` / `generate_asset` / `generate_asset.py` or `sound` / `generate_sound` / `generate_sound.py`. |
| `prompt` | yes | Text prompt passed to the generator. |
| `output` | yes | Target path. `.glb` for assets, `.flac` or `.wav` for sounds. |
| `args` | no | Extra CLI flags for the generator (see below). |

`output` may be relative (resolved against the current directory) or absolute.
Parent directories are created automatically. `name` and `outdir` are reserved
and cannot be set in `args` (bake controls them).

### Named styles

Define one or more prompts per section and pick one when running:

```bash
python bake.py assets.json --list-styles          # show available names
python bake.py assets.json --style voxel          # apply to every selected task
python bake.py assets.json --style voxel --force  # re-render everything in that style
```

`--style NAME` looks the name up in the section matching each task's tool and
**overrides any per-task `args.style`** (global wins). A task whose section does
not define the name is left unchanged; the run fails only if no selected task
can use the name. The prompt replaces the generator's built-in style suffix for
assets (`generate_asset.py --style`) and is appended for sounds
(`generate_sound.py --style`).

### `args` mapping

Keys are converted to CLI flags: `render` -> `--render`, `no_style` ->
`--no-style`. Boolean `true` adds the flag, `false`/`null` omits it; lists repeat
the flag; other values become `--key value`.

Per-tool options:

- **asset** (`generate_asset.py`): `render`, `seed`, `faces`, `style`,
  `no_style`, `timeout`, `host`, `workflow`, `blender`.
- **sound** (`generate_sound.py`): `duration`, `seed`, `steps`, `negative`,
  `style`, `no_style`, `timeout`, `host`, `workflow`, and `wav` (auto-enabled
  when the target ends in `.wav`).

`faces` (alias `vertices`) sets the asset's mesh simplification target — the
output poly/vertex budget — e.g. `{"faces": 2000}` for a chunkier low-poly model
or `{"faces": 20000}` for a detailed one. It maps to `generate_asset.py --faces`
(default 5000, the workflow's Target Face Number). Use it to control mesh density
per task; raise it for hero assets and lower it for distant props.

> Sound note: a `.wav` target requires the transcode step to work, i.e.
> `soundfile` or `ffmpeg` available on the machine running bake. If neither is
> present the task fails rather than mislabeling a FLAC as WAV.

## Example task file (`assets.json`)

```json
{
  "asset_styles": {
    "voxel": "voxel art. 16 color palette. vibrant colors.",
    "pixel": "pixel art. crisp edges. limited palette."
  },
  "sound_styles": {
    "foley": "close-up foley, dry recording",
    "retro": "chiptune, 8-bit game audio"
  },
  "tasks": [
    {"label": "treasure_chest", "tool": "asset",
     "prompt": "low poly treasure chest, 16 color palette, vibrant colors",
     "output": "build/models/treasure_chest.glb", "args": {"render": true, "seed": 42}},

    {"label": "wooden_barrel", "tool": "asset",
     "prompt": "wooden barrel, game asset",
     "output": "build/models/wooden_barrel.glb", "args": {"faces": 3000}},

    {"label": "chest_open", "tool": "sound",
     "prompt": "wooden chest lid creaking open, foley",
     "output": "build/sfx/chest_open.flac", "args": {"duration": 3}},

    {"label": "coin_pickup", "tool": "sound",
     "prompt": "bright coin pickup chime, short",
     "output": "build/sfx/coin_pickup.wav", "args": {"duration": 1.5, "steps": 30}}
  ]
}
```

## Usage

```bash
# run every task, skipping ones whose output already exists
python bake.py assets.json

# preview
python bake.py assets.json --list
python bake.py assets.json --list-styles
python bake.py assets.json --dry-run
python bake.py assets.json --dry-run --style voxel   # inspect the chosen style

# re-run a subset by label glob and/or 1-based index
python bake.py assets.json --only 'treasure*' --only coin_pickup
python bake.py assets.json --index 1,3-5

# re-render everything in one named style, overwriting existing outputs
python bake.py assets.json --style voxel --force

# force regeneration and keep going past failures
python bake.py assets.json --only 'chest*' --force --keep-going
```

Options: `--only GLOB` (repeatable, matches label/output/basename/stem),
`--index SPEC` (repeatable, `1,3-5`), `--style NAME`, `--list`, `--list-styles`,
`--dry-run`, `--force`, `--keep-going`. Exit code is `0` when nothing failed, `1`
if a task failed, `2` for bad input/selection.

## How it works

1. Validates and normalizes every task (tool, prompt, output extension, args)
   and any named style maps.
2. For each selected task: creates a temp staging dir, runs
   `python generate_*.py <prompt> --name <stem> --outdir <staging> <args>`,
   then copies the produced `<stem>.<ext>` to the target path.
3. Staging dirs are always removed; only the final artifact is copied.
4. The output extension must match what the target asks for. A `.glb` task
   copies the GLB; a `.flac`/`.wav` task copies the audio of that exact type.
5. `--style NAME` is resolved per selected task before running; the chosen prompt
   is injected as the task's `style` arg, overriding any per-task value.

Tasks run sequentially (ComfyUI queues them). The remote endpoint honors
`COMFY_REMOTE_HOST` / `COMFY_REMOTE_PORT`; pass `--host` per task via `args`;
bake itself forwards nothing global.

## Recommended workflow

1. Check the remote is up: `curl -s "http://delphi:8188/system_stats"`.
2. `python bake.py assets.json --list` to confirm ordering/labels; add
   `--list-styles` to see the named style prompts.
3. `python bake.py assets.json --dry-run` to inspect commands; add
   `--style NAME` to preview the injected style.
4. Run for real. Everything is skipped if outputs already exist, so re-runs are
   incremental; use `--force` or a narrowed `--only`/`--index` to redo.
5. First run also downloads TRELLIS.2 GGUF weights; assets take ~50-140 s each,
   sounds ~5-15 s each once models are warm.

## Guardrails

- Do not use `GGUF Q4_K_M` for TRELLIS.2 (noise textures); the workflows are
  fixed so bake inherits the safe defaults.
- Only the final artifact is copied; use the generator's own flags (e.g. asset
  `render`) if you need extra outputs.
- Do not hand-edit generated outputs; re-run the relevant task instead.
