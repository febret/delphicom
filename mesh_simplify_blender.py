"""Blender worker that simplifies a GLB mesh in place.

Invoked by ``mesh_simplify.py`` (and therefore by ``bake.py``) as:

    blender --background --factory-startup --python mesh_simplify_blender.py -- <json>

The JSON payload carries the source/output paths, the target triangle count,
the strategy and the cleanup tolerances. Blender's bundled Python runs this
file, so it must stay self-contained.

Strategies
----------
``decimate``
    Weld/limited-dissolve where safe, then collapse-decimate. Cheap and ideal
    for flat planar assets, but thin shells and organic solids tear at low
    budgets.
``retopo``
    Voxel-remesh into a manifold surface, decimate to budget, re-UV and bake
    the original base colour and normal maps onto the new topology. Handles
    thin shells and organic solids without tearing. Destroys fine branching
    geometry, so it is not used for foliage.

An adaptive pass measures how far the simplified surface drifts from the
original and raises the budget when the deviation exceeds a threshold.
"""

from __future__ import annotations

import json
import math
import sys

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree


def _clear_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _mesh_objects() -> list:
    return [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]


def _activate(obj) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _triangle_count(obj) -> int:
    mesh = obj.data
    mesh.calc_loop_triangles()
    return len(mesh.loop_triangles)


def _in_edit(fn) -> None:
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    fn()
    bpy.ops.object.mode_set(mode="OBJECT")


def _decimate_object(obj, target_tris: int) -> int:
    for _ in range(8):
        current = _triangle_count(obj)
        if current == 0 or current <= target_tris:
            break
        ratio = max(0.005, min(1.0, target_tris / current))
        _activate(obj)
        modifier = obj.modifiers.new(name="MeshSimplify", type="DECIMATE")
        modifier.decimate_type = "COLLAPSE"
        modifier.ratio = ratio
        modifier.use_collapse_triangulate = True
        bpy.ops.object.modifier_apply(modifier=modifier.name)
        after = _triangle_count(obj)
        if after >= current - max(1, current // 100):
            break
    return _triangle_count(obj)


def _recalc_normals(obj) -> None:
    _activate(obj)
    if bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    _in_edit(lambda: bpy.ops.mesh.normals_make_consistent(inside=False))


def _shade(obj, smooth_deg: float) -> None:
    _activate(obj)
    try:
        bpy.ops.object.shade_smooth_by_angle(angle=math.radians(smooth_deg))
    except (AttributeError, RuntimeError, TypeError):
        try:
            bpy.ops.object.shade_smooth()
        except (AttributeError, RuntimeError, TypeError):
            pass


def _planar_cleanup(obj, weld: float, dissolve_deg: float) -> None:
    _activate(obj)
    if bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    def run() -> None:
        bpy.ops.mesh.remove_doubles(threshold=weld)
        bpy.ops.mesh.dissolve_limited(
            angle_limit=math.radians(dissolve_deg),
            use_dissolve_boundaries=False,
            delimit={"UV"},
        )
        bpy.ops.mesh.quads_convert_to_tris(quad_method="BEAUTY", ngon_method="BEAUTY")

    _in_edit(run)


def _per_object_targets(objects, target: int) -> dict:
    counts = {obj.name: _triangle_count(obj) for obj in objects}
    total = sum(counts.values()) or 1
    return {obj.name: max(4, round(target * counts[obj.name] / total)) for obj in objects}


def _decimate_pipeline(source: str, target: int, planar: bool, weld: float, dissolve_deg: float, smooth_deg: float) -> dict:
    _clear_scene()
    bpy.ops.import_scene.gltf(filepath=source)
    objects = _mesh_objects()
    if planar:
        for obj in objects:
            _planar_cleanup(obj, weld, dissolve_deg)
    targets = _per_object_targets(objects, target)
    after = {}
    for obj in objects:
        after[obj.name] = _decimate_object(obj, targets[obj.name])
        _recalc_normals(obj)
        _shade(obj, smooth_deg)
    return after


def _join_meshes() -> object | None:
    objects = _mesh_objects()
    if not objects:
        return None
    _activate(objects[0])
    if len(objects) > 1:
        for obj in objects:
            obj.select_set(True)
        bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


def _build_remesh(high, target: int, resolution: int):
    low = high.copy()
    low.data = high.data.copy()
    bpy.context.scene.collection.objects.link(low)
    _activate(low)
    diagonal = low.dimensions.length
    low.data.remesh_voxel_size = max(diagonal / resolution, 1e-6)
    bpy.ops.object.voxel_remesh()
    _decimate_object(low, target)
    _recalc_normals(low)
    return low


def _deviation(high, low, sample_cap: int = 4000) -> float:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    bvh_high = BVHTree.FromObject(high, depsgraph)
    bvh_low = BVHTree.FromObject(low, depsgraph)
    corner_min = Vector((1e18, 1e18, 1e18))
    corner_max = Vector((-1e18, -1e18, -1e18))
    for vertex in high.data.vertices:
        co = vertex.co
        for axis in range(3):
            corner_min[axis] = min(corner_min[axis], co[axis])
            corner_max[axis] = max(corner_max[axis], co[axis])
    diagonal = (corner_max - corner_min).length or 1.0
    distances = []
    for vertex in low.data.vertices:
        hit = bvh_high.find_nearest(vertex.co)
        if hit and hit[3] is not None:
            distances.append(hit[3])
    count = len(high.data.vertices)
    step = max(1, count // sample_cap)
    for index in range(0, count, step):
        hit = bvh_low.find_nearest(high.data.vertices[index].co)
        if hit and hit[3] is not None:
            distances.append(hit[3])
    if not distances:
        return 0.0
    distances.sort()
    percentile = distances[min(len(distances) - 1, int(0.95 * len(distances)))]
    return percentile / diagonal


def _unwrap(obj) -> None:
    _activate(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(island_margin=0.02)
    bpy.ops.object.mode_set(mode="OBJECT")


def _bake_maps(high, low, resolution: int, cage: float) -> None:
    albedo = bpy.data.images.new("MeshSimplifyBakedAlbedo", resolution, resolution, alpha=False)
    albedo.colorspace_settings.name = "sRGB"
    normal = bpy.data.images.new("MeshSimplifyBakedNormal", resolution, resolution, alpha=False)
    normal.colorspace_settings.name = "Non-Color"
    material = bpy.data.materials.new("MeshSimplifyBaked")
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    albedo_node = nodes.new("ShaderNodeTexImage")
    albedo_node.image = albedo
    normal_node = nodes.new("ShaderNodeTexImage")
    normal_node.image = normal
    normal_map = nodes.new("ShaderNodeNormalMap")
    principled = nodes.new("ShaderNodeBsdfPrincipled")
    output = nodes.new("ShaderNodeOutputMaterial")
    links = material.node_tree.links
    links.new(albedo_node.outputs["Color"], principled.inputs["Base Color"])
    links.new(normal_node.outputs["Color"], normal_map.inputs["Color"])
    links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])
    links.new(principled.outputs["BSDF"], output.inputs["Surface"])
    low.data.materials.clear()
    low.data.materials.append(material)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 1
    bake = scene.render.bake
    bake.use_selected_to_active = True
    bake.use_pass_direct = False
    bake.use_pass_indirect = False
    bake.use_pass_color = True
    bake.margin = 8
    bake.cage_extrusion = cage
    bake.max_ray_distance = cage * 5.0
    bpy.ops.object.select_all(action="DESELECT")
    high.select_set(True)
    low.select_set(True)
    bpy.context.view_layer.objects.active = low
    nodes.active = albedo_node
    albedo_node.select = True
    bpy.ops.object.bake(type="DIFFUSE")
    nodes.active = normal_node
    normal_node.select = True
    bpy.ops.object.bake(type="NORMAL")


def _retopo_pipeline(
    source: str,
    target: int,
    remesh_resolution: int,
    bake_resolution: int,
    smooth_deg: float,
    max_deviation: float,
    max_tris: int,
) -> tuple[dict, float, int, str]:
    _clear_scene()
    bpy.ops.import_scene.gltf(filepath=source)
    high = _join_meshes()
    if high is None:
        return {}, 0.0, target, "empty"
    original_tris = _triangle_count(high)
    cap = min(original_tris, max(target, max_tris))
    chosen = None
    deviation = 0.0
    attempts = 0
    current_target = target
    while True:
        low = _build_remesh(high, current_target, remesh_resolution)
        deviation = _deviation(high, low)
        attempts += 1
        if deviation <= max_deviation or current_target >= cap or attempts >= 5:
            chosen = low
            break
        bpy.data.objects.remove(low, do_unlink=True)
        current_target = min(cap, int(current_target * 1.6))
    cage = high.dimensions.length * 0.01
    _unwrap(chosen)
    _bake_maps(high, chosen, bake_resolution, cage)
    _shade(chosen, smooth_deg)
    bpy.data.objects.remove(high, do_unlink=True)
    _activate(chosen)
    return {chosen.name: _triangle_count(chosen)}, deviation, current_target, "ok"


def _decimate_run(source: str, target: int, requested: str, weld: float, dissolve_deg: float, smooth_deg: float) -> tuple[dict, str]:
    if requested == "direct":
        return _decimate_pipeline(source, target, False, weld, dissolve_deg, smooth_deg), "direct"
    if requested == "planar":
        return _decimate_pipeline(source, target, True, weld, dissolve_deg, smooth_deg), "planar"
    after = _decimate_pipeline(source, target, True, weld, dissolve_deg, smooth_deg)
    strategy = "planar"
    total = sum(after.values())
    if total > target * 1.15:
        direct = _decimate_pipeline(source, target, False, weld, dissolve_deg, smooth_deg)
        direct_total = sum(direct.values())
        if direct_total < total:
            after = direct
            strategy = "direct"
        else:
            after = _decimate_pipeline(source, target, True, weld, dissolve_deg, smooth_deg)
    return after, strategy


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if not argv:
        print("mesh_simplify_blender: missing job payload", file=sys.stderr)
        return 2
    job = json.loads(argv[0])
    source = job["source"]
    output = job["output"]
    target = int(job["target_tris"])
    dissolve_deg = float(job.get("dissolve_deg", 10.0))
    smooth_deg = float(job.get("smooth_deg", 35.0))
    weld = float(job.get("weld", 0.0001))
    resolution = int(job.get("bake_resolution", 1024))
    remesh_resolution = int(job.get("remesh_resolution", 160))
    max_deviation = float(job.get("max_deviation", 0.05))
    max_tris = int(job.get("max_tris", max(target, 500)))
    requested = str(job.get("strategy", "auto"))

    deviation = None
    effective_target = target
    if requested == "retopo":
        after, deviation, effective_target, strategy = _retopo_pipeline(
            source, target, remesh_resolution, resolution, smooth_deg, max_deviation, max_tris
        )
    else:
        after, strategy = _decimate_run(source, target, requested, weld, dissolve_deg, smooth_deg)

    if requested == "retopo" and job.get("decimate_after_retopo", False):
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.export_scene.gltf(filepath=output, export_format="GLB", export_image_format="AUTO")
        after, strategy = _decimate_run(output, target, "decimate", weld, dissolve_deg, smooth_deg)

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.export_scene.gltf(filepath=output, export_format="GLB", export_image_format="AUTO")

    result = {
        "source": source,
        "output": output,
        "strategy": strategy,
        "target": effective_target,
        "deviation": deviation,
        "tris": sum(after.values()),
        "objects": after,
    }
    result_path = job.get("result_path")
    if result_path:
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
    print("MESH_SIMPLIFY_RESULT " + json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
