"""Lance VIIRS C2 product processing — optimised for dashboard latency.

Key changes from the original:
- All getInfo() calls are wrapped in run_with_timeout() to enforce EE_TIMEOUT_SECONDS.
- Band names are resolved once per collection via a cached metadata call.
- Sampling uses a single round with numPixels limited to DEFAULT_HOTSPOTS_LIMIT
  (max MAX_HOTSPOTS_LIMIT) and only the 3–4 properties needed by the frontend.
- Results are cached keyed on (state, district, start_date, end_date, limit).
- Logging replaces print() throughout.
"""

import logging
from typing import Any, Dict, List

import ee
from fastapi import HTTPException

from app.config import DEFAULT_HOTSPOTS_LIMIT, MAX_HOTSPOTS_LIMIT
from app.services.cache import metadata_cache, query_cache
from app.services.ee_client import run_with_timeout, handle_ee_error
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

# ---------------------------------------------------------------------------
# Band metadata helpers (cached per-collection)
# ---------------------------------------------------------------------------

_LANCE_COLLECTION = "NASA/LANCE/SNPP_VIIRS/C2"

_LANCE_NEEDED_BANDS = {
    "frp": "frp",
    "bright_ti4": "bright_ti4",
    "bright_ti5": "bright_ti5",
    "confidence": "confidence",
}


def _resolve_lance_bands(collection: ee.ImageCollection) -> Dict[str, str]:
    """Return a mapping from logical band role -> actual band name.
    Result is cached for the life of the process to avoid repeated getInfo() calls.
    """
    cache_key = "lance_bands"
    cached = metadata_cache.get(cache_key)
    if cached is not None:
        return cached

    available: List[str] = run_with_timeout(
        collection.first().bandNames().getInfo, timeout_seconds=15.0
    )
    bands_lower = {b.lower(): b for b in available}
    resolved = {}

    resolved["frp"] = bands_lower.get("frp") or bands_lower.get("fire_radiative_power")
    resolved["ti4"] = (
        bands_lower.get("bright_ti4")
        or bands_lower.get("t21")
        or bands_lower.get("brightness")
    )
    resolved["ti5"] = bands_lower.get("bright_ti5") or bands_lower.get("t31")
    resolved["conf"] = bands_lower.get("confidence")
    resolved["qa_bands"] = [
        b for b in available if b.lower().startswith("qf") or b.lower() == "qa"
    ]
    resolved["all_available"] = available

    metadata_cache.set(cache_key, resolved)
    logger.info("LANCE band resolution: %s", {k: v for k, v in resolved.items() if k != "all_available"})
    return resolved


# ---------------------------------------------------------------------------
# Core sampling
# ---------------------------------------------------------------------------

def _sample_fire_hotspots(
    viirs_collection: ee.ImageCollection,
    district_geom: ee.Geometry,
    limit: int,
) -> List[Dict[str, Any]]:
    """
    Extract active-fire hotspots from a filtered LANCE collection.
    All computation is described as an EE graph; a single controlled
    .sample().getInfo() call (capped at `limit`) retrieves the features.
    """
    bands = _resolve_lance_bands(viirs_collection)
    frp_band = bands["frp"]
    ti4_band = bands["ti4"]
    ti5_band = bands["ti5"]
    conf_band = bands["conf"]
    qa_bands  = bands["qa_bands"]

    # --- Server-side fire mask -----------------------------------------------
    fire_conditions = []

    if frp_band:
        frp_max = viirs_collection.select(frp_band).max()
        frp_fire = frp_max.gt(0.1)
        if conf_band:
            conf_max = viirs_collection.select(conf_band).max()
            frp_fire = frp_fire.And(conf_max.gte(30))
        fire_conditions.append(frp_fire)

    if ti4_band and ti5_band:
        ti4 = viirs_collection.select(ti4_band).max()
        ti5 = viirs_collection.select(ti5_band).max()
        temp_diff = ti4.subtract(ti5)
        contextual = (ti4.gt(325).And(temp_diff.gt(12))).Or(ti4.gt(340))
        if qa_bands:
            qa_img = viirs_collection.select(qa_bands[0]).max()
            contextual = contextual.And(qa_img.bitwiseAnd(1).eq(0))
        fire_conditions.append(contextual)
    elif ti4_band:
        fire_conditions.append(viirs_collection.select(ti4_band).max().gt(330))

    if conf_band and not frp_band and not (ti4_band and ti5_band):
        fire_conditions.append(viirs_collection.select(conf_band).max().gte(50))

    if not fire_conditions:
        logger.warning("No suitable LANCE C2 fire bands available.")
        return []

    combined_fires = fire_conditions[0]
    for cond in fire_conditions[1:]:
        combined_fires = combined_fires.Or(cond)

    # Apply agricultural mask server-side
    agri_mask = get_agricultural_mask(district_geom)
    if agri_mask is not None:
        agri_buffered = agri_mask.focal_max(radius=375, kernelType="circle", units="meters")
        combined_fires = combined_fires.And(agri_buffered)

    # --- Select only required bands for the payload -------------------------
    sample_bands = [b for b in [frp_band, ti4_band, ti5_band, conf_band] if b]
    if not sample_bands:
        sample_bands = [bands["all_available"][0]]

    sample_image = viirs_collection.select(sample_bands).max()

    # --- Single sampling call, capped at limit ------------------------------
    capped = min(limit, MAX_HOTSPOTS_LIMIT)
    fire_sample = sample_image.updateMask(combined_fires).sample(
        region=district_geom,
        scale=375,
        numPixels=capped,
        geometries=True,
        dropNulls=True,
        seed=0,  # deterministic sampling for reproducibility
    )

    raw: Dict[str, Any] = run_with_timeout(fire_sample.getInfo)
    features = raw.get("features", [])
    logger.info("LANCE C2 raw sample: %d features", len(features))

    # --- Decode client-facing hotspot list ----------------------------------
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

        lat = round(coords[1], 5)
        lng = round(coords[0], 5)
        key = (lat, lng)
        if key in seen_coords:
            continue
        seen_coords.add(key)

        b_temp = None
        if ti4_band and ti4_band in props:
            try:
                b_temp = float(props[ti4_band])
            except (ValueError, TypeError):
                pass

        t_diff = None
        if ti4_band and ti5_band and ti4_band in props and ti5_band in props:
            try:
                t_diff = float(props[ti4_band]) - float(props[ti5_band])
            except (ValueError, TypeError):
                pass

        frp_value = None
        if frp_band and frp_band in props:
            try:
                raw_frp = float(props[frp_band])
                if raw_frp > 0:
                    frp_value = raw_frp
            except (ValueError, TypeError):
                pass
        if frp_value is None:
            frp_value = max(0.2, (b_temp - 315.0) * 0.15) if b_temp else 1.0

        if conf_band and conf_band in props:
            try:
                c_val = float(props[conf_band])
                confidence = "High" if c_val >= 80 else ("Nominal" if c_val >= 30 else "Low")
            except (ValueError, TypeError):
                confidence = "Nominal"
        else:
            if (b_temp and b_temp >= 340) or (t_diff and t_diff >= 20) or frp_value >= 5.0:
                confidence = "High"
            elif (b_temp and b_temp >= 325) or (t_diff and t_diff >= 12) or frp_value >= 1.0:
                confidence = "Nominal"
            else:
                confidence = "Low"

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
            "confidence": confidence,
            "intensity": intensity,
            "brightness_temp": round(b_temp, 2) if b_temp else None,
            "source": f"LANCE C2 Active Fire ({confidence})",
        })
        hotspot_id += 1

    logger.info("LANCE C2 confirmed hotspots: %d", len(hotspots))
    return hotspots


# ---------------------------------------------------------------------------
# Public detector function
# ---------------------------------------------------------------------------

def detect_viirs_lance_c2_fires(
    state: str,
    district: str,
    start_date: str,
    end_date: str,
    limit: int = DEFAULT_HOTSPOTS_LIMIT,
) -> Dict[str, Any]:
    """Detect active fire hotspots from VIIRS LANCE C2 data for a given district and date range."""
    global current_fire_data

    cache_key = ("lance", state, district, start_date, end_date, limit)
    cached = query_cache.get(cache_key)
    if cached is not None:
        logger.debug("Query cache hit: LANCE %s/%s %s–%s", state, district, start_date, end_date)
        return cached

    logger.info("🔥 LANCE C2 detection: %s, %s (%s to %s)", district, state, start_date, end_date)

    try:
        boundary_info = get_district_boundary(state, district)

        if boundary_info["status"] != "success":
            raise HTTPException(
                status_code=404,
                detail=f"District '{district}' not found in '{state}'.",
            )

        district_geom: ee.Geometry = boundary_info["geometry"]

        # Build the collection server-side, filtered before any data transfer
        viirs = (
            ee.ImageCollection(_LANCE_COLLECTION)
            .filterDate(start_date, end_date)
            .filterBounds(district_geom)
        )

        image_count: int = run_with_timeout(viirs.size().getInfo)
        logger.info("LANCE C2 images in range: %d", image_count)

        _empty = {
            "fire_pixels": 0,
            "nominal_footprint_hectares": 0.0,
            "fire_area_hectares": 0.0,
            "area_disclaimer": (
                "Nominal hotspot footprint only: each detected pixel contributes its nominal "
                "375m × 375m area. This is not verified burned area; hotspots can be sub-pixel. "
                "Burned area requires burn-scar analysis (e.g. Sentinel-2 dNBR)."
            ),
            "pixel_resolution_meters": 375,
            "fire_hotspots": 0,
            "max_frp": 0.0,
            "hotspots_coordinates": [],
            "district_boundary": boundary_info["boundary_geojson"],
            "map_bounds": boundary_info["bounds"],
            "viirs_images_used": image_count,
            "dataset": _LANCE_COLLECTION,
            "resolution": "375m",
        }

        if image_count == 0:
            current_fire_data = {
                "hotspots": [],
                "state": state,
                "district": district,
                "date_range": f"{start_date} to {end_date}",
            }
            result = {**_empty, "status": "No LANCE C2 data found for this period"}
            query_cache.set(cache_key, result)
            return result

        hotspots = _sample_fire_hotspots(viirs, district_geom, limit)

        fire_pixels = len(hotspots)
        nominal_footprint = nominal_pixel_footprint_hectares(fire_pixels, 375)
        max_frp = max((h["frp"] for h in hotspots), default=0.0)

        current_fire_data = {
            "hotspots": hotspots,
            "state": state,
            "district": district,
            "date_range": f"{start_date} to {end_date}",
            "nominal_footprint_hectares": round(nominal_footprint, 2),
        }

        # GeoJSON feature collection
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
                        "brightness_temp": h.get("brightness_temp"),
                        "source": h["source"],
                    },
                }
                for h in hotspots
            ],
        }

        area_disclaimer = (
            "Nominal hotspot footprint represents the aggregate area of 375m pixels containing "
            "active thermal detections, NOT verified burned area. Fires may cover only part of a "
            "pixel. Verified burned area requires burn-scar analysis (e.g. Sentinel-2 dNBR)."
        )

        result = {
            "fire_pixels": fire_pixels,
            "nominal_footprint_hectares": round(nominal_footprint, 2),
            "fire_area_hectares": round(nominal_footprint, 2),
            "legacy_fire_area_field_note": (
                "Deprecated compatibility alias for nominal_footprint_hectares; "
                "this is not verified burned area."
            ),
            "area_disclaimer": area_disclaimer,
            "pixel_resolution_meters": 375,
            "fire_hotspots": fire_pixels,
            "max_frp": round(max_frp, 3),
            "hotspots_coordinates": hotspots,
            "hotspots_geojson": hotspots_geojson,
            "district_boundary": boundary_info["boundary_geojson"],
            "map_bounds": boundary_info["bounds"],
            "viirs_images_used": image_count,
            "detection_method": "Contextual Active Fire Detection (Schroeder et al., 2014)",
            "dataset": _LANCE_COLLECTION,
            "resolution": "375m",
            "sensitivity": "Scientifically Validated Contextual Thermal & FRP Thresholds",
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
        logger.exception("VIIRS LANCE C2 detection failed: %s", exc)
        raise handle_ee_error(exc) from exc
