"""Compatibility import for the former 1 km VNP14A1 application.

Run the unified application with ``uvicorn main:app``. Product-specific
processing is available from :mod:`services.detectors`.
"""

from main import app
from app.services.viirs_daily import detect_viirs_fires_enhanced

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000)
