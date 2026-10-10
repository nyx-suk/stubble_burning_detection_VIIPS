"""Download the latest result and manage asynchronous local export jobs."""
import csv
import io
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from app import state
from app.config import STATES_DISTRICTS
from app.schemas.fire import BatchExportRequest
from app.services import export_jobs

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
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/exports/batch", status_code=202)
async def create_batch_export(request: BatchExportRequest):
    if request.state not in STATES_DISTRICTS or request.district not in STATES_DISTRICTS[request.state]:
        raise HTTPException(400, detail="Unknown state or district")
    payload = request.model_dump(mode="json")
    try:
        return export_jobs.create_job(payload)
    except ValueError as exc:
        raise HTTPException(501 if request.export_destination != "local" else 400, detail=str(exc)) from exc


@router.get("/exports")
async def list_batch_exports():
    return {"jobs": export_jobs.list_jobs()}


@router.get("/exports/{job_id}")
async def batch_export_status(job_id: str):
    job = export_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, detail="Export job not found")
    return job


@router.post("/exports/{job_id}/cancel")
async def cancel_batch_export(job_id: str):
    try:
        job = export_jobs.cancel_job(job_id)
    except RuntimeError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(404, detail="Export job not found")
    return job


@router.post("/exports/{job_id}/retry", status_code=202)
async def retry_batch_export(job_id: str):
    try:
        job = export_jobs.retry_job(job_id)
    except RuntimeError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(404, detail="Export job not found")
    return job


@router.get("/exports/{job_id}/download")
async def download_batch_export(job_id: str):
    path = export_jobs.output_path(job_id)
    if path is None:
        job = export_jobs.get_job(job_id)
        if job is None:
            raise HTTPException(404, detail="Export job not found")
        raise HTTPException(409, detail=f"Export is not ready (status: {job['status']}).")
    media = "application/geo+json" if path.suffix.lower() == ".geojson" else "text/csv"
    return FileResponse(path, media_type=media, filename=Path(path).name)
