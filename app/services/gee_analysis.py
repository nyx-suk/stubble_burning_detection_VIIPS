"""Shared geographic helpers and selector data for all products."""

import ee

from app.config import STATES_DISTRICTS


def nominal_pixel_footprint_hectares(pixel_count: int, pixel_size_meters: float) -> float:
    """Return the summed nominal square-pixel footprint in hectares."""
    if pixel_count < 0:
        raise ValueError("pixel_count must be non-negative")
    if pixel_size_meters <= 0:
        raise ValueError("pixel_size_meters must be positive")
    return pixel_count * pixel_size_meters * pixel_size_meters / 10_000


def get_district_boundary(state: str, district: str):
    """Get district boundary from FAO/GAUL dataset"""
    try:
        print(f"📍 Getting boundary for {district}, {state}")
        
        # Load FAO/GAUL administrative boundaries
        gaul = ee.FeatureCollection("FAO/GAUL/2015/level2")
        
        # Filter for specific district
        district_boundary = gaul.filter(ee.Filter.eq('ADM1_NAME', state)) \
                              .filter(ee.Filter.eq('ADM2_NAME', district))
        
        boundary_count = district_boundary.size().getInfo()
        print(f"📍 Found {boundary_count} boundary features")
        
        if boundary_count == 0:
            # Try alternative field names
            district_boundary = gaul.filter(ee.Filter.eq('NAME_1', state)) \
                                  .filter(ee.Filter.eq('NAME_2', district))
            boundary_count = district_boundary.size().getInfo()
            print(f"📍 Alternative search: {boundary_count} boundary features")
        
        if boundary_count > 0:
            # Get boundary as GeoJSON
            boundary_geojson = district_boundary.getInfo()
            
            # Get boundary geometry for map centering
            boundary_geometry = district_boundary.geometry()
            bounds = boundary_geometry.bounds().getInfo()
            
            return {
                'boundary_geojson': boundary_geojson,
                'bounds': bounds['coordinates'][0],
                'geometry': boundary_geometry,
                'status': 'success'
            }
        else:
            return {
                'boundary_geojson': None,
                'bounds': None,
                'geometry': None,
                'status': 'not_found'
            }
            
    except Exception as e:
        print(f"❌ Boundary error: {e}")
        return {
            'boundary_geojson': None,
            'bounds': None,
            'geometry': None,
            'status': 'error',
            'error': str(e)
        }


def get_agricultural_mask(district_geom):
    """Create mask for agricultural areas using land cover data"""
    try:
        # Use MODIS Land Cover Collection 6.1 for agricultural areas
        landcover = ee.ImageCollection("MODIS/061/MCD12Q1") \
                     .filterDate('2020-01-01', '2022-12-31') \
                     .first() \
                     .select('LC_Type1')
        
        # Agricultural land cover types (12: Croplands, 14: Cropland/Natural vegetation mosaic)
        agri_mask = landcover.eq(12).Or(landcover.eq(14))
        
        return agri_mask.clip(district_geom)
    except Exception as e:
        print(f"⚠️ Agricultural mask creation failed: {e}")
        return None
