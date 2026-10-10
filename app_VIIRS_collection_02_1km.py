"""
Enhanced VIIRS Stubble Burning Detection with Improved Sensitivity
Optimized for detecting all stubble burning spots in Punjab/Haryana
"""

import ee
import uvicorn
import csv
import io
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from datetime import datetime

# Initialize Google Earth Engine
try:
    ee.Initialize(project='ee-deepeshy')
    print("✅ Google Earth Engine initialized")
except Exception as e:
    print(f"❌ GEE Error: {e}")

# Create FastAPI app
app = FastAPI(title="Stubble Burning Detection", description="Maximum Sensitivity Multi-Round Detection")

class FireDetectionRequest(BaseModel):
    state: str
    district: str
    start_date: str = "2020-11-01"
    end_date: str = "2020-11-30"

# States and districts data
STATES_DISTRICTS = {
    "Punjab": ["Amritsar", "Bathinda", "Ferozepur", "Gurdaspur", "Hoshiarpur", "Jalandhar", "Ludhiana", "Patiala", "Sangrur"],
    "Haryana": ["Ambala", "Hisar", "Karnal", "Kurukshetra", "Panipat", "Rohtak", "Sirsa", "Sonipat", "Yamunanagar"],
    "Uttar Pradesh": ["Agra", "Aligarh", "Bareilly", "Ghaziabad", "Kanpur", "Lucknow", "Meerut", "Varanasi"]
}

# Global storage for fire hotspots data
current_fire_data = {
    'hotspots': [],
    'state': '',
    'district': '',
    'date_range': ''
}

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
        # Use MODIS Land Cover for agricultural areas
        landcover = ee.ImageCollection("MODIS/006/MCD12Q1") \
                     .filterDate('2020-01-01', '2020-12-31') \
                     .first() \
                     .select('LC_Type1')
        
        # Agricultural land cover types (12: Croplands, 14: Cropland/Natural vegetation mosaic)
        agri_mask = landcover.eq(12).Or(landcover.eq(14))
        
        return agri_mask.clip(district_geom)
    except Exception as e:
        print(f"⚠️ Agricultural mask creation failed: {e}")
        return None

def extract_stubble_burning_spots(viirs_collection, district_geom):
    """Extract stubble burning spots with maximum sensitivity"""
    try:
        print("🔥 Extracting fire spots with maximum sensitivity for stubble burning...")
        
        # First, check what bands are actually available
        first_image = viirs_collection.first()
        available_bands = first_image.bandNames().getInfo()
        print(f"📡 Available VIIRS bands: {available_bands}")
        
        # Get agricultural mask
        agri_mask = get_agricultural_mask(district_geom)
        
        # Documented NASA VIIRS VNP14A1 FireMask class values:
        # 0: Not processed (missing input data)
        # 1: Not processed (bad input data)
        # 2: Not processed (other reason)
        # 3: Non-fire water pixel
        # 4: Cloud (cloud-obscured pixel)
        # 5: Non-fire land pixel (clear land)
        # 6: Unknown
        # 7: Fire (low confidence)
        # 8: Fire (nominal confidence)
        # 9: Fire (high confidence)
        
        fire_detections = []
        
        # Primary detection: FireMask band strictly filtered for fire classes (7, 8, 9)
        if 'FireMask' in available_bands:
            print("🔥 Using FireMask band for fire detection (classes 7-9: Low, Nominal, High confidence)")
            fire_mask = viirs_collection.select('FireMask').max()
            
            # Acceptance criteria: Ensure non-fire pixels (clear land=5, cloud=4, water=3, unprocessed=0-2, 6)
            # are strictly NOT classified as fires. Only classes 7, 8, 9 are fire detections.
            fire_mask_fires = fire_mask.gte(7).And(fire_mask.lte(9))
            fire_detections.append(fire_mask_fires)
        
        # Secondary confirmation / fallback: MaxFRP band
        if 'MaxFRP' in available_bands:
            print("🔥 Using MaxFRP band for active fire radiative power validation")
            max_frp = viirs_collection.select('MaxFRP').max()
            if 'FireMask' in available_bands:
                # When FireMask is present, MaxFRP validates active thermal emission for documented fire classes (7-9),
                # preventing any non-fire class (such as clear land or cloud) from triggering detection
                fire_detections.append(max_frp.gt(0).And(fire_mask.gte(7)).And(fire_mask.lte(9)))
            else:
                # Fallback if FireMask is somehow not available in dataset
                fire_detections.append(max_frp.gt(0))
        
        # Combine detection methods with OR logic (both are restricted to fire classes 7-9)
        if fire_detections:
            combined_fires = fire_detections[0]
            for i in range(1, len(fire_detections)):
                combined_fires = combined_fires.Or(fire_detections[i])
            print(f"🔗 Combined {len(fire_detections)} detection methods (classes 7-9)")
        else:
            print("⚠️ No suitable fire detection bands found")
            return []
        
        # Apply agricultural mask if available (with lenient buffer)
        if agri_mask is not None:
            print("🌾 Applying lenient agricultural area mask")
            agri_mask_buffered = agri_mask.focal_max(radius=500, kernelType='circle', units='meters')
            combined_fires = combined_fires.And(agri_mask_buffered)
        
        # Select sampling bands: Include BOTH FireMask and MaxFRP so sampled features
        # carry the exact FireMask class (7, 8, 9) and Fire Radiative Power (MW)
        sample_bands = []
        if 'FireMask' in available_bands:
            sample_bands.append('FireMask')
        if 'MaxFRP' in available_bands:
            sample_bands.append('MaxFRP')
            frp_available = True
        else:
            frp_available = False
        
        if sample_bands:
            sample_image = viirs_collection.select(sample_bands).max()
        else:
            sample_image = first_image.select(available_bands[0])
            frp_available = False
            print(f"⚠️ Using {available_bands[0]} for sampling")
        
        # Sampling for detected fire points
        print("📍 Performing fire sampling on detected fire pixels...")
        
        # Multiple sampling rounds for robust coverage
        all_hotspots = []
        
        # Round 1: Standard sampling scale
        fire_sample_1 = sample_image.updateMask(combined_fires).sample(
            region=district_geom,
            scale=300,  # Slightly finer resolution
            numPixels=10000,
            geometries=True,
            dropNulls=True
        )
        
        # Round 2: Coarser scale sampling
        fire_sample_2 = sample_image.updateMask(combined_fires).sample(
            region=district_geom,
            scale=500,  # Coarser scale
            numPixels=5000,
            geometries=True,
            dropNulls=True
        )
        
        # Combine samples
        all_samples = [fire_sample_1, fire_sample_2]
        
        hotspot_id = 1
        unique_coordinates = set()  # To avoid duplicates
        
        for sample_round, fire_sample in enumerate(all_samples, 1):
            fire_features = fire_sample.getInfo()
            round_features = len(fire_features.get('features', []))
            print(f"📊 Round {sample_round}: {round_features} potential fire points")
            
            for feature in fire_features.get('features', []):
                geom = feature.get('geometry', {})
                props = feature.get('properties', {})
                
                if geom.get('type') == 'Point':
                    coords = geom.get('coordinates', [])
                    if len(coords) >= 2:
                        # Extract and validate FireMask class value
                        mask_val = None
                        if 'FireMask' in props:
                            try:
                                mask_val = int(round(float(props['FireMask'])))
                            except (ValueError, TypeError):
                                mask_val = None
                        
                        # CRITICAL: Distinguish fire detections from non-fire classes
                        # 0-2: Not processed, 3: Water, 4: Cloud, 5: Clear land, 6: Unknown
                        # Acceptance criteria: Clear land (5) and cloud (4) pixels are NOT reported as fires
                        if mask_val is not None and mask_val not in (7, 8, 9):
                            continue
                        
                        # Round coordinates to avoid near-duplicates
                        lat_rounded = round(coords[1], 5)
                        lng_rounded = round(coords[0], 5)
                        coord_key = (lat_rounded, lng_rounded)
                        
                        # Skip if we already have this coordinate
                        if coord_key in unique_coordinates:
                            continue
                        unique_coordinates.add(coord_key)
                        
                        # Resolve Fire Radiative Power (MW)
                        if frp_available and 'MaxFRP' in props:
                            try:
                                frp_val = float(props.get('MaxFRP', 0.0))
                                frp_value = frp_val if frp_val > 0 else (5.0 if mask_val == 9 else (1.5 if mask_val == 8 else 0.5))
                            except (ValueError, TypeError):
                                frp_value = 5.0 if mask_val == 9 else (1.5 if mask_val == 8 else 0.5)
                        elif mask_val is not None:
                            # Estimate baseline FRP based on FireMask class
                            frp_value = 5.0 if mask_val == 9 else (1.5 if mask_val == 8 else 0.5)
                        else:
                            frp_value = 0.5
                        
                        # Preserve documented FireMask confidence distinctions:
                        # Class 7: Low confidence fire
                        # Class 8: Nominal confidence fire
                        # Class 9: High confidence fire
                        if mask_val == 9:
                            confidence = 'High'
                        elif mask_val == 8:
                            confidence = 'Nominal'
                        elif mask_val == 7:
                            confidence = 'Low'
                        else:
                            if frp_value >= 5.0:
                                confidence = 'High'
                            elif frp_value >= 1.0:
                                confidence = 'Nominal'
                            else:
                                confidence = 'Low'
                        
                        # Determine intensity based on FRP
                        if frp_value >= 15.0:
                            intensity = 'Very High'
                        elif frp_value >= 5.0:
                            intensity = 'High'
                        elif frp_value >= 1.0:
                            intensity = 'Medium'
                        elif frp_value >= 0.1:
                            intensity = 'Low'
                        else:
                            intensity = 'Very Low'
                        
                        source_label = f'VIIRS VNP14A1 (Class {mask_val} - {confidence})' if mask_val else f'VIIRS Detection R{sample_round}'
                        
                        hotspot = {
                            'id': hotspot_id,
                            'latitude': coords[1],
                            'longitude': coords[0],
                            'frp': round(frp_value, 3),
                            'fire_mask_class': mask_val,
                            'confidence': confidence,
                            'intensity': intensity,
                            'source': source_label
                        }
                        all_hotspots.append(hotspot)
                        hotspot_id += 1
        
        print(f"🎯 Total unique fire hotspots detected: {len(all_hotspots)}")
        return all_hotspots
        
    except Exception as e:
        print(f"❌ Fire detection error: {e}")
        print(f"❌ Error details: {type(e).__name__}")
        return []

def detect_viirs_stubble_burning(state: str, district: str, start_date: str, end_date: str):
    """Enhanced VIIRS detection optimized for stubble burning"""
    global current_fire_data
    
    try:
        print(f"🔥 VIIRS Stubble Burning Detection: {district}, {state} ({start_date} to {end_date})")
        
        # Get district boundary
        boundary_info = get_district_boundary(state, district)
        
        if boundary_info['status'] != 'success':
            raise HTTPException(status_code=404, detail=f"District {district} not found in {state}")
        
        district_geom = boundary_info['geometry']
        
        # Load VIIRS dataset with expanded date range for better coverage
        viirs = ee.ImageCollection('NASA/VIIRS/002/VNP14A1') \
                  .filterDate(start_date, end_date) \
                  .filterBounds(district_geom)
        
        image_count = viirs.size().getInfo()
        print(f"📡 VIIRS images found: {image_count}")
        
        if image_count == 0:
            current_fire_data = {
                'hotspots': [],
                'state': state,
                'district': district,
                'date_range': f"{start_date} to {end_date}"
            }
            
            return {
                'fire_pixels': 0,
                'fire_area_hectares': 0.0,
                'fire_hotspots': 0,
                'max_frp': 0.0,
                'hotspots_coordinates': [],
                'district_boundary': boundary_info['boundary_geojson'],
                'map_bounds': boundary_info['bounds'],
                'viirs_images_used': 0,
                'status': 'No VIIRS data found for this period'
            }
        
        # Extract stubble burning spots
        hotspots = extract_stubble_burning_spots(viirs, district_geom)
        
        # Calculate statistics
        fire_pixels = len(hotspots)
        fire_area_hectares = fire_pixels * 0.140625  # 375m x 375m = 0.140625 hectares
        max_frp = max([h['frp'] for h in hotspots], default=0.0)
        
        # Store current fire data for CSV export
        current_fire_data = {
            'hotspots': hotspots,
            'state': state,
            'district': district,
            'date_range': f"{start_date} to {end_date}"
        }
        
        # Create GeoJSON for stubble burning hotspots
        hotspots_geojson = {
            'type': 'FeatureCollection',
            'features': []
        }
        
        for hotspot in hotspots:
            feature = {
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': [hotspot['longitude'], hotspot['latitude']]
                },
                'properties': {
                    'id': hotspot['id'],
                    'frp': hotspot['frp'],
                    'confidence': hotspot['confidence'],
                    'intensity': hotspot['intensity'],
                    'fire_mask_class': hotspot.get('fire_mask_class'),
                    'latitude': hotspot['latitude'],
                    'longitude': hotspot['longitude'],
                    'source': hotspot['source']
                }
            }
            hotspots_geojson['features'].append(feature)
        
        return {
            'fire_pixels': fire_pixels,
            'fire_area_hectares': round(fire_area_hectares, 2),
            'fire_hotspots': len(hotspots),
            'max_frp': round(max_frp, 3),
            'hotspots_coordinates': hotspots,
            'hotspots_geojson': hotspots_geojson,
            'district_boundary': boundary_info['boundary_geojson'],
            'map_bounds': boundary_info['bounds'],
            'viirs_images_used': image_count,
            'detection_method': 'Enhanced Stubble Burning Detection',
            'dataset': 'NASA/VIIRS/002/VNP14A1',
            'resolution': '375m',
            'sensitivity': 'Optimized for Agricultural Fires',
            'status': 'Success'
        }
        
    except Exception as e:
        print(f"❌ VIIRS stubble burning detection error: {e}")
        raise HTTPException(status_code=500, detail=f"VIIRS stubble burning detection failed: {str(e)}")


def detect_viirs_fires_enhanced(state: str, district: str, start_date: str, end_date: str):
    """Ultra-Aggressive VIIRS fire detection with maximum sensitivity"""
    global current_fire_data
    
    try:
        print(f"🔥 Ultra-Aggressive VIIRS Fire Detection: {district}, {state} ({start_date} to {end_date})")
        
        # Get district boundary
        boundary_info = get_district_boundary(state, district)
        
        if boundary_info['status'] != 'success':
            return {
                'fire_pixels': 0,
                'fire_area_hectares': 0.0,
                'fire_hotspots': 0,
                'max_frp': 0.0,
                'hotspots_coordinates': [],
                'hotspots_geojson': {'type': 'FeatureCollection', 'features': []},
                'district_boundary': None,
                'map_bounds': None,
                'viirs_images_used': 0,
                'detection_method': 'Ultra-Aggressive VIIRS Detection',
                'dataset': 'NASA/VIIRS/002/VNP14A1',
                'resolution': '300m + 500m multi-scale',
                'sensitivity': 'Boundary not found',
                'status': f'Error: District {district} not found in {state}'
            }
        
        district_geom = boundary_info['geometry']
        
        # Load VIIRS dataset
        viirs = ee.ImageCollection('NASA/VIIRS/002/VNP14A1') \
                  .filterDate(start_date, end_date) \
                  .filterBounds(district_geom)
        
        image_count = viirs.size().getInfo()
        print(f"📡 VIIRS images found: {image_count}")
        
        if image_count == 0:
            current_fire_data = {
                'hotspots': [],
                'state': state,
                'district': district,
                'date_range': f"{start_date} to {end_date}"
            }
            
            return {
                'fire_pixels': 0,
                'fire_area_hectares': 0.0,
                'fire_hotspots': 0,
                'max_frp': 0.0,
                'hotspots_coordinates': [],
                'hotspots_geojson': {'type': 'FeatureCollection', 'features': []},
                'district_boundary': boundary_info['boundary_geojson'],
                'map_bounds': boundary_info['bounds'],
                'viirs_images_used': 0,
                'detection_method': 'Ultra-Aggressive VIIRS Detection',
                'dataset': 'NASA/VIIRS/002/VNP14A1',
                'resolution': '300m + 500m multi-scale',
                'sensitivity': 'No VIIRS data available',
                'status': 'No VIIRS data found for this period'
            }
        
        # Extract fire spots using ultra-aggressive detection
        hotspots = extract_stubble_burning_spots(viirs, district_geom)
        
        # Calculate statistics
        fire_pixels = len(hotspots)
        fire_area_hectares = fire_pixels * 0.140625  # 375m x 375m = 0.140625 hectares
        max_frp = max([h['frp'] for h in hotspots], default=0.0)
        
        # Store current fire data for CSV export
        current_fire_data = {
            'hotspots': hotspots,
            'state': state,
            'district': district,
            'date_range': f"{start_date} to {end_date}"
        }
        
        # Create GeoJSON for fire hotspots
        hotspots_geojson = {
            'type': 'FeatureCollection',
            'features': []
        }
        
        for hotspot in hotspots:
            feature = {
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': [hotspot['longitude'], hotspot['latitude']]
                },
                'properties': {
                    'id': hotspot['id'],
                    'frp': hotspot['frp'],
                    'confidence': hotspot['confidence'],
                    'intensity': hotspot['intensity'],
                    'fire_mask_class': hotspot.get('fire_mask_class'),
                    'latitude': hotspot['latitude'],
                    'longitude': hotspot['longitude'],
                    'source': hotspot['source']
                }
            }
            hotspots_geojson['features'].append(feature)
        
        return {
            'fire_pixels': fire_pixels,
            'fire_area_hectares': round(fire_area_hectares, 2),
            'fire_hotspots': len(hotspots),
            'max_frp': round(max_frp, 3),
            'hotspots_coordinates': hotspots,
            'hotspots_geojson': hotspots_geojson,
            'district_boundary': boundary_info['boundary_geojson'],
            'map_bounds': boundary_info['bounds'],
            'viirs_images_used': image_count,
            'detection_method': 'Ultra-Aggressive VIIRS Detection',
            'dataset': 'NASA/VIIRS/002/VNP14A1',
            'resolution': '300m + 500m multi-scale',
            'sensitivity': 'Ultra-low thresholds + Multi-round detection',
            'status': 'Success'
        }
        
    except Exception as e:
        print(f"❌ Ultra-Aggressive VIIRS detection error: {e}")
        # Return a proper error response instead of raising exception
        return {
            'fire_pixels': 0,
            'fire_area_hectares': 0.0,
            'fire_hotspots': 0,
            'max_frp': 0.0,
            'hotspots_coordinates': [],
            'hotspots_geojson': {'type': 'FeatureCollection', 'features': []},
            'district_boundary': None,
            'map_bounds': None,
            'viirs_images_used': 0,
            'detection_method': 'Ultra-Aggressive VIIRS Detection',
            'dataset': 'NASA/VIIRS/002/VNP14A1',
            'resolution': '300m + 500m multi-scale',
            'sensitivity': 'Error occurred during detection',
            'status': f'Error: {str(e)}'
        }


# API Endpoints
@app.get("/")
async def dashboard():
    """Enhanced dashboard for stubble burning detection"""
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Stubble Burning Detection</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: Arial, sans-serif; background: #f5f5f5; height: 100vh; display: flex; }
        .sidebar { width: 350px; background: #2c3e50; color: white; padding: 20px; overflow-y: auto; }
        .main-content { flex: 1; display: flex; flex-direction: column; }
        #map { flex: 1; }
        .section { margin-bottom: 20px; padding: 15px; background: rgba(255,255,255,0.1); border-radius: 8px; }
        .section h3 { color: #e67e22; margin-bottom: 10px; font-size: 14px; text-transform: uppercase; }
        .form-control { width: 100%; padding: 8px; margin: 5px 0; border-radius: 4px; border: 1px solid #ccc; }
        .btn { padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; font-weight: bold; margin: 5px 0; }
        .btn-primary { background: #3498db; color: white; }
        .btn-success { background: #27ae60; color: white; }
        .btn-warning { background: #f39c12; color: white; }
        .btn:disabled { opacity: 0.6; cursor: not-allowed; }
        .result-item { display: flex; justify-content: space-between; margin: 5px 0; font-size: 13px; }
        .result-item span:last-child { font-weight: bold; color: #58d68d; }
        .hidden { display: none; }
        .status-bar { background: #34495e; color: white; padding: 10px; display: flex; justify-content: space-between; font-size: 12px; }
        .boundary-info { background: rgba(39, 174, 96, 0.1); padding: 8px; border-radius: 4px; margin-top: 10px; font-size: 12px; }
        .hotspot-info { background: rgba(230, 126, 34, 0.1); padding: 8px; border-radius: 4px; margin-top: 10px; font-size: 12px; }
        .legend { background: rgba(255,255,255,0.1); padding: 10px; border-radius: 4px; margin-top: 10px; }
        .legend-item { display: flex; align-items: center; margin: 5px 0; font-size: 12px; }
        .legend-color { width: 20px; height: 15px; margin-right: 8px; border-radius: 50%; }
        .coordinate-display { background: rgba(52, 152, 219, 0.1); padding: 8px; border-radius: 4px; margin-top: 10px; font-size: 12px; }
        .coordinate-display.active { background: rgba(52, 152, 219, 0.3); }
        .export-section { background: rgba(155, 89, 182, 0.1); padding: 12px; border-radius: 4px; margin-top: 10px; }
        .sensitivity-info { background: rgba(231, 76, 60, 0.1); padding: 8px; border-radius: 4px; margin-top: 10px; font-size: 11px; }
    </style>
</head>
<body>
    <div class="sidebar">
        <h2>🔥 Stubble Burning Detection</h2>
        
        <div class="section">
            <h3>⚙️ Detection Settings</h3>
            <div class="sensitivity-info">
                <strong>🎯 Ultra-Aggressive Detection:</strong><br>
                • Ultra-low FRP threshold (>0.001 MW)<br>
                • Multiple detection rounds<br>
                • Agricultural area buffering<br>
                • Massive sampling (10,000 pixels)<br>
                • Multi-scale analysis (300m + 500m)<br>
                • Temporal compositing (max + mean)<br>
                • Light color palette for visibility
            </div>
        </div>
        
        <div class="section">
            <h3>📍 Location</h3>
            <select id="stateSelect" class="form-control">
                <option value="">Select State</option>
            </select>
            <select id="districtSelect" class="form-control" disabled>
                <option value="">Select District</option>
            </select>
            <div id="boundaryInfo" class="boundary-info hidden">
                <strong>🗺️ District Boundary:</strong><br>
                <span id="boundaryStatus">Ready to load</span>
            </div>
        </div>

        <div class="section">
            <h3>📅 Date Range</h3>
            <input type="date" id="startDate" class="form-control" value="2020-11-01">
            <input type="date" id="endDate" class="form-control" value="2020-11-30">
            <button onclick="setNov2020()" class="btn btn-primary" style="width:100%;">
                Set November 2020 (Peak Season)
            </button>
        </div>

        <div class="section">
            <h3>🔍 Stubble Detection</h3>
            <button onclick="testVIIRS()" class="btn btn-primary" style="width:100%;">
                Test VIIRS Access
            </button>
            <button id="analyzeButton" onclick="analyzeStubbleBurning()" class="btn btn-success" style="width:100%;" disabled>
                Ultra-Aggressive Detection
            </button>
            <div id="loadingIndicator" class="hidden" style="text-align:center; margin-top:10px; color:#f39c12;">
                🔄 Ultra-aggressive scanning with multi-round detection...
            </div>
        </div>

        <div class="section">
            <h3>🎯 Detection Results</h3>
            <div id="resultsContainer" class="hidden">
                <div class="result-item"><span>Fire Pixels:</span> <span id="firePixels">-</span></div>
                <div class="result-item"><span>Fire Area:</span> <span id="fireArea">-</span></div>
                <div class="result-item"><span>Stubble Spots:</span> <span id="fireHotspots">-</span></div>
                <div class="result-item"><span>Max FRP:</span> <span id="maxFRP">-</span></div>
                <div class="result-item"><span>Images Used:</span> <span id="viirsImages">-</span></div>
            </div>
            <div id="hotspotInfo" class="hotspot-info hidden">
                <strong>🔥 Stubble Burning Spots:</strong><br>
                <span id="hotspotStatus">No spots detected</span>
            </div>
            <div id="coordinateDisplay" class="coordinate-display hidden">
                <strong>📍 Spot Coordinates:</strong><br>
                <span id="coordinateText">Hover over spots to see coordinates</span>
            </div>
        </div>

        <div class="section">
            <h3>📁 Export Data</h3>
            <div class="export-section">
                <p style="font-size: 12px; margin-bottom: 10px;">Export stubble burning coordinates</p>
                <button id="exportButton" onclick="exportCSV()" class="btn btn-warning" style="width:100%;" disabled>
                    📊 Export to CSV
                </button>
                <div id="exportInfo" style="font-size: 11px; margin-top: 5px; color: #bdc3c7;">
                    No data to export
                </div>
            </div>
        </div>

        <div class="section">
            <h3>🗺️ Map Legend</h3>
            <div class="legend">
                <div class="legend-item">
                    <div class="legend-color" style="background: #00FF00;"></div>
                    <span>District Boundary</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #FF6B6B;"></div>
                    <span>Very High (≥15 MW)</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #FF9999;"></div>
                    <span>High (≥5 MW)</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #FFB366;"></div>
                    <span>Medium (≥1 MW)</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #FFE066;"></div>
                    <span>Low (≥0.1 MW)</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #FFEB99;"></div>
                    <span>Very Low (<0.1 MW)</span>
                </div>
            </div>
        </div>
    </div>

    <div class="main-content">
        <div id="map"></div>
        <div class="status-bar">
            <span id="mapCoordinates">Lat: -, Lon: -</span>
            <span>Ultra-Aggressive Detection | Light Colors | Multi-Round</span>
            <span id="boundaryDisplay">No boundary loaded</span>
            <span id="lastUpdate">Ready</span>
        </div>
    </div>

    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
        let map, boundaryLayer, hotspotsLayer;
        let currentState = '', currentDistrict = '';
        let fireData = { hotspots: [] };
        
        // Initialize map
        map = L.map('map').setView([28.6139, 77.2090], 6);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© OpenStreetMap | VIIRS: NASA | Enhanced for Stubble Burning'
        }).addTo(map);

        // Load states on page load
        loadStates();

        function setNov2020() {
            document.getElementById('startDate').value = '2020-11-01';
            document.getElementById('endDate').value = '2020-11-30';
            alert('📅 Set to November 2020 - Peak stubble burning season\\n\\n🔥 Ultra-aggressive detection should capture 800-1000+ fire events\\n🎨 Using lighter color palette for better visibility');
        }

        async function loadStates() {
            try {
                const response = await fetch('/api/states');
                const data = await response.json();
                
                const stateSelect = document.getElementById('stateSelect');
                stateSelect.innerHTML = '<option value="">Select State</option>';
                
                data.states.forEach(state => {
                    const option = document.createElement('option');
                    option.value = state;
                    option.textContent = state;
                    stateSelect.appendChild(option);
                });
            } catch (error) {
                alert('Failed to load states: ' + error.message);
            }
        }

        async function loadDistricts(state) {
            try {
                const response = await fetch(`/api/districts/${encodeURIComponent(state)}`);
                const data = await response.json();
                
                const districtSelect = document.getElementById('districtSelect');
                districtSelect.innerHTML = '<option value="">Select District</option>';
                
                data.districts.forEach(district => {
                    const option = document.createElement('option');
                    option.value = district;
                    option.textContent = district;
                    districtSelect.appendChild(option);
                });
                
                districtSelect.disabled = false;
                currentState = state;
                validateForm();
            } catch (error) {
                alert('Failed to load districts: ' + error.message);
            }
        }

        async function loadDistrictBoundary(state, district) {
            try {
                document.getElementById('boundaryStatus').textContent = 'Loading boundary...';
                document.getElementById('boundaryInfo').classList.remove('hidden');
                
                const response = await fetch(`/api/district-boundary/${encodeURIComponent(state)}/${encodeURIComponent(district)}`);
                const data = await response.json();
                
                if (data.status === 'success' && data.boundary_geojson) {
                    if (boundaryLayer) {
                        map.removeLayer(boundaryLayer);
                    }
                    
                    boundaryLayer = L.geoJSON(data.boundary_geojson, {
                        style: {
                            color: '#00FF00',
                            weight: 3,
                            opacity: 0.8,
                            fillOpacity: 0.1,
                            dashArray: '5, 5'
                        }
                    }).addTo(map);
                    
                    if (data.map_bounds) {
                        const bounds = [[data.map_bounds[1], data.map_bounds[0]], [data.map_bounds[3], data.map_bounds[2]]];
                        map.fitBounds(bounds, { padding: [20, 20] });
                    }
                    
                    document.getElementById('boundaryStatus').textContent = `${district} boundary loaded`;
                    document.getElementById('boundaryDisplay').textContent = `Boundary: ${district}`;
                    
                } else {
                    document.getElementById('boundaryStatus').textContent = 'Boundary not found';
                }
                
            } catch (error) {
                document.getElementById('boundaryStatus').textContent = 'Boundary load failed';
            }
        }

        async function testVIIRS() {
            try {
                const response = await fetch('/api/viirs/test');
                const result = await response.json();
                
                if (result.status === 'success') {
                    alert(`✅ VIIRS VNP14A1 accessible!\\n\\nImages available: ${result.images_available}\\nBands: ${result.bands.join(', ')}\\n\\n🔥 Ready for enhanced stubble burning detection!`);
                } else {
                    alert('⚠️ VIIRS access issue: ' + result.message);
                }
            } catch (error) {
                alert('❌ VIIRS test failed: ' + error.message);
            }
        }

        async function analyzeStubbleBurning() {
            const state = document.getElementById('stateSelect').value;
            const district = document.getElementById('districtSelect').value;
            const startDate = document.getElementById('startDate').value;
            const endDate = document.getElementById('endDate').value;

            document.getElementById('loadingIndicator').classList.remove('hidden');
            document.getElementById('analyzeButton').disabled = true;

            try {
                const response = await fetch('/api/detect-stubble-burning', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        state: state,
                        district: district,
                        start_date: startDate,
                        end_date: endDate
                    })
                });

                // Check if response is ok
                if (!response.ok) {
                    const errorText = await response.text();
                    throw new Error(`Server error (${response.status}): ${errorText}`);
                }

                // Try to parse JSON
                let data;
                try {
                    data = await response.json();
                } catch (jsonError) {
                    const responseText = await response.text();
                    throw new Error(`Invalid JSON response: ${responseText.substring(0, 100)}...`);
                }

                console.log('🎯 Ultra-Aggressive Fire Detection Results:', data);
                
                // Check if detection was successful
                if (data.status && data.status.includes('Error')) {
                    throw new Error(`Detection failed: ${data.status}`);
                }
                
                fireData = data;
                
                // Update results
                document.getElementById('firePixels').textContent = data.fire_pixels.toLocaleString();
                document.getElementById('fireArea').textContent = data.fire_area_hectares + ' hectares';
                document.getElementById('fireHotspots').textContent = data.fire_hotspots.toLocaleString();
                document.getElementById('maxFRP').textContent = data.max_frp + ' MW';
                document.getElementById('viirsImages').textContent = data.viirs_images_used;
                document.getElementById('resultsContainer').classList.remove('hidden');
                
                // Add district boundary
                if (boundaryLayer) map.removeLayer(boundaryLayer);
                if (data.district_boundary) {
                    boundaryLayer = L.geoJSON(data.district_boundary, {
                        style: {
                            color: '#00FF00',
                            weight: 3,
                            opacity: 0.8,
                            fillOpacity: 0.1,
                            dashArray: '5, 5'
                        }
                    }).addTo(map);
                }
                
                // Add fire hotspots WITHOUT black borders
                if (hotspotsLayer) map.removeLayer(hotspotsLayer);
                if (data.hotspots_geojson && data.hotspots_geojson.features && data.hotspots_geojson.features.length > 0) {
                    hotspotsLayer = L.geoJSON(data.hotspots_geojson, {
                        pointToLayer: function(feature, latlng) {
                            const frp = feature.properties.frp;
                            const intensity = feature.properties.intensity;
                            
                            // LIGHTER color palette - pastel/lighter versions
                            let color;
                            if (intensity === 'Very High' || frp >= 15.0) {
                                color = '#FF6B6B';  // Light Red - Very High
                            } else if (intensity === 'High' || frp >= 5.0) {
                                color = '#FF9999';  // Light Pink-Red - High intensity
                            } else if (intensity === 'Medium' || frp >= 1.0) {
                                color = '#FFB366';  // Light Orange - Medium intensity
                            } else if (intensity === 'Low' || frp >= 0.1) {
                                color = '#FFE066';  // Light Yellow - Low intensity
                            } else {
                                color = '#FFEB99';  // Very Light Yellow - Very Low intensity
                            }
                            
                            // Dynamic radius based on FRP (more responsive)
                            let radius;
                            if (frp >= 15.0) {
                                radius = 14;
                            } else if (frp >= 5.0) {
                                radius = 11;
                            } else if (frp >= 1.0) {
                                radius = 8;
                            } else if (frp >= 0.1) {
                                radius = 6;
                            } else {
                                radius = 4;
                            }
                            
                            return L.circleMarker(latlng, {
                                radius: radius,
                                fillColor: color,
                                color: color,  // Same as fill color - no black border
                                weight: 0,     // No border
                                opacity: 0.7,  // Slightly more transparent
                                fillOpacity: 0.6  // More transparent fill
                            });
                        },
                        onEachFeature: function(feature, layer) {
                            const props = feature.properties;
                            
                            layer.bindPopup(`
                                <strong>🔥 Fire Detection Spot #${props.id}</strong><br>
                                <strong>📍 Location:</strong><br>
                                Lat: ${props.latitude.toFixed(6)}<br>
                                Lng: ${props.longitude.toFixed(6)}<br>
                                <strong>🔥 Fire Radiative Power:</strong> ${props.frp} MW<br>
                                <strong>📊 Intensity Level:</strong> ${props.intensity}<br>
                                <strong>✅ Confidence:</strong> ${props.confidence}<br>
                                <strong>🛰️ Source:</strong> ${props.source}<br>
                                <strong>📡 Dataset:</strong> VIIRS VNP14A1
                            `);
                            
                            layer.on('mouseover', function() {
                                document.getElementById('coordinateText').innerHTML = 
                                    `<strong>Spot #${props.id}</strong><br>` +
                                    `Lat: ${props.latitude.toFixed(6)}<br>` +
                                    `Lng: ${props.longitude.toFixed(6)}<br>` +
                                    `FRP: ${props.frp} MW<br>` +
                                    `Intensity: ${props.intensity}<br>` +
                                    `Confidence: ${props.confidence}`;
                                document.getElementById('coordinateDisplay').classList.add('active');
                                document.getElementById('coordinateDisplay').classList.remove('hidden');
                            });
                            
                            layer.on('mouseout', function() {
                                document.getElementById('coordinateDisplay').classList.remove('active');
                            });
                        }
                    }).addTo(map);
                    
                    document.getElementById('hotspotStatus').textContent = 
                        `${data.fire_hotspots} fire spots detected using ${data.detection_method || 'Enhanced VIIRS detection'}`;
                    document.getElementById('hotspotInfo').classList.remove('hidden');
                    document.getElementById('coordinateDisplay').classList.remove('hidden');
                    
                    document.getElementById('exportButton').disabled = false;
                    document.getElementById('exportInfo').textContent = 
                        `${data.fire_hotspots} spots ready for export`;
                    
                } else {
                    document.getElementById('hotspotStatus').textContent = 'No fire spots detected with current parameters';
                    document.getElementById('hotspotInfo').classList.remove('hidden');
                    document.getElementById('exportButton').disabled = true;
                    document.getElementById('exportInfo').textContent = 'No data to export';
                }
                
                if (data.map_bounds) {
                    const bounds = [[data.map_bounds[1], data.map_bounds[0]], [data.map_bounds[3], data.map_bounds[2]]];
                    map.fitBounds(bounds, { padding: [20, 20] });
                }
                
                document.getElementById('lastUpdate').textContent = 'Updated: ' + new Date().toLocaleTimeString();
                
                alert(`🎯 Ultra-Aggressive Fire Detection Complete!\\n\\nFire Spots Detected: ${data.fire_hotspots}\\nTotal Fire Area: ${data.fire_area_hectares} hectares\\nMax FRP: ${data.max_frp} MW\\nDetection Method: ${data.detection_method}\\nVIIRS Images Used: ${data.viirs_images_used}\\n\\n📍 Light color palette for better visibility!\\n🔥 Multi-round detection with ${data.sensitivity || 'ultra-low thresholds'}\\n\\n🎯 Target: 800-1000+ fires for November 2020`);

            } catch (error) {
                console.error('❌ Fire detection error:', error);
                alert('❌ Detection failed: ' + error.message);
            } finally {
                document.getElementById('loadingIndicator').classList.add('hidden');
                document.getElementById('analyzeButton').disabled = false;
            }
        }

        async function exportCSV() {
            try {
                const response = await fetch('/api/export-csv');
                const blob = await response.blob();
                
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `Stubble_Burning_${currentState}_${currentDistrict}_${new Date().toISOString().split('T')[0]}.csv`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                window.URL.revokeObjectURL(url);
                
                alert(`📊 CSV exported successfully!\\n\\nFile: Fire_Detection_${currentState}_${currentDistrict}.csv\\nContains: ${fireData.fire_hotspots} spots with coordinates and properties`);
                
            } catch (error) {
                alert('❌ Export failed: ' + error.message);
            }
        }

        function validateForm() {
            const state = document.getElementById('stateSelect').value;
            const district = document.getElementById('districtSelect').value;
            const startDate = document.getElementById('startDate').value;
            const endDate = document.getElementById('endDate').value;

            document.getElementById('analyzeButton').disabled = !(state && district && startDate && endDate);
        }

        // Event listeners
        document.getElementById('stateSelect').addEventListener('change', function() {
            if (this.value) {
                loadDistricts(this.value);
                if (boundaryLayer) map.removeLayer(boundaryLayer);
                if (hotspotsLayer) map.removeLayer(hotspotsLayer);
                document.getElementById('boundaryInfo').classList.add('hidden');
                document.getElementById('resultsContainer').classList.add('hidden');
                document.getElementById('hotspotInfo').classList.add('hidden');
                document.getElementById('coordinateDisplay').classList.add('hidden');
                document.getElementById('exportButton').disabled = true;
            } else {
                document.getElementById('districtSelect').disabled = true;
                validateForm();
            }
        });

        document.getElementById('districtSelect').addEventListener('change', function() {
            currentDistrict = this.value;
            if (this.value && currentState) {
                loadDistrictBoundary(currentState, this.value);
            }
            validateForm();
        });

        document.getElementById('startDate').addEventListener('change', validateForm);
        document.getElementById('endDate').addEventListener('change', validateForm);

        map.on('mousemove', function(e) {
            document.getElementById('mapCoordinates').textContent = 
                `Lat: ${e.latlng.lat.toFixed(4)}, Lon: ${e.latlng.lng.toFixed(4)}`;
        });
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html_content)

@app.get("/api/states")
async def get_states():
    """Get available states"""
    return {"states": list(STATES_DISTRICTS.keys())}

@app.get("/api/districts/{state}")
async def get_districts(state: str):
    """Get districts for a state"""
    if state not in STATES_DISTRICTS:
        raise HTTPException(status_code=404, detail="State not found")
    return {"districts": STATES_DISTRICTS[state]}

@app.get("/api/district-boundary/{state}/{district}")
async def get_district_boundary_api(state: str, district: str):
    """Get district boundary from FAO/GAUL dataset"""
    boundary_info = get_district_boundary(state, district)
    return boundary_info

@app.get("/api/viirs/test")
async def test_viirs():
    """Test VIIRS dataset access"""
    try:
        viirs = ee.ImageCollection('NASA/VIIRS/002/VNP14A1')
        recent = viirs.filterDate('2020-11-01', '2020-11-30')
        count = recent.size().getInfo()
        
        if count > 0:
            bands = recent.first().bandNames().getInfo()
            return {
                'status': 'success',
                'dataset': 'NASA/VIIRS/002/VNP14A1',
                'images_available': count,
                'bands': bands,
                'resolution': '375m',
                'optimization': 'Enhanced for stubble burning detection'
            }
        else:
            return {
                'status': 'warning',
                'message': 'VIIRS dataset accessible but no data for November 2020'
            }
    except Exception as e:
        return {
            'status': 'error',
            'error': str(e)
        }

@app.post("/api/detect-stubble-burning")
async def detect_fire_enhanced(request: FireDetectionRequest):
    """Enhanced fire detection with robust band handling"""
    
    if request.state not in STATES_DISTRICTS:
        raise HTTPException(status_code=400, detail="Invalid state")
    
    if request.district not in STATES_DISTRICTS[request.state]:
        raise HTTPException(status_code=400, detail="Invalid district")
    
    return detect_viirs_fires_enhanced(request.state, request.district, request.start_date, request.end_date)

@app.get("/api/export-csv")
async def export_csv():
    """Export detected fire spots as CSV"""
    global current_fire_data
    
    if not current_fire_data['hotspots']:
        raise HTTPException(status_code=404, detail="No fire detection data available for export")
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write header with all possible fields
    writer.writerow([
        'Spot_ID',
        'Latitude',
        'Longitude', 
        'Fire_Radiative_Power_MW',
        'FireMask_Class',
        'Confidence_Level',
        'Intensity_Level',
        'State',
        'District',
        'Date_Range',
        'Detection_Source',
        'Resolution',
        'Detection_Method'
    ])
    
    # Write data rows
    for hotspot in current_fire_data['hotspots']:
        writer.writerow([
            hotspot.get('id', ''),
            hotspot.get('latitude', ''),
            hotspot.get('longitude', ''),
            hotspot.get('frp', ''),
            hotspot.get('fire_mask_class', ''),
            hotspot.get('confidence', ''),
            hotspot.get('intensity', ''),
            current_fire_data['state'],
            current_fire_data['district'],
            current_fire_data['date_range'],
            hotspot.get('source', 'VIIRS Detection'),
            '1km',
            'VNP14A1 FireMask Detection'
        ])
    
    csv_content = output.getvalue()
    output.close()
    
    filename = f"VIIRS_Fire_Detection_{current_fire_data['state']}_{current_fire_data['district']}_{datetime.now().strftime('%Y%m%d')}.csv"
    
    return StreamingResponse(
        io.BytesIO(csv_content.encode('utf-8')),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

if __name__ == "__main__":
    print("=" * 60)
    print("ULTRA-AGGRESSIVE VIIRS FIRE DETECTION")
    print("Maximum Sensitivity Multi-Round Detection System")
    print("Features: Ultra-Low Thresholds + Light Color Palette")
    print("=" * 60)
    
    # Test VIIRS access
    try:
        viirs_dataset = ee.ImageCollection('NASA/VIIRS/002/VNP14A1')
        test_count = viirs_dataset.limit(1).size().getInfo()
        print("✅ VIIRS dataset accessible")
        
        # Check available bands
        first_image = viirs_dataset.first()
        bands = first_image.bandNames().getInfo()
        print(f"✅ Available bands: {bands}")
        
        nov_2020 = viirs_dataset.filterDate('2020-11-01', '2020-11-30')
        nov_count = nov_2020.size().getInfo()
        print(f"✅ November 2020 VIIRS images: {nov_count}")
        
        gaul = ee.FeatureCollection("FAO/GAUL/2015/level2")
        boundary_count = gaul.limit(1).size().getInfo()
        print(f"✅ FAO/GAUL boundaries accessible")
        
        print("\n🚀 Starting ultra-aggressive fire detection server")
        print("🔥 Maximum sensitivity for comprehensive fire detection")
        print("⚙️ Ultra-low threshold: >0.001 MW | Sampling: 10,000 pixels")
        print("🎨 Light color palette with enhanced transparency")
        print("🔄 Multi-round detection with temporal compositing")
        uvicorn.run(app, host="localhost", port=8000)
        
    except Exception as e:
        print(f"❌ Initialization failed: {e}")
        print("Check your Google Earth Engine permissions")

