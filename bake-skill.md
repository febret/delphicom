---
name: bake
description: Use bake.py to batch-generate game content (2D+3D GLB models, sound-effect FLAC/WAV files, and diffusion images) on the remote delphi ComfyUI from a JSON task list, with progress reporting, per-task selection, and reusable prompt/args templates. Trigger when the user wants to generate multiple assets/sounds/images at once, author or edit a bake task file, or re-run only some tasks.
---

# bake skill

`bake.py` runs a **JSON list of generation tasks** in order against the remote
ComfyUI instance and copies each result to its target path. It is a thin,
deterministic driver over `generate_asset.py`, `generate_sound.py` and
`generate_image.py`.

Use it whenever more than one artifact is needed, when outputs must land at
stable, known paths (e.g. for a game build), or when a subset of a batch must be
regenerated.

## Task file format

The file is either a **bare JSON array** of entries, or an object holding
optional named style prompts, reusable templates, and the task array:

```json
{
  "asset_styles": {"voxel": "voxel art. 16 color palette. vibrant."},
  "sound_styles": {"foley": "close-up foley, dry recording"},
  "image_styles": {"illustration": "vibrant illustration, clean lines"},
  "tasks": [ ... ]
}
```

`asset_styles` are used by asset tasks, `sound_styles` by sound tasks, and
`image_styles` by image tasks. Each is a map of `name` -> prompt string. Select
one at run time with `--style NAME` to override the style used by every selected
task (see below). A bare array is still accepted; it simply has no named styles.

Each task object:

| Field | Required | Meaning |
|-------|----------|---------|
| `label` | no | Short name for progress + `--only` selection. Default: output stem. |
| `tool` | yes | `asset` / `sound` / `image` (aliases below), or `TEMPLATE` (see Templates). |
| `prompt` | yes | Text prompt passed to the generator. May contain `{{template}}` references. |
| `output` | yes | Target path. `.glb` assets, `.flac`/`.wav` sounds, `.png` images. |
| `args` | no | Extra CLI flags for the generator (see below). |
| `simplify` | no | Asset-only local mesh postprocessing: `true`, `false`, or an options object. |

Tool aliases: `asset`/`generate_asset`/`3d`/`model`, `sound`/`generate_sound`/`sfx`/`audio`,
`image`/`generate_image`/`img`.

`output` may be relative (resolved against the current directory) or absolute.
Parent directories are created automatically. `name` and `outdir` are reserved
and cannot be set in `args` (bake controls them). Image tasks also reject
`batch`/`batch_size` (bake copies only one artifact) and `serve` (queues nothing).

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
assets (`generate_asset.py --style`), is appended for sounds
(`generate_sound.py --style`) and images (`generate_image.py --style`).

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
- **image** (`generate_image.py`): `workflow`, `seed`, `negative`, `width`,
  `height`, `resolution` (`WxH`), `steps`, `cfg`, `sampler`, `scheduler`,
  `denoise`, `clip_skip`, `prefix`, `style`, `no_prompt_template`, `upscale`,
  `upscale_model`, `upscale_scale`, `upscale_method`, `host`, `timeout`.

`faces` (alias `vertices`) sets the asset's mesh simplification target — the
output poly/vertex budget — e.g. `{"faces": 2000}` for a chunkier low-poly model
or `{"faces": 20000}` for a detailed one. It maps to `generate_asset.py --faces`
(default 5000, the workflow's Target Face Number). Use it to control mesh density
per task; raise it for hero assets and lower it for distant props.

> Sound note: a `.wav` target requires the transcode step to work, i.e.
> `soundfile` or `ffmpeg` available on the machine running bake. If neither is
> present the task fails rather than mislabeling a FLAC as WAV.

## Optional local mesh simplification

Add document-level defaults beside `tasks` to simplify generated GLBs with
local Blender before publishing them to their output paths:

```json
{
  "simplify": {
    "strategy": "auto", "target": 500, "flat_target": 100,
    "wall_target": 200, "bake_resolution": 1024,
    "remesh_resolution": 160, "max_deviation": 0.05
  },
  "tasks": [
    {"label": "chair", "tool": "asset", "prompt": "a wooden chair",
     "output": "build/chair.glb", "args": {"faces": 5000}},
    {"label": "tree", "tool": "asset", "prompt": "an oak tree",
     "output": "build/tree.glb", "simplify": {"strategy": "decimate"}},
    {"label": "hero", "tool": "asset", "prompt": "an ornate statue",
     "output": "build/hero.glb", "simplify": false}
  ]
}
```

Simplification is opt-in. Task options merge over document defaults; a task's
`false` disables it. This is a task field, **not** a generator `args` flag.
Generator `args.faces` controls the remote raw mesh independently. Existing
outputs are still skipped unless `--force` is set. `--simplify` enables default
postprocessing for asset tasks; `--no-simplify` disables it globally.
`--dry-run --force` previews both steps without running ComfyUI or Blender.
Sound and image tasks never use this pass.

`auto` chooses decimation for names ending in `-rug`, `-painting`, `-mirror`
(also `gilt-oval-tray` and `porcelain-dish`), with `flat_target`. Names ending in
`-wall` use retopo with `wall_target`; other names, including doors/windows,
use retopo with `target`. Force `decimate` for foliage/thin branching geometry.
`retopo` voxel-remeshes, unwraps, and bakes base colour and normals using Cycles.

Options: `enabled`, `strategy` (`auto`, `decimate`, `retopo`), `target`,
`flat_target`, `wall_target`, `bake_resolution`, `remesh_resolution`,
`max_deviation`, `dissolve_deg` (10), `smooth_deg` (35), `weld` (0.0001),
`blender` (executable path). `decimate_after_retopo: true` adds a second
UV-preserving decimation pass after retopo/rebaking for reviewed custom recipes.

Adaptive retopo measures sampled bidirectional surface-distance P95, relative
to the bounding-box diagonal, and can raise a lower budget up to `target`.
The threshold is a heuristic, not a visual-quality guarantee. At the ceiling,
the attempted mesh is emitted even if deviation remains high. Blender's targets
are approximate; inspect the reported actual triangle counts.

Standalone inspection: `python mesh_simplify.py build --dry-run`.
Standalone processing: `python mesh_simplify.py build --apply --backup-dir backups`.

## Templates

An entry with `"tool": "TEMPLATE"` is a **reusable definition, not a task**. It
needs a name (`label`, alias `name`/`id`) and either a `prompt`, an `args`
object, or both. Templates are excluded from runs and from `--index` numbering.

Reuse a template two ways:

1. **Prompt interpolation** — write `{{name}}` inside any prompt; it is replaced
   by that template's `prompt`. Substitution is recursive (templates can
   reference other templates) and applies **only to prompt strings**, never to
   arg values.
2. **Args inheritance** — set `"template": "name"` (or a list of names) inside a
   task's `args`; the template's `args` are merged in as a base, then the task's
   own args override. Inheritance is recursive too.

A template's `prompt` is *not* added automatically when you inherit its args —
include it explicitly with `{{name}}`. Unknown `{{name}}` references and
circular references are hard errors (`bake: ...` + exit code 2). Listing the
templates before running helps:

```bash
python bake.py tasks.json --list-templates
```

Example:

```json
{
  "tasks": [
    {"tool": "TEMPLATE", "label": "hero",
     "prompt": "a young adventurer with teal hair, green cloak",
     "args": {"width": 1024, "height": 1024, "style": "clean cel shading"}},

    {"label": "hero_smile", "tool": "image",
     "prompt": "{{hero}}, smiling happily, waving, full body",
     "output": "build/hero_smile.png", "args": {"template": "hero"}}
  ]
}
```

### Image character demo

`bake_image_demo.json` (repo root) is a runnable example: it defines a `settings`
template (resolution/steps/style), a `hero` character template, and `expr_*` /
`pose_*` fragment templates, then produces eight expression/pose variants under
`build/characters/`. Inspect it with `--list-templates`, preview with
`--dry-run`, and run a subset with `--only 'hero_happy*'`.

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
  "image_styles": {
    "illustration": "vibrant illustration, clean lines, soft shading"
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
     "output": "build/sfx/coin_pickup.wav", "args": {"duration": 1.5, "steps": 30}},

    {"label": "chest_portrait", "tool": "image",
     "prompt": "a glowing treasure chest in a dark cave, dramatic light",
     "output": "build/concept/chest_portrait.png",
     "args": {"width": 1024, "height": 1024, "upscale": true, "style": "concept art"}}
  ]
}
```

## Usage

```bash
# run every task, skipping ones whose output already exists
python bake.py assets.json

# preview
python bake.py assets.json --list
python bake.py assets.json --list-templates
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
`--index SPEC` (repeatable, `1,3-5`, templates not counted), `--style NAME`,
`--list`, `--list-templates`, `--list-styles`, `--dry-run`, `--force`,
`--keep-going`. Exit code is `0` when nothing failed, `1` if a task failed, `2`
for bad input/selection.

## How it works

1. Splits the entries into templates and tasks, validates named style maps,
   resolves `{{name}}` prompt references (recursively, with cycle detection),
   and merges inherited `args` (`args.template`), with task args winning.
2. Validates and normalizes every task (tool, prompt, output extension, args).
3. For each selected task: creates a temp staging dir, runs
   `python generate_*.py <prompt> --name <stem> --outdir <staging> <args>`,
    optionally simplifies the staged GLB, then copies the final `<stem>.<ext>`
    to the target path. Failed simplification leaves existing outputs intact.
4. Staging dirs are always removed; only the final artifact is copied.
5. The output extension must match what the target asks for. A `.glb` task
   copies the GLB; a `.flac`/`.wav` task copies the audio of that exact type; a
   `.png` task copies the image.
6. `--style NAME` is resolved per selected task before running; the chosen prompt
   is injected as the task's `style` arg, overriding any per-task value.

Tasks run sequentially (ComfyUI queues them). The remote endpoint honors
`COMFY_REMOTE_HOST` / `COMFY_REMOTE_PORT`; pass `--host` per task via `args`;
bake itself forwards nothing global.

## Recommended workflow

1. Check the remote is up: `curl -s "http://delphi:8188/system_stats"`.
2. `python bake.py tasks.json --list` to confirm ordering/labels; add
   `--list-templates` and `--list-styles` to see reusable prompts.
3. `python bake.py tasks.json --dry-run` to inspect commands; check that
   `{{templates}}` expanded as expected.
4. Run for real. Everything is skipped if outputs already exist, so re-runs are
   incremental; use `--force` or a narrowed `--only`/`--index` to redo.
5. First run also downloads TRELLIS.2 GGUF weights and (for image tasks) the
   Pony checkpoint / upscaler. Assets take ~50-140 s each, sounds ~5-15 s each
   once models are warm.

## Guardrails

- Do not use `GGUF Q4_K_M` for TRELLIS.2 (noise textures); the workflows are
  fixed so bake inherits the safe defaults.
- Only the final artifact is copied; use the generator's own flags (e.g. asset
  `render`) if you need extra outputs.
- Image tasks produce one PNG each; for several images, add one task per output
  (use templates to avoid repeating the prompt).
- Do not hand-edit generated outputs; re-run the relevant task instead.
