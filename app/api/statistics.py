"""Statistics for the most recent detection."""

from fastapi import APIRouter

from app import state

router = APIRouter(prefix="/api/statistics", tags=["statistics"])


@router.get("/latest")
async def latest_statistics():
    data = state.latest_fire_data
    return {
        "state": data.get("state"),
        "district": data.get("district"),
        "date_range": data.get("date_range"),
        "hotspot_count": len(data.get("hotspots", [])),
        "nominal_footprint_hectares": data.get("nominal_footprint_hectares"),
        "area_disclaimer": data.get("area_disclaimer"),
        "fusion_info": data.get("fusion_info", {}),
    }
