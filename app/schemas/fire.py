"""API request schemas for fire detection."""

from datetime import date, timedelta

from pydantic import BaseModel, Field, model_validator


class FireDetectionRequest(BaseModel):
    state: str
    district: str
    start_date: date = Field(default_factory=lambda: date.today() - timedelta(days=7))
    end_date: date = Field(default_factory=date.today)

    @model_validator(mode="after")
    def validate_date_range(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self
