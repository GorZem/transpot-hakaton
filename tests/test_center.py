from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from center.app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"), run_hub=False)
    with TestClient(app) as c:
        c.app = app
        yield c


def tick(client, seconds: float, start: float = 0.0):
    hub = client.app.state.hub
    for k in range(round(seconds / 0.1)):
        for rt in hub.runtimes.values():
            rt.tick(start + k * 0.1, 0.1, datetime(2026, 10, 1, 12, 0))


def test_overview_has_all_pilot_sites(client):
    sites = client.get("/api/overview").json()["sites"]
    assert len(sites) == 10
    assert {s["kind"] for s in sites} == {"crossing", "tee", "cross"}
    assert all(55.6 < s["lat"] < 55.7 and 37.7 < s["lon"] < 37.8 for s in sites)


@pytest.mark.parametrize("query, expected", [
    ("краснодарская краснодонская", "x-krasnodarskaya-krasnodonskaya"),
    ("Новороссийская × Ставропольская", "t-novorossiyskaya-stavropolskaya"),
    ("ул. Судакова, 17", "p-krasnodonskaya-south"),
])
def test_search_by_intersection_and_address(client, query, expected):
    res = client.get("/api/search", params={"q": query}).json()
    assert res, query
    assert res[0]["site_id"] == expected


def test_params_validation(client):
    sid = "p-krasnodonskaya-mid"
    r = client.put(f"/api/sites/{sid}/params", json={"group_threshold": 99})
    assert r.status_code == 422
    r = client.put(f"/api/sites/{sid}/params", json={"group_threshold": 8})
    assert r.status_code == 200 and r.json()["group_threshold"] == 8
    assert client.get(f"/api/sites/{sid}").json()["params"]["group_threshold"] == 8


def test_camera_fault_changes_mode(client):
    sid = "x-krasnodonskaya-sovkhoznaya"
    tick(client, 2)
    client.post(f"/api/sites/{sid}/cameras/cam1", json={"ok": False})
    tick(client, 1, 2)
    st = client.get(f"/api/sites/{sid}/state").json()
    assert st["mode"] == "degraded" and st["status"] == "warn"
    assert any(not c["ok"] for c in st["cameras"])


def test_demo_history_feeds_stats(client):
    r = client.get("/api/sites/p-krasnodarskaya/stats", params={"hours": 168}).json()
    assert r["summary"]["ped_served"] > 1000
    assert r["summary"]["demo_share"] > 0.9
    assert len(r["series"]) > 100
