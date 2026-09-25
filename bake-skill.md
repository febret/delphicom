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

A JSON **array** of task objects. Each object:

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

### `args` mapping

Keys are converted to CLI flags: `render` -> `--render`, `no_style` ->
`--no-style`. Boolean `true` adds the flag, `false`/`null` omits it; lists repeat
the flag; other values become `--key value`.

Per-tool options:

- **asset** (`generate_asset.py`): `render`, `seed`, `timeout`, `host`,
  `workflow`, `blender`.
- **sound** (`generate_sound.py`): `duration`, `seed`, `steps`, `negative`,
  `no_style`, `timeout`, `host`, `workflow`, and `wav` (auto-enabled when the
  target ends in `.wav`).

> Sound note: a `.wav` target requires the transcode step to work, i.e.
> `soundfile` or `ffmpeg` available on the machine running bake. If neither is
> present the task fails rather than mislabeling a FLAC as WAV.

## Example task file (`assets.json`)

```json
[
  {"label": "treasure_chest", "tool": "asset",
   "prompt": "low poly treasure chest, 16 color palette, vibrant colors",
   "output": "build/models/treasure_chest.glb", "args": {"render": true, "seed": 42}},

  {"label": "wooden_barrel", "tool": "asset",
   "prompt": "wooden barrel, game asset",
   "output": "build/models/wooden_barrel.glb"},

  {"label": "chest_open", "tool": "sound",
   "prompt": "wooden chest lid creaking open, foley",
   "output": "build/sfx/chest_open.flac", "args": {"duration": 3}},

  {"label": "coin_pickup", "tool": "sound",
   "prompt": "bright coin pickup chime, short",
   "output": "build/sfx/coin_pickup.wav", "args": {"duration": 1.5, "steps": 30}}
]
```

## Usage

```bash
# run every task, skipping ones whose output already exists
python bake.py assets.json

# preview
python bake.py assets.json --list
python bake.py assets.json --dry-run

# re-run a subset by label glob and/or 1-based index
python bake.py assets.json --only 'treasure*' --only coin_pickup
python bake.py assets.json --index 1,3-5

# force regeneration and keep going past failures
python bake.py assets.json --only 'chest*' --force --keep-going
```

Options: `--only GLOB` (repeatable, matches label/output/basename/stem),
`--index SPEC` (repeatable, `1,3-5`), `--list`, `--dry-run`, `--force`,
`--keep-going`. Exit code is `0` when nothing failed, `1` if a task failed, `2`
for bad input/selection.

## How it works

1. Validates and normalizes every task (tool, prompt, output extension, args).
2. For each selected task: creates a temp staging dir, runs
   `python generate_*.py <prompt> --name <stem> --outdir <staging> <args>`,
   then copies the produced `<stem>.<ext>` to the target path.
3. Staging dirs are always removed; only the final artifact is copied.
4. The output extension must match what the target asks for. A `.glb` task
   copies the GLB; a `.flac`/`.wav` task copies the audio of that exact type.

Tasks run sequentially (ComfyUI queues them). The remote endpoint honors
`COMFY_REMOTE_HOST` / `COMFY_REMOTE_PORT`; pass `--host` per task via `args`;
bake itself forwards nothing global.

## Recommended workflow

1. Check the remote is up: `curl -s "http://delphi:8188/system_stats"`.
2. `python bake.py assets.json --list` to confirm ordering/labels.
3. `python bake.py assets.json --dry-run` to inspect commands.
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
