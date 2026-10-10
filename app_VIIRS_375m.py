"""Compatibility import for the former 375 m standalone application.

Run the unified application with ``uvicorn main:app``. Detection logic now lives
in :mod:`services.detectors` and all product APIs are registered by ``main``.
"""

from main import app
from app.services.viirs_lance import detect_viirs_lance_c2_fires

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000)
