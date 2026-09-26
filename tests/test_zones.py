from smartcross.config import ZoneConfig
from smartcross.vision.zones import Detection, ZoneAnalyzer, point_in_polygon

W, H = 1000, 1000
ZONES = [
    ZoneConfig(id="wa", type="ped_wait", side="A", points=[(0, 0), (0.2, 0), (0.2, 1), (0, 1)]),
    ZoneConfig(id="cw", type="crosswalk", points=[(0.2, 0.4), (0.8, 0.4), (0.8, 0.6), (0.2, 0.6)]),
    ZoneConfig(id="ap", type="approach", direction="1", length_m=40,
               points=[(0.3, 0), (0.5, 0), (0.5, 0.4), (0.3, 0.4)]),
    ZoneConfig(id="ln", type="count_line", direction="1", points=[(0.3, 0.7), (0.5, 0.7)]),
]


def person(tid, x, y):
    return Detection(tid, "person", "pedestrian", (x - 10, y - 50, x + 10, y), 0.9)


def car(tid, x, y):
    return Detection(tid, "car", "vehicle", (x - 30, y - 30, x + 30, y), 0.9)


def test_point_in_polygon():
    assert point_in_polygon((5, 5), [(0, 0), (10, 0), (10, 10), (0, 10)])
    assert not point_in_polygon((15, 5), [(0, 0), (10, 0), (10, 10), (0, 10)])


def test_counts_waiting_crossing_and_approach():
    za = ZoneAnalyzer(ZONES)
    r = za.analyze([person(1, 100, 500), person(2, 150, 800), person(3, 500, 500), car(10, 400, 200)], W, H)
    assert r.ped_waiting == {"A": 2}
    assert r.ped_on_crossing == 1 and r.ped_new == 0  # not counted until the track is stable
    assert r.veh_in_approach == {"1": 1}


def test_line_crossing_counted_once_even_with_missed_frames():
    za = ZoneAnalyzer(ZONES)
    total = 0
    for i, y in enumerate([600, 650, None, None, 760, 800, 690, 720]):
        dets = [] if y is None else [car(7, 400, y)]
        total += sum(za.analyze(dets, W, H).veh_passed.values())
    assert total == 1


def test_unique_pedestrians():
    za = ZoneAnalyzer(ZONES)
    n = sum(za.analyze([person(5, 500, 500)], W, H).ped_new for _ in range(10))
    assert n == 1
    # a flickering id seen only once or twice isn't counted
    assert sum(za.analyze([person(k, 500, 500)], W, H).ped_new for k in (50, 51, 52)) == 0


def test_line_crossing_when_point_lands_on_line():
    za = ZoneAnalyzer(ZONES)
    total = sum(sum(za.analyze([car(3, 400, y)], W, H).veh_passed.values()) for y in (660, 700, 740))
    assert total == 1
