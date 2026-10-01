#!/usr/bin/env python3
"""Analyse and simplify generated GLB meshes.

Two ways to use it:

* As a library (what ``bake.py`` calls): build a :class:`SimplifierConfig`,
  then call :func:`simplify_glb` on a single generated ``.glb``. It decides a
  face budget from the model name, picks a strategy and runs the Blender
  worker (``mesh_simplify_blender.py``) to rewrite the file in place.

* As a CLI: scan a tree of GLBs and either report complexity (default
  ``--dry-run``) or simplify them in place with ``--apply``.

Face budgets are chosen per model:
    planar  (``*-rug``/``*-painting``/``*-mirror`` + flat decor) -> 100
    wall    (``*-wall``)                                        -> 200
    opening (``*-door``/``*-window``) and solids                -> 500

Strategies:
    planar  -> ``decimate``
    solids/walls/openings -> ``retopo`` (voxel remesh + re-UV + bake)
    any model may force ``decimate`` or ``retopo`` via ``strategy``; foliage
    and other fine branching geometry must force ``decimate``.

Only the Python standard library is used for analysis. Simplification needs
Blender and runs as a headless subprocess.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path


HERE = Path(__file__).resolve().parent
WORKER_PATH = HERE / "mesh_simplify_blender.py"

DEFAULT_TARGET = 500
DEFAULT_FLAT_TARGET = 100
DEFAULT_WALL_TARGET = 200
DEFAULT_MAX_DEVIATION = 0.05
DEFAULT_BAKE_RESOLUTION = 1024
DEFAULT_REMESH_RESOLUTION = 160
DEFAULT_DISSOLVE_DEG = 10.0
DEFAULT_SMOOTH_DEG = 35.0
DEFAULT_WELD = 0.0001
STRATEGIES = ("auto", "decimate", "retopo")

PLANAR_SUFFIXES = ("-rug", "-painting", "-mirror")
WALL_SUFFIXES = ("-wall",)
OPENING_SUFFIXES = ("-door", "-window")
FLAT_NAMES = frozenset({"gilt-oval-tray", "porcelain-dish"})
COPLANAR_DOT = math.cos(math.radians(1.0))

_COMPONENT_TYPES = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}
_TYPE_COUNTS = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}
_TRIANGLE_MODE = 4

_RESULT_PREFIX = "MESH_SIMPLIFY_RESULT "


@dataclass
class SimplifierConfig:
    """Tunable knobs for mesh simplification (serialised into bake.json)."""

    enabled: bool = True
    strategy: str = "auto"
    target: int = DEFAULT_TARGET
    flat_target: int = DEFAULT_FLAT_TARGET
    wall_target: int = DEFAULT_WALL_TARGET
    max_deviation: float = DEFAULT_MAX_DEVIATION
    bake_resolution: int = DEFAULT_BAKE_RESOLUTION
    remesh_resolution: int = DEFAULT_REMESH_RESOLUTION
    dissolve_deg: float = DEFAULT_DISSOLVE_DEG
    smooth_deg: float = DEFAULT_SMOOTH_DEG
    weld: float = DEFAULT_WELD
    blender: str | None = None
    decimate_after_retopo: bool = False

    @classmethod
    def from_mapping(cls, mapping, base: "SimplifierConfig | None" = None, where: str = "simplify"):
        """Validate a JSON mapping and merge it over ``base`` (or defaults)."""
        if mapping is None:
            return base or cls()
        if not isinstance(mapping, dict):
            raise ValueError(f"{where} must be a JSON object")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(mapping) - known)
        if unknown:
            raise ValueError(
                f"{where}: unknown option(s) {', '.join(unknown)} "
                f"(allowed: {', '.join(sorted(known))})"
            )
        data = asdict(base) if base is not None else {}
        data.update(mapping)
        config = cls(**data)
        config.validate(where)
        return config

    def merged(self, override, where: str = "simplify") -> "SimplifierConfig":
        if override is False:
            return SimplifierConfig.from_mapping({"enabled": False}, self)
        if override is True:
            return SimplifierConfig.from_mapping({"enabled": True}, self)
        return SimplifierConfig.from_mapping(override, self, where)

    def validate(self, where: str = "simplify") -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError(f"{where}.enabled must be a boolean")
        if not isinstance(self.decimate_after_retopo, bool):
            raise ValueError(f"{where}.decimate_after_retopo must be a boolean")
        if self.strategy not in STRATEGIES:
            raise ValueError(
                f"{where}.strategy must be one of {', '.join(STRATEGIES)}"
            )
        for name in ("target", "flat_target", "wall_target"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{where}.{name} must be a positive integer")
        for name in ("bake_resolution", "remesh_resolution"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 8:
                raise ValueError(f"{where}.{name} must be an integer >= 8")
        if isinstance(self.max_deviation, bool) or not isinstance(self.max_deviation, (int, float)) or not 0.0 <= float(self.max_deviation) <= 1.0:
            raise ValueError(f"{where}.max_deviation must be a number in [0, 1]")
        if self.blender is not None and not isinstance(self.blender, str):
            raise ValueError(f"{where}.blender must be a string path")
        for name in ("dissolve_deg", "smooth_deg", "weld"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{where}.{name} must be a finite nonnegative number")


@dataclass
class FileAnalysis:
    path: str
    propset: str
    name: str
    tris: int = 0
    verts: int = 0
    primitives: int = 0
    materials: int = 0
    images: int = 0
    animations: int = 0
    nodes: int = 0
    size_bytes: int = 0
    regions: int = 0
    dissolvable: int = 0
    flat_area_ratio: float = 0.0
    tri_in_flat_ratio: float = 0.0
    target: int = DEFAULT_TARGET
    category: str = "solid"
    skip: bool = False
    error: str | None = None

    @property
    def over_budget(self) -> int:
        return max(0, self.tris - self.target)


def parse_glb(path: Path) -> tuple[dict, bytes]:
    data = Path(path).read_bytes()
    if len(data) < 12:
        raise ValueError("file too small to be a GLB")
    magic, _version, _length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        raise ValueError("not a GLB file")
    offset = 12
    gltf: dict | None = None
    blob = b""
    while offset < len(data):
        chunk_length, chunk_type = struct.unpack_from("<I4s", data, offset)
        payload = data[offset + 8 : offset + 8 + chunk_length]
        if chunk_type == b"JSON":
            gltf = json.loads(payload.decode("utf-8"))
        elif chunk_type[:3] == b"BIN":
            blob = payload
        offset += 8 + chunk_length
    if gltf is None:
        raise ValueError("GLB has no JSON chunk")
    return gltf, blob


def _read_accessor(gltf: dict, blob: bytes, index: int) -> list[tuple]:
    accessor = gltf["accessors"][index]
    view = gltf["bufferViews"][accessor["bufferView"]]
    fmt, size = _COMPONENT_TYPES[accessor["componentType"]]
    components = _TYPE_COUNTS[accessor["type"]]
    count = accessor["count"]
    start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    stride = view.get("byteStride") or size * components
    pattern = "<" + fmt * components
    if stride == size * components:
        end = start + size * components * count
        return list(struct.iter_unpack(pattern, blob[start:end]))
    return [struct.unpack_from(pattern, blob, start + i * stride) for i in range(count)]


def _face(point_a, point_b, point_c) -> tuple[tuple[float, float, float], float]:
    ux, uy, uz = (point_b[i] - point_a[i] for i in range(3))
    vx, vy, vz = (point_c[i] - point_a[i] for i in range(3))
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    area = 0.5 * length
    if length < 1e-20:
        return (0.0, 0.0, 0.0), area
    return (nx / length, ny / length, nz / length), area


def _coplanar_stats(
    positions: list[tuple],
    indices: list[int],
) -> tuple[int, int, float, float, int]:
    tri_count = len(indices) // 3
    if tri_count == 0:
        return 0, 0, 0.0, 0.0, 0
    quantized: dict[tuple[int, int, int], int] = {}
    canonical = [0] * len(positions)
    for i, point in enumerate(positions):
        key = (round(point[0] / 1e-5), round(point[1] / 1e-5), round(point[2] / 1e-5))
        identity = quantized.get(key)
        if identity is None:
            identity = len(quantized)
            quantized[key] = identity
        canonical[i] = identity
    normals = []
    areas = []
    edges: dict[tuple[int, int], list[int]] = {}
    for t in range(tri_count):
        ia, ib, ic = indices[3 * t], indices[3 * t + 1], indices[3 * t + 2]
        va, vb, vc = canonical[ia], canonical[ib], canonical[ic]
        normal, area = _face(positions[ia], positions[ib], positions[ic])
        normals.append(normal)
        areas.append(area)
        for u, v in ((va, vb), (vb, vc), (vc, va)):
            key = (u, v) if u < v else (v, u)
            edges.setdefault(key, []).append(t)
    parent = list(range(tri_count))

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for linked in edges.values():
        if len(linked) < 2:
            continue
        for i in range(len(linked)):
            ni = normals[linked[i]]
            if ni == (0.0, 0.0, 0.0):
                continue
            for j in range(i + 1, len(linked)):
                nj = normals[linked[j]]
                if ni[0] * nj[0] + ni[1] * nj[1] + ni[2] * nj[2] > COPLANAR_DOT:
                    ri, rj = find(linked[i]), find(linked[j])
                    if ri != rj:
                        parent[rj] = ri

    root_of = [find(t) for t in range(tri_count)]
    sizes: dict[int, int] = {}
    for root in root_of:
        sizes[root] = sizes.get(root, 0) + 1
    flat_area = 0.0
    total_area = 0.0
    tri_in_flat = 0
    for t, area in enumerate(areas):
        total_area += area
        if sizes[root_of[t]] > 1:
            flat_area += area
            tri_in_flat += 1
    return len(sizes), tri_count - len(sizes), flat_area, total_area, tri_in_flat


def category_for(name: str) -> str:
    if name.endswith(PLANAR_SUFFIXES) or name in FLAT_NAMES:
        return "planar"
    if name.endswith(WALL_SUFFIXES):
        return "wall"
    if name.endswith(OPENING_SUFFIXES):
        return "opening"
    return "solid"


def target_for(category: str, config: SimplifierConfig) -> int:
    if category == "planar":
        return config.flat_target
    if category == "wall":
        return config.wall_target
    return config.target


def resolve_strategy(category: str, strategy: str) -> str:
    if strategy in ("decimate", "retopo"):
        return strategy
    return "decimate" if category == "planar" else "retopo"


def analyze_file(path, config: SimplifierConfig) -> FileAnalysis:
    path = Path(path)
    analysis = FileAnalysis(path=str(path), propset=path.parent.name, name=path.stem)
    try:
        gltf, blob = parse_glb(path)
    except (OSError, ValueError, KeyError, struct.error) as error:
        analysis.error = str(error)
        return analysis
    analysis.size_bytes = path.stat().st_size
    analysis.materials = len(gltf.get("materials", []))
    analysis.images = len(gltf.get("images", []))
    analysis.animations = len(gltf.get("animations", []))
    analysis.nodes = len(gltf.get("nodes", []))
    flat_area = total_area = 0.0
    tri_in_flat = 0
    for mesh in gltf.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            if primitive.get("mode", _TRIANGLE_MODE) != _TRIANGLE_MODE:
                continue
            position_index = primitive.get("attributes", {}).get("POSITION")
            if position_index is None:
                continue
            analysis.primitives += 1
            positions = [tuple(float(v) for v in p) for p in _read_accessor(gltf, blob, position_index)]
            analysis.verts += len(positions)
            indices_index = primitive.get("indices")
            if indices_index is None:
                indices = list(range(len(positions)))
            else:
                indices = [int(v[0]) for v in _read_accessor(gltf, blob, indices_index)]
            regions, dissolvable, prim_flat_area, prim_total_area, prim_flat_tris = _coplanar_stats(
                positions, indices
            )
            analysis.tris += len(indices) // 3
            analysis.regions += regions
            analysis.dissolvable += dissolvable
            flat_area += prim_flat_area
            total_area += prim_total_area
            tri_in_flat += prim_flat_tris
    analysis.flat_area_ratio = flat_area / total_area if total_area else 0.0
    analysis.tri_in_flat_ratio = tri_in_flat / analysis.tris if analysis.tris else 0.0
    analysis.category = category_for(analysis.name)
    analysis.target = target_for(analysis.category, config)
    analysis.skip = analysis.tris <= analysis.target
    return analysis


def find_blender(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if os.environ.get("BLENDER"):
        return os.environ["BLENDER"]
    found = shutil.which("blender")
    if found:
        return found
    base = Path("C:/Program Files/Blender Foundation")
    if base.exists():
        versions = sorted((p for p in base.iterdir() if (p / "blender.exe").exists()), reverse=True)
        if versions:
            return str(versions[0] / "blender.exe")
    raise FileNotFoundError("Blender not found; pass blender=... or add it to PATH")


def run_worker(blender: str, source: Path, output: Path, target: int, strategy: str, config: SimplifierConfig) -> subprocess.CompletedProcess:
    job = json.dumps(
        {
            "source": str(source),
            "output": str(output),
            "target_tris": target,
            "strategy": strategy,
            "dissolve_deg": config.dissolve_deg,
            "smooth_deg": config.smooth_deg,
            "weld": config.weld,
            "bake_resolution": config.bake_resolution,
            "remesh_resolution": config.remesh_resolution,
            "max_deviation": config.max_deviation,
            "max_tris": config.target,
            "decimate_after_retopo": config.decimate_after_retopo,
        }
    )
    command = [blender, "--background", "--factory-startup", "--python", str(WORKER_PATH), "--", job]
    return subprocess.run(command, capture_output=True, text=True)


def _worker_result(process: subprocess.CompletedProcess) -> dict | None:
    for line in (process.stdout or "").splitlines():
        if line.startswith(_RESULT_PREFIX):
            try:
                return json.loads(line[len(_RESULT_PREFIX) :])
            except json.JSONDecodeError:
                return None
    return None


def simplify_glb(path, config: SimplifierConfig, blender: str | None = None, backup_dir=None) -> dict:
    """Simplify one GLB in place. Returns a result dict; raises on failure."""
    path = Path(path)
    config.validate()
    analysis = analyze_file(path, config)
    if analysis.error:
        raise RuntimeError(f"{path.name}: {analysis.error}")
    result = {
        "path": str(path),
        "name": path.stem,
        "category": analysis.category,
        "tris_before": analysis.tris,
        "target": analysis.target,
    }
    if not config.enabled or analysis.tris <= analysis.target:
        result.update(status="skipped", strategy=None, deviation=None, tris_after=analysis.tris)
        return result
    strategy = resolve_strategy(analysis.category, config.strategy)
    executable = find_blender(blender or config.blender)
    if backup_dir is not None:
        backup = Path(backup_dir) / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(path, backup)
        result["backup"] = str(backup)
    with tempfile.TemporaryDirectory(prefix="mesh_simplify_", dir=path.parent) as staging:
        temporary = Path(staging) / path.name
        process = run_worker(executable, path.resolve(), temporary.resolve(), analysis.target, strategy, config)
        worker = _worker_result(process)
        if process.returncode != 0 or worker is None or not temporary.is_file():
            tail = (process.stderr or process.stdout or "").strip().splitlines()[-5:]
            raise RuntimeError(
                f"{path.name}: Blender exited with {process.returncode}: " + " | ".join(tail)
            )
        after = analyze_file(temporary, config)
        if after.error or after.tris == 0:
            raise RuntimeError(f"{path.name}: Blender produced an invalid or empty mesh")
        os.replace(temporary, path)
    result.update(
        status="ok",
        strategy=strategy,
        deviation=worker.get("deviation"),
        tris_after=after.tris,
    )
    return result


def collect_paths(paths, roots, only):
    files: list[Path] = []
    for item in paths:
        candidate = Path(item)
        if candidate.is_file():
            files.append(candidate)
        elif candidate.is_dir():
            files.extend(sorted(candidate.glob("**/*.glb")))
    for root in roots:
        files.extend(sorted(Path(root).glob("**/*.glb")))
    unique = sorted({p.resolve() for p in files})
    if only:
        unique = [p for p in unique if p.stem in only]
    return unique


def _print_analysis(analyses: list[FileAnalysis]) -> None:
    candidates = [a for a in analyses if not a.skip and not a.error]
    candidates.sort(key=lambda a: (-a.over_budget, a.name))
    total = sum(a.tris for a in analyses)
    print(
        f"{len(analyses)} models, {total:,} tris total; "
        f"{len(candidates)} over budget, {sum(a.skip for a in analyses)} within budget"
    )
    for item in analyses:
        if item.error:
            print(f"  ! {item.name}: {item.error}")
    print(f"{'tris':>6} {'target':>6} {'over':>6} {'flat%':>6} {'dissolv':>7}  category/model")
    for item in candidates:
        print(
            f"{item.tris:6d} {item.target:6d} {item.over_budget:6d} "
            f"{100 * item.flat_area_ratio:6.1f} {item.dissolvable:7d}  "
            f"{item.category}/{item.name}"
        )


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", help="GLB files or directories to process")
    parser.add_argument("--roots", action="append", default=[], help="directory to scan for *.glb (repeatable)")
    parser.add_argument("--apply", action="store_true", help="Simplify in place (default: report only)")
    parser.add_argument("--dry-run", action="store_true", help="Report only")
    parser.add_argument("--only", default="", help="Comma-separated model names to process")
    parser.add_argument("--strategy", choices=STRATEGIES, default="auto")
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET)
    parser.add_argument("--flat-target", type=int, default=DEFAULT_FLAT_TARGET)
    parser.add_argument("--wall-target", type=int, default=DEFAULT_WALL_TARGET)
    parser.add_argument("--max-deviation", type=float, default=DEFAULT_MAX_DEVIATION)
    parser.add_argument("--bake-resolution", type=int, default=DEFAULT_BAKE_RESOLUTION)
    parser.add_argument("--remesh-resolution", type=int, default=DEFAULT_REMESH_RESOLUTION)
    parser.add_argument("--blender", default=None)
    parser.add_argument("--backup-dir", default=None, help="Copy originals here before --apply overwrites")
    parser.add_argument("--report", default=None, help="Write a JSON report to this path")
    args = parser.parse_args(argv)
    args.only = {item.strip() for item in args.only.split(",") if item.strip()}
    return args


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    config = SimplifierConfig.from_mapping(
        {
            "strategy": args.strategy,
            "target": args.target,
            "flat_target": args.flat_target,
            "wall_target": args.wall_target,
            "max_deviation": args.max_deviation,
            "bake_resolution": args.bake_resolution,
            "remesh_resolution": args.remesh_resolution,
            "blender": args.blender,
        },
        where="cli",
    )
    paths = collect_paths(args.paths, args.roots or ([] if args.paths else ["."]), args.only)
    if not paths:
        print("mesh_simplify: no GLB files matched", file=sys.stderr)
        return 2
    analyses = [analyze_file(path, config) for path in paths]
    _print_analysis(analyses)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps([asdict(a) for a in analyses], indent=2), encoding="utf-8")
        print(f"Report: {report_path}")
    if not args.apply or args.dry_run:
        return 0
    failures = 0
    for analysis in analyses:
        if analysis.error or analysis.skip:
            continue
        try:
            result = simplify_glb(analysis.path, config, backup_dir=args.backup_dir)
            print(
                f"  [ok] {result['name']}: {result['tris_before']} -> {result['tris_after']} "
                f"tris (target {result['target']}, {result['strategy']})"
            )
        except Exception as error:  # noqa: BLE001 - surface any worker failure
            failures += 1
            print(f"  [FAILED] {analysis.name}: {error}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
