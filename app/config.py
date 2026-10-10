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
