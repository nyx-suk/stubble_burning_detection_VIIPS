"""Daily VIIRS VNP14A1 product processing — optimised for dashboard latency.

Key changes from the original:
- Replaces two sampling rounds (10 000 + 5 000 pixels) with a single round
  capped at DEFAULT_HOTSPOTS_LIMIT to stay within EE memory limits.
- Band names are resolved once and cached; the check is done server-side with
  a FireMask >= 7 AND FireMask <= 9 image-level filter.
- All getInfo() calls are wrapped in run_with_timeout().
- Results are cached keyed on (state, district, start_date, end_date, limit).
- Logging replaces print() throughout.
"""

import logging
from typing import Any, Dict, List

import ee
from fastapi import HTTPException

from app.config import DEFAULT_HOTSPOTS_LIMIT, MAX_HOTSPOTS_LIMIT
from app.services.cache import metadata_cache, query_cache
from app.services.ee_client import handle_ee_error, run_with_timeout
from app.services.gee_analysis import (
    get_agricultural_mask,
    get_district_boundary,
    nominal_pixel_footprint_hectares,
)

logger = logging.getLogger(__name__)

current_fire_data: Dict[str, Any] = {
    "hotspots": [],
    "state": "",
    "district": "",
    "date_range": "",
    "fusion_info": {},
}

_VNP14A1_COLLECTION = "NASA/VIIRS/002/VNP14A1"


def is_viirs_fire_mask(value) -> bool:
    """True for documented VNP14A1 active-fire classes (7, 8, or 9)."""
    try:
        return int(round(float(value))) in (7, 8, 9)
    except (TypeError, ValueError):
        return False


def _resolve_vnp14a1_bands(collection: ee.ImageCollection) -> Dict[str, Any]:
    """Return a mapping of logical role → actual band name; cached for the process lifetime."""
    cache_key = "vnp14a1_bands"
    cached = metadata_cache.get(cache_key)
    if cached is not None:
        return cached

    available: List[str] = run_with_timeout(
        collection.first().bandNames().getInfo, timeout_seconds=15.0
    )
    resolved = {
        "fire_mask": "FireMask" if "FireMask" in available else None,
        "max_frp": "MaxFRP" if "MaxFRP" in available else None,
        "all_available": available,
    }
    metadata_cache.set(cache_key, resolved)
    logger.info("VNP14A1 band resolution: %s", {k: v for k, v in resolved.items() if k != "all_available"})
    return resolved


def _sample_fire_hotspots(
    viirs_collection: ee.ImageCollection,
    district_geom: ee.Geometry,
    limit: int,
) -> List[Dict[str, Any]]:
    """
    Sample confirmed fire hotspots (FireMask class 7, 8, or 9) from a VNP14A1 collection.
    A single server-side filtered .sample() call keeps memory usage bounded.
    """
    bands = _resolve_vnp14a1_bands(viirs_collection)
    fire_mask_band = bands["fire_mask"]
    max_frp_band = bands["max_frp"]
    frp_available = max_frp_band is not None

    # --- Server-side fire detection mask ------------------------------------
    if fire_mask_band is None:
        logger.warning("FireMask band not available in VNP14A1; returning empty result.")
        return []

    fire_mask_img = viirs_collection.select(fire_mask_band).max()
    # Only classes 7 (low), 8 (nominal), 9 (high) are confirmed fires
    fire_pixels = fire_mask_img.gte(7).And(fire_mask_img.lte(9))

    # Add MaxFRP validation where available (provides additional filter on server)
    if max_frp_band:
        max_frp_img = viirs_collection.select(max_frp_band).max()
        fire_pixels = fire_pixels.And(max_frp_img.gte(0))  # filter nulls

    # Apply agricultural mask
    agri_mask = get_agricultural_mask(district_geom)
    if agri_mask is not None:
        agri_buffered = agri_mask.focal_max(radius=500, kernelType="circle", units="meters")
        fire_pixels = fire_pixels.And(agri_buffered)

    # --- Request only the bands the frontend needs -------------------------
    sample_bands = [b for b in [fire_mask_band, max_frp_band] if b]
    sample_image = viirs_collection.select(sample_bands).max()

    capped = min(limit, MAX_HOTSPOTS_LIMIT)
    fire_sample = sample_image.updateMask(fire_pixels).sample(
        region=district_geom,
        scale=1000,  # native VNP14A1 resolution
        numPixels=capped,
        geometries=True,
        dropNulls=True,
        seed=0,
    )

    raw: Dict[str, Any] = run_with_timeout(fire_sample.getInfo)
    features = raw.get("features", [])
    logger.info("VNP14A1 raw sample: %d features", len(features))

    hotspots: List[Dict[str, Any]] = []
    seen_coords: set = set()
    hotspot_id = 1

    for feature in features:
        geom = feature.get("geometry", {})
        props = feature.get("properties", {})
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates", [])
        if len(coords) < 2:
            continue

        # Validate FireMask class server-side already filtered these; double-check client-side
        mask_val = None
        if fire_mask_band in props:
            try:
                mask_val = int(round(float(props[fire_mask_band])))
            except (ValueError, TypeError):
                pass
        if mask_val is not None and not is_viirs_fire_mask(mask_val):
            continue

        lat = round(coords[1], 5)
        lng = round(coords[0], 5)
        key = (lat, lng)
        if key in seen_coords:
            continue
        seen_coords.add(key)

        # Resolve FRP
        if frp_available and max_frp_band in props:
            try:
                raw_frp = float(props[max_frp_band])
                frp_value = raw_frp if raw_frp > 0 else (5.0 if mask_val == 9 else (1.5 if mask_val == 8 else 0.5))
            except (ValueError, TypeError):
                frp_value = 5.0 if mask_val == 9 else (1.5 if mask_val == 8 else 0.5)
        elif mask_val is not None:
            frp_value = 5.0 if mask_val == 9 else (1.5 if mask_val == 8 else 0.5)
        else:
            frp_value = 0.5

        # Confidence from documented FireMask classes
        if mask_val == 9:
            confidence = "High"
        elif mask_val == 8:
            confidence = "Nominal"
        elif mask_val == 7:
            confidence = "Low"
        else:
            confidence = "High" if frp_value >= 5.0 else ("Nominal" if frp_value >= 1.0 else "Low")

        intensity = (
            "Very High" if frp_value >= 15.0
            else "High" if frp_value >= 5.0
            else "Medium" if frp_value >= 1.0
            else "Low" if frp_value >= 0.1
            else "Very Low"
        )

        hotspots.append({
            "id": hotspot_id,
            "latitude": coords[1],
            "longitude": coords[0],
            "frp": round(frp_value, 3),
            "fire_mask_class": mask_val,
            "confidence": confidence,
            "intensity": intensity,
            "source": f"VIIRS VNP14A1 (Class {mask_val} – {confidence})" if mask_val else "VIIRS VNP14A1",
        })
        hotspot_id += 1

    logger.info("VNP14A1 confirmed hotspots: %d", len(hotspots))
    return hotspots


# ---------------------------------------------------------------------------
# Public detector
# ---------------------------------------------------------------------------

_EMPTY_RESPONSE_TEMPLATE = {
    "fire_pixels": 0,
    "nominal_footprint_hectares": 0.0,
    "fire_area_hectares": 0.0,
    "area_disclaimer": (
        "Nominal footprint represents aggregate spatial extent of 1km pixels containing active "
        "thermal detections, NOT verified burned area. Agricultural fires are typically sub-pixel. "
        "Verified burned area requires high-resolution burn scar analysis (e.g. Sentinel-2 dNBR)."
    ),
    "legacy_fire_area_field_note": (
        "Deprecated compatibility alias for nominal_footprint_hectares; this is not verified burned area."
    ),
    "pixel_resolution_meters": 1000,
    "fire_hotspots": 0,
    "max_frp": 0.0,
    "hotspots_coordinates": [],
    "hotspots_geojson": {"type": "FeatureCollection", "features": []},
    "detection_method": "VNP14A1 FireMask Detection",
    "dataset": _VNP14A1_COLLECTION,
    "resolution": "1km",
}


def detect_viirs_fires_enhanced(
    state: str,
    district: str,
    start_date: str,
    end_date: str,
    limit: int = DEFAULT_HOTSPOTS_LIMIT,
) -> Dict[str, Any]:
    """Detect fire hotspots from VIIRS VNP14A1 data for a district and date range."""
    global current_fire_data

    cache_key = ("daily", state, district, start_date, end_date, limit)
    cached = query_cache.get(cache_key)
    if cached is not None:
        logger.debug("Query cache hit: VNP14A1 %s/%s %s–%s", state, district, start_date, end_date)
        return cached

    logger.info("🔥 VNP14A1 detection: %s, %s (%s to %s)", district, state, start_date, end_date)

    try:
        boundary_info = get_district_boundary(state, district)

        if boundary_info["status"] != "success":
            result = {
                **_EMPTY_RESPONSE_TEMPLATE,
                "district_boundary": None,
                "map_bounds": None,
                "viirs_images_used": 0,
                "sensitivity": "Boundary not found",
                "status": f"Error: District '{district}' not found in '{state}'",
            }
            return result

        district_geom: ee.Geometry = boundary_info["geometry"]

        viirs = (
            ee.ImageCollection(_VNP14A1_COLLECTION)
            .filterDate(start_date, end_date)
            .filterBounds(district_geom)
        )

        image_count: int = run_with_timeout(viirs.size().getInfo)
        logger.info("VNP14A1 images in range: %d", image_count)

        if image_count == 0:
            current_fire_data = {
                "hotspots": [],
                "state": state,
                "district": district,
                "date_range": f"{start_date} to {end_date}",
            }
            result = {
                **_EMPTY_RESPONSE_TEMPLATE,
                "district_boundary": boundary_info["boundary_geojson"],
                "map_bounds": boundary_info["bounds"],
                "viirs_images_used": 0,
                "sensitivity": "No VIIRS data available",
                "status": "No VIIRS data found for this period",
            }
            query_cache.set(cache_key, result)
            return result

        hotspots = _sample_fire_hotspots(viirs, district_geom, limit)

        fire_pixels = len(hotspots)
        nominal_footprint = nominal_pixel_footprint_hectares(fire_pixels, 1000)
        max_frp = max((h["frp"] for h in hotspots), default=0.0)

        current_fire_data = {
            "hotspots": hotspots,
            "state": state,
            "district": district,
            "date_range": f"{start_date} to {end_date}",
            "nominal_footprint_hectares": round(nominal_footprint, 2),
        }

        hotspots_geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [h["longitude"], h["latitude"]]},
                    "properties": {
                        "id": h["id"],
                        "frp": h["frp"],
                        "confidence": h["confidence"],
                        "intensity": h["intensity"],
                        "fire_mask_class": h.get("fire_mask_class"),
                        "source": h["source"],
                    },
                }
                for h in hotspots
            ],
        }

        result = {
            "fire_pixels": fire_pixels,
            "nominal_footprint_hectares": round(nominal_footprint, 2),
            "fire_area_hectares": round(nominal_footprint, 2),
            "legacy_fire_area_field_note": _EMPTY_RESPONSE_TEMPLATE["legacy_fire_area_field_note"],
            "area_disclaimer": _EMPTY_RESPONSE_TEMPLATE["area_disclaimer"],
            "pixel_resolution_meters": 1000,
            "fire_hotspots": fire_pixels,
            "max_frp": round(max_frp, 3),
            "hotspots_coordinates": hotspots,
            "hotspots_geojson": hotspots_geojson,
            "district_boundary": boundary_info["boundary_geojson"],
            "map_bounds": boundary_info["bounds"],
            "viirs_images_used": image_count,
            "detection_method": "VNP14A1 FireMask Detection",
            "dataset": _VNP14A1_COLLECTION,
            "resolution": "1km",
            "sensitivity": "FireMask classes 7–9 (Low, Nominal, High confidence)",
            "status": "Success",
            "pagination": {
                "returned": fire_pixels,
                "limit": limit,
                "note": "For complete historical datasets, use /api/exports/batch.",
            },
        }
        query_cache.set(cache_key, result)
        return result

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("VNP14A1 detection failed: %s", exc)
        http_exc = handle_ee_error(exc)
        # Return a structured error response rather than raising for backward compatibility
        return {
            **_EMPTY_RESPONSE_TEMPLATE,
            "district_boundary": None,
            "map_bounds": None,
            "viirs_images_used": 0,
            "sensitivity": "Error occurred during detection",
            "status": f"Error: {http_exc.detail}",
        }
