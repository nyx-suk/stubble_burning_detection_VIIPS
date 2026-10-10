"""Shared geographic helpers, district boundary lookup, and agricultural mask utilities."""

import logging
from typing import Any, Dict

import ee

from app.config import STATES_DISTRICTS
from app.services.cache import boundary_cache
from app.services.ee_client import run_with_timeout

logger = logging.getLogger(__name__)


def nominal_pixel_footprint_hectares(pixel_count: int, pixel_size_meters: float) -> float:
    """Return the summed nominal square-pixel footprint in hectares."""
    if pixel_count < 0:
        raise ValueError("pixel_count must be non-negative")
    if pixel_size_meters <= 0:
        raise ValueError("pixel_size_meters must be positive")
    return pixel_count * pixel_size_meters * pixel_size_meters / 10_000


def _fetch_boundary_from_ee(state: str, district: str) -> Dict[str, Any]:
    """Execute Earth Engine queries to retrieve district boundary GeoJSON and bounds."""
    # Load FAO/GAUL administrative boundaries
    gaul = ee.FeatureCollection("FAO/GAUL/2015/level2")

    # Filter for specific district server-side
    district_boundary = gaul.filter(ee.Filter.eq("ADM1_NAME", state)).filter(
        ee.Filter.eq("ADM2_NAME", district)
    )

    boundary_count = district_boundary.size().getInfo()

    if boundary_count == 0:
        # Try alternative GAUL field names
        district_boundary = gaul.filter(ee.Filter.eq("NAME_1", state)).filter(
            ee.Filter.eq("NAME_2", district)
        )
        boundary_count = district_boundary.size().getInfo()

    if boundary_count > 0:
        # Request only geometry properties to avoid transferring heavy GAUL attribute tables
        boundary_geom = district_boundary.geometry()
        bounds = boundary_geom.bounds().getInfo()
        boundary_geojson = district_boundary.select(["ADM1_NAME", "ADM2_NAME"]).getInfo()

        return {
            "boundary_geojson": boundary_geojson,
            "bounds": bounds["coordinates"][0],
            "geometry": boundary_geom,
            "status": "success",
        }

    return {
        "boundary_geojson": None,
        "bounds": None,
        "geometry": None,
        "status": "not_found",
    }


def get_district_boundary(state: str, district: str) -> Dict[str, Any]:
    """Get district boundary from FAO/GAUL dataset with server-side filtering, timeouts, and in-memory caching."""
    cache_key = (state, district)
    cached = boundary_cache.get(cache_key)
    if cached is not None:
        logger.debug("Boundary cache hit for %s, %s", district, state)
        return cached

    logger.info("📍 Fetching boundary for %s, %s (cache miss)", district, state)
    try:
        result = run_with_timeout(_fetch_boundary_from_ee, state, district)
        if result.get("status") == "success":
            boundary_cache.set(cache_key, result)
        return result
    except Exception as exc:
        logger.warning("❌ Boundary lookup failed for %s, %s: %s", district, state, exc)
        return {
            "boundary_geojson": None,
            "bounds": None,
            "geometry": None,
            "status": "error",
            "error": str(exc),
        }


def get_agricultural_mask(district_geom: ee.Geometry):
    """Create mask for agricultural areas using land cover data server-side."""
    try:
        # Use MODIS Land Cover Collection 6.1 for agricultural areas
        landcover = (
            ee.ImageCollection("MODIS/061/MCD12Q1")
            .filterDate("2020-01-01", "2022-12-31")
            .first()
            .select("LC_Type1")
        )

        # Agricultural land cover types (12: Croplands, 14: Cropland/Natural vegetation mosaic)
        agri_mask = landcover.eq(12).Or(landcover.eq(14))
        return agri_mask.clip(district_geom)
    except Exception as exc:
        logger.warning("⚠️ Agricultural mask creation failed: %s", exc)
        return None
