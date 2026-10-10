"""Download endpoints for the most recent detection."""

import csv
import io

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app import state

router = APIRouter(prefix="/api", tags=["exports"])


@router.get("/export-csv")
async def export_csv():
    data = state.latest_fire_data
    hotspots = data.get("hotspots", [])
    if not hotspots:
        raise HTTPException(status_code=404, detail="No fire detection data available for export")
    metadata = {key: data.get(key, "") for key in ("state", "district", "date_range")}
    rows = [{**hotspot, **metadata} for hotspot in hotspots]
    fields = sorted({field for row in rows for field in row})
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    filename = f"{data.get('district', 'hotspots').replace(' ', '_')}_hotspots.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
