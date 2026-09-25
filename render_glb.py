"""Render a GLB to PNG using Blender (headless).

Usage:
  blender --background --python render_glb.py -- <glb> <out_png> <size> <azimuth_deg> <elevation_deg>
"""
import bpy, sys, math, mathutils

argv = sys.argv[sys.argv.index('--') + 1:]
glb = argv[0]
out = argv[1]
size = int(argv[2]) if len(argv) > 2 else 512
az = float(argv[3]) if len(argv) > 3 else 35.0
el = float(argv[4]) if len(argv) > 4 else 22.0

# fresh scene
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=glb)

objs = [o for o in bpy.context.scene.objects if o.type == 'MESH']
if not objs:
    raise SystemExit('no mesh in glb')

mn = mathutils.Vector((1e9, 1e9, 1e9))
mx = mathutils.Vector((-1e9, -1e9, -1e9))
for o in objs:
    for c in o.bound_box:
        w = o.matrix_world @ mathutils.Vector(c)
        for i in range(3):
            mn[i] = min(mn[i], w[i]); mx[i] = max(mx[i], w[i])
center = (mn + mx) / 2
extent = max((mx - mn))
radius = max(extent / 2, 1e-6)

cam_data = bpy.data.cameras.new('cam')
cam_data.type = 'ORTHO'
cam_data.ortho_scale = extent * 1.35
cam = bpy.data.objects.new('cam', cam_data)
bpy.context.scene.collection.objects.link(cam)
bpy.context.scene.camera = cam

a = math.radians(az); e = math.radians(el)
d = radius * 8
cam.location = center + mathutils.Vector((math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e))) * d
cam.rotation_euler = (center - cam.location).to_track_quat('-Z', 'Y').to_euler()

# neutral light-gray world like the reference image
world = bpy.data.worlds.new('w')
bpy.context.scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes.get('Background')
bg.inputs[0].default_value = (0.90, 0.90, 0.90, 1.0)
bg.inputs[1].default_value = 1.0

sun_data = bpy.data.lights.new('sun', type='SUN')
sun_data.energy = 3.5
sun = bpy.data.objects.new('sun', sun_data)
bpy.context.scene.collection.objects.link(sun)
sun.rotation_euler = (math.radians(55), math.radians(10), math.radians(40))

area_data = bpy.data.lights.new('fill', type='AREA')
area_data.energy = extent * extent * 60
area_data.size = extent * 2
fill = bpy.data.objects.new('fill', area_data)
bpy.context.scene.collection.objects.link(fill)
fill.location = center + mathutils.Vector((-d, -d, d))
fill.rotation_euler = (center - fill.location).to_track_quat('-Z', 'Y').to_euler()

sc = bpy.context.scene
try:
    sc.render.engine = 'BLENDER_EEVEE_NEXT'
except TypeError:
    sc.render.engine = 'BLENDER_EEVEE'
sc.render.resolution_x = size
sc.render.resolution_y = size
sc.render.film_transparent = False
sc.render.image_settings.file_format = 'PNG'
sc.render.filepath = out
bpy.ops.render.render(write_still=True)
print('RENDERED', out)
