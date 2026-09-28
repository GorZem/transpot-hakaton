"""Слои карты: подходы объектов с загруженностью и сигналом для транспорта."""
import math

from center.hub import _cut, _load_level


def test_cut_polyline_by_distance():
    line = _cut([[0, 0], [10, 0], [10, 50]], 3.0, 30.0)
    assert line[0] == (3.0, 0.0) and line[-1] == (10.0, 20.0)
    assert math.isclose(sum(math.dist(a, b) for a, b in zip(line, line[1:])), 27.0)


def test_load_levels():
    assert _load_level(None) == "unknown"
    assert [_load_level({"stopped": n, "moving": 0}) for n in (0, 2, 5, 9)] == ["free", "moderate", "heavy", "jam"]
