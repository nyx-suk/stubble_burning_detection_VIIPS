"""Local observation ledger, transparent district outlooks, and persistent alerts."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import ALERT_RISK_THRESHOLD, MONITOR_DB_PATH, STATES_DISTRICTS

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(MONITOR_DB_PATH)
if not DB_PATH.is_absolute():
    DB_PATH = ROOT / DB_PATH
DEMO_PATH = ROOT / "data" / "demo_viirs_observations.csv"


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def initialize_db():
    with _connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS observations (
          record_id TEXT PRIMARY KEY, state TEXT NOT NULL, district TEXT NOT NULL,
          latitude REAL NOT NULL, longitude REAL NOT NULL, acquired_on TEXT NOT NULL,
          source TEXT NOT NULL, confidence TEXT, frp REAL, mode TEXT NOT NULL,
          details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_observations_area_date ON observations(state,district,acquired_on);
        CREATE INDEX IF NOT EXISTS ix_observations_mode ON observations(mode);
        CREATE TABLE IF NOT EXISTS alerts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL UNIQUE,
          state TEXT NOT NULL, district TEXT NOT NULL, severity TEXT NOT NULL,
          risk_score INTEGER NOT NULL, reason TEXT NOT NULL, recommendation TEXT NOT NULL,
          evidence_level TEXT NOT NULL, mode TEXT NOT NULL, is_test INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'Open', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_alerts_status ON alerts(status,created_at);
        CREATE TABLE IF NOT EXISTS alert_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id INTEGER NOT NULL,
          action TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY(alert_id) REFERENCES alerts(id)
        );
        """)


def normalize_observations(rows: list[dict[str, Any]], mode: str, default_state: str|None=None, default_district: str|None=None):
    aliases = {"acq_date":"acquired_on", "date":"acquired_on",
        "latitude":"latitude", "lat":"latitude", "longitude":"longitude", "lon":"longitude", "lng":"longitude",
        "state":"state", "district":"district", "frp":"frp", "confidence":"confidence", "source":"source",
        "satellite":"source", "instrument":"source"}
    valid=[]; rejected=[]
    for line, original in enumerate(rows, start=2):
        row={aliases.get(str(k).strip().lower()):v for k,v in original.items() if aliases.get(str(k).strip().lower())}
        try:
            lat=float(row["latitude"]); lon=float(row["longitude"])
            if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180): raise ValueError("coordinate out of range")
            acquired=str(row.get("acquired_on") or "")[:10]
            acquired=date.fromisoformat(acquired).isoformat()
            state=str(row.get("state") or default_state or "").strip(); district=str(row.get("district") or default_district or "").strip()
            if not state or not district: raise ValueError("state and district required")
            if state not in STATES_DISTRICTS or district not in STATES_DISTRICTS[state]: raise ValueError("unknown state/district; select a supported area")
            frp_value=row.get("frp")
            frp=float(frp_value) if frp_value not in (None, "") else None
            if frp is not None and (not math.isfinite(frp) or frp < 0): raise ValueError("FRP must be a finite non-negative value")
            conf=str(row.get("confidence") or "Unrated").strip()
            source=str(row.get("source") or "Uploaded VIIRS CSV").strip()
            clean={"state":state,"district":district,"latitude":lat,"longitude":lon,"acquired_on":acquired,
                "source":source,"confidence":conf,"frp":frp,"mode":mode,"details":original}
            clean["record_id"]=hashlib.sha256(json.dumps([mode,state,district,round(lat,5),round(lon,5),acquired,source],separators=(",",":" )).encode()).hexdigest()
            valid.append(clean)
        except (KeyError,TypeError,ValueError) as exc:
            rejected.append({"row":line,"reason":str(exc)})
    return valid,rejected


def ingest(rows: list[dict[str, Any]], mode: str, default_state: str|None=None, default_district: str|None=None):
    valid,rejected=normalize_observations(rows,mode,default_state,default_district)
    inserted=0
    now=datetime.now(timezone.utc).isoformat()
    with _connect() as db:
        for row in valid:
            cur=db.execute("""INSERT OR IGNORE INTO observations
                (record_id,state,district,latitude,longitude,acquired_on,source,confidence,frp,mode,details_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (row["record_id"],row["state"],row["district"],row["latitude"],row["longitude"],row["acquired_on"],
                 row["source"],row["confidence"],row["frp"],row["mode"],json.dumps(row["details"],default=str),now))
            inserted+=cur.rowcount
    dates=[r["acquired_on"] for r in valid]
    return {"received":len(rows),"valid":len(valid),"inserted":inserted,"duplicates":len(valid)-inserted,
        "rejected":len(rejected),"rejection_examples":rejected[:10],"mode":mode,
        "start_date":min(dates) if dates else None,"end_date":max(dates) if dates else None}


def load_demo():
    if not DEMO_PATH.is_file(): raise FileNotFoundError(f"Bundled demo data is missing: {DEMO_PATH.name}")
    with DEMO_PATH.open(newline="",encoding="utf-8-sig") as f: rows=list(csv.DictReader(f))
    summary=ingest(rows,"demo")
    dates=[r["acquired_on"] for r in normalize_observations(rows,"demo")[0]]
    summary.update({"label":"SIMULATED DEMONSTRATION DATA — not live satellite detections",
        "start_date":min(dates) if dates else None,"end_date":max(dates) if dates else None})
    return summary


def observations(state: str | None = None, district: str | None = None, start_date: str | None = None,
                 end_date: str | None = None, mode: str = "all", limit: int = 5000):
    clauses=[]; params=[]
    for col,val in (("state",state),("district",district),("acquired_on >=",start_date),("acquired_on <=",end_date)):
        if val:
            if col.endswith(" >=") or col.endswith(" <="):
                field,op=col.split(); clauses.append(f"{field} {op} ?")
            else: clauses.append(f"{col} = ?")
            params.append(val)
    if mode!="all": clauses.append("mode = ?"); params.append(mode)
    where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
    sql="SELECT * FROM observations"+where+" ORDER BY acquired_on DESC LIMIT ?"; params.append(min(max(int(limit),1),10000))
    with _connect() as db: rows=[dict(r) for r in db.execute(sql,params).fetchall()]
    for row in rows:
        row["details"]=json.loads(row.pop("details_json","{}"))
    return rows


def risk_assessment(state: str, district: str, mode: str="all", as_of: str | None=None):
    rows=observations(state,district,mode=mode,limit=10000)
    if not rows:
        return {"state":state,"district":district,"mode":mode,"score":None,"category":"Insufficient data","evidence_level":"No data",
            "as_of":as_of,"factors":[{"name":"Observation coverage","points":0,"evidence":"No observations in selected data scope"}],
            "recommendation":"Load demo data, upload VIIRS detections, or connect Earth Engine before assessing this district.",
            "urgency":"No action from this dataset","follow_up":"After data is available"}
    latest=max(date.fromisoformat(r["acquired_on"]) for r in rows)
    ref=date.fromisoformat(as_of) if as_of else latest
    recent_start=ref-timedelta(days=6); prior_start=ref-timedelta(days=13); prior_end=ref-timedelta(days=7); month_start=ref-timedelta(days=29)
    recent=[r for r in rows if recent_start<=date.fromisoformat(r["acquired_on"])<=ref]
    prior=[r for r in rows if prior_start<=date.fromisoformat(r["acquired_on"])<=prior_end]
    month=[r for r in rows if month_start<=date.fromisoformat(r["acquired_on"])<=ref]
    locations={}
    for r in month:
        k=(round(r["latitude"],2),round(r["longitude"],2)); locations[k]=locations.get(k,0)+1
    recurrent=sum(n>=2 for n in locations.values())
    f_recent=min(35,len(recent)*7); f_hist=min(25,len(month)*2); f_recurrence=min(20,recurrent*5)
    if len(recent)==0: f_trend=0
    elif len(prior)==0: f_trend=5 if len(recent)>0 else 0
    else: f_trend=max(0,min(15,round(15*(len(recent)-len(prior))/max(1,len(prior)))))
    f_season=10 if ref.month in (9,10,11) else 0
    score=min(100,f_recent+f_hist+f_recurrence+f_trend+f_season)
    category="Critical" if score>=75 else "High" if score>=50 else "Moderate" if score>=25 else "Low"
    evidence="Moderate evidence" if len(month)>=10 else "Limited evidence"
    factors=[{"name":"Recent detections (7 days)","points":f_recent,"evidence":f"{len(recent)} observed record(s)"},
        {"name":"Activity (30 days)","points":f_hist,"evidence":f"{len(month)} observed record(s)"},
        {"name":"Location recurrence","points":f_recurrence,"evidence":f"{recurrent} coordinate cell(s) repeated within 30 days"},
        {"name":"Change vs prior 7 days","points":f_trend,"evidence":f"Recent {len(recent)} vs preceding {len(prior)}"},
        {"name":"Burning-season context","points":f_season,"evidence":"September–November rule" if f_season else "Outside the configured seasonal window"}]
    if score>=75:
        rec="Coordinate an agricultural officer visit and targeted field verification; notify the district response lead."
        urgency="Urgent"; follow="Within 6 hours"
    elif score>=50:
        rec="Inspect repeated locations and contact local agricultural staff for targeted crop-residue outreach."
        urgency="High"; follow="Within 24 hours"
    elif score>=25:
        rec="Share residue-management guidance with local extension staff and review detections again tomorrow."
        urgency="Routine prevention"; follow="Within 3 days"
    else:
        rec="Continue routine monitoring; this score does not rule out unobserved fires."
        urgency="Monitor"; follow="Next scheduled review"
    return {"state":state,"district":district,"mode":mode,"score":score,"category":category,"evidence_level":evidence,
        "observations_7d":len(recent),"observations_30d":len(month),"previous_7d":len(prior),"as_of":ref.isoformat(),
        "factors":factors,"recommendation":rec,"urgency":urgency,"follow_up":follow,
        "method":"Transparent rule-based index (not a trained or validated model). Thresholds: Low 0–24; Moderate 25–49; High 50–74; Critical 75–100."}


def next_day_outlook(state: str, district: str, mode: str="all"):
    risk=risk_assessment(state,district,mode)
    if risk["evidence_level"]=="No data":
        return {"state":state,"district":district,"mode":mode,"prediction":None,"evidence_level":"No data",
            "message":"No historical observations are available. Load/upload data before generating an outlook."}
    as_of=date.fromisoformat(risk["as_of"])
    outlook="Elevated" if risk["score"]>=50 else "Watch" if risk["score"]>=25 else "Lower observed activity"
    return {"state":state,"district":district,"mode":mode,"target_date":(as_of+timedelta(days=1)).isoformat(),
        "target":"Whether the district records elevated VIIRS thermal-anomaly activity on the next day",
        "prediction":outlook,"rule_score":risk["score"],"evidence_level":risk["evidence_level"],
        "basis":{"recent_7d":risk["observations_7d"],"prior_7d":risk["previous_7d"],"seasonal_context":as_of.month in (9,10,11)},
        "limitations":"Rule-based risk outlook, not a calibrated probability or validated fire prediction. Weather, crop area, satellite overpass, cloud and independent field labels are not included. A lower outlook is not a no-fire guarantee."}


def _severity(score): return "Critical" if score>=75 else "High" if score>=50 else "Moderate" if score>=25 else "Low"


def create_risk_alerts(mode: str="all"):
    rows=observations(mode=mode,limit=10000)
    areas=sorted({(r["state"],r["district"]) for r in rows})
    created=[]
    for state,district in areas:
        risk=risk_assessment(state,district,mode)
        if risk["score"]<ALERT_RISK_THRESHOLD: continue
        severity=_severity(risk["score"]); key=f"risk:{mode}:{state}:{district}:{risk['as_of']}:{severity}"
        now=datetime.now(timezone.utc).isoformat()
        with _connect() as db:
            reason="; ".join(f'{f["name"]}: {f["evidence"]}' for f in risk["factors"] if f["points"])
            cur=db.execute("""INSERT OR IGNORE INTO alerts(dedupe_key,state,district,severity,risk_score,reason,recommendation,
                evidence_level,mode,is_test,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,0,'Open',?,?)""",
                (key,state,district,severity,risk["score"],reason,
                 risk["recommendation"],risk["evidence_level"],mode,now,now))
            if cur.rowcount:
                alert_id=cur.lastrowid
                db.execute("INSERT INTO alert_events(alert_id,action,note,created_at) VALUES(?,?,?,?)",
                    (alert_id,"Generated",f"Risk threshold {ALERT_RISK_THRESHOLD} crossed.",now))
                created.append(alert_id)
            else:
                db.execute("UPDATE alerts SET risk_score=?,reason=?,recommendation=?,evidence_level=?,updated_at=? WHERE dedupe_key=? AND status='Open'",
                    (risk["score"],reason,risk["recommendation"],risk["evidence_level"],now,key))
    return created


def test_alert(state: str,district: str):
    now=datetime.now(timezone.utc).isoformat(); day=date.today().isoformat()
    key=f"test:{state}:{district}:{day}"
    with _connect() as db:
        cur=db.execute("""INSERT OR IGNORE INTO alerts(dedupe_key,state,district,severity,risk_score,reason,recommendation,
            evidence_level,mode,is_test,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,'Open',?,?)""",
            (key,state,district,"Test",0,"User-triggered test alert. Simulated lifecycle check; no incident is asserted.",
             "Acknowledge or dismiss this test alert to verify the workflow.","Simulated","demo",now,now))
        row=db.execute("SELECT id FROM alerts WHERE dedupe_key=?",(key,)).fetchone()
        if cur.rowcount:
            db.execute("INSERT INTO alert_events(alert_id,action,note,created_at) VALUES(?,?,?,?)",
                (row["id"],"Test generated","Simulated test alert",now))
        return get_alert(row["id"],db)


def list_alerts(state: str|None=None,district: str|None=None,status: str|None=None,mode: str="all",limit=100):
    where=[]; args=[]
    for name,value in (("state",state),("district",district),("status",status)):
        if value: where.append(name+"=?"); args.append(value)
    if mode!="all": where.append("mode=?"); args.append(mode)
    sql="SELECT * FROM alerts"+(" WHERE "+" AND ".join(where) if where else "")+" ORDER BY created_at DESC LIMIT ?"; args.append(min(max(int(limit),1),500))
    with _connect() as db: return [dict(r) for r in db.execute(sql,args).fetchall()]


def get_alert(alert_id: int,db=None):
    own=db is None; db=db or _connect()
    try:
        alert=db.execute("SELECT * FROM alerts WHERE id=?",(alert_id,)).fetchone()
        if not alert:return None
        result=dict(alert); result["history"]=[dict(x) for x in db.execute("SELECT action,note,created_at FROM alert_events WHERE alert_id=? ORDER BY id",(alert_id,)).fetchall()]
        return result
    finally:
        if own: db.close()


def update_alert(alert_id: int,action: str,note: str|None=None):
    if action not in ("acknowledge","dismiss"): raise ValueError("Unsupported alert action")
    status="Acknowledged" if action=="acknowledge" else "Dismissed"; now=datetime.now(timezone.utc).isoformat()
    with _connect() as db:
        cur=db.execute("UPDATE alerts SET status=?,updated_at=? WHERE id=?",(status,now,alert_id))
        if not cur.rowcount:return None
        db.execute("INSERT INTO alert_events(alert_id,action,note,created_at) VALUES(?,?,?,?)",(alert_id,status,note,now))
    return get_alert(alert_id)


initialize_db()
