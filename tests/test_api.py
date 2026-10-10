from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import create_app, state
from app.api import fires
from main import app as unified_app


def test_dashboard_and_product_routes_are_registered():
    with TestClient(create_app(initialize_earth_engine=False)) as client:
        assert client.get("/").status_code == 200
        assert {item["id"] for item in client.get("/api/products").json()["products"]} == {
            "lance", "daily", "fusion"
        }
        assert client.get("/api/states").status_code == 200
        assert client.get("/api/statistics/latest").status_code == 200


def test_legacy_entry_modules_point_to_unified_application():
    import app_VIIRS_375m
    import app_VIIRS_collection_02_1km
    import app_VIIRS_combination

    assert app_VIIRS_375m.app is unified_app
    assert app_VIIRS_collection_02_1km.app is unified_app
    assert app_VIIRS_combination.app is unified_app


def test_detection_request_dispatches_to_product_service(monkeypatch):
    captured = {}
    source = SimpleNamespace(current_fire_data={
        "hotspots": [{"id": 1, "latitude": 30.0, "longitude": 75.0}],
        "state": "Punjab",
        "district": "Amritsar",
        "date_range": "2025-10-01 to 2025-10-02",
        "nominal_footprint_hectares": 14.0625,
    })

    def detector(*args):
        captured["args"] = args
        return {"status": "Success", "hotspots_coordinates": source.current_fire_data["hotspots"]}

    monkeypatch.setitem(fires.DETECTORS, "lance", (detector, source))
    with TestClient(create_app(initialize_earth_engine=False)) as client:
        response = client.post("/api/detect/lance", json={
            "state": "Punjab",
            "district": "Amritsar",
            "start_date": "2025-10-01",
            "end_date": "2025-10-02",
        })

    assert response.status_code == 200
    assert captured["args"] == ("Punjab", "Amritsar", "2025-10-01", "2025-10-02")
    assert state.latest_fire_data["nominal_footprint_hectares"] == 14.0625


def test_invalid_product_and_district_are_rejected():
    with TestClient(create_app(initialize_earth_engine=False)) as client:
        body = {"state": "Punjab", "district": "Unknown", "start_date": "2025-10-01", "end_date": "2025-10-02"}
        assert client.post("/api/detect/unknown", json=body).status_code == 404
        assert client.post("/api/detect/lance", json=body).status_code == 400


def test_reversed_date_range_is_rejected_by_schema():
    with TestClient(create_app(initialize_earth_engine=False)) as client:
        response = client.post("/api/detect/lance", json={
            "state": "Punjab",
            "district": "Amritsar",
            "start_date": "2025-10-02",
            "end_date": "2025-10-01",
        })
    assert response.status_code == 422
