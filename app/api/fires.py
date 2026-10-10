"""Product discovery, district lookup, health checks, and detection endpoints."""

from datetime import date, timedelta

import ee
from fastapi import APIRouter, HTTPException

from app.config import PRODUCTS, STATES_DISTRICTS
from app.schemas.fire import FireDetectionRequest
from app.services import fire_fusion, gee_analysis, viirs_daily, viirs_lance
from app import state

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
        raise HTTPException(status_code=404, detail="State not found")
    return {"districts": STATES_DISTRICTS[state_name]}


@router.get("/district-boundary/{state_name}/{district}")
async def get_district_boundary(state_name: str, district: str):
    if state_name not in STATES_DISTRICTS or district not in STATES_DISTRICTS[state_name]:
        raise HTTPException(status_code=404, detail="Unknown state or district")
    boundary = gee_analysis.get_district_boundary(state_name, district)
    return {
        "boundary_geojson": boundary.get("boundary_geojson"),
        "bounds": boundary.get("bounds"),
        "status": boundary.get("status"),
        **({"error": boundary["error"]} if boundary.get("error") else {}),
    }


def _first_image_bands(collection, count):
    return collection.first().bandNames().getInfo() if count else []


@router.get("/viirs/test")
async def test_daily_legacy():
    return await test_product("daily")


@router.get("/{product}/test")
async def test_product(product: str):
    if product not in PRODUCTS:
        raise HTTPException(status_code=404, detail=f"Unknown product: {product}")
    start = (date.today() - timedelta(days=30)).isoformat()
    end = (date.today() + timedelta(days=1)).isoformat()
    try:
        if product == "fusion":
            lance = ee.ImageCollection(PRODUCTS["lance"]["collection"]).filterDate(start, end)
            daily = ee.ImageCollection(PRODUCTS["daily"]["collection"]).filterDate(start, end)
            counts = {"lance": lance.size().getInfo(), "daily": daily.size().getInfo()}
            bands = {"lance": _first_image_bands(lance, counts["lance"]), "daily": _first_image_bands(daily, counts["daily"])}
            return {"status": "success", "product": PRODUCTS[product]["name"], "images_available": counts, "bands": bands}
        collection = ee.ImageCollection(PRODUCTS[product]["collection"]).filterDate(start, end)
        count = collection.size().getInfo()
        return {
            "status": "success",
            "product": PRODUCTS[product]["name"],
            "collection": PRODUCTS[product]["collection"],
            "images_available": count,
            "bands": _first_image_bands(collection, count),
        }
    except Exception as exc:
        return {"status": "error", "product": PRODUCTS[product]["name"], "error": str(exc)}


@router.post("/detect/{product}")
async def detect(product: str, request: FireDetectionRequest):
    if product not in DETECTORS:
        raise HTTPException(status_code=404, detail=f"Unknown product: {product}")
    if request.state not in STATES_DISTRICTS:
        raise HTTPException(status_code=400, detail="Invalid state")
    if request.district not in STATES_DISTRICTS[request.state]:
        raise HTTPException(status_code=400, detail="Invalid district")
    detector, data_source = DETECTORS[product]
    result = detector(request.state, request.district, request.start_date.isoformat(), request.end_date.isoformat())
    state.latest_fire_data = data_source.current_fire_data
    return result


@router.post("/detect-lance-fires")
async def detect_lance_legacy(request: FireDetectionRequest):
    return await detect("lance", request)


@router.post("/detect-stubble-burning")
async def detect_daily_legacy(request: FireDetectionRequest):
    return await detect("daily", request)


@router.post("/detect-fusion-fires")
async def detect_fusion_legacy(request: FireDetectionRequest):
    return await detect("fusion", request)
