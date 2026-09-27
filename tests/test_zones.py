import json
from pathlib import Path

from node.vision.geometry import CameraModel, SiteGeometry

SITES = {s["id"]: s for s in json.loads((Path(__file__).parent.parent / "data" / "sites.json").read_text(encoding="utf-8"))["sites"]}
KINDS = {"crossing": "p-krasnodonskaya-mid"}


def test_drawn_zone_overrides_computed_zone():
    """Нарисованная на кадре зона ожидания решает, где человек считается ждущим."""
    site = SITES[KINDS["crossing"]]
    cams = [CameraModel(c) for c in site["cameras"]]
    geo = SiteGeometry(site)
    cw = geo.crosswalks[0]
    spot = cw.wait[0].mean(axis=0)
    assert geo.crosswalk_at(spot)[1] == "wait"
    # оператор нарисовал зону ожидания стороны 1 в другом месте кадра обеих камер
    tiny = [[0.0, 0.0], [0.05, 0.0], [0.05, 0.05], [0.0, 0.05]]
    geo.set_image_zones(cams, {c.id: [{"key": ["wait", cw.id, 0], "points": tiny}] for c in cams})
    assert geo.crosswalk_at(spot)[1] != "wait"
    # автоматическая разметка в виде кадра совпадает с расчётной зоной
    geo.set_image_zones(cams, {c.id: geo.auto_zones(c) for c in cams})
    assert geo.crosswalk_at(spot)[1] == "wait"
