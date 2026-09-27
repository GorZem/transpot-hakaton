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
        assert lane.out or lane.to_node.kind == "end", f"тупиковая полоса {lane.id} на {lane.edge.name}"


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
