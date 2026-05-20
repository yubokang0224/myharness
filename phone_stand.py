"""
Desktop phone stand generator.
- Base: 100mm x 80mm x 3mm
- Support arm at 70 degrees from horizontal, 3mm thick
- Phone stop lip at the top
- Anti-slip rubber feet on the base bottom
"""

from math import sin, cos, radians
import build123d as bd


def gen_step():
    # === Parameters ===
    base_length = 100.0        # X direction
    base_width = 80.0          # Y direction
    base_thickness = 3.0       # Z direction
    support_thickness = 3.0    # arm thickness
    support_angle_deg = 70     # tilt from horizontal
    support_length = 55.0      # length along the arm
    support_width = 70.0       # width of the arm
    lip_thickness = 3.0        # lip depth (along arm direction)
    lip_height = 18.0          # lip height (perpendicular to arm surface)
    lip_width = 72.0           # lip width (Y direction)

    angle_rad = radians(support_angle_deg)

    # === 1. Base plate ===
    with bd.BuildPart() as base_part:
        bd.Box(
            base_length, base_width, base_thickness,
            align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN),
        )

    # === 2. Support arm ===
    # Flat box along X axis, then rotate around Y to 70° from horizontal
    # Front face of the box sits at the back edge of the base
    with bd.BuildPart() as arm_part:
        bd.Box(
            support_length, support_width, support_thickness,
            align=(bd.Align.MIN, bd.Align.CENTER, bd.Align.MIN),
        )

    arm = arm_part.part.rotate(bd.Axis.Y, -20).moved(
        bd.Location((0, base_width / 2, base_thickness))
    )

    # === 3. Phone stop lip at the arm tip ===
    # Position lip at the top tip, with its front face at the tip point
    # Then rotate it to face the correct direction
    with bd.BuildPart() as lip_part:
        bd.Box(
            lip_thickness, lip_width, lip_height,
            align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN),
        )

    # Tip position on the arm (front face after rotation):
    #   X = support_length * cos(20°) = 55 * 0.9397 = 51.68
    #   Z = support_length * sin(20°) = 55 * 0.3420 = 18.81
    tip_x = support_length * cos(radians(20))  # ~51.68
    tip_z = support_length * sin(radians(20))  # ~18.81

    # Lip center offset from tip (half lip depth behind, half lip height up)
    lip_center_x = tip_x - lip_thickness / 2  # ~50.18
    lip_center_z = tip_z + lip_height / 2      # ~27.81

    lip = lip_part.part.move(bd.Location((lip_center_x, base_width / 2, lip_center_z)))
    lip = lip.rotate(bd.Axis.Y, -20)

    # === 4. Anti-slip feet on the base bottom ===
    bump_radius = 6.0
    bump_height = 2.0
    bump_positions = [
        (-base_length / 2 + 10,  base_width / 2 - 10),
        ( base_length / 2 - 10,  base_width / 2 - 10),
        (-base_length / 2 + 10, -base_width / 2 + 10),
        ( base_length / 2 - 10, -base_width / 2 + 10),
    ]

    bumps = []
    for bx, by in bump_positions:
        with bd.BuildPart() as bump_part:
            bd.Cylinder(
                bump_radius, bump_height,
                align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MAX),
            )
        bumps.append(bump_part.part.moved(bd.Location((bx, by, 0))))

    # === 5. Union all parts ===
    solid = base_part.part + arm + lip
    for bump in bumps:
        solid = solid + bump

    assert solid.is_valid, "Generated solid is not valid"

    return {
        "shape": solid,
        "step_output": "phone_stand.step",
    }


if __name__ == "__main__":
    result = gen_step()
    print(f"Generated shape with {result['shape'].volume:.1f} mm³ volume")
    bb = result['shape'].bounding_box()
    print(f"Bounds: X=[{bb.min.X:.1f}, {bb.max.X:.1f}], "
          f"Y=[{bb.min.Y:.1f}, {bb.max.Y:.1f}], "
          f"Z=[{bb.min.Z:.1f}, {bb.max.Z:.1f}]")
