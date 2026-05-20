"""Enclosure body generator - 200x100x50mm, 2mm wall/bottom thickness, open top."""

from build123d import Box, Solid


def gen_step() -> dict:
    # Outer shell: 200 x 100 x 50, centered at origin
    outer = Box(200, 100, 50)

    # Inner void: 196 x 96 x 48
    # 2mm walls on 4 sides (X: -98..98, Y: -48..48), 2mm bottom (Z: -23..25)
    # Cavity center Z = (-23 + 25) / 2 = 1
    inner = Box(196, 96, 48)
    inner = inner.translate((0, 0, 1))

    # Subtract inner from outer
    solid: Solid = outer.cut(inner)

    # Validate
    bounds = solid.bounding_box()
    assert solid.volume > 0, "Body must be a valid solid"
    assert bounds.min.X == -100 and bounds.max.X == 100, f"X: {bounds.min.X}..{bounds.max.X}"
    assert bounds.min.Y == -50 and bounds.max.Y == 50, f"Y: {bounds.min.Y}..{bounds.max.Y}"
    assert bounds.min.Z == -25 and bounds.max.Z == 25, f"Z: {bounds.min.Z}..{bounds.max.Z}"

    return {
        "shape": solid,
        "step_output": "enclosure/enclosure_body.step",
        "export_stl": True,
        "stl_output": "enclosure/enclosure_body.stl",
        "export_3mf": True,
        "3mf_output": "enclosure/enclosure_body.3mf",
    }
