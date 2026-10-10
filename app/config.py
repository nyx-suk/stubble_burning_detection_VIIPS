"""Application and Earth Engine configuration."""

import os

EARTHENGINE_PROJECT = os.getenv("EARTHENGINE_PROJECT", "ee-deepeshy")
STATIC_DIR_NAME = "static"

STATES_DISTRICTS = {
    "Punjab": ["Amritsar", "Bathinda", "Ferozepur", "Gurdaspur", "Hoshiarpur", "Jalandhar", "Ludhiana", "Patiala", "Sangrur"],
    "Haryana": ["Ambala", "Hisar", "Karnal", "Kurukshetra", "Panipat", "Rohtak", "Sirsa", "Sonipat", "Yamunanagar"],
    "Uttar Pradesh": ["Agra", "Aligarh", "Bareilly", "Ghaziabad", "Kanpur", "Lucknow", "Meerut", "Varanasi"],
    "Delhi": ["Central Delhi", "East Delhi", "New Delhi", "North Delhi", "South Delhi", "West Delhi"],
}

PRODUCTS = {
    "lance": {"name": "VIIRS LANCE C2 (375 m)", "collection": "NASA/LANCE/SNPP_VIIRS/C2"},
    "daily": {"name": "VIIRS VNP14A1 (1 km)", "collection": "NASA/VIIRS/002/VNP14A1"},
    "fusion": {"name": "VIIRS data fusion", "collection": None},
}

# Earth Engine optimization settings
EE_TIMEOUT_SECONDS = float(os.getenv("EE_TIMEOUT_SECONDS", "30.0"))
DEFAULT_HOTSPOTS_LIMIT = int(os.getenv("DEFAULT_HOTSPOTS_LIMIT", "1000"))
MAX_HOTSPOTS_LIMIT = int(os.getenv("MAX_HOTSPOTS_LIMIT", "5000"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "600"))  # 10 minutes
BOUNDARY_CACHE_TTL_SECONDS = int(os.getenv("BOUNDARY_CACHE_TTL_SECONDS", "86400"))  # 24 hours
HISTORICAL_ANALYSIS_DAYS_THRESHOLD = int(os.getenv("HISTORICAL_ANALYSIS_DAYS_THRESHOLD", "30"))
EXPORTS_DIR = os.getenv("EXPORTS_DIR", "exports")

