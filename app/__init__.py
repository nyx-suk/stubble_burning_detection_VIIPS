"""Unified fire detection application package and app factory."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import ee
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.exports import router as exports_router
from app.api.fires import router as fires_router
from app.api.monitoring import router as monitoring_router
from app.api.statistics import router as statistics_router
from app.config import EARTHENGINE_PROJECT, EARTHENGINE_ENABLED

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


def create_app(initialize_earth_engine: bool | None = None) -> FastAPI:
    if initialize_earth_engine is None:
        initialize_earth_engine = EARTHENGINE_ENABLED
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if initialize_earth_engine:
            try:
                ee.Initialize(project=EARTHENGINE_PROJECT)
                logger.info("Earth Engine initialized with project %s", EARTHENGINE_PROJECT)
            except Exception:
                logger.exception("Earth Engine initialization failed; EE requests may fail")
        yield

    application = FastAPI(
        title="AgniWatch AI — Satellite Fire Monitor",
        description="Bounded VIIRS anomaly review, mixed-resolution corroboration, and local export-job workflow",
        lifespan=lifespan,
    )
    application.include_router(fires_router)
    application.include_router(monitoring_router)
    application.include_router(statistics_router)
    application.include_router(exports_router)
    application.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @application.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(ROOT / "static" / "index.html")

    @application.get("/health")
    async def health():
        return {"status": "ok", "earthengine_enabled": initialize_earth_engine,
                "earthengine_project": EARTHENGINE_PROJECT}

    return application
