"""Enclosure lid generator - removable top cover, 2mm thick with downward flange."""

from build123d import Box, Solid


def gen_step() -> dict:
    # Lid base: 200 x 100 x 2mm, centered at origin
    # Z from -1 to 1
    lid_base = Box(200, 100, 2)

    # Flange: extends DOWN 3mm from base bottom (Z=-1 to Z=-4)
    # Slightly smaller than base to catch on body rim
    # Body opening: 196x96mm, flange outer: 197x97mm (0.5mm clearance each side)
    # Flange width: 0.5mm per side, depth: 3mm

    # Left rail: X=-99.25..-98.75, Y=-48.5..48.5, Z=-4..-1
    rail_lr = Box(0.5, 97, 3)  # X=0.5, Y=97, Z=3
    rail_lr_l = rail_lr.translate((-99.125, 0, -2.5))
    rail_lr_r = rail_lr.translate((99.125, 0, -2.5))

    # Front/Back rails between L/R rails
    # X from -98.75 to 98.75 = 197.5mm wide
    rail_fb = Box(197.5, 0.5, 3)  # X=197.5, Y=0.5, Z=3
    rail_fb_f = rail_fb.translate((0, -48.75, -2.5))
    rail_fb_b = rail_fb.translate((0, 48.75, -2.5))

    # Combine all
    solid: Solid = lid_base.fuse(rail_lr_l.fuse(rail_lr_r).fuse(rail_fb_f.fuse(rail_fb_b)))

    bounds = solid.bounding_box()
    assert solid.volume > 0, "Lid must be a valid solid"
    assert bounds.min.Z >= -4.1 and bounds.min.Z <= -4.0, f"Z min: {bounds.min.Z}"
    assert bounds.max.Z >= 1.0, f"Z max: {bounds.max.Z}"

    return {
        "shape": solid,
        "step_output": "enclosure/enclosure_lid.step",
        "export_stl": True,
        "stl_output": "enclosure/enclosure_lid.stl",
        "export_3mf": True,
        "3mf_output": "enclosure/enclosure_lid.3mf",
    }
