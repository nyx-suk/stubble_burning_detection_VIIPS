from app.services import monitoring


def use_tmp_db(tmp_path,monkeypatch):
    monkeypatch.setattr(monitoring,"DB_PATH",tmp_path/"test.sqlite3")
    monitoring.initialize_db()


def test_demo_load_is_labeled_idempotent_and_supports_risk_prediction(tmp_path,monkeypatch):
    use_tmp_db(tmp_path,monkeypatch)
    first=monitoring.load_demo()
    second=monitoring.load_demo()
    assert "not live satellite" in first["label"]
    assert first["inserted"] > 20
    assert second["inserted"] == 0
    risk=monitoring.risk_assessment("Punjab","Amritsar","demo")
    assert risk["category"] in ("High","Critical")
    assert risk["score"] >= 50
    assert risk["factors"] and "not a trained" in risk["method"]
    prediction=monitoring.next_day_outlook("Punjab","Amritsar","demo")
    assert prediction["target_date"] == "2025-10-16"
    assert "not a calibrated probability" in prediction["limitations"]


def test_risk_threshold_creates_one_alert_and_response_history_persists(tmp_path,monkeypatch):
    use_tmp_db(tmp_path,monkeypatch)
    monitoring.load_demo()
    ids=monitoring.create_risk_alerts("demo")
    assert ids
    assert monitoring.create_risk_alerts("demo") == []
    alert=monitoring.get_alert(ids[0])
    updated=monitoring.update_alert(alert["id"],"acknowledge","Officer notified")
    assert updated["status"]=="Acknowledged"
    assert [event["action"] for event in updated["history"]][-1]=="Acknowledged"
    monitoring.initialize_db()
    assert monitoring.get_alert(alert["id"])["status"]=="Acknowledged"


def test_csv_normalization_rejects_bad_coordinates_and_separates_sources(tmp_path,monkeypatch):
    use_tmp_db(tmp_path,monkeypatch)
    rows=[{"lat":"30.2","lon":"75.8","acq_date":"2025-10-01","frp":"2.5"},
          {"lat":"111","lon":"75.8","acq_date":"2025-10-01","frp":"2"},
          {"lat":"30.2","lon":"75.8","acq_date":"bad","frp":"2"},
          {"lat":"NaN","lon":"75.8","acq_date":"2025-10-01","frp":"2"}]
    result=monitoring.ingest(rows,"uploaded","Punjab","Amritsar")
    assert (result["valid"],result["inserted"],result["rejected"])==(1,1,3)
    assert monitoring.observations(mode="uploaded")[0]["acquired_on"]=="2025-10-01"
    assert monitoring.observations(mode="demo")==[]
    assert monitoring.next_day_outlook("Punjab","Amritsar","demo")["prediction"] is None


def test_test_alert_is_explicitly_simulated_and_can_be_dismissed(tmp_path,monkeypatch):
    use_tmp_db(tmp_path,monkeypatch)
    alert=monitoring.test_alert("Punjab","Amritsar")
    assert alert["is_test"]==1 and alert["severity"]=="Test"
    assert "Simulated" in alert["evidence_level"]
    dismissed=monitoring.update_alert(alert["id"],"dismiss")
    assert dismissed["status"]=="Dismissed"
