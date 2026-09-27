"""Тесты эмулятора без графики: сеть, светофорные контроллеры, модель движения."""
from __future__ import annotations

import time

import pytest

from emulator.config import OBJECTS, Settings
from emulator.world.network import Network
from emulator.world.signals import SignalController
from emulator.world.sim import World


@pytest.fixture()
def net() -> Network:
    return Network.load()


def test_all_pilot_objects_found(net):
    assert set(net.objects) == {o.id for o in OBJECTS}
    for o in OBJECTS:
        node = net.objects[o.id]
        expected = {"crossing": "crossing", "cross": "junction", "tee": "junction"}[o.kind]
        assert node.kind == expected, o.id
        if o.kind == "cross":
            assert node.degree == 4
        if o.kind == "tee":
            assert node.degree == 3


def test_every_lane_leads_somewhere(net):
    for lane in net.lanes:
        if lane.ending:  # улица сужается: из этой полосы нужно перестроиться в соседнюю
            assert any(l.out for l in lane.siblings if l is not lane), f"сужение без продолжения {lane.id}"
            continue
        assert lane.out or lane.to_node.kind == "end", f"тупиковая полоса {lane.id} на {lane.edge.name}"


def test_lane_turns_follow_markings(net):
    """На подъезде из трёх полос: левая — налево, средняя — прямо, правая — направо."""
    node = net.objects["x-krasnodonskaya-stavropolskaya"]
    for arm in node.arms:
        lanes = sorted(arm.lanes_in(), key=lambda l: l.index)
        if len(lanes) == 3:
            assert [sorted(l.turns) for l in lanes] == [["left"], ["straight"], ["right"]]


def test_no_overlaps_and_lane_order(net):
    from emulator.tools.traffic_check import overlaps
    world = World(net, Settings())
    worst = total = 0
    for k in range(int(360 / 0.05)):
        world.step(0.05)
        for cont in world.containers:
            cars = cont.cars
            for i in range(1, len(cars)):
                assert cars[i - 1].s - cars[i - 1].length >= cars[i].s - 0.05, "машины в полосе наехали друг на друга"
        if k % 40 == 0:
            world.update_poses()
            o = overlaps(world.cars)
            total += o
            worst = max(worst, o)
    assert worst <= 3 and total <= 12, f"наездов: всего {total}, одновременно до {worst}"


def test_cars_use_turn_lanes(net):
    """Машина въезжает на перекрёсток из полосы, где её манёвр разрешён разметкой."""
    world = World(net, Settings())
    wrong = right = 0
    seen = set()
    for _ in range(int(300 / 0.05)):
        world.step(0.05)
        for c in world.cars:
            if hasattr(c.lane, "from_lane") and c.id not in seen and c.lane.node.kind == "junction":
                seen.add(c.id)
                right += 1
                if c.lane.turn not in c.lane.from_lane.turns:
                    wrong += 1
    assert right > 50 and wrong == 0


def test_clear_and_fill(net):
    world = World(net, Settings())
    world.warmup(60)
    assert world.cars and world.peds
    removed = world.clear()
    assert removed["cars"] > 0 and not world.cars and not world.peds
    assert all(not l.cars for l in net.lanes)
    added = world.fill()
    assert added > 30 and len(world.cars) == added
    for _ in range(200):
        world.step(0.05)


def test_crosswalks_at_objects(net):
    for oid, node in net.objects.items():
        assert any(a.crosswalk is not None for a in node.arms), oid


def _controller(net, oid):
    world = World(net, Settings())
    return world, net.objects[oid].signal


def test_conflict_sets_flash(net):
    world, sc = _controller(net, "x-krasnodarskaya-krasnodonskaya")
    ok, _ = sc.command({"veh_A": "red", "veh_B": "red", "ped_A": "red", "ped_B": "red"}, world.t)
    assert ok and sc.mode == "remote"
    ok, msg = sc.command({"veh_A": "green", "ped_A": "green"}, world.t)
    assert not ok and sc.mode == "flash"
    assert all(g.state in ("flash_yellow", "off") for g in sc.groups.values())
    ok, _ = sc.command({"veh_A": "red"}, world.t)
    assert not ok  # до сброса команды не принимаются
    sc.release(world.t)
    assert sc.mode == "local"


def test_crossing_command_and_watchdog(net):
    world, sc = _controller(net, "p-krasnodonskaya-mid")
    ok, _ = sc.command({"veh": "red", "ped": "green"}, world.t)
    assert ok and sc.groups["ped"].state == "green"
    sc.watchdog_s = 0.05
    time.sleep(0.1)
    sc.tick(0.05, world.t)
    assert sc.mode == "local"


def test_unknown_group_and_state_rejected(net):
    world, sc = _controller(net, "p-krasnodonskaya-mid")
    assert not sc.command({"veh_A": "red"}, world.t)[0]
    assert not sc.command({"ped": "yellow"}, world.t)[0]


def test_cars_stop_on_red(net):
    world = World(net, Settings())
    sc: SignalController = net.objects["p-krasnodonskaya-mid"].signal
    sc.command({"veh": "red", "ped": "red"}, world.t)
    sc.watchdog_s = 1e9
    node = net.objects["p-krasnodonskaya-mid"]
    for _ in range(int(120 / 0.05)):
        world.step(0.05)
    passed = [c for c in world.cars if any(c.lane is conn for conn in net.connectors if conn.node is node)]
    assert not passed, "на красный никто не въезжает на переход"
    queued = sum(len(a.lanes_in()[0].cars) for a in node.arms if a.lanes_in())
    assert queued > 0


def test_group_crosses_on_green(net):
    world = World(net, Settings())
    node = net.objects["p-krasnodonskaya-mid"]
    sc = node.signal
    cw = node.arms[0].crosswalk
    sc.command({"veh": "red", "ped": "red"}, world.t)
    sc.watchdog_s = 1e9
    world.add_group(cw, 0, 8)
    for _ in range(int(20 / 0.05)):
        world.step(0.05)
    waiting = sum(1 for p in world.peds if p.cw is cw and p.state == "wait" and p.group is not None)
    assert waiting == 8
    sc.command({"ped": "green"}, world.t)
    for _ in range(int(3 / 0.05)):
        world.step(0.05)
    crossing = sum(1 for p in world.peds if p.cw is cw and p.state == "cross" and p.group is not None)
    assert crossing == 8


def test_labels_inside_frame(net):
    """Разметка без видеокарты: камера объекта как NodePath, рамки внутри кадра, классы известные."""
    import numpy as np
    from panda3d.core import NodePath

    from emulator.render.cameras import Distortion, place_cameras
    from emulator.render.labels import Labeler

    s = Settings()
    world = World(net, s)
    world.warmup(120)
    world.update_poses()
    lab = Labeler(world, Distortion(s.camera), s.camera)
    spec = next(sp for sp in place_cameras(net, 6.0) if sp.id == "x-krasnodarskaya-krasnodonskaya-cam1")
    cam = NodePath("cam")
    cam.setPos(*spec.pos)
    cam.lookAt(*spec.target)
    m = cam.getMat()
    mat = np.array([[m.getCell(r, c) for c in range(4)] for r in range(4)])
    objs = lab.label(mat, np.asarray(spec.pos, float))
    assert objs, "на перекрёстке в кадре должны быть агенты"
    for o in objs:
        x1, y1, x2, y2 = o["bbox"]
        assert 0 <= x1 < x2 <= s.camera.width and 0 <= y1 < y2 <= s.camera.height
        assert o["class"] in {"car", "bus", "truck", "emergency", "person"}
        assert 0 <= o["occluded_frac"] <= 1
