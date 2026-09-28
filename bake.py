#!/usr/bin/env python3
"""Bake a list of generation tasks into final asset files.

Reads a JSON file holding an array of tasks and, for each task, runs the
matching generator (generate_asset.py or generate_sound.py) against the remote
ComfyUI instance, then copies the produced artifact to the task's target path.

The file is either a bare JSON array of tasks, or an object with optional
named style prompts plus the task array:

    {
      "asset_styles": {"voxel": "voxel art. 16 color palette.", "...": "..."},
      "sound_styles": {"retro": "chiptune, 8-bit", "...": "..."},
      "tasks": [ ... ]
    }

`asset_styles` apply to asset tasks and `sound_styles` to sound tasks. Pick one
name with --style NAME to override the style used by every selected task (it
wins over any per-task "style" arg).

Task object fields:
    label   optional  short name used for progress/selection (default: output stem)
    tool    required  "asset" (generate_asset.py) or "sound" (generate_sound.py)
    prompt  required  text prompt passed to the generator
    output  required  target path; ".glb" for assets, ".flac"/".wav" for sounds
    args    optional  extra CLI flags for the generator, e.g. {"seed": 42,
                      "render": true, "duration": 5, "wav": true}

Example task file:
    {
      "asset_styles": {"voxel": "voxel art. 16 color palette. vibrant."},
      "sound_styles": {"retro": "chiptune, 8-bit", "foley": "close-up foley"},
      "tasks": [
        {"label": "chest", "tool": "asset",
         "prompt": "low poly treasure chest, 16 color palette",
         "output": "build/chest.glb", "args": {"render": true}},
        {"label": "cat_meow", "tool": "sound",
         "prompt": "kitty meowing, foley",
         "output": "build/cat_meow.wav", "args": {"duration": 5}}
      ]
    }

Examples:
    python bake.py tasks.json
    python bake.py tasks.json --only 'chest*' --only cat_meow
    python bake.py tasks.json --index 1,3-5 --keep-going
    python bake.py tasks.json --list
    python bake.py tasks.json --list-styles
    python bake.py tasks.json --style voxel --force
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
}

ASSET_EXTS = (".glb",)
SOUND_EXTS = (".flac", ".wav")
RESERVED_ARGS = ("name", "outdir")

# Which named-style section each generator draws from.
STYLE_SECTIONS = {
    "generate_asset.py": "asset_styles",
    "generate_sound.py": "sound_styles",
}

_RANGE = re.compile(r"^(\d+)\s*-\s*(\d+)$")


def _resolve_tool(value):
    key = os.path.basename(str(value or "").strip()).lower()
    return GENERATORS.get(key)


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


def load_tasks(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        styles = {
            "asset_styles": _load_styles(data.get("asset_styles"), "asset_styles"),
            "sound_styles": _load_styles(data.get("sound_styles"), "sound_styles"),
        }
        raw_tasks = data.get("tasks")
    elif isinstance(data, list):
        styles = {"asset_styles": {}, "sound_styles": {}}
        raw_tasks = data
    else:
        raise ValueError("task file must be a JSON array or object")

    if not isinstance(raw_tasks, list):
        raise ValueError("'tasks' must be a JSON array of tasks")

    tasks = []
    for i, raw in enumerate(raw_tasks, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"task #{i} is not a JSON object")

        tool = _resolve_tool(raw.get("tool"))
        if not tool:
            raise ValueError(
                f"task #{i}: unknown tool {raw.get('tool')!r} "
                "(expected 'asset'/'generate_asset' or 'sound'/'generate_sound')")

        prompt = raw.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"task #{i}: missing non-empty 'prompt'")

        output = raw.get("output")
        if not isinstance(output, str) or not output.strip():
            raise ValueError(f"task #{i}: missing non-empty 'output'")

        exts = ASSET_EXTS if tool == "generate_asset.py" else SOUND_EXTS
        if os.path.splitext(output)[1].lower() not in exts:
            raise ValueError(
                f"task #{i}: output {output!r} must end with "
                f"{' or '.join(exts)} for tool {tool}")

        extra = raw.get("args") or {}
        if not isinstance(extra, dict):
            raise ValueError(f"task #{i}: 'args' must be a JSON object")
        for key in extra:
            if str(key).lstrip("-").replace("_", "-").lower() in RESERVED_ARGS:
                raise ValueError(f"task #{i}: 'args' may not set {key!r}")

        label = raw.get("label") or os.path.splitext(os.path.basename(output))[0]
        tasks.append({
            "label": str(label),
            "tool": tool,
            "prompt": prompt,
            "output": output,
            "args": extra,
        })
    return styles, tasks


def apply_style(tasks, selected, styles, name):
    """Force the named style prompt onto every selected task that defines it.

    The name is resolved in the section matching each task's tool (asset vs
    sound). Tasks whose section does not define the name are left unchanged
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


def run_task(task, force, dry_run):
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

        target = os.path.abspath(task["output"])
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(artifact, target)
        print(f"    ok: {target}")
        return "ok", target, elapsed
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tasks", help="JSON file containing an array of tasks")
    ap.add_argument("--only", action="append", default=[], metavar="GLOB",
                    help="run only tasks whose label/output matches GLOB "
                         "(repeatable)")
    ap.add_argument("--index", action="append", default=[], metavar="SPEC",
                    help="run only these 1-based task indices, e.g. 1,3-5 "
                         "(repeatable)")
    ap.add_argument("--list", action="store_true",
                    help="list the tasks in the file and exit")
    ap.add_argument("--list-styles", action="store_true",
                    help="list the named style prompts in the file and exit")
    ap.add_argument("--style", metavar="NAME",
                    help="named style prompt (asset_styles/sound_styles) to apply "
                         "to every selected task, overriding per-task styles")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the generator commands without running them")
    ap.add_argument("--force", action="store_true",
                    help="re-run tasks even when the output already exists")
    ap.add_argument("--keep-going", action="store_true",
                    help="continue with remaining tasks after a failure")
    args = ap.parse_args()

    try:
        styles, tasks = load_tasks(args.tasks)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"bake: {e}", file=sys.stderr)
        return 2

    print(f"[bake] {args.tasks}: {len(tasks)} task(s)")

    if args.list_styles:
        for section in ("asset_styles", "sound_styles"):
            names = sorted(styles[section])
            print(f"[bake] {section}: {', '.join(names) if names else '(none)'}")
        return 0

    if args.list:
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
            status, _, elapsed = run_task(task, args.force, args.dry_run)
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
