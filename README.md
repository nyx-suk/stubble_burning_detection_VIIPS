# Satellite Stubble Burning Detection

This repository contains FastAPI applications and Google Earth Engine scripts for exploring satellite fire detections and agricultural burning in India. The web apps let you select a state, district, and date range, view detected hotspots on a map, and export results as CSV.

## Repository contents

| File | Purpose |
| --- | --- |
| `app_VIIRS_375m.py` | Hotspot detection using NASA/NOAA VIIRS LANCE C2 data. Runs on port 8080. |
| `app_VIIRS_collection_02_1km.py` | Hotspot analysis using the VIIRS VNP14A1 daily product. Runs on port 8000. |
| `app_VIIRS_combination.py` | Detection app combining LANCE C2 and VNP14A1 data. Runs on port 7000. |
| `main.py` | Separate FastAPI entry point; currently initializes Earth Engine but does not define the app routes used by the three standalone applications. |
| `stubble_burning_data_embedding.py` | Earth Engine analysis of fire, burned-area, atmospheric, and satellite-embedding indicators. |
| `crop_classification_data_embedding.py` | Earth Engine embedding and clustering example for crop-land analysis in Cerro Gordo County, Iowa. |
| `static/` | HTML, CSS, and JavaScript frontend assets. |

The three VIIRS applications are independent alternatives. Start the app you want to use; they are not one combined service. Each exposes state and district selection, district boundary and data checks, a detection endpoint, and CSV export. Exact detection behavior depends on the selected data source and the application’s implementation.

## Requirements

- Python 3.9 or newer
- A Google Earth Engine account with access to the datasets used by the selected app
- A Google Cloud project configured for Earth Engine

Install the Python dependencies:

```bash
git clone https://github.com/deepeshyadav760/stubble-burning-detection.git
cd stubble-burning-detection
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
```

## Configure Earth Engine

Authenticate Earth Engine before launching an app:

```bash
earthengine authenticate
```

The web application scripts initialize Earth Engine with the project ID `ee-deepeshy`. If you use a different Cloud project, update the `ee.Initialize(project=...)` calls in the script you plan to run. The analysis scripts also call Earth Engine initialization directly; configure them for your own project and credentials as needed.

## Run a web application

Run one of the following from the repository root:

```bash
python app_VIIRS_combination.py
```

Then open <http://localhost:7000>. Or run either standalone detector:

```bash
python app_VIIRS_375m.py
# Open http://localhost:8080
```

```bash
python app_VIIRS_collection_02_1km.py
# Open http://localhost:8000
```

The applications bind to localhost by default. Keep the process running while using the app. Earth Engine requests need network access and valid credentials; dataset availability and request limits can affect results.

## API overview

Each detector provides endpoints for its web interface and data workflow. Open its interactive API documentation at `/docs` after starting it. Detection is submitted with a state, district, and date range; the browser interface also provides hotspot display and CSV export.

## Run the analysis scripts

The analysis files are standalone examples, not API endpoints. They make Earth Engine calls and may retrieve results directly into Python. Review and configure their region, year, datasets, and Earth Engine project before running:

```bash
python stubble_burning_data_embedding.py
python crop_classification_data_embedding.py
```

The crop classification example currently targets Cerro Gordo County, Iowa and labels clusters using an area-based heuristic. Its output should be treated as exploratory, not as a validated crop classification. The stubble-burning analysis combines several indicators; its results likewise require independent validation before being used as authoritative measurements.

## Data and interpretation

Hotspots represent satellite fire detections, not a definitive determination that a field was intentionally burned. Spatial resolution, cloud conditions, overpass timing, product latency, and the selected detection logic all affect what is observed. The scripts and applications are research prototypes; validate results against suitable ground truth before operational or policy use.

## Dependencies and license

Python dependencies are listed in [`requirements.txt`](requirements.txt). See [`LICENSE`](LICENSE) for the repository license.
