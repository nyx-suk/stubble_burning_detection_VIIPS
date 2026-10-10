from fastapi.testclient import TestClient

from app import create_app
from app.services import monitoring


def client_for_db(tmp_path,monkeypatch):
    monkeypatch.setattr(monitoring,"DB_PATH",tmp_path/"api.sqlite3")
    monitoring.initialize_db()
    return TestClient(create_app(initialize_earth_engine=False))


def test_demo_map_risk_next_day_and_alert_lifecycle(tmp_path,monkeypatch):
    with client_for_db(tmp_path,monkeypatch) as client:
        loaded=client.post("/api/demo/load")
        assert loaded.status_code==200
        assert "SIMULATED" in loaded.json()["label"]
        points=client.get("/api/monitor/observations",params={"state":"Punjab","district":"Amritsar","mode":"demo"}).json()
        assert points["count"] > 0
        risk=client.get("/api/risk",params={"state":"Punjab","district":"Amritsar","mode":"demo"})
        assert risk.status_code==200 and risk.json()["score"] >= 50
        forecast=client.get("/api/prediction/next-day",params={"state":"Punjab","district":"Amritsar","mode":"demo"})
        assert forecast.json()["target_date"]=="2025-10-16"
        alerts=client.get("/api/alerts",params={"state":"Punjab","district":"Amritsar","mode":"demo"}).json()["alerts"]
        assert alerts
        alert_id=alerts[0]["id"]
        changed=client.post(f"/api/alerts/{alert_id}/acknowledge",params={"note":"Tested by officer"})
        assert changed.json()["status"]=="Acknowledged"
        assert client.get(f"/api/alerts/{alert_id}").json()["history"][-1]["note"]=="Tested by officer"
        test_alert=client.post("/api/alerts/test",params={"state":"Punjab","district":"Amritsar"})
        assert test_alert.status_code==200 and test_alert.json()["is_test"]==1
        repeated=client.post("/api/alerts/test",params={"state":"Punjab","district":"Amritsar"})
        assert repeated.json()["id"]==test_alert.json()["id"]
        assert len(repeated.json()["history"])==1


def test_csv_upload_validates_and_assigns_selected_district(tmp_path,monkeypatch):
    with client_for_db(tmp_path,monkeypatch) as client:
        csv_data="latitude,longitude,acq_date,confidence,frp\n30.25,75.85,2025-10-15,high,4.2\n999,75,2025-10-15,high,2\n"
        response=client.post("/api/observations/upload?default_state=Punjab&district=Sangrur",content=csv_data,
            headers={"Content-Type":"text/csv"})
        assert response.status_code==200
        assert response.json()["inserted"]==1 and response.json()["rejected"]==1
        row=client.get("/api/monitor/observations",params={"state":"Punjab","district":"Sangrur","mode":"uploaded"}).json()["observations"][0]
        assert row["source"]=="Uploaded VIIRS CSV"
        assert row["acquired_on"]=="2025-10-15"


def test_demo_and_alert_endpoints_have_useful_empty_and_invalid_states(tmp_path,monkeypatch):
    with client_for_db(tmp_path,monkeypatch) as client:
        empty=client.get("/api/prediction/next-day",params={"state":"Punjab","district":"Amritsar","mode":"demo"})
        assert empty.status_code==200 and empty.json()["prediction"] is None
        invalid=client.post("/api/alerts/test",params={"state":"Punjab","district":"No District"})
        assert invalid.status_code==400
        malformed=client.post("/api/observations/upload?default_state=Punjab&district=Amritsar",content="not-csv")
        assert malformed.status_code==422
