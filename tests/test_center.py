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
    assert all(s["equipped"] for s in sites)  # в эмуляторе оснащены все светофорные объекты участка
    assert {s["kind"] for s in sites} == {"crossing", "tee", "cross"}


@pytest.mark.parametrize("query, expected", [
    ("краснодарская краснодонская", "x-krasnodarskaya-krasnodonskaya"),
    ("Новороссийская × Ставропольская", "t-novorossiyskaya-stavropolskaya"),
    ("Краснодарская Совхозная", "x-krasnodarskaya-sovkhoznaya"),
])
def test_search_by_intersection(client, query, expected):
    res = client.get("/api/search", params={"q": query}).json()
    assert res and res[0]["site_id"] == expected


def test_unknown_site_is_404(client):
    assert client.get("/api/sites/net-takogo").status_code == 404
    assert client.get("/api/sites/net-takogo/state").status_code == 404


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


def test_operator_standard_program_and_back(client):
    """Стандартный режим: система отпускает светофор на штатную программу контроллера и возвращает управление."""
    sid = "p-krasnodonskaya-mid"
    rt = client.app.state.hub.runtimes[sid]
    rt._t_start = 0.0  # без паузы на подключение камер
    st = client.post(f"/api/sites/{sid}/mode", json={"mode": "local"}).json()
    assert st["forced"] == "local"
    for k in range(5):
        rt.tick(k * 0.1, 0.1)
    st = client.get(f"/api/sites/{sid}/state").json()
    assert st["engaged"] is False and st["mode_title"].startswith("Статический режим")
    assert rt.link is None or rt.link.active is False
    st = client.post(f"/api/sites/{sid}/mode", json={"mode": None}).json()
    assert st["forced"] is None and rt.released is False
    assert client.post(f"/api/sites/{sid}/mode", json={"mode": "что-то"}).status_code == 422
    # переключатель «Статический режим»
    st = client.post(f"/api/sites/{sid}/static", json={"on": True}).json()
    assert st["static"] == "operator" and st["forced"] == "local"
    st = client.post(f"/api/sites/{sid}/static", json={"on": False}).json()
    assert st["static"] is None and rt.released is False


def test_one_unusable_camera_means_static_and_red(client):
    """Одна камера из двух непригодна — весь объект на штатной программе, на карте красный."""
    sid = "p-krasnodonskaya-mid"
    rt = client.app.state.hub.runtimes[sid]
    rt._t_start = 0.0
    cams = list(rt.streams)
    for c in cams:
        rt.streams[c].health = lambda: None
    rt.tick(0.0, 0.1)
    rt.streams[cams[0]].health = lambda: "blind"
    rt.tick(0.1, 0.1)
    s = next(x for x in client.app.state.hub.overview() if x["id"] == sid)
    assert s["static"] == "auto" and s["status"] == "alarm" and rt.engaged is False
    rt.streams[cams[0]].health = lambda: None
    rt.tick(0.2, 0.1)
    assert rt.auto_static is None
