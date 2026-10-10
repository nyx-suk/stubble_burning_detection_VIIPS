# Satellite Stubble Burning Detection

A FastAPI dashboard for exploring satellite fire hotspots across selected districts. It supports VIIRS LANCE C2 detections, the daily VIIRS VNP14A1 product, and a fusion view combining both sources. The dashboard displays hotspot locations and district boundaries and can export the latest results as CSV.

## Run the application

Requirements: Python 3.9+, a Google Earth Engine account, and a Google Cloud project enabled for Earth Engine.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
earthengine authenticate
```

Set the Earth Engine project if it differs from the default project ID:

```bash
export EARTHENGINE_PROJECT=your-earth-engine-project
```

Start the unified application from the repository root:

```bash
uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>. API documentation is available at <http://127.0.0.1:8000/docs>.

`main.py` is the sole application entry point. The three `app_VIIRS_*.py` files remain compatibility launchers for the unified app. Shared routes are split across `app/api/`; Earth Engine logic is in `app/services/`; request validation is in `app/schemas/fire.py`; and project settings are in `app/config.py`. The dashboard files are in `static/`.

## API

- `GET /api/products` lists detection products (`lance`, `daily`, `fusion`).
- `GET /api/states` and `GET /api/districts/{state}` provide the area selectors.
- `GET /api/district-boundary/{state}/{district}` returns the selected district boundary.
- `GET /api/{product}/test` checks Earth Engine access and returns available bands.
- `POST /api/detect/{product}` runs detection. The JSON body contains `state`, `district`, `start_date`, and `end_date` (`YYYY-MM-DD`).
- `GET /api/export-csv` exports hotspots from the latest detection request.

The original detection POST routes remain as compatibility endpoints: `/api/detect-lance-fires`, `/api/detect-stubble-burning`, and `/api/detect-fusion-fires`.

## Analysis scripts

The independent analysis scripts live in `scripts/`. The root-level script names remain compatibility launchers. Review region and year settings before running them. These are exploratory examples, not validated production measurements.

## Tests

Install the development dependencies and run the focused test suite:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

## Interpreting detections

An active-fire hotspot is a satellite thermal detection, not proof that the entire pixel burned. The dashboard’s nominal pixel footprint is not verified burned area. Burned-area estimates require separate burn-scar analysis and validation. Results can also vary with satellite overpass timing, clouds, product latency, and the selected detection source.

Python dependencies are listed in [`requirements.txt`](requirements.txt). See [`LICENSE`](LICENSE) for license terms.
