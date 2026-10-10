"""Compatibility import for the former VIIRS fusion application.

Run the unified application with ``uvicorn main:app``. Fusion processing now
lives in :mod:`services.detectors` and uses the shared dashboard/API.
"""

from main import app
from app.services.fire_fusion import detect_fusion_viirs_fires

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000)
