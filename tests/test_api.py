from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import create_app, state
from app.api import fires
from app.services.ee_client import inclusive_end_to_exclusive, handle_ee_error
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

    def detector(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
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
    assert captured["kwargs"] == {"limit": 1000}
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


def test_pagination_response_and_page_slicing(monkeypatch):
    source = SimpleNamespace(current_fire_data={"hotspots": [], "nominal_footprint_hectares": 0})
    points = [{"id": i, "latitude": 30 + i / 100, "longitude": 75} for i in range(6)]
    def detector(*args, **kwargs):
        return {"status": "Success", "fire_hotspots": len(points), "hotspots_coordinates": points,
                "hotspots_geojson": {"type": "FeatureCollection", "features": []}}
    monkeypatch.setitem(fires.DETECTORS, "lance", (detector, source))
    with TestClient(create_app(initialize_earth_engine=False)) as client:
        response = client.post("/api/detect/lance", json={"state":"Punjab","district":"Amritsar",
            "start_date":"2025-10-01","end_date":"2025-10-02","limit":6,"page":2,"page_size":3})
        assert response.status_code == 200
        body = response.json()
        assert [p["id"] for p in body["hotspots_coordinates"]] == [3,4,5]
        assert body["pagination"]["has_previous"] is True
        assert body["pagination"]["has_next"] is False


def test_pagination_invalid_size_offset_and_large_interactive_range():
    with TestClient(create_app(initialize_earth_engine=False)) as client:
        base={"state":"Punjab","district":"Amritsar","start_date":"2025-01-01","end_date":"2025-01-02"}
        assert client.post("/api/detect/lance",json={**base,"limit":10,"page_size":11}).status_code==422
        assert client.post("/api/detect/lance",json={**base,"limit":10,"page":2,"page_size":10}).status_code==422
        long={**base,"start_date":"2024-01-01","end_date":"2024-02-15"}
        response=client.post("/api/detect/lance",json=long)
        assert response.status_code==413
        assert "/api/exports/batch" in response.json()["detail"]


def test_earth_engine_end_date_is_inclusive_and_errors_are_actionable():
    assert inclusive_end_to_exclusive("2025-10-02") == "2025-10-03"
    assert handle_ee_error(TimeoutError("timeout")).status_code == 504
    assert "batch export" in handle_ee_error(RuntimeError("User memory limit exceeded")).detail
