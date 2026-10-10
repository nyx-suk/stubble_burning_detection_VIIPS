"""Product discovery, bounded detection, compatibility routes, and health checks."""
from datetime import date, timedelta
import logging
import ee
from fastapi import APIRouter, HTTPException
from app.config import PRODUCTS, STATES_DISTRICTS, MAX_INTERACTIVE_DAYS
from app.schemas.fire import FireDetectionRequest
from app.services import fire_fusion, gee_analysis, viirs_daily, viirs_lance
from app.services.ee_client import handle_ee_error, run_with_timeout, inclusive_end_to_exclusive
from app import state

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
DETECTORS = {
    "lance": (viirs_lance.detect_viirs_lance_c2_fires, viirs_lance),
    "daily": (viirs_daily.detect_viirs_fires_enhanced, viirs_daily),
    "fusion": (fire_fusion.detect_fusion_viirs_fires, fire_fusion),
}

@router.get("/products")
async def list_products():
    return {"products": [{"id": key, **value} for key, value in PRODUCTS.items()]}

@router.get("/states")
async def get_states():
    return {"states": list(STATES_DISTRICTS)}

@router.get("/districts/{state_name}")
async def get_districts(state_name: str):
    if state_name not in STATES_DISTRICTS:
        raise HTTPException(404, detail="State not found")
    return {"districts": STATES_DISTRICTS[state_name]}

@router.get("/district-boundary/{state_name}/{district}")
async def get_district_boundary(state_name: str, district: str):
    if state_name not in STATES_DISTRICTS or district not in STATES_DISTRICTS[state_name]:
        raise HTTPException(404, detail="Unknown state or district")
    boundary = gee_analysis.get_district_boundary(state_name, district)
    if boundary.get("status") == "error":
        raise HTTPException(502, detail=f"District boundary service failed: {boundary.get('error', 'upstream error')}")
    return {"boundary_geojson": boundary.get("boundary_geojson"), "bounds": boundary.get("bounds"),
            "status": boundary.get("status"), **({"error": boundary["error"]} if boundary.get("error") else {})}

def _first_image_bands(collection, count):
    return run_with_timeout(collection.first().bandNames().getInfo) if count else []

@router.get("/viirs/test")
async def test_daily_legacy():
    return await test_product("daily")

@router.get("/{product}/test")
async def test_product(product: str):
    if product not in PRODUCTS:
        raise HTTPException(404, detail=f"Unknown product: {product}")
    start=(date.today()-timedelta(days=30)).isoformat(); end=date.today().isoformat()
    try:
        if product == "fusion":
            collections={k:ee.ImageCollection(PRODUCTS[k]["collection"]).filterDate(start,inclusive_end_to_exclusive(end)) for k in ("lance","daily")}
            counts={k:int(run_with_timeout(v.size().getInfo)) for k,v in collections.items()}
            bands={k:_first_image_bands(v,counts[k]) for k,v in collections.items()}
            return {"status":"success","product":PRODUCTS[product]["name"],"images_available":counts,"bands":bands}
        collection=ee.ImageCollection(PRODUCTS[product]["collection"]).filterDate(start,inclusive_end_to_exclusive(end))
        count=int(run_with_timeout(collection.size().getInfo))
        return {"status":"success","product":PRODUCTS[product]["name"],"collection":PRODUCTS[product]["collection"],"images_available":count,"bands":_first_image_bands(collection,count)}
    except Exception as exc:
        raise handle_ee_error(exc) from exc

def _paginate(result: dict, request: FireDetectionRequest) -> dict:
    points=list(result.get("hotspots_coordinates") or [])
    # Preserve the legacy fields as the bounded sample total; the new page only
    # changes hotspots_coordinates and adds explicit metadata.
    total=min(int(result.get("fire_hotspots", len(points)) or 0), request.limit)
    offset=(request.page-1)*request.page_size
    page_points=points[offset:offset+request.page_size]
    result["hotspots_coordinates"]=page_points
    if isinstance(result.get("hotspots_geojson"),dict):
        result["hotspots_geojson"]={**result["hotspots_geojson"],"features":result["hotspots_geojson"].get("features",[])[offset:offset+request.page_size]}
    result["pagination"]={"page":request.page,"page_size":request.page_size,"limit":request.limit,
        "total_sampled":total,"returned":len(page_points),"total_pages":(total+request.page_size-1)//request.page_size,
        "has_next":offset+len(page_points)<total,"has_previous":request.page>1,
        "truncated":total>=request.limit,
        "note":"Counts describe the bounded sample, not a complete census. Use /api/exports/batch for larger historical queries."}
    result.setdefault("legacy_response_note","Legacy fields remain available; hotspots_coordinates is now paginated. fire_area_hectares is a deprecated alias for nominal pixel footprint, not burned area.")
    return result

@router.post("/detect/{product}")
async def detect(product: str, request: FireDetectionRequest):
    if product not in DETECTORS:
        raise HTTPException(404, detail=f"Unknown product: {product}")
    if request.state not in STATES_DISTRICTS:
        raise HTTPException(400, detail="Invalid state")
    if request.district not in STATES_DISTRICTS[request.state]:
        raise HTTPException(400, detail="Invalid district")
    duration=(request.end_date-request.start_date).days+1
    if duration>MAX_INTERACTIVE_DAYS:
        raise HTTPException(413,detail=f"Interactive analysis is limited to {MAX_INTERACTIVE_DAYS} days. Submit this range as a background export at POST /api/exports/batch.")
    detector, data_source=DETECTORS[product]
    try:
        result=detector(request.state,request.district,request.start_date.isoformat(),request.end_date.isoformat(),limit=request.limit)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Detection request failed for %s",product)
        raise handle_ee_error(exc) from exc
    if not isinstance(result,dict):
        raise HTTPException(502,detail="Detection service returned an invalid response")
    if str(result.get("status","")).lower().startswith("error"):
        raise handle_ee_error(RuntimeError(result["status"]))
    result=_paginate(result,request)
    state.latest_fire_data=data_source.current_fire_data
    return result

@router.post("/detect-lance-fires")
async def detect_lance_legacy(request: FireDetectionRequest): return await detect("lance",request)

@router.post("/detect-stubble-burning")
async def detect_daily_legacy(request: FireDetectionRequest): return await detect("daily",request)

@router.post("/detect-fusion-fires")
async def detect_fusion_legacy(request: FireDetectionRequest): return await detect("fusion",request)
