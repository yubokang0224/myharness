"""Enclosure 2D cutting patterns (DXF) - laser/plasma cut flat patterns.

Dimensions: 200 x 100 x 50mm, wall/bottom thickness 2mm, removable lid.

Panel layout (cut from sheet, thickness 2mm):
  - Base:          200 x 100  mm
  - Long sides x2: 200 x 46   mm  (50 - 2*2 = 46mm height after corner notches)
  - Short sides x2: 96 x 46   mm  (100 - 2*2 = 96mm width after corner notches)
  - Lid:            200 x 100  mm (with 0.5mm clearance flange rails)
"""

from build123d import Box, Solid


def gen_step() -> dict:
    # Dummy shape - this file only generates DXF, not STEP
    dummy: Solid = Box(0.001, 0.001, 0.001)
    return {
        "shape": dummy,
        "step_output": "enclosure/enclosure_2d.dxf.step",
    }


def gen_dxf() -> dict:
    import ezdxf
    from ezdxf.units import MM

    doc = ezdxf.new(dxfversion="R2018")
    msp = doc.modelspace()

    # Material thickness
    t = 2.0

    # ============================================================
    # 1. Base plate: 200 x 100
    # ============================================================
    base_w, base_d = 200.0, 100.0
    base_layer = "BASE"
    if base_layer not in doc.layers:
        doc.layers.add(base_layer, color=7)

    # Draw rectangle centered at origin
    msp.add_line((-base_w / 2, -base_d / 2), (base_w / 2, -base_d / 2), dxfattribs={"layer": base_layer})
    msp.add_line((base_w / 2, -base_d / 2), (base_w / 2, base_d / 2), dxfattribs={"layer": base_layer})
    msp.add_line((base_w / 2, base_d / 2), (-base_w / 2, base_d / 2), dxfattribs={"layer": base_layer})
    msp.add_line((-base_w / 2, base_d / 2), (-base_w / 2, -base_d / 2), dxfattribs={"layer": base_layer})

    # Dimension annotations for base
    msp.add_line((-base_w / 2, -base_d / 2 - 8), (-base_w / 2, -base_d / 2 - 15), dxfattribs={"layer": "DIMENSION", "lineweight": -1})
    msp.add_line((base_w / 2, -base_d / 2 - 8), (base_w / 2, -base_d / 2 - 15), dxfattribs={"layer": "DIMENSION", "lineweight": -1})
    msp.add_line((-base_w / 2, -base_d / 2 - 13), (base_w / 2, -base_d / 2 - 13), dxfattribs={"layer": "DIMENSION", "lineweight": -1})
    msp.add_text("200", dxfattribs={"height": 3, "layer": "DIMENSION"})
    msp.add_text("100", dxfattribs={"height": 3, "layer": "DIMENSION"})

    # ============================================================
    # 2. Long side walls x2 (left/right): 200 x (50-2*2) = 200 x 46
    # ============================================================
    long_wall_w = 200.0
    long_wall_h = 50.0 - 2 * t  # 46mm
    side_layer = "SIDE"
    if side_layer not in doc.layers:
        doc.layers.add(side_layer, color=5)

    # Wall A
    wall_a_x = -base_w / 2 - 60
    msp.add_line((wall_a_x, 0), (wall_a_x + long_wall_w, 0), dxfattribs={"layer": side_layer})
    msp.add_line((wall_a_x + long_wall_w, 0), (wall_a_x + long_wall_w, long_wall_h), dxfattribs={"layer": side_layer})
    msp.add_line((wall_a_x + long_wall_w, long_wall_h), (wall_a_x, long_wall_h), dxfattribs={"layer": side_layer})
    msp.add_line((wall_a_x, long_wall_h), (wall_a_x, 0), dxfattribs={"layer": side_layer})

    # Dimension for long wall height
    msp.add_line((wall_a_x - 5, 0), (wall_a_x - 12, 0), dxfattribs={"layer": "DIMENSION", "lineweight": -1})
    msp.add_line((wall_a_x - 5, long_wall_h), (wall_a_x - 12, long_wall_h), dxfattribs={"layer": "DIMENSION", "lineweight": -1})
    msp.add_line((wall_a_x - 10, 0), (wall_a_x - 10, long_wall_h), dxfattribs={"layer": "DIMENSION", "lineweight": -1})
    msp.add_text("46", dxfattribs={"height": 3, "layer": "DIMENSION"})

    # Wall B (next to Wall A)
    wall_b_x = wall_a_x + long_wall_w + 10
    msp.add_line((wall_b_x, 0), (wall_b_x + long_wall_w, 0), dxfattribs={"layer": side_layer})
    msp.add_line((wall_b_x + long_wall_w, 0), (wall_b_x + long_wall_w, long_wall_h), dxfattribs={"layer": side_layer})
    msp.add_line((wall_b_x + long_wall_w, long_wall_h), (wall_b_x, long_wall_h), dxfattribs={"layer": side_layer})
    msp.add_line((wall_b_x, long_wall_h), (wall_b_x, 0), dxfattribs={"layer": side_layer})

    # Label
    msp.add_text("LONG SIDE x2", dxfattribs={"height": 3, "layer": "DIMENSION"})

    # ============================================================
    # 3. Short side walls x2 (front/back): (100-2*2) x (50-2*2) = 96 x 46
    # ============================================================
    short_wall_w = 100.0 - 2 * t  # 96mm
    short_wall_h = 46.0
    short_layer = "SHORT_SIDE"
    if short_layer not in doc.layers:
        doc.layers.add(short_layer, color=4)

    # Wall C
    wall_c_x = -base_w / 2 - 30
    wall_c_y = long_wall_h + 15
    msp.add_line((wall_c_x, wall_c_y), (wall_c_x + short_wall_w, wall_c_y), dxfattribs={"layer": short_layer})
    msp.add_line((wall_c_x + short_wall_w, wall_c_y), (wall_c_x + short_wall_w, wall_c_y + short_wall_h), dxfattribs={"layer": short_layer})
    msp.add_line((wall_c_x + short_wall_w, wall_c_y + short_wall_h), (wall_c_x, wall_c_y + short_wall_h), dxfattribs={"layer": short_layer})
    msp.add_line((wall_c_x, wall_c_y + short_wall_h), (wall_c_x, wall_c_y), dxfattribs={"layer": short_layer})

    # Wall D
    wall_d_x = wall_c_x + short_wall_w + 10
    msp.add_line((wall_d_x, wall_c_y), (wall_d_x + short_wall_w, wall_c_y), dxfattribs={"layer": short_layer})
    msp.add_line((wall_d_x + short_wall_w, wall_c_y), (wall_d_x + short_wall_w, wall_c_y + short_wall_h), dxfattribs={"layer": short_layer})
    msp.add_line((wall_d_x + short_wall_w, wall_c_y + short_wall_h), (wall_d_x, wall_c_y + short_wall_h), dxfattribs={"layer": short_layer})
    msp.add_line((wall_d_x, wall_c_y + short_wall_h), (wall_d_x, wall_c_y), dxfattribs={"layer": short_layer})

    # Label
    msp.add_text("SHORT SIDE x2", dxfattribs={"height": 3, "layer": "DIMENSION"})

    # ============================================================
    # 4. Lid: 200 x 100 with mounting tabs
    # ============================================================
    lid_layer = "LID"
    if lid_layer not in doc.layers:
        doc.layers.add(lid_layer, color=3)

    lid_y = wall_c_y + short_wall_h + 20
    msp.add_line((-base_w / 2, lid_y), (base_w / 2, lid_y), dxfattribs={"layer": lid_layer})
    msp.add_line((base_w / 2, lid_y), (base_w / 2, lid_y + base_d), dxfattribs={"layer": lid_layer})
    msp.add_line((base_w / 2, lid_y + base_d), (-base_w / 2, lid_y + base_d), dxfattribs={"layer": lid_layer})
    msp.add_line((-base_w / 2, lid_y + base_d), (-base_w / 2, lid_y), dxfattribs={"layer": lid_layer})

    # Flange cutout lines (showing where bends would be)
    flange_offset = 2.0
    # Bottom flange
    msp.add_line((-base_w / 2, lid_y + 0.5), (base_w / 2, lid_y + 0.5), dxfattribs={"layer": "FLANGE", "linetype": "DASHED"})
    # Left flange
    msp.add_line((-base_w / 2 + 0.5, lid_y), (-base_w / 2 + 0.5, lid_y + base_d), dxfattribs={"layer": "FLANGE", "linetype": "DASHED"})
    # Right flange
    msp.add_line((base_w / 2 - 0.5, lid_y), (base_w / 2 - 0.5, lid_y + base_d), dxfattribs={"layer": "FLANGE", "linetype": "DASHED"})
    # Top flange
    msp.add_line((-base_w / 2, lid_y + base_d - 0.5), (base_w / 2, lid_y + base_d - 0.5), dxfattribs={"layer": "FLANGE", "linetype": "DASHED"})

    # Flange callout arrows
    msp.add_line((base_w / 2 + 10, lid_y + 50), (base_w / 2 - 5, lid_y + 50), dxfattribs={"layer": "DIMENSION"})
    msp.add_text("0.5mm flange", dxfattribs={"height": 2.5, "layer": "DIMENSION"})

    # Title
    msp.add_text("ENCLOSURE ALUMINUM BOX", dxfattribs={"height": 5, "layer": "TITLE"})
    msp.add_text("200 x 100 x 50mm | 2mm thickness | Removable lid", dxfattribs={"height": 3, "layer": "TITLE"})
    msp.add_text("Material: 6063 Aluminum Alloy", dxfattribs={"height": 3, "layer": "TITLE"})

    # Sheet metadata
    doc.header["$LTSCALE"] = 1
    doc.header["$INSUNITS"] = MM

    return {
        "document": doc,
        "dxf_output": "enclosure/enclosure_2d.dxf",
    }
