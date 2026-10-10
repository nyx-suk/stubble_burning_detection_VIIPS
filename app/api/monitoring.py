"""Demo/import ledger, district outlooks, and alert response routes."""
import csv
import io
import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request

from app.config import STATES_DISTRICTS
from app.services import monitoring

router=APIRouter(prefix="/api",tags=["operations"])
log=logging.getLogger(__name__)


def _validate_area(state: str,district: str):
    if state not in STATES_DISTRICTS or district not in STATES_DISTRICTS[state]:
        raise HTTPException(400,detail="Select a supported state and district.")


@router.post("/demo/load")
async def load_demo_data():
    try: summary=monitoring.load_demo()
    except (OSError,csv.Error) as exc: raise HTTPException(500,detail=f"Bundled demo data could not be loaded: {exc}") from exc
    summary["alerts_created"]=monitoring.create_risk_alerts("demo")
    return summary


@router.post("/observations/upload")
async def upload_observations(request: Request,default_state: str,district: str):
    _validate_area(default_state,district)
    chunks=[]; size=0
    async for chunk in request.stream():
        size+=len(chunk)
        if size>8*1024*1024: raise HTTPException(413,detail="CSV exceeds the 8 MB upload limit.")
        chunks.append(chunk)
    raw=b"".join(chunks)
    try:
        text=raw.decode("utf-8-sig")
        reader=csv.DictReader(io.StringIO(text),strict=True)
        if not reader.fieldnames: raise ValueError("CSV header row is missing")
        rows=list(reader)
        if len(rows)>50000: raise ValueError("CSV may contain at most 50,000 rows")
        summary=monitoring.ingest(rows,"uploaded",default_state,district)
    except (UnicodeDecodeError,csv.Error,ValueError) as exc:
        raise HTTPException(400,detail=f"Could not read uploaded CSV: {exc}") from exc
    if summary["valid"]==0:
        raise HTTPException(422,detail={"message":"No usable observations. Required fields: latitude, longitude, and acq_date/date; this UI's selected district is applied when state/district columns are absent.","rejected":summary["rejection_examples"]})
    summary["alerts_created"]=monitoring.create_risk_alerts("uploaded")
    return summary


@router.get("/monitor/observations")
async def get_observations(state: str|None=None,district: str|None=None,start_date: date|None=None,
                           end_date: date|None=None,mode: str=Query("all",pattern="^(all|demo|uploaded|live)$"),limit: int=5000):
    if state and state not in STATES_DISTRICTS: raise HTTPException(400,detail="Unknown state")
    if state and district and district not in STATES_DISTRICTS[state]: raise HTTPException(400,detail="Unknown district for selected state")
    if start_date and end_date and end_date<start_date: raise HTTPException(422,detail="end_date must be on or after start_date")
    rows=monitoring.observations(state,district,start_date.isoformat() if start_date else None,
        end_date.isoformat() if end_date else None,mode,limit)
    return {"observations":rows,"count":len(rows),"mode":mode,"warning":"Demo records are simulated examples, not real fire observations." if mode=="demo" else None}


@router.get("/risk")
async def district_risk(state: str,district: str,mode: str=Query("all",pattern="^(all|demo|uploaded|live)$"),as_of: date|None=None):
    _validate_area(state,district)
    result=monitoring.risk_assessment(state,district,mode,as_of.isoformat() if as_of else None)
    result["alerts_created"]=monitoring.create_risk_alerts(mode)
    return result


@router.get("/prediction/next-day")
async def next_day_prediction(state: str,district: str,mode: str=Query("all",pattern="^(all|demo|uploaded|live)$")):
    _validate_area(state,district)
    return monitoring.next_day_outlook(state,district,mode)


@router.get("/alerts")
async def get_alerts(state: str|None=None,district: str|None=None,status: str|None=None,
                     mode: str=Query("all",pattern="^(all|demo|uploaded|live)$")):
    if state and state not in STATES_DISTRICTS: raise HTTPException(400,detail="Unknown state")
    if state and district and district not in STATES_DISTRICTS[state]: raise HTTPException(400,detail="Unknown district for selected state")
    if status and status not in ("Open","Acknowledged","Dismissed"): raise HTTPException(422,detail="Invalid alert status")
    return {"alerts":monitoring.list_alerts(state,district,status,mode)}


@router.get("/alerts/{alert_id}")
async def get_alert(alert_id: int):
    result=monitoring.get_alert(alert_id)
    if result is None: raise HTTPException(404,detail="Alert not found")
    return result


@router.post("/alerts/test")
async def create_test_alert(state: str,district: str):
    _validate_area(state,district)
    return monitoring.test_alert(state,district)


@router.post("/alerts/{alert_id}/{action}")
async def act_on_alert(alert_id: int,action: str,note: str|None=None):
    if action not in ("acknowledge","dismiss"): raise HTTPException(404,detail="Alert action not found")
    result=monitoring.update_alert(alert_id,action,note)
    if result is None: raise HTTPException(404,detail="Alert not found")
    return result
