"""API request schemas for fire detection and batch exports."""

from datetime import date, timedelta
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.config import DEFAULT_HOTSPOTS_LIMIT, MAX_HOTSPOTS_LIMIT, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE


class FireDetectionRequest(BaseModel):
    state: str
    district: str
    start_date: date = Field(default_factory=lambda: date.today() - timedelta(days=7))
    end_date: date = Field(default_factory=date.today)
    limit: int = Field(default=DEFAULT_HOTSPOTS_LIMIT, ge=1, le=MAX_HOTSPOTS_LIMIT, description="Server-side limit on number of fire hotspots")
    page: int = Field(default=1, ge=1, description="Page number for pagination")
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Number of hotspots per page")

    @model_validator(mode="after")
    def validate_date_range(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if self.page_size > self.limit:
            raise ValueError("page_size must be less than or equal to limit")
        if (self.page - 1) * self.page_size >= self.limit:
            raise ValueError("page offset must be less than limit")
        return self


class BatchExportRequest(BaseModel):
    product: str = Field(description="Detection product: 'lance', 'daily', or 'fusion'")
    state: str
    district: str
    start_date: date = Field(default_factory=lambda: date.today() - timedelta(days=30))
    end_date: date = Field(default_factory=date.today)
    export_destination: str = Field(
        default="local",
        description="Destination: 'local' (server-side download), 'drive' (Google Drive), or 'cloud_storage' (Google Cloud Storage)"
    )
    folder: Optional[str] = Field(default="EarthEngine_Exports", description="Google Drive folder name")
    bucket: Optional[str] = Field(default=None, description="Google Cloud Storage bucket name")
    file_format: str = Field(default="CSV", description="Export format: 'CSV' or 'GeoJSON'")
    scale: Optional[int] = Field(default=None, description="Spatial scale in meters (e.g. 375 or 1000)")
    limit: int = Field(default=MAX_HOTSPOTS_LIMIT, ge=1, le=MAX_HOTSPOTS_LIMIT, description="Maximum sampled hotspots in export")

    @model_validator(mode="after")
    def validate_export_params(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if self.product not in ("lance", "daily", "fusion"):
            raise ValueError("product must be 'lance', 'daily', or 'fusion'")
        if self.export_destination == "cloud_storage" and not self.bucket:
            raise ValueError("bucket is required when export_destination is 'cloud_storage'")
        if self.export_destination not in ("local", "drive", "cloud_storage"):
            raise ValueError("export_destination must be 'local', 'drive', or 'cloud_storage'")
        if self.file_format.upper() not in ("CSV", "GEOJSON"):
            raise ValueError("file_format must be 'CSV' or 'GeoJSON'")
        return self
