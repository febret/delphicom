#!/usr/bin/env python3
"""Bake a list of generation tasks into final asset files.

Reads a JSON file holding an array of tasks and, for each task, runs the
matching generator (generate_asset.py, generate_sound.py or generate_image.py)
against the remote ComfyUI instance, then copies the produced artifact to the
task's target path.

The file is either a bare JSON array of tasks, or an object with optional
named style prompts plus the task array:

    {
      "asset_styles": {"voxel": "voxel art. 16 color palette.", "...": "..."},
      "sound_styles": {"retro": "chiptune, 8-bit", "...": "..."},
      "image_styles": {"illustration": "vibrant illustration, clean lines"},
      "tasks": [ ... ]
    }

`asset_styles` apply to asset tasks, `sound_styles` to sound tasks and
`image_styles` to image tasks. Pick one name with --style NAME to override the
style used by every selected task (it wins over any per-task "style" arg).

Task object fields:
    label   optional  short name used for progress/selection (default: output stem)
    tool    required  "asset" (generate_asset.py), "sound" (generate_sound.py) or
                      "image" (generate_image.py); or "TEMPLATE" (see below)
    prompt  required  text prompt passed to the generator
    output  required  target path; ".glb" for assets, ".flac"/".wav" for sounds,
                      ".png" for images
    args    optional  extra CLI flags for the generator, e.g. {"seed": 42,
                      "render": true, "duration": 5, "width": 1024,
                      "upscale": true}
    simplify optional  asset tasks only, optional mesh simplification of the
                      generated .glb (see "Mesh simplification" below). `true`
                      enables it with the document defaults, `false` disables it
                      for this task, or pass an options object to override.

Mesh simplification:
    Asset tasks can run the generated .glb back through mesh_simplify.py to cut
    the triangle count to a face budget. This is separate from the generator's
    own `faces` arg: the generator emits the raw mesh, then this pass reduces it
    (retopo + texture rebake for solids, collapse decimation for flat/foliage).
    It is off unless enabled by a document-level "simplify" object, a per-task
    "simplify", or the --simplify flag.

    Document-level defaults apply to every asset task:

        {
          "simplify": {"target": 500, "flat_target": 100, "wall_target": 200},
          "tasks": [ ... ]
        }

    Per-task "simplify" merges over the document defaults; `true`/`false` enable
    or disable with the defaults. Options: enabled, strategy
    (auto|decimate|retopo), target, flat_target, wall_target, max_deviation,
    bake_resolution, remesh_resolution, dissolve_deg, smooth_deg, weld, blender.
    Budgets are chosen by model name: `*-rug`/`*-painting`/`*-mirror` -> flat,
    `*-wall` -> wall, `*-door`/`*-window` and everything else -> target. Force
    `strategy: "decimate"` for fine branching geometry (trees, foliage).

Templates:
    An entry with "tool": "TEMPLATE" is a reusable definition, not a task. It
    needs a name (`label`, alias `name`/`id`) and either a `prompt`, an `args`
    object, or both. Reference a template's prompt inside another task's prompt
    with {{name}}; reference a template's args by setting "template": "name"
    (or a list of names) inside the task's args. Task args override inherited
    ones. Templates are excluded from runs and from --index numbering.

    {
      "tool": "TEMPLATE", "label": "hero",
      "prompt": "a brave knight in silver armor, red cape",
      "args": {"width": 1024, "upscale": true}
    }

Example task file:
    {
      "image_styles": {"illustration": "vibrant illustration, clean lines"},
      "tasks": [
        {"tool": "TEMPLATE", "label": "hero",
         "prompt": "a young adventurer with teal hair, green cloak",
         "args": {"width": 1024, "height": 1024, "upscale": true}},
        {"label": "hero_smile", "tool": "image",
         "prompt": "{{hero}}, smiling happily, waving, full body",
         "output": "build/hero_smile.png", "args": {"template": "hero"}}
      ]
    }

Examples:
    python bake.py tasks.json
    python bake.py tasks.json --only 'chest*' --only cat_meow
    python bake.py tasks.json --index 1,3-5 --keep-going
    python bake.py tasks.json --list
    python bake.py tasks.json --list-templates
    python bake.py tasks.json --list-styles
    python bake.py tasks.json --style voxel --force
    python bake.py tasks.json --simplify --force
    python bake.py tasks.json --no-simplify
"""
import argparse
import fnmatch
import glob as globmod
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import mesh_simplify  # noqa: E402  (local module, resolved after sys.path tweak)

GENERATORS = {
    "asset": "generate_asset.py",
    "generate_asset": "generate_asset.py",
    "generate_asset.py": "generate_asset.py",
    "3d": "generate_asset.py",
    "model": "generate_asset.py",
    "sound": "generate_sound.py",
    "generate_sound": "generate_sound.py",
    "generate_sound.py": "generate_sound.py",
    "sfx": "generate_sound.py",
    "audio": "generate_sound.py",
    "image": "generate_image.py",
    "generate_image": "generate_image.py",
    "generate_image.py": "generate_image.py",
    "img": "generate_image.py",
}

ASSET_EXTS = (".glb",)
SOUND_EXTS = (".flac", ".wav")
IMAGE_EXTS = (".png",)
TOOL_EXTS = {
    "generate_asset.py": ASSET_EXTS,
    "generate_sound.py": SOUND_EXTS,
    "generate_image.py": IMAGE_EXTS,
}
RESERVED_ARGS = ("name", "outdir")
# generate_image.py can only emit one artifact per run and --serve queues
# nothing, so these make no sense as bake args.
FORBIDDEN_ARGS = {"generate_image.py": ("batch", "batch-size", "serve")}

# Which named-style section each generator draws from.
STYLE_SECTIONS = {
    "generate_asset.py": "asset_styles",
    "generate_sound.py": "sound_styles",
    "generate_image.py": "image_styles",
}
STYLE_SECTION_NAMES = ("asset_styles", "sound_styles", "image_styles")

_RANGE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


def _resolve_tool(value):
    key = os.path.basename(str(value or "").strip()).lower()
    return GENERATORS.get(key)


def _is_template(value):
    return isinstance(value, str) and value.strip().lower() == "template"


def _template_name(raw):
    for key in ("label", "name", "id"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _norm_arg(key):
    return str(key).lstrip("-").replace("_", "-").lower()


def _as_list(value, where):
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        out = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{where}: 'template' entries must be non-empty strings")
            out.append(item)
        return out
    raise ValueError(f"{where}: 'template' must be a string or a list of strings")


def _avail(templates):
    return ", ".join(sorted(templates)) or "none"


def _load_styles(raw, source):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{source} must be a JSON object of name -> prompt")
    styles = {}
    for name, text in raw.items():
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{source}[{name!r}] must be a non-empty string")
        styles[str(name)] = text
    return styles


def _expand_prompt(text, templates, stack, where):
    if not text or "{{" not in text:
        return text

    def repl(match):
        name = match.group(1).strip()
        if name in stack:
            chain = " -> ".join(stack + [name])
            raise ValueError(f"{where}: circular template reference: {chain}")
        if name not in templates:
            raise ValueError(
                f"{where}: unknown template {{{{{name}}}}} "
                f"(available: {_avail(templates)})")
        return _expand_prompt(templates[name]["prompt"], templates,
                              stack + [name], where)

    return _TEMPLATE_RE.sub(repl, text)


def _resolve_template_args(name, templates, stack):
    if name in stack:
        chain = " -> ".join(stack + [name])
        raise ValueError(f"circular template args reference: {chain}")
    tpl = templates[name]
    base = {}
    for ref in _as_list(tpl["args"].get("template"), f"template {name!r}"):
        if ref not in templates:
            raise ValueError(
                f"template {name!r}: unknown template {ref!r} in args "
                f"(available: {_avail(templates)})")
        base.update(_resolve_template_args(ref, templates, stack + [name]))
    own = {k: v for k, v in tpl["args"].items() if k != "template"}
    base.update(own)
    return base


def _resolve_task_args(extra, templates, where):
    merged = {}
    for ref in _as_list(extra.get("template"), where):
        if ref not in templates:
            raise ValueError(
                f"{where}: unknown template {ref!r} in args "
                f"(available: {_avail(templates)})")
        merged.update(_resolve_template_args(ref, templates, []))
    own = {k: v for k, v in extra.items() if k != "template"}
    merged.update(own)
    return merged


def load_tasks(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        styles = {name: _load_styles(data.get(name), name)
                  for name in STYLE_SECTION_NAMES}
        raw_tasks = data.get("tasks")
        raw_simplify = data.get("simplify")
    elif isinstance(data, list):
        styles = {name: {} for name in STYLE_SECTION_NAMES}
        raw_tasks = data
        raw_simplify = None
    else:
        raise ValueError("task file must be a JSON array or object")

    if raw_simplify is None or raw_simplify is False:
        document_simplify = None
    else:
        try:
            document_simplify = mesh_simplify.SimplifierConfig.from_mapping(
                {} if raw_simplify is True else raw_simplify, where="simplify")
        except ValueError as e:
            raise ValueError(str(e))

    if not isinstance(raw_tasks, list):
        raise ValueError("'tasks' must be a JSON array of tasks")

    templates = {}
    task_entries = []
    for i, raw in enumerate(raw_tasks, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"entry #{i} is not a JSON object")
        if _is_template(raw.get("tool")):
            name = _template_name(raw)
            if not name:
                raise ValueError(f"template #{i}: needs a 'label' (or 'name'/'id')")
            if name in templates:
                raise ValueError(f"template #{i}: duplicate template name {name!r}")
            if raw.get("output"):
                raise ValueError(f"template #{i}: 'output' is not allowed on a template")
            prompt = raw.get("prompt") or ""
            if not isinstance(prompt, str):
                raise ValueError(f"template #{i}: 'prompt' must be a string")
            args = raw.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError(f"template #{i}: 'args' must be a JSON object")
            if not prompt.strip() and not args:
                raise ValueError(
                    f"template #{i}: needs a non-empty 'prompt' or 'args'")
            templates[name] = {"prompt": prompt, "args": args}
        else:
            task_entries.append((i, raw))

    tasks = []
    for i, raw in task_entries:
        tool = _resolve_tool(raw.get("tool"))
        if not tool:
            raise ValueError(
                f"task #{i}: unknown tool {raw.get('tool')!r} "
                "(expected 'asset'/'generate_asset', 'sound'/'generate_sound', "
                "'image'/'generate_image', or 'TEMPLATE')")

        prompt = raw.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"task #{i}: missing non-empty 'prompt'")

        output = raw.get("output")
        if not isinstance(output, str) or not output.strip():
            raise ValueError(f"task #{i}: missing non-empty 'output'")

        exts = TOOL_EXTS[tool]
        if os.path.splitext(output)[1].lower() not in exts:
            raise ValueError(
                f"task #{i}: output {output!r} must end with "
                f"{' or '.join(exts)} for tool {tool}")

        extra = raw.get("args") or {}
        if not isinstance(extra, dict):
            raise ValueError(f"task #{i}: 'args' must be a JSON object")
        try:
            prompt = _expand_prompt(prompt, templates, [], f"task #{i}")
            extra = _resolve_task_args(extra, templates, f"task #{i}")
        except ValueError as e:
            raise ValueError(str(e))

        for key in extra:
            if _norm_arg(key) in RESERVED_ARGS:
                raise ValueError(f"task #{i}: 'args' may not set {key!r}")
        for key in extra:
            if _norm_arg(key) == "simplify":
                raise ValueError(
                    f"task #{i}: 'simplify' is a task-level field, not an 'args' "
                    "flag (put it beside 'output')")
        for key in extra:
            if _norm_arg(key) in FORBIDDEN_ARGS.get(tool, ()):
                raise ValueError(
                    f"task #{i}: 'args' may not set {key!r} for {tool} "
                    "(bake produces a single artifact per task)")

        simplify = _parse_task_simplify(raw, i, tool)
        label = raw.get("label") or os.path.splitext(os.path.basename(output))[0]
        tasks.append({
            "label": str(label),
            "tool": tool,
            "prompt": prompt,
            "output": output,
            "args": extra,
            "simplify": simplify,
        })
    return styles, templates, tasks, document_simplify


def _parse_task_simplify(raw, i, tool):
    """Validate a task's optional `simplify` field. None means 'use default'."""
    if "simplify" not in raw or raw.get("simplify") is None:
        return None
    value = raw["simplify"]
    if isinstance(value, bool):
        if value and tool != "generate_asset.py":
            raise ValueError(
                f"task #{i}: 'simplify' is only valid for .glb asset tasks")
        return value
    if isinstance(value, dict):
        if tool != "generate_asset.py":
            raise ValueError(
                f"task #{i}: 'simplify' is only valid for .glb asset tasks")
        try:
            mesh_simplify.SimplifierConfig.from_mapping(
                value, where=f"task #{i}.simplify")
        except ValueError as e:
            raise ValueError(str(e))
        return value
    raise ValueError(
        f"task #{i}: 'simplify' must be true, false, or an options object")


def resolve_simplify(document, task, force_on, force_off):
    """Merge document/task/CLI simplify settings into a config or None."""
    if force_off or task["tool"] != "generate_asset.py":
        return None
    override = task.get("simplify")
    if override is False:
        return None
    base = document if document is not None else (
        mesh_simplify.SimplifierConfig() if (override is not None or force_on) else None)
    if base is None:
        return None
    if override is True:
        config = base.merged({"enabled": True})
    elif isinstance(override, dict):
        config = base.merged(override, where=f"{task['label']}.simplify")
    else:
        config = base
    if force_on:
        config = config.merged({"enabled": True})
    return config if config.enabled else None


def apply_style(tasks, selected, styles, name):
    """Force the named style prompt onto every selected task that defines it.

    The name is resolved in the section matching each task's tool (asset, sound
    or image). Tasks whose section does not define the name are left unchanged
    (they keep their per-task style or generator default); the call only fails
    if no selected task can use the name. Returns (applied, skipped) index lists.
    """
    applied, skipped = [], []
    for i in selected:
        task = tasks[i]
        section = STYLE_SECTIONS.get(task["tool"], "asset_styles")
        if name in styles[section]:
            tasks[i]["args"]["style"] = styles[section][name]
            applied.append(i)
        else:
            skipped.append((i, task, section))
    if not applied:
        available = {s: sorted(v) for s, v in styles.items()}
        raise ValueError(
            f"style {name!r} is not defined for any selected task "
            f"(available: {available})")
    return applied, skipped


def parse_indices(specs, total):
    idx = set()
    for spec in specs:
        for part in str(spec).split(","):
            part = part.strip()
            if not part:
                continue
            m = _RANGE.match(part)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                if a > b:
                    a, b = b, a
                idx.update(range(a, b + 1))
            elif part.isdigit():
                idx.add(int(part))
            else:
                raise ValueError(f"bad index {part!r} (use e.g. 1,3-5)")
    bad = sorted(i for i in idx if i < 1 or i > total)
    if bad:
        raise ValueError(f"index out of range 1..{total}: {bad}")
    return idx


def select_tasks(tasks, only, index_specs):
    selected = list(range(len(tasks)))
    if index_specs:
        wanted = parse_indices(index_specs, len(tasks))
        selected = [i for i in selected if (i + 1) in wanted]
    if only:
        def matches(task):
            stem = os.path.splitext(os.path.basename(task["output"]))[0]
            keys = (task["label"], task["output"], os.path.basename(task["output"]), stem)
            return any(fnmatch.fnmatch(k, pat) for pat in only for k in keys)
        selected = [i for i in selected if matches(tasks[i])]
    return selected


def build_argv(tool, prompt, name, staging, extra):
    argv = [sys.executable, os.path.join(HERE, tool), prompt,
            "--name", name, "--outdir", staging]
    for key, val in extra.items():
        bare = str(key).lstrip("-").replace("_", "-").lower()
        if bare in RESERVED_ARGS:
            raise ValueError(f"reserved argument {key!r}")
        flag = "--" + bare
        if isinstance(val, bool):
            if val:
                argv.append(flag)
        elif val is None:
            continue
        elif isinstance(val, (list, tuple)):
            for item in val:
                argv += [flag, str(item)]
        else:
            argv += [flag, str(val)]
    return argv


def find_artifact(staging, name, ext):
    p = os.path.join(staging, name + ext)
    if os.path.isfile(p):
        return p
    hits = sorted(globmod.glob(os.path.join(staging, name + "*" + ext)))
    return hits[0] if hits else None


def _display(path):
    try:
        rel = os.path.relpath(path)
        return rel if not rel.startswith("..") else path
    except ValueError:
        return path


def _run_simplify(target, config, dry_run):
    """Simplify a generated GLB. Returns None on success, else an error string."""
    if dry_run:
        name = os.path.splitext(os.path.basename(target))[0]
        category = mesh_simplify.category_for(name)
        budget = mesh_simplify.target_for(category, config)
        strategy = mesh_simplify.resolve_strategy(category, config.strategy)
        print(f"    simplify: target {budget}, category {category}, strategy {strategy} (would run)")
        return None
    analysis = mesh_simplify.analyze_file(target, config)
    if analysis.error:
        return analysis.error
    preview = f"target {analysis.target}, category {analysis.category}"
    if analysis.skip:
        print(f"    simplify: {preview} - already within budget, skipped")
        return None
    try:
        result = mesh_simplify.simplify_glb(target, config)
    except Exception as e:  # noqa: BLE001 - report any worker failure
        return str(e)
    detail = f" [{result['strategy']}]" if result.get("strategy") else ""
    deviation = result.get("deviation")
    if deviation is not None:
        detail += f" dev={deviation:.3f}"
    print(
        f"    simplify: {result['tris_before']} -> {result['tris_after']} tris "
        f"(target {result['target']}){detail}"
    )
    return None


def run_task(task, force, dry_run, simplify_config=None):
    tool = task["tool"]
    name = os.path.splitext(os.path.basename(task["output"]))[0]
    target_ext = os.path.splitext(task["output"])[1].lower()

    extra = dict(task["args"])
    if tool == "generate_sound.py" and target_ext == ".wav":
        extra["wav"] = True

    if os.path.isfile(task["output"]) and not force:
        print(f"    skip: {os.path.abspath(task['output'])} already exists "
              "(use --force to redo)")
        return "skipped", os.path.abspath(task["output"]), 0.0

    staging = tempfile.mkdtemp(prefix="bake_")
    try:
        argv = build_argv(tool, task["prompt"], name, staging, extra)
        print(f"    tool: {tool}")
        print(f"    prompt: {task['prompt']}")
        print(f"    run: {' '.join(argv)}")
        if dry_run:
            if simplify_config is not None and target_ext == ".glb":
                _run_simplify(os.path.abspath(task["output"]), simplify_config, True)
            return "dry-run", None, 0.0

        t0 = time.time()
        rc = subprocess.run(argv).returncode
        elapsed = time.time() - t0
        if rc != 0:
            print(f"    FAILED: {tool} exited with code {rc}")
            return "failed", None, elapsed

        artifact = find_artifact(staging, name, target_ext)
        if not artifact:
            print(f"    FAILED: no {name}{target_ext} produced by {tool} "
                  "(for .wav, install 'soundfile' or 'ffmpeg')")
            return "failed", None, elapsed

        if simplify_config is not None and target_ext == ".glb":
            error = _run_simplify(artifact, simplify_config, False)
            if error:
                print(f"    FAILED: simplify: {error}")
                return "failed", None, time.time() - t0

        target = os.path.abspath(task["output"])
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(artifact, target)
        print(f"    ok: {target}")

        return "ok", target, time.time() - t0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _preview(text, width=70):
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "\u2026"


def _print_templates(templates):
    if not templates:
        return
    width = max((len(n) for n in templates), default=8)
    print(f"[bake] {len(templates)} template(s):")
    for name in sorted(templates):
        tpl = templates[name]
        args = tpl["args"]
        if args:
            print(f"      {name:<{width}}  args={_preview(json.dumps(args), 60)}")
        if tpl["prompt"].strip():
            print(f"      {name:<{width}}  {_preview(tpl['prompt'])}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tasks", help="JSON file containing an array of tasks")
    ap.add_argument("--only", action="append", default=[], metavar="GLOB",
                    help="run only tasks whose label/output matches GLOB "
                         "(repeatable)")
    ap.add_argument("--index", action="append", default=[], metavar="SPEC",
                    help="run only these 1-based task indices, e.g. 1,3-5 "
                         "(repeatable; templates are not counted)")
    ap.add_argument("--list", action="store_true",
                    help="list the templates and tasks in the file and exit")
    ap.add_argument("--list-templates", action="store_true",
                    help="list the templates in the file and exit")
    ap.add_argument("--list-styles", action="store_true",
                    help="list the named style prompts in the file and exit")
    ap.add_argument("--style", metavar="NAME",
                    help="named style prompt (asset_styles/sound_styles/"
                         "image_styles) to apply to every selected task, "
                         "overriding per-task styles")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the generator commands without running them")
    ap.add_argument("--force", action="store_true",
                    help="re-run tasks even when the output already exists")
    ap.add_argument("--keep-going", action="store_true",
                    help="continue with remaining tasks after a failure")
    simplify_flags = ap.add_mutually_exclusive_group()
    simplify_flags.add_argument("--simplify", dest="simplify", action="store_true", default=None,
                    help="simplify every selected asset task's GLB with the "
                         "default (or document) settings")
    simplify_flags.add_argument("--no-simplify", dest="simplify", action="store_false",
                    help="skip mesh simplification even where the task file enables it")
    args = ap.parse_args()

    try:
        styles, templates, tasks, document_simplify = load_tasks(args.tasks)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"bake: {e}", file=sys.stderr)
        return 2

    summary = f"{len(tasks)} task(s)"
    if templates:
        summary += f", {len(templates)} template(s)"
    print(f"[bake] {args.tasks}: {summary}")

    if args.list_styles:
        for section in STYLE_SECTION_NAMES:
            names = sorted(styles[section])
            print(f"[bake] {section}: {', '.join(names) if names else '(none)'}")
        return 0

    if args.list_templates:
        if not templates:
            print("[bake] no templates")
            return 0
        _print_templates(templates)
        return 0

    if args.list:
        _print_templates(templates)
        if templates:
            print()
        width = max((len(t["label"]) for t in tasks), default=5)
        print(f"[bake] {'#':>3}  {'label':<{width}}  {'tool':<17}  "
              f"{'style':<12}  output")
        for i, t in enumerate(tasks, 1):
            style = t["args"].get("style", "")
            style = "(own)" if style else "-"
            print(f"      {i:>3}  {t['label']:<{width}}  {t['tool']:<17}  "
                  f"{style:<12}  {t['output']}")
        return 0

    try:
        selected = select_tasks(tasks, args.only, args.index)
    except ValueError as e:
        print(f"bake: {e}", file=sys.stderr)
        return 2

    if not selected:
        print("[bake] no tasks matched the given selection", file=sys.stderr)
        return 2

    if args.style:
        try:
            applied, skipped = apply_style(tasks, selected, styles, args.style)
        except ValueError as e:
            print(f"bake: {e}", file=sys.stderr)
            return 2
        print(f"[bake] applying style {args.style!r} to {len(applied)} task(s)")
        for i, task, section in skipped:
            print(f"    note: #{i + 1} {task['label']} has no "
                  f"{section}[{args.style!r}], left unchanged")
    print(f"[bake] running {len(selected)}/{len(tasks)} task(s)")

    counts = {"ok": 0, "skipped": 0, "failed": 0, "dry-run": 0}
    t_start = time.time()
    for n, i in enumerate(selected, 1):
        task = tasks[i]
        print(f"\n[bake] ({n}/{len(selected)}) #{i + 1} {task['label']} "
              f"[{task['tool']}]")
        try:
            config = resolve_simplify(document_simplify, task,
                                      args.simplify is True, args.simplify is False)
            status, _, elapsed = run_task(task, args.force, args.dry_run, config)
        except KeyboardInterrupt:
            print("\n[bake] interrupted", file=sys.stderr)
            return 130
        except Exception as e:
            print(f"    FAILED: {e}")
            status, elapsed = "failed", 0.0
        counts[status] += 1
        if status == "ok":
            print(f"    done in {elapsed:.1f}s")
        if status == "failed" and not args.keep_going:
            print("[bake] stopping (use --keep-going to continue)",
                  file=sys.stderr)
            break

    total = time.time() - t_start
    print(f"\n[bake] summary: {counts['ok']} ok, {counts['skipped']} skipped, "
          f"{counts['failed']} failed, {counts['dry-run']} dry-run "
          f"in {total:.1f}s")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
