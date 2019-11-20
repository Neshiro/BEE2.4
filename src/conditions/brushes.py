"""Results relating to brushwork."""
import random
from collections import defaultdict

import brushLoc
import conditions
import srctools.logger
import template_brush
import vbsp
import vbsp_options
import tiling
import texturing
import comp_consts as const
import instance_traits
from conditions import (
    make_result, make_result_setup, SOLIDS
)
from srctools import (
    Property, NoKeyError, Vec, Output, Entity, Side, conv_bool,
    VMF,
)

from typing import Dict, Tuple, Optional, Callable, Set, Iterable, List


COND_MOD_NAME = 'Brushes'

LOGGER = srctools.logger.get_logger(__name__)


# The spawnflags that we need to toggle for each classname
FLAG_ROTATING = {
    'func_rotating': {
        'rev': 2,  # Spin counterclockwise
        'x': 4,  # Spinning in X axis
        'y': 8,  # Spin in Y axis
        'solid_flags': 64,  # 'Not solid'
    },
    'func_door_rotating': {
        'rev': 2,
        'x': 64,
        'y': 128,
        'solid_flags': 8 | 4,  # 'Non-solid to player', 'passable'
    },
    'func_rot_button': {
        'rev': 2,
        'x': 64,
        'y': 128,
        'solid_flags': 1,  # 'Not solid'
    },
    'momentary_rot_button': {
        'x': 64,
        'z': 128,
        # Reversed is set by keyvalue
        'solid_flags': 1,  # 'Not solid'
    },
    'func_platrot': {
        'x': 64,
        'y': 128,
        'solid_flags': 0,  # There aren't any
    }
}


@make_result('GenRotatingEnt')
def res_fix_rotation_axis(vmf: VMF, ent: Entity, res: Property):
    """Properly setup rotating brush entities to match the instance.

    This uses the orientation of the instance to determine the correct
    spawnflags to make it rotate in the correct direction.

    This can either modify an existing entity (which may be in an instance),
    or generate a new one. The generated brush will be 2x2x2 units large,
    and always set to be non-solid.

    For both modes:
    - `Axis`: specifies the rotation axis local to the instance.
    - `Reversed`: If set, flips the direction around.
    - `Classname`: Specifies which entity, since the spawnflags required varies.

    For application to an existing entity:
    - `ModifyTarget`: The local name of the entity to modify.

    For brush generation mode:

    - `Pos` and `name` are local to the
      instance, and will set the `origin` and `targetname` respectively.
    - `Keys` are any other keyvalues to be be set.
    - `Flags` sets additional spawnflags. Multiple values may be
       separated by `+`, and will be added together.
    - `Classname` specifies which entity will be created, as well as
       which other values will be set to specify the correct orientation.
    - `AddOut` is used to add outputs to the generated entity. It takes
       the options `Output`, `Target`, `Input`, `Inst_targ`, `Param` and `Delay`. If
       `Inst_targ` is defined, it will be used with the input to construct
       an instance proxy input. If `OnceOnly` is set, the output will be
       deleted when fired.

    Permitted entities:

       * [`func_door_rotating`](https://developer.valvesoftware.com/wiki/func_door_rotating)
       * [`func_platrot`](https://developer.valvesoftware.com/wiki/func_platrot)
       * [`func_rot_button`](https://developer.valvesoftware.com/wiki/func_rot_button)
       * [`func_rotating`](https://developer.valvesoftware.com/wiki/func_rotating)
       * [`momentary_rot_button`](https://developer.valvesoftware.com/wiki/momentary_rot_button)
    """
    des_axis = res['axis', 'z'].casefold()
    reverse = res.bool('reversed')
    door_type = res['classname', 'func_door_rotating']

    axis = Vec.with_axes(des_axis, 1).rotate_by_str(ent['angles', '0 0 0'])

    if axis.x > 0 or axis.y > 0 or axis.z > 0:
        # If it points forward, we need to reverse the rotating door
        reverse = not reverse

    try:
        flag_values = FLAG_ROTATING[door_type]
    except KeyError:
        LOGGER.warning('Unknown rotating brush type "{}"!', door_type)
        return

    name = res['ModifyTarget', '']
    if name:
        name = conditions.local_name(ent, name)
        setter_loc = ent['origin']
    else:
        # Generate a brush.
        name = conditions.local_name(ent, res['name', ''])

        pos = res.vec('Pos').rotate_by_str(ent['angles', '0 0 0'])
        pos += Vec.from_str(ent['origin', '0 0 0'])
        setter_loc = str(pos)

        door_ent = vmf.create_ent(
            classname=door_type,
            targetname=name,
            origin=pos.join(' '),
            # Extra stuff to apply to the flags (USE, toggle, etc)
            spawnflags=sum(map(
                # Add together multiple values
                srctools.conv_int,
                res['flags', '0'].split('+')
                # Make the door always non-solid!
            )) | flag_values.get('solid_flags', 0),
        )

        conditions.set_ent_keys(door_ent, ent, res)

        for output in res.find_all('AddOut'):
            door_ent.add_out(Output(
                out=output['Output', 'OnUse'],
                inp=output['Input', 'Use'],
                targ=output['Target', ''],
                inst_in=output['Inst_targ', None],
                param=output['Param', ''],
                delay=srctools.conv_float(output['Delay', '']),
                times=(
                    1 if
                    srctools.conv_bool(output['OnceOnly', False])
                    else -1),
            ))

        # Generate brush
        door_ent.solids = [vbsp.VMF.make_prism(pos - 1, pos + 1).solid]

    # Add or remove flags as needed by creating KV setters.

    for flag, value in zip(
        ('x', 'y', 'z', 'rev'),
        [axis.x != 0, axis.y != 0, axis.z != 0, reverse],
    ):
        if flag in flag_values:
            vmf.create_ent(
                'comp_kv_setter',
                origin=setter_loc,
                target=name,
                mode='flags',
                kv_name=flag_values[flag],
                kv_value_local=value,
            )

    # This ent uses a keyvalue for reversing...
    if door_type == 'momentary_rot_button':
        vmf.create_ent(
            'comp_kv_setter',
            origin=setter_loc,
            target=name,
            mode='kv',
            kv_name='StartDirection',
            kv_value_local='1' if reverse else '-1',
        )


@make_result('AlterTexture', 'AlterTex', 'AlterFace')
def res_set_texture(inst: Entity, res: Property):
    """Set the tile at a particular place to use a specific texture.

    This can only be set for an entire voxel side at once.

    `pos` is the position, relative to the instance (0 0 0 is the floor-surface).
    `dir` is the normal of the texture (pointing out)
    If `gridPos` is true, the position will be snapped so it aligns with
     the 128 brushes (Useful with fizzler/light strip items).

    `tex` is the texture to use.

    If `template` is set, the template should be an axis aligned cube. This
    will be rotated by the instance angles, and then the face with the same
    orientation will be applied to the face (with the rotation and texture).
    """
    pos = Vec.from_str(res['pos', '0 0 0'])
    pos.z -= 64  # Subtract so origin is the floor-position
    pos = pos.rotate_by_str(inst['angles', '0 0 0'])

    # Relative to the instance origin
    pos += Vec.from_str(inst['origin', '0 0 0'])

    norm = Vec.from_str(res['dir', '0 0 1']).rotate_by_str(
        inst['angles', '0 0 0']
    )

    if srctools.conv_bool(res['gridpos', '0']):
        for axis in 'xyz':
            # Don't realign things in the normal's axis -
            # those are already fine.
            if not norm[axis]:
                pos[axis] //= 128
                pos[axis] *= 128
                pos[axis] += 64

    try:
        # The user expects the tile to be at it's surface pos, not the
        # position of the voxel.
        tile = tiling.TILES[(pos - 64 * norm).as_tuple(), norm.as_tuple()]
    except KeyError:
        LOGGER.warning(
            '"{}": Could not find tile at {} with orient {}!',
            inst['targetname'],
            pos, norm,
        )
        return

    temp_id = res['template', None]
    if temp_id:
        temp = template_brush.get_scaling_template(temp_id)
    else:
        temp = template_brush.ScalingTemplate.world()

    tex = res['tex']

    if (
        tex.startswith('[') and tex.endswith(']') or
        tex.startswith('<') and tex.endswith('>')
    ):
        LOGGER.warning(
            'Special lookups for AlterTexture are '
            'no longer usable! ("{}")',
            tex
        )
    else:
        tile.override = (tex, temp)


@make_result('AddBrush')
def res_add_brush(inst: Entity, res: Property) -> None:
    """Spawn in a brush at the indicated points.

    - `point1` and `point2` are locations local to the instance, with `0 0 0`
      as the floor-position.
    - `type` is either `black` or `white`.
    - detail should be set to `1/0`. If true the brush will be a
      func_detail instead of a world brush.

    The sides will be textured with 1x1, 2x2 or 4x4 wall, ceiling and floor
    textures as needed.
    """
    import vbsp

    point1 = Vec.from_str(res['point1'])
    point2 = Vec.from_str(res['point2'])

    point1.z -= 64  # Offset to the location of the floor
    point2.z -= 64

    # Rotate to match the instance
    point1.rotate_by_str(inst['angles'])
    point2.rotate_by_str(inst['angles'])

    origin = Vec.from_str(inst['origin'])
    point1 += origin  # Then offset to the location of the instance
    point2 += origin

    try:
        tex_type = texturing.Portalable(res['type', 'black'])
    except ValueError:
        LOGGER.warning(
            'AddBrush: "{}" is not a valid brush '
            'color! (white or black)',
            res['type'],
        )
        tex_type = texturing.Portalable.BLACK

    dim = point2 - point1
    dim.max(-dim)

    # Figure out what grid size and scale is needed
    # Check the dimensions in two axes to figure out the largest
    # tile size that can fit in it.
    tile_grids = {
        'x': tiling.TileSize.TILE_4x4,
        'y': tiling.TileSize.TILE_4x4,
        'z': tiling.TileSize.TILE_4x4,
    }

    for axis in 'xyz':
        u, v = Vec.INV_AXIS[axis]
        max_size = min(dim[u], dim[v])
        if max_size % 128 == 0:
            tile_grids[axis] = tiling.TileSize.TILE_1x1
        elif dim[u] % 64 == 0 and dim[v] % 128 == 0:
            tile_grids[axis] = tiling.TileSize.TILE_2x1
        elif max_size % 64 == 0:
            tile_grids[axis] = tiling.TileSize.TILE_2x2
        else:
            tile_grids[axis] = tiling.TileSize.TILE_4x4

    grid_offset = origin // 128  # type: Vec

    # All brushes in each grid have the same textures for each side.
    random.seed(grid_offset.join(' ') + '-partial_block')

    solids = vbsp.VMF.make_prism(point1, point2)

    # Ensure the faces aren't re-textured later
    vbsp.IGNORED_FACES.update(solids.solid.sides)

    solids.north.mat = texturing.gen(
        texturing.GenCat.NORMAL,
        Vec(Vec.N),
        tex_type,
    ).get(solids.north.get_origin(), tile_grids['y'])
    solids.south.mat = texturing.gen(
        texturing.GenCat.NORMAL,
        Vec(Vec.S),
        tex_type,
    ).get(solids.north.get_origin(), tile_grids['y'])
    solids.east.mat = texturing.gen(
        texturing.GenCat.NORMAL,
        Vec(Vec.E),
        tex_type,
    ).get(solids.north.get_origin(), tile_grids['x'])
    solids.west.mat = texturing.gen(
        texturing.GenCat.NORMAL,
        Vec(Vec.W),
        tex_type,
    ).get(solids.north.get_origin(), tile_grids['x'])
    solids.top.mat = texturing.gen(
        texturing.GenCat.NORMAL,
        Vec(Vec.T),
        tex_type,
    ).get(solids.north.get_origin(), tile_grids['z'])
    solids.bottom.mat = texturing.gen(
        texturing.GenCat.NORMAL,
        Vec(Vec.B),
        tex_type,
    ).get(solids.north.get_origin(), tile_grids['z'])

    if srctools.conv_bool(res['detail', False], False):
        # Add the brush to a func_detail entity
        vbsp.VMF.create_ent(
            classname='func_detail'
        ).solids = [
            solids.solid
        ]
    else:
        # Add to the world
        vbsp.VMF.add_brush(solids.solid)


@make_result_setup('TemplateBrush')
def res_import_template_setup(res: Property):
    if res.has_children():
        temp_id = res['id']
    else:
        temp_id = res.value
        res = Property('TemplateBrush', [])

    force = res['force', ''].casefold().split()
    if 'white' in force:
        force_colour = texturing.Portalable.white
    elif 'black' in force:
        force_colour = texturing.Portalable.black
    elif 'invert' in force:
        force_colour = 'INVERT'
    else:
        force_colour = None

    if 'world' in force:
        force_type = template_brush.TEMP_TYPES.world
    elif 'detail' in force:
        force_type = template_brush.TEMP_TYPES.detail
    else:
        force_type = template_brush.TEMP_TYPES.default

    force_grid: Optional[texturing.TileSize]
    for size in texturing.TileSize:
        if size in force:
            force_grid = size
            break
    else:
        force_grid = None

    if 'bullseye' in force:
        surf_cat = texturing.GenCat.BULLSEYE
    elif 'special' in force or 'panel' in force:
        surf_cat = texturing.GenCat.PANEL
    else:
        surf_cat = texturing.GenCat.NORMAL

    replace_tex = defaultdict(list)
    for prop in res.find_key('replace', []):
        replace_tex[prop.name].append(prop.value)

    rem_replace_brush = True
    additional_ids = set()
    transfer_overlays = '1'
    try:
        replace_brush = res.find_key('replaceBrush')
    except NoKeyError:
        replace_brush_pos = None
    else:
        if replace_brush.has_children():
            replace_brush_pos = replace_brush['Pos', '0 0 0']
            additional_ids = set(map(
                srctools.conv_int,
                replace_brush['additionalIDs', ''].split(),
            ))
            rem_replace_brush = replace_brush.bool('removeBrush', True)
            transfer_overlays = replace_brush['transferOverlay', '1']
        else:
            replace_brush_pos = replace_brush.value  # type: str

        replace_brush_pos = Vec.from_str(replace_brush_pos)
        replace_brush_pos.z -= 64  # 0 0 0 defaults to the floor.

    key_values = res.find_key("Keys", [])
    if key_values:
        keys = Property("", [
            key_values,
            res.find_key("LocalKeys", []),
        ])
        # Ensure we have a 'origin' keyvalue - we automatically offset that.
        if 'origin' not in key_values:
            key_values['origin'] = '0 0 0'

        # Spawn everything as detail, so they get put into a brush
        # entity.
        force_type = template_brush.TEMP_TYPES.detail
        outputs = [
            Output.parse(prop)
            for prop in
            res.find_children('Outputs')
        ]
    else:
        keys = None
        outputs = []

    visgroup_func: Callable[[Set[str]], Iterable[str]]

    def visgroup_func(groups):
        """none = don't add any visgroups."""
        return ()

    visgroup_prop = res.find_key('visgroup', 'none')
    if visgroup_prop.has_children():
        visgroup_vars = list(visgroup_prop)
    else:
        visgroup_vars = []
        visgroup_mode = res['visgroup', 'none'].casefold()
        # Generate the function which picks which visgroups to add to the map.
        if visgroup_mode == 'none':
            pass
        elif visgroup_mode == 'choose':
            def visgroup_func(groups):
                """choose = add one random group."""
                return [random.choice(groups)]
        else:
            percent = srctools.conv_float(visgroup_mode.rstrip('%'), 0.00)
            if percent > 0.0:
                def visgroup_func(groups):
                    """Number = percent chance for each to be added"""
                    for group in groups:
                        val = random.uniform(0, 100)
                        if val <= percent:
                            yield group

    picker_vars = [
        (prop.real_name, prop.value)
        for prop in res.find_children('pickerVars')
    ]

    return (
        temp_id,
        dict(replace_tex),
        force_colour,
        force_grid,
        force_type,
        surf_cat,
        replace_brush_pos,
        rem_replace_brush,
        transfer_overlays,
        additional_ids,
        res['invertVar', ''],
        res['colorVar', ''],
        visgroup_func,
        # If true, force visgroups to all be used.
        res['forceVisVar', ''],
        visgroup_vars,
        keys,
        picker_vars,
        outputs,
        res.vec('senseOffset'),
    )


@make_result('TemplateBrush')
def res_import_template(inst: Entity, res: Property):
    """Import a template VMF file, retexturing it to match orientation.

    It will be placed overlapping the given instance. If no block is used, only
    ID can be specified.
    Options:

    - `ID`: The ID of the template to be inserted. Add visgroups to additionally
            add after a colon, comma-seperated (`temp_id:vis1,vis2`).
            Either section, or the whole value can be a `$fixup`.
    - `force`: a space-seperated list of overrides. If 'white' or 'black' is
             present, the colour of tiles will be overridden. If `invert` is
            added, white/black tiles will be swapped. If a tile size
            (`2x2`, `4x4`, `wall`, `special`) is included, all tiles will
            be switched to that size (if not a floor/ceiling). If 'world' or
            'detail' is present, the brush will be forced to that type.
    - `replace`: A block of template material -> replacement textures.
            This is case insensitive - any texture here will not be altered
            otherwise. If the material starts with a `#`, it is instead a
            list of face IDs separated by spaces. If the result evaluates
            to "", no change occurs. Both can be $fixups (parsed first).
    - `replaceBrush`: The position of a brush to replace (`0 0 0`=the surface).
            This brush will be removed, and overlays will be fixed to use
            all faces with the same normal. Can alternately be a block:

            - `Pos`: The position to replace.
            - `additionalIDs`: Space-separated list of face IDs in the template
              to also fix for overlays. The surface should have close to a
              vertical normal, to prevent rescaling the overlay.
            - `removeBrush`: If true, the original brush will not be removed.
            - `transferOverlay`: Allow disabling transferring overlays to this
              template. The IDs will be removed instead. (This can be a `$fixup`).
    - `keys`/`localkeys`: If set, a brush entity will instead be generated with
            these values. This overrides force world/detail.
            Specially-handled keys:
            - `"origin"`, offset automatically.
            - `"movedir"` on func_movelinear - set a normal surrounded by `<>`,
              this gets replaced with angles.
    - `colorVar`: If this fixup var is set
            to `white` or `black`, that colour will be forced.
            If the value is `<editor>`, the colour will be chosen based on
            the color of the surface for ItemButtonFloor, funnels or
            entry/exit frames.
    - `invertVar`: If this fixup value is true, tile colour will be
            swapped to the opposite of the current force option. This applies
            after colorVar.
    - `visgroup`: Sets how visgrouped parts are handled. Several values are possible:
            - A property block: Each name should match a visgroup, and the
              value should be a block of flags that if true enables that group.
            - 'none' (default): All extra groups are ignored.
            - 'choose': One group is chosen randomly.
            - a number: The percentage chance for each visgroup to be added.
    - `visgroup_force_var`: If set and True, visgroup is ignored and all groups
            are added.
    - `pickerVars`:
            If this is set, the results of colorpickers can be read
            out of the template. The key is the name of the picker, the value
            is the fixup name to write to. The output is either 'white',
            'black' or ''.
    - `outputs`: Add outputs to the brush ent. Syntax is like VMFs, and all names
            are local to the instance.
    - `senseOffset`: If set, colorpickers and tilesetters will be treated
            as being offset by this amount.
    """
    (
        orig_temp_id,
        replace_tex,
        force_colour,
        force_grid,
        force_type,
        surf_cat,
        replace_brush_pos,
        rem_replace_brush,
        transfer_overlays,
        additional_replace_ids,
        invert_var,
        color_var,
        visgroup_func,
        visgroup_force_var,
        visgroup_instvars,
        key_block,
        picker_vars,
        outputs,
        sense_offset,
    ) = res.value

    if ':' in orig_temp_id:
        # Split, resolve each part, then recombine.
        temp_id, visgroup = orig_temp_id.split(':', 1)
        temp_id = (
            conditions.resolve_value(inst, temp_id) + ':' +
            conditions.resolve_value(inst, visgroup)
        )
    else:
        temp_id = conditions.resolve_value(inst, orig_temp_id)

    if srctools.conv_bool(conditions.resolve_value(inst, visgroup_force_var)):
        def visgroup_func(group):
            """Use all the groups."""
            yield from group

    temp_name, visgroups = template_brush.parse_temp_name(temp_id)
    try:
        template = template_brush.get_template(temp_name)
    except template_brush.InvalidTemplateName:
        # If we did lookup, display both forms.
        if temp_id != orig_temp_id:
            LOGGER.warning(
                '{} -> "{}" is not a valid template!',
                orig_temp_id,
                temp_name
            )
        else:
            LOGGER.warning(
                '"{}" is not a valid template!',
                temp_name
            )
        # We don't want an error, just quit.
        return

    for vis_flag_block in visgroup_instvars:
        if all(conditions.check_flag(flag, inst) for flag in vis_flag_block):
            visgroups.add(vis_flag_block.real_name)

    if color_var.casefold() == '<editor>':
        # Check traits for the colour it should be.
        traits = instance_traits.get(inst)
        if 'white' in traits:
            force_colour = texturing.Portalable.white
        elif 'black' in traits:
            force_colour = texturing.Portalable.black
        else:
            LOGGER.warning(
                '"{}": Instance "{}" '
                "isn't one with inherent color!",
                temp_id,
                inst['file'],
            )
    elif color_var:
        color_val = conditions.resolve_value(inst, color_var).casefold()

        if color_val == 'white':
            force_colour = texturing.Portalable.white
        elif color_val == 'black':
            force_colour = texturing.Portalable.black
    # else: no color var

    if srctools.conv_bool(conditions.resolve_value(inst, invert_var)):
        force_colour = template_brush.TEMP_COLOUR_INVERT[force_colour]
    # else: False value, no invert.

    origin = Vec.from_str(inst['origin'])
    angles = Vec.from_str(inst['angles', '0 0 0'])
    temp_data = template_brush.import_template(
        template,
        origin,
        angles,
        targetname=inst['targetname', ''],
        force_type=force_type,
        visgroup_choose=visgroup_func,
        add_to_map=True,
        additional_visgroups=visgroups,
    )

    if key_block is not None:
        conditions.set_ent_keys(temp_data.detail, inst, key_block)
        br_origin = Vec.from_str(key_block.find_key('keys')['origin'])
        br_origin.localise(origin, angles)
        temp_data.detail['origin'] = br_origin

        move_dir = temp_data.detail['movedir', '']
        if move_dir.startswith('<') and move_dir.endswith('>'):
            move_dir = Vec.from_str(move_dir).rotate(*angles)
            temp_data.detail['movedir'] = move_dir.to_angle()

        for out in outputs:  # type: Output
            out = out.copy()
            out.target = conditions.local_name(inst, out.target)
            temp_data.detail.add_out(out)

        # Add it to the list of ignored brushes, so vbsp.change_brush() doesn't
        # modify it.
        vbsp.IGNORED_BRUSH_ENTS.add(temp_data.detail)

    if replace_brush_pos is not None:
        LOGGER.warning(
            '"{}": "Replace" option in templates is no longer available.'
            'Use tilesetters instead.',
            template.id,
        )

    template_brush.retexture_template(
        temp_data,
        origin,
        inst.fixup,
        replace_tex,
        force_colour,
        force_grid,
        surf_cat,
        sense_offset,
    )

    for picker_name, picker_var in picker_vars:
        picker_val = temp_data.picker_results.get(
            picker_name, None,
        )  # type: Optional[texturing.Portalable]
        if picker_val is not None:
            inst.fixup[picker_var] = picker_val.value
        else:
            inst.fixup[picker_var] = ''


# Position -> entity
# We merge ones within 3 blocks of our item.
CHECKPOINT_TRIG = {}  # type: Dict[Tuple[float, float, float], Entity]

# Approximately a 3-distance from
# the center.
#   x
#  xxx
# xx xx
#  xxx
#   x
CHECKPOINT_NEIGHBOURS = list(Vec.iter_grid(
    Vec(-128, -128, 0),
    Vec(128, 128, 0),
    stride=128,
))
CHECKPOINT_NEIGHBOURS.extend([
    Vec(-256, 0, 0),
    Vec(256, 0, 0),
    Vec(0, -256, 0),
    Vec(0, 256, 0),
])
# Don't include ourself..
CHECKPOINT_NEIGHBOURS.remove(Vec(0, 0, 0))


@make_result('CheckpointTrigger')
def res_checkpoint_trigger(inst: Entity, res: Property) -> None:
    """Generate a trigger underneath coop checkpoint items.

    """

    if vbsp.GAME_MODE == 'SP':
        # We can't have a respawn dropper in singleplayer.
        # Not generating the trigger means it's not going to
        # do anything.
        return

    pos = brushLoc.POS.raycast_world(
        Vec.from_str(inst['origin']),
        direction=Vec(0, 0, -1),
    )
    bbox_min = pos - (192, 192, 64)
    bbox_max = pos + (192, 192, 64)

    # Find triggers already placed next to ours, and
    # merge with them if that's the case
    for offset in CHECKPOINT_NEIGHBOURS:
        near_pos = pos + offset
        try:
            trig = CHECKPOINT_TRIG[near_pos.as_tuple()]
            break
        except KeyError:
            pass
    else:
        # None found, make one.
        trig = inst.map.create_ent(
            classname='trigger_playerteam',
            origin=pos,
        )
        trig.solids = []
        CHECKPOINT_TRIG[pos.as_tuple()] = trig

    trig.solids.append(inst.map.make_prism(
        bbox_min,
        bbox_max,
        mat=const.Tools.TRIGGER,
    ).solid)

    for prop in res:
        out = Output.parse(prop)
        out.target = conditions.local_name(inst, out.target)
        trig.add_out(out)


@make_result('SetTile', 'SetTiles')
def res_set_tile(inst: Entity, res: Property) -> None:
    """Set 4x4 parts of a tile to the given values.

    `Offset` defines the position of the upper-left tile in the grid.
    Each `Tile` section defines a row of the positions to edit like so:
        "Tile" "bbbb"
        "Tile" "b..b"
        "Tile" "b..b"
        "Tile" "bbbb"
    If `Force` is true, the specified tiles will override any existing ones
    and create the tile if necessary.
    Otherwise they will be merged in - white/black tiles will not replace
    tiles set to nodraw or void for example.

    Allowed tile characters:
    - `W`: White tile.
    - `w`: White 4x4 only tile.
    - `B`: Black tile.
    - `b`: Black 4x4 only tile.
    - `g`: Goo sides (4x4 black, in Clean).
    - `n`: Nodraw surface.
    - `1`: Convert to a 1x1 only tile, if a black/white tile.
    - `4`: Convert to a 4x4 only tile, if a black/white tile.
    - `.`: Void (remove the tile in this position).
    - `_` or ` `: Placeholder (don't modify this space).
    - `x`: Cutout Tile (Broken)
    - `o`: Cutout Tile (Partial)
    """
    origin = Vec.from_str(inst['origin'])
    angles = Vec.from_str(inst['angles'])

    offset = (res.vec('offset', -48, 48) - (0, 0, 64)).rotate(*angles)
    offset += origin

    norm = Vec(0, 0, 1).rotate(*angles)

    force_tile = res.bool('force')

    tiles: List[str] = [
        row.value
        for row in res.find_all('tile')
    ]

    for y, row in enumerate(tiles):
        for x, val in enumerate(row):
            if val in '_ ':
                continue

            pos = Vec(32 * x, -32 * y, 0).rotate(*angles) + offset

            if val == '4':
                size = tiling.TileSize.TILE_4x4
            elif val == '1':
                size = tiling.TileSize.TILE_1x1
            else:
                try:
                    new_tile = tiling.TILETYPE_FROM_CHAR[val]  # type: tiling.TileType
                except KeyError:
                    LOGGER.warning('Unknown tiletype "{}"!', val)
                else:
                    tiling.edit_quarter_tile(pos, norm, new_tile, force_tile)
                continue

            # Edit the existing tile.
            try:
                tile, u, v = tiling.find_tile(pos, norm)
            except KeyError:
                LOGGER.warning(
                    'Expected tile, but none found: {}, {}',
                    pos,
                    norm,
                )
                continue

            if tile[u, v].is_tile:
                tile[u, v] = tiling.TileType.with_color_and_size(
                    size,
                    tile[u, v].color
                )
            elif force_tile:
                # If forcing, make it black. Otherwise no need to change.
                tile[u, v] = tiling.TileType.with_color_and_size(
                    size,
                    tiling.Portalable.BLACK
                )


@make_result('addPlacementHelper')
def res_add_placement_helper(inst: Entity, res: Property):
    """Add a placement helper to a specific tile.

    `Offset` and `normal` specify the position and direction out of the surface
    the helper should be added to. If `upDir` is specified, this is the
    direction of the top of the portal.
    """
    angles = Vec.from_str(inst['angles'])

    pos = conditions.resolve_offset(inst, res['offset', '0 0 0'], zoff=-64)
    normal = res.vec('normal', 0, 0, 1).rotate(*angles)

    up_dir: Optional[Vec]
    try:
        up_dir = Vec.from_str(res['upDir']).rotate(*angles)
    except NoKeyError:
        up_dir = None

    try:
        tile = tiling.TILES[(pos - 64 * normal).as_tuple(), normal.as_tuple()]
    except KeyError:
        LOGGER.warning('No tile at {} @ {}', pos, normal)
        return

    tile.add_portal_helper(up_dir)
