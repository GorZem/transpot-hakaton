import pytest
from fastapi.testclient import TestClient

from center.app import create_app

CFG = {"equipment": {"base_url": "http://127.0.0.1:9"}, "detector": {"model": "yolo11s.pt", "device": "cpu"}}


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"), cfg=CFG, run_hub=False, start_io=False)
    with TestClient(app) as c:
        c.app = app
        yield c


def test_overview_matches_emulator_objects(client):
    sites = client.get("/api/overview").json()["sites"]
    assert len(sites) == 14
    assert sum(s["equipped"] for s in sites) == 10
    assert {s["kind"] for s in sites} == {"crossing", "tee", "cross"}
    off = [s for s in sites if not s["equipped"]]
    assert all(s["status"] == "off" for s in off)


@pytest.mark.parametrize("query, expected", [
    ("краснодарская краснодонская", "x-krasnodarskaya-krasnodonskaya"),
    ("Новороссийская × Ставропольская", "t-novorossiyskaya-stavropolskaya"),
    ("Краснодарская Совхозная", "x-krasnodarskaya-sovkhoznaya"),
])
def test_search_by_intersection(client, query, expected):
    res = client.get("/api/search", params={"q": query}).json()
    assert res and res[0]["site_id"] == expected


def test_unequipped_site_is_read_only(client):
    info = client.get("/api/sites/x-krasnodarskaya-sovkhoznaya").json()
    assert info["equipped"] is False and info["layout"] is None
    assert client.get("/api/sites/x-krasnodarskaya-sovkhoznaya/state").status_code == 409


def test_equipped_site_info(client):
    info = client.get("/api/sites/x-krasnodonskaya-sovkhoznaya").json()
    assert {g["id"] for g in info["layout"]["groups"]} == {"veh_A", "veh_B", "ped_A", "ped_B"}
    assert len(info["cameras"]) == 2


def test_params_validation(client):
    sid = "p-krasnodonskaya-mid"
    assert client.put(f"/api/sites/{sid}/params", json={"group_threshold": 99}).status_code == 422
    r = client.put(f"/api/sites/{sid}/params", json={"group_threshold": 8})
    assert r.status_code == 200 and r.json()["group_threshold"] == 8
    assert client.get(f"/api/sites/{sid}").json()["params"]["group_threshold"] == 8


def test_no_equipment_means_no_control(client):
    rt = client.app.state.hub.runtimes["p-krasnodonskaya-mid"]
    for k in range(30):
        rt.tick(k * 0.1, 0.1)
    st = client.get("/api/sites/p-krasnodonskaya-mid/state").json()
    assert st["engaged"] is False and st["status"] == "alarm"
    assert st["mode"] == "local"
