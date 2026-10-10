import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import create_app
from app.api import fires
from app.services import export_jobs
import pytest


def _wait_for(client, job_id, status):
    deadline=time.time()+3
    while time.time()<deadline:
        data=client.get(f"/api/exports/{job_id}").json()
        if data["status"]==status: return data
        time.sleep(.02)
    raise AssertionError(f"job did not reach {status}: {data}")


def test_export_job_lifecycle_persistence_and_duplicate_prevention(tmp_path, monkeypatch):
    monkeypatch.setattr(export_jobs,"EXPORT_ROOT",tmp_path)
    detector=lambda *args, **kwargs: {"status":"Success","hotspots_coordinates":[
        {"latitude":30.1,"longitude":75.2,"frp":1.4,"confidence":"Nominal"}],
        "dataset":"mocked VIIRS","nominal_footprint_hectares":14.06}
    monkeypatch.setitem(fires.DETECTORS,"lance",(detector,SimpleNamespace(current_fire_data={})))
    payload={"product":"lance","state":"Punjab","district":"Amritsar","start_date":"2025-10-01",
        "end_date":"2025-10-02","file_format":"CSV","limit":50}
    with TestClient(create_app(initialize_earth_engine=False)) as client:
        created=client.post("/api/exports/batch",json=payload)
        assert created.status_code==202
        job=created.json()
        duplicate=client.post("/api/exports/batch",json=payload)
        assert duplicate.status_code==202
        assert duplicate.json()["id"]==job["id"]
        assert duplicate.json()["deduplicated"] is True
        complete=_wait_for(client,job["id"],"completed")
        assert complete["record_count"]==1
        assert (tmp_path/f"{job['id']}.job.json").exists()
        download=client.get(complete["download_url"])
        assert download.status_code==200
        assert "latitude" in download.text
        assert client.get("/api/exports").json()["jobs"]


def test_export_failure_and_remote_destination_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr(export_jobs,"EXPORT_ROOT",tmp_path)
    calls={"count":0}
    def detector(*args, **kwargs):
        calls["count"]+=1
        if calls["count"]==1: return {"status":"Error: mocked upstream failure"}
        return {"status":"Success","hotspots_coordinates":[]}
    monkeypatch.setitem(fires.DETECTORS,"daily",(detector,SimpleNamespace(current_fire_data={})))
    with TestClient(create_app(initialize_earth_engine=False)) as client:
        remote={"product":"lance","state":"Punjab","district":"Amritsar","start_date":"2025-10-01",
            "end_date":"2025-10-02","export_destination":"drive"}
        rejected=client.post("/api/exports/batch",json=remote)
        assert rejected.status_code==501
        assert "local exports only" in rejected.json()["detail"]
        invalid_scale={**remote,"export_destination":"local","scale":375}
        assert client.post("/api/exports/batch",json=invalid_scale).status_code==400
        payload={**remote,"product":"daily","export_destination":"local"}
        job=client.post("/api/exports/batch",json=payload).json()
        failed=_wait_for(client,job["id"],"failed")
        assert "mocked upstream failure" in failed["error"]
        assert client.get(f"/api/exports/{job['id']}/download").status_code==409
        retried=client.post(f"/api/exports/{job['id']}/retry")
        assert retried.status_code==202
        assert retried.json()["id"] != job["id"]
        assert retried.json()["request"]["retry_of"] == job["id"]
        _wait_for(client,retried.json()["id"],"completed")


def test_cancel_unknown_and_unavailable_job_endpoints():
    with TestClient(create_app(initialize_earth_engine=False)) as client:
        assert client.get("/api/exports/not-a-job").status_code==404
        assert client.post("/api/exports/not-a-job/cancel").status_code==404


def test_timed_out_jobs_are_not_automatically_retried(monkeypatch):
    monkeypatch.setattr(export_jobs,"get_job",lambda job_id:{"id":job_id,"status":"failed",
        "error":"Earth Engine request timed out","request":{"product":"lance"}})
    with pytest.raises(RuntimeError,match="task history"):
        export_jobs.retry_job("EXP-TIMEOUT")
