"""Enclosure assembly - body + removable lid."""


def gen_step() -> dict:
    identity = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    # Lid sits on top: body max Z=25, lid min Z=-1 (after flange) -> Z=26 lifts it to sit flush
    lid_transform = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 26, 0, 0, 0, 1]

    return {
        "instances": [
            {
                "path": "enclosure/enclosure_body.step",
                "name": "enclosure_body",
                "transform": identity,
            },
            {
                "path": "enclosure/enclosure_lid.step",
                "name": "enclosure_lid",
                "transform": lid_transform,
            },
        ],
        "step_output": "enclosure/enclosure_assembly.step",
    }
