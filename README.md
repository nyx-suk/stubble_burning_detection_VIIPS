# AgniWatch AI — Practical VIIRS Fire Monitoring & Alert Demo

This is the enhanced project from the uploaded `agni3` folder. It now has an offline path that works without Earth Engine credentials: load the bundled, clearly simulated sample; view its mapped observations; calculate a transparent district risk index and next-day activity outlook; and exercise a persistent alert lifecycle. You can also import a VIIRS/FIRMS CSV and run the same analysis on your own dated observations.

**Data honesty:** bundled rows are synthetic demo examples, never live observations. Uploaded and live satellite hotspots are thermal anomalies, not confirmed stubble burning. The “prediction” is a rules-based district activity outlook, not a trained or scientifically validated fire-occurrence model or probability. See limitations below.

## Windows quick start (Python 3.12)

Open PowerShell in the extracted project directory:

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Or double-click `run_demo.bat` to start it (the script installs requirements if needed). Open <http://127.0.0.1:8000>. Earth Engine is **off by default**, so this quick start does not require Google credentials. In the left panel:

1. Click **Load usable demo dataset**. The project loads sample rows, draws them on the map, computes a risk score and next-day outlook, and creates threshold alerts.
2. Review the selected Punjab/Amritsar data, score factors, suggested response, and next-day outlook. Change the dataset filter, or select another district after loading.
3. Acknowledge/dismiss a risk alert, or click **Test alert** to demonstrate the simulated test-alert lifecycle.
4. For your own data, select state and district, choose **Import VIIRS/FIRMS CSV**, then **Import selected CSV**. The CSV needs latitude, longitude, and `acq_date` (or `date`). `frp`, `confidence`, and `satellite` are optional. If state/district columns are absent, the selected area is assigned to every row; only upload a file scoped to that district in this case. Invalid coordinates/dates are rejected and duplicates are ignored.

Data, alerts, acknowledgements, and alert history are stored in local SQLite at `data/agniwatch.sqlite3`; restart-safe. Keep this local database private if it contains operational information.

## Enable real Earth Engine queries

The existing live LANCE C2, VNP14A1, and fused VIIRS detection routes require Earth Engine authentication. Enable them in the same PowerShell window after installing dependencies:

```powershell
earthengine authenticate
$env:EARTHENGINE_ENABLED = "true"
$env:EARTHENGINE_PROJECT = "your-earth-engine-project-id"
py -3.12 -m uvicorn main:app --host 127.0.0.1 --port 8000
```

The project must be authorized for Earth Engine. The original **Analyze hotspots** workflow calls the selected Earth Engine collection; error messages indicate when the account/project is not configured. The no-credential demo and CSV ingestion flows are independent of Earth Engine.

## What the new workflows do

### Detection and map

- **Offline demo detections:** visible, deterministic demonstration points marked `DEMO`; do not use them operationally.
- **CSV observations:** validates coordinates, dates, FRP and supported district labels; deduplicates rows; stores source/mode; shows the selected records on the Leaflet map.
- **Live satellite detection:** retains the project's NASA VIIRS Earth Engine services. A satellite anomaly is a prompt for review, not proof of agricultural burning.
- **Source separation:** UI/API distinguish `demo`, `uploaded`, `live`, and `all` stored data.

### Transparent district risk

The rule-based 0–100 score uses recent seven-day count (up to 35 points), 30-day activity (up to 25), repeated coordinate cells (up to 20), increase against the previous seven-day period (up to 15), and a September–November seasonal context (10). Thresholds: Low 0–24; Moderate 25–49; High 50–74; Critical 75–100. Empty data is labeled **Insufficient data**, not low risk. Each factor and action recommendation is shown. These weights are heuristics, not validated coefficients.

### Next-day outlook

The outlook target is whether the district records elevated VIIRS thermal-anomaly activity on the day after the latest observation date in the selected dataset. It reports a rule score and “Lower observed activity / Watch / Elevated” band, not a probability. It cannot account for weather, crop area, satellite overpass gaps, clouds, or missed events. It must not be presented as validated fire prediction.

### Alerts

High/Critical district risk at or above `ALERT_RISK_THRESHOLD` (default 50) creates a persistent alert with score, evidence, reason, recommendation and timestamps. A uniqueness key prevents repeat creation for the same district/data mode/date/risk category. Operators can acknowledge or dismiss; an audit trail records each action. The **Test alert** is explicitly simulated and stored as such. No external notification is claimed or sent.

## API endpoints

- `POST /api/demo/load` — load the bundled simulated observations and produce risk alerts.
- `POST /api/observations/upload?default_state=Punjab&district=Amritsar` — send CSV bytes with `Content-Type: text/csv` (8 MB / 50,000 row caps).
- `GET /api/monitor/observations?state=Punjab&district=Amritsar&mode=demo` — retrieve map records.
- `GET /api/risk?state=Punjab&district=Amritsar&mode=demo` — explainable district score and recommended action.
- `GET /api/prediction/next-day?state=Punjab&district=Amritsar&mode=demo` — explicitly limited next-day outlook.
- `GET /api/alerts`, `GET /api/alerts/{id}` — review alerts and audit history.
- `POST /api/alerts/{id}/acknowledge?note=...` or `/dismiss` — update the local alert workflow.
- `POST /api/alerts/test?state=Punjab&district=Amritsar` — create a labeled simulated test alert.
- Existing routes remain: `/api/detect/{lance|daily|fusion}`, `/api/exports/batch`, and FastAPI interactive docs at `/docs`.

## Configuration

Copy `.env.example` as a reference; the application does not auto-load `.env`. Set variables in PowerShell. `EARTHENGINE_ENABLED` defaults to `false`. `AGNIWATCH_DB_PATH` can move the SQLite file. `ALERT_RISK_THRESHOLD` changes the rule-alert threshold. Existing Earth Engine timeout, interactive range, and export settings are in `app/config.py`.

## Tests

```powershell
py -3.12 -m pip install -r requirements-dev.txt
py -3.12 -m pytest -q
```

Tests cover CSV validation/deduplication, persistence, risk factors, next-day outlook limits, alert generation/deduplication/acknowledgement/test lifecycle, existing detection API behavior, export jobs, and caching. Earth Engine is mocked in unit/API tests; run a separate authenticated check to validate your own Google project and the upstream VIIRS collections.

## Limits and what would make the prediction genuinely useful

A validated prediction requires a multi-year, timestamped and geographically assigned VIIRS history, observation-coverage metadata, weather and crop/residue context, and independently verified stubble-burning labels. The included dataset is only for exercising the application. The rules do not prove stubble burning and are not model metrics. The live query composites also do not automatically become the daily prediction history; use an acquisition-dated VIIRS/FIRMS CSV for historical risk/outlook analysis.

A next-day outlook from one district's short sample can be unstable. Do not use it alone to dispatch enforcement or claim a fire was prevented. Use it to prioritize awareness or field verification, then record what actually happened separately.


