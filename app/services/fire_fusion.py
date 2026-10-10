"""Bounded cross-product VIIRS hotspot sampling.

This is evidence fusion, not a validated accuracy model: it samples each source
at its own nominal scale and marks close detections as spatially corroborated.
"""
from __future__ import annotations

import logging
from typing import Any

import ee
from fastapi import HTTPException

from app.config import DEFAULT_HOTSPOTS_LIMIT, MAX_HOTSPOTS_LIMIT
from app.services.cache import query_cache
from app.services.ee_client import handle_ee_error, run_with_timeout, inclusive_end_to_exclusive
from app.services.gee_analysis import get_district_boundary, nominal_pixel_footprint_hectares
from app.services.viirs_daily import _resolve_vnp14a1_bands, is_viirs_fire_mask
from app.services.viirs_lance import _resolve_lance_bands

logger = logging.getLogger(__name__)
current_fire_data: dict[str, Any] = {"hotspots": [], "state": "", "district": "", "date_range": ""}


def fusion_confidence(frp, persistence, has_consensus, temperature_validated):
    factors = []
    if frp >= 15: factors.append("Very High FRP")
    elif frp >= 5: factors.append("High FRP")
    elif frp >= 1: factors.append("Medium FRP")
    else: factors.append("Low FRP")
    if persistence >= 3: factors.append("High Persistence")
    elif persistence >= 2: factors.append("Medium Persistence")
    if has_consensus: factors.append("Multi-Source Consensus")
    if temperature_validated: factors.append("Temperature Validated")
    if len(factors) >= 3: level = "Very High"
    elif len(factors) >= 2: level = "High"
    elif any("High" in x for x in factors): level = "Medium"
    else: level = "Low"
    return factors, level


def lance_quality_valid(frp, confidence, quality_clear=True):
    """Deterministic LANCE screen; absent values are not silently accepted."""
    try:
        return float(frp) > 0.1 and float(confidence) >= 30 and bool(quality_clear)
    except (TypeError, ValueError):
        return False


def _deduplicate(features: list[dict], tolerance_degrees: float = 0.01) -> list[dict]:
    """Merge source observations in the same small spatial cell.

    Composite products do not preserve a common event timestamp; therefore the
    resulting source consensus is spatial corroboration within the query window,
    not proof of simultaneous detections.
    """
    buckets: dict[tuple[int, int], dict] = {}
    for item in features:
        lat, lon = float(item["latitude"]), float(item["longitude"])
        key = (round(lat / tolerance_degrees), round(lon / tolerance_degrees))
        if key not in buckets:
            buckets[key] = {**item, "sources": [item["source"]], "native_resolutions_m": [item["native_resolution_m"]]}
            continue
        target = buckets[key]
        if item["source"] not in target["sources"]: target["sources"].append(item["source"])
        if item["native_resolution_m"] not in target["native_resolutions_m"]: target["native_resolutions_m"].append(item["native_resolution_m"])
        target["frp"] = max(float(target.get("frp") or 0), float(item.get("frp") or 0))
        if item["confidence"] == "High": target["confidence"] = "High"
        target["corroboration"] = "Spatially corroborated within query period" if len(target["sources"]) > 1 else "Single-source observation"
    for item in buckets.values():
        item.setdefault("corroboration", "Single-source observation")
        item["source"] = ", ".join(item.pop("sources"))
        item["native_resolutions_m"] = sorted(item["native_resolutions_m"])
    return list(buckets.values())


def _feature_collection(image, mask, geom, bands, scale, source):
    if not bands: return ee.FeatureCollection([])
    return (image.select(bands).updateMask(mask).sample(region=geom, scale=scale,
            numPixels=MAX_HOTSPOTS_LIMIT, geometries=True, dropNulls=True, seed=0)
            .map(lambda f: f.set({"_source": source, "_scale": scale})))


def _point_features(raw: dict) -> list[dict]:
    points=[]
    for f in raw.get("features", []):
        geom=f.get("geometry") or {}; props=f.get("properties") or {}
        coords=geom.get("coordinates") or []
        if geom.get("type") != "Point" or len(coords)<2: continue
        lon,lat=map(float,coords[:2])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180): continue
        source=props.get("_source", "VIIRS")
        scale=int(props.get("_scale",1000))
        frp=props.get("frp") or props.get("MaxFRP") or props.get("max_frp") or 0
        conf=props.get("confidence")
        fm=props.get("FireMask")
        try: frp=float(frp)
        except (ValueError,TypeError): frp=0.0
        try: confidence_value=float(conf) if conf is not None else None
        except (TypeError,ValueError): confidence_value=None
        confidence=("High" if (fm==9 or (confidence_value is not None and confidence_value>=80)) else
                    "Nominal" if (fm==8 or (confidence_value is not None and confidence_value>=30)) else
                    "Low" if fm==7 else "Unrated")
        points.append({"latitude":lat,"longitude":lon,"frp":round(frp,3),"confidence":confidence,
            "intensity":"High" if frp>=5 else "Moderate" if frp>=1 else "Low",
            "source":source,"native_resolution_m":scale,"fire_mask_class":fm,
            "detection_method":"VIIRS thermal anomaly; agricultural burning not confirmed"})
    return points


def detect_fusion_viirs_fires(state: str, district: str, start_date: str, end_date: str,
                              limit: int = DEFAULT_HOTSPOTS_LIMIT):
    global current_fire_data
    limit=max(1,min(int(limit),MAX_HOTSPOTS_LIMIT))
    key=("fusion",state,district,start_date,end_date,limit)
    cached=query_cache.get(key)
    if cached is not None: return cached
    try:
        boundary=get_district_boundary(state,district)
        if boundary.get("status")!="success": raise HTTPException(404,detail=f"District '{district}' not found in '{state}'.")
        geom=boundary["geometry"]
        ee_end=inclusive_end_to_exclusive(end_date)
        lance=(ee.ImageCollection("NASA/LANCE/SNPP_VIIRS/C2").filterDate(start_date,ee_end).filterBounds(geom))
        daily=(ee.ImageCollection("NASA/VIIRS/002/VNP14A1").filterDate(start_date,ee_end).filterBounds(geom))
        lance_n=int(run_with_timeout(lance.size().getInfo)); daily_n=int(run_with_timeout(daily.size().getInfo))
        samples=[]
        if lance_n:
            b=_resolve_lance_bands(lance); fb,cb=b.get("frp"),b.get("conf")
            if fb:
                frp=lance.select(fb).max(); mask=frp.gt(0.1)
                if cb: mask=mask.And(lance.select(cb).max().gte(30))
                if b.get("qa_bands"):
                    # Match the LANCE service's documented clear-quality bit test.
                    mask=mask.And(lance.select(b["qa_bands"][0]).max().bitwiseAnd(1).eq(0))
                bands=[x for x in (fb,cb,b.get("ti4"),b.get("ti5")) if x]
                samples.append(_feature_collection(lance.select(bands).max(),mask,geom,bands,375,"VIIRS LANCE C2"))
        if daily_n:
            b=_resolve_vnp14a1_bands(daily); fm=b.get("fire_mask"); fb=b.get("max_frp")
            if fm:
                mask=daily.select(fm).max().gte(7).And(daily.select(fm).max().lte(9))
                bands=[x for x in (fm,fb) if x]
                samples.append(_feature_collection(daily.select(bands).max(),mask,geom,bands,1000,"VIIRS VNP14A1"))
        if samples:
            merged=samples[0]
            for fc in samples[1:]: merged=merged.merge(fc)
            # Shuffle deterministically before the cap so the first product in
            # the merge cannot crowd the second product out of the sample.
            merged=merged.randomColumn("_sample_rank", seed=0).sort("_sample_rank").limit(limit)
            raw=run_with_timeout(merged.getInfo)
            points=_deduplicate(_point_features(raw))
        else: points=[]
        for i,p in enumerate(points,1): p["id"]=f"{district[:3].upper()}-{i:04d}"
        footprint=sum(sum((scale**2)/10000 for scale in p["native_resolutions_m"]) for p in points)
        max_frp=max((p["frp"] for p in points),default=0)
        current_fire_data={"hotspots":points,"state":state,"district":district,"date_range":f"{start_date} to {end_date}"}
        geo={"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","coordinates":[p["longitude"],p["latitude"]]},"properties":{k:v for k,v in p.items() if k not in ("latitude","longitude")}} for p in points]}
        empty_status=("No VIIRS imagery found for fusion" if lance_n+daily_n==0 else "No fire detections found in available VIIRS imagery")
        result={"status":"Success" if points else empty_status,"fire_pixels":len(points),"fire_hotspots":len(points),
            "hotspots_coordinates":points,"hotspots_geojson":geo,"max_frp":round(max_frp,3),
            "nominal_footprint_hectares":round(footprint,2),"fire_area_hectares":round(footprint,2),
            "legacy_fire_area_field_note":"Deprecated compatibility alias for nominal pixel footprint; not measured burned area.",
            "area_disclaimer":"Nominal source-pixel footprint only, weighted using each source's native pixel scale; not verified burned area.",
            "pixel_resolution_meters":None,"resolution":"Mixed native resolutions: LANCE 375 m; VNP14A1 1 km",
            "dataset":"NASA VIIRS LANCE C2 + VNP14A1","detection_method":"Single bounded retrieval; source-native sampling, spatial corroboration only",
            "lance_images":lance_n,"vnp14a1_images":daily_n,"total_images":lance_n+daily_n,
            "fusion_info":{"fusion_success":bool(points),"lance_images":lance_n,"vnp14a1_images":daily_n,
                "sample_limit":limit,"sampling_calls":1,"corroboration_semantics":"Spatial within query window; timestamps are not guaranteed simultaneous."},
            "district_boundary":boundary.get("boundary_geojson"),"map_bounds":boundary.get("bounds")}
        query_cache.set(key,result)
        return result
    except HTTPException: raise
    except Exception as exc:
        logger.exception("VIIRS fusion failed")
        raise handle_ee_error(exc) from exc
