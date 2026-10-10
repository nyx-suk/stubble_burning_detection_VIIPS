"""
Enhanced VIIRS LANCE C2 Fire Detection with Improved Sensitivity
Optimized for detecting all fire spots using NASA/LANCE/SNPP_VIIRS/C2
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
app = FastAPI(title="Ultra-Aggressive VIIRS LANCE C2 Fire Detection", description="Maximum Sensitivity Multi-Round Detection")

class FireDetectionRequest(BaseModel):
    state: str
    district: str
    start_date: str = "2025-07-01"
    end_date: str = "2025-07-13"

# States and districts data
STATES_DISTRICTS = {
    "Punjab": ["Amritsar", "Bathinda", "Ferozepur", "Gurdaspur", "Hoshiarpur", "Jalandhar", "Ludhiana", "Patiala", "Sangrur"],
    "Haryana": ["Ambala", "Hisar", "Karnal", "Kurukshetra", "Panipat", "Rohtak", "Sirsa", "Sonipat", "Yamunanagar"],
    "Uttar Pradesh": ["Agra", "Aligarh", "Bareilly", "Ghaziabad", "Kanpur", "Lucknow", "Meerut", "Varanasi"],
    "Delhi": ["Central Delhi", "East Delhi", "New Delhi", "North Delhi", "South Delhi", "West Delhi"]
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

def extract_fire_spots_lance_c2(viirs_collection, district_geom):
    """Extract fire spots using NASA/LANCE/SNPP_VIIRS/C2 dataset with maximum sensitivity"""
    try:
        print("🔥 Extracting fire spots with maximum sensitivity using LANCE C2...")
        
        # First, check what bands are actually available
        first_image = viirs_collection.first()
        available_bands = first_image.bandNames().getInfo()
        print(f"📡 Available LANCE C2 bands: {available_bands}")
        
        # Get agricultural mask
        agri_mask = get_agricultural_mask(district_geom)
        
        # ULTRA-AGGRESSIVE detection using multiple approaches for LANCE C2
        fire_detections = []
        
        # Method 1: Use FRP (Fire Radiative Power) - primary fire detection
        if 'FRP' in available_bands:
            print("🔥 Using FRP band for primary fire detection")
            frp_max = viirs_collection.select('FRP').max()
            # Ultra-low threshold to catch all possible fires
            fire_detections.append(frp_max.gt(0.001))
            
            # Also use mean FRP over time period
            frp_mean = viirs_collection.select('FRP').mean()
            fire_detections.append(frp_mean.gt(0.005))
            
            # Additional threshold for very low fires
            fire_detections.append(frp_max.gt(0.0001))
        
        # Method 2: Use Bright_ti4 (Brightness Temperature) for fire detection
        if 'Bright_ti4' in available_bands:
            print("🔥 Using Bright_ti4 band for temperature-based detection")
            bright_ti4 = viirs_collection.select('Bright_ti4').max()
            # Fire threshold - temperatures above 320K typically indicate fires
            fire_detections.append(bright_ti4.gt(320))
            
            # Lower threshold for potential fires
            fire_detections.append(bright_ti4.gt(310))
            
            # Mean temperature over time period
            bright_ti4_mean = viirs_collection.select('Bright_ti4').mean()
            fire_detections.append(bright_ti4_mean.gt(315))
        
        # Method 3: Use Bright_ti5 for additional temperature detection
        if 'Bright_ti5' in available_bands:
            print("🔥 Using Bright_ti5 band for additional temperature detection")
            bright_ti5 = viirs_collection.select('Bright_ti5').max()
            # Fire threshold for TI5 band
            fire_detections.append(bright_ti5.gt(300))
            
            # Mean temperature
            bright_ti5_mean = viirs_collection.select('Bright_ti5').mean()
            fire_detections.append(bright_ti5_mean.gt(295))
        
        # Method 4: Use quality flags QF1, QF2, QF3 for fire detection
        for qf_band in ['QF1', 'QF2', 'QF3']:
            if qf_band in available_bands:
                print(f"🔥 Using {qf_band} band for quality-based detection")
                qf_max = viirs_collection.select(qf_band).max()
                # Quality flags often have specific bit patterns for fires
                fire_detections.append(qf_max.gt(0))
                
                # Check for specific fire-related quality flag values
                # Bit operations for fire detection in quality flags
                fire_detections.append(qf_max.bitwiseAnd(1).gt(0))  # Check fire bit
                fire_detections.append(qf_max.bitwiseAnd(2).gt(0))  # Check confidence bit
        
        # Method 5: Combined temperature approach (if both TI4 and TI5 available)
        if 'Bright_ti4' in available_bands and 'Bright_ti5' in available_bands:
            print("🔥 Using combined temperature analysis")
            ti4 = viirs_collection.select('Bright_ti4').max()
            ti5 = viirs_collection.select('Bright_ti5').max()
            
            # Temperature difference can indicate fires
            temp_diff = ti4.subtract(ti5)
            fire_detections.append(temp_diff.gt(10))  # Significant temperature difference
            
            # Combined high temperature
            combined_temp = ti4.add(ti5).divide(2)
            fire_detections.append(combined_temp.gt(305))
        
        # Combine ALL detection methods with OR logic
        if fire_detections:
            combined_fires = fire_detections[0]
            for i in range(1, len(fire_detections)):
                combined_fires = combined_fires.Or(fire_detections[i])
            print(f"🔗 Combined {len(fire_detections)} detection methods")
        else:
            print("⚠️ No suitable fire detection bands found")
            return []
        
        # Apply agricultural mask if available (but make it lenient)
        if agri_mask is not None:
            print("🌾 Applying lenient agricultural area mask")
            # Expand agricultural mask to catch nearby fires
            agri_mask_buffered = agri_mask.focal_max(radius=500, kernelType='circle', units='meters')
            combined_fires = combined_fires.And(agri_mask_buffered)
        
        # Get the best available image for sampling
        if 'FRP' in available_bands:
            sample_image = viirs_collection.select(['FRP', 'Bright_ti4'] if 'Bright_ti4' in available_bands else 'FRP').max()
            frp_available = True
        elif 'Bright_ti4' in available_bands:
            sample_image = viirs_collection.select('Bright_ti4').max()
            frp_available = False
        else:
            sample_image = first_image.select(available_bands[0])
            frp_available = False
            print(f"⚠️ Using {available_bands[0]} for sampling")
        
        # MASSIVE sampling increase for comprehensive detection
        print("📍 Performing comprehensive fire sampling...")
        
        # Multiple sampling rounds to ensure we don't miss fires
        all_hotspots = []
        
        # Round 1: High-density sampling
        fire_sample_1 = sample_image.updateMask(combined_fires).sample(
            region=district_geom,
            scale=375,  # LANCE C2 native resolution
            numPixels=15000,  # Increased for LANCE C2
            geometries=True,
            dropNulls=True
        )
        
        # Round 2: Different scale sampling to catch missed fires
        fire_sample_2 = sample_image.updateMask(combined_fires).sample(
            region=district_geom,
            scale=500,  # Coarser scale might catch different patterns
            numPixels=8000,
            geometries=True,
            dropNulls=True
        )
        
        # Round 3: Fine scale sampling for detailed detection
        fire_sample_3 = sample_image.updateMask(combined_fires).sample(
            region=district_geom,
            scale=250,  # Finer scale
            numPixels=5000,
            geometries=True,
            dropNulls=True
        )
        
        # Combine samples
        all_samples = [fire_sample_1, fire_sample_2, fire_sample_3]
        
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
                        # Round coordinates to avoid near-duplicates
                        lat_rounded = round(coords[1], 5)
                        lng_rounded = round(coords[0], 5)
                        coord_key = (lat_rounded, lng_rounded)
                        
                        # Skip if we already have this coordinate
                        if coord_key in unique_coordinates:
                            continue
                        unique_coordinates.add(coord_key)
                        
                        # Get FRP value if available, otherwise derive from temperature
                        if frp_available and 'FRP' in props:
                            frp_value = props.get('FRP', 0.01)
                        elif 'Bright_ti4' in props:
                            # Convert brightness temperature to pseudo-FRP
                            temp_value = props.get('Bright_ti4', 300)
                            # Simple conversion: higher temp = higher FRP
                            frp_value = max(0.1, (float(temp_value) - 300) * 0.1)
                        else:
                            # Default for detected fires
                            frp_value = 0.5
                        
                        # Ensure FRP is a valid number
                        try:
                            frp_value = float(frp_value) if frp_value is not None else 0.1
                            if frp_value <= 0:
                                frp_value = 0.1  # Minimum for detected fires
                        except (ValueError, TypeError):
                            frp_value = 0.1
                        
                        # Get brightness temperature for additional info
                        brightness_temp = props.get('Bright_ti4', 0)
                        
                        # Ultra-lenient confidence classification to include all fires
                        if frp_value >= 15.0:
                            confidence = 'Very High'
                            intensity = 'Very High'
                        elif frp_value >= 5.0:
                            confidence = 'High'
                            intensity = 'High'
                        elif frp_value >= 1.0:
                            confidence = 'Medium'
                            intensity = 'Medium'
                        elif frp_value >= 0.1:
                            confidence = 'Low'
                            intensity = 'Low'
                        else:
                            confidence = 'Very Low'
                            intensity = 'Very Low'
                        
                        hotspot = {
                            'id': hotspot_id,
                            'latitude': coords[1],
                            'longitude': coords[0],
                            'frp': round(frp_value, 3),
                            'confidence': confidence,
                            'intensity': intensity,
                            'brightness_temp': brightness_temp,
                            'source': f'LANCE C2 Detection R{sample_round}'
                        }
                        all_hotspots.append(hotspot)
                        hotspot_id += 1
        
        print(f"🎯 Total unique fire hotspots detected: {len(all_hotspots)}")
        return all_hotspots
        
    except Exception as e:
        print(f"❌ Fire detection error: {e}")
        print(f"❌ Error details: {type(e).__name__}")
        return []

def detect_viirs_lance_c2_fires(state: str, district: str, start_date: str, end_date: str):
    """Enhanced VIIRS LANCE C2 detection optimized for fire detection"""
    global current_fire_data
    
    try:
        print(f"🔥 VIIRS LANCE C2 Fire Detection: {district}, {state} ({start_date} to {end_date})")
        
        # Get district boundary
        boundary_info = get_district_boundary(state, district)
        
        if boundary_info['status'] != 'success':
            raise HTTPException(status_code=404, detail=f"District {district} not found in {state}")
        
        district_geom = boundary_info['geometry']
        
        # Load VIIRS LANCE C2 dataset
        viirs = ee.ImageCollection('NASA/LANCE/SNPP_VIIRS/C2') \
                  .filterDate(start_date, end_date) \
                  .filterBounds(district_geom)
        
        image_count = viirs.size().getInfo()
        print(f"📡 LANCE C2 images found: {image_count}")
        
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
                'status': 'No LANCE C2 data found for this period'
            }
        
        # Extract fire spots using LANCE C2
        hotspots = extract_fire_spots_lance_c2(viirs, district_geom)
        
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
                    'brightness_temp': hotspot.get('brightness_temp', 0),
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
            'detection_method': 'Enhanced LANCE C2 Fire Detection',
            'dataset': 'NASA/LANCE/SNPP_VIIRS/C2',
            'resolution': '375m',
            'sensitivity': 'Optimized for Real-time Fire Detection',
            'status': 'Success'
        }
        
    except Exception as e:
        print(f"❌ VIIRS LANCE C2 fire detection error: {e}")
        raise HTTPException(status_code=500, detail=f"VIIRS LANCE C2 fire detection failed: {str(e)}")

# API Endpoints
@app.get("/")
async def dashboard():
    """Enhanced dashboard for LANCE C2 fire detection"""
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ultra-Aggressive VIIRS LANCE C2 Fire Detection</title>
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
        <h2>🔥 LANCE C2 Fire Detection</h2>
        <p style="font-size: 12px; color: #bdc3c7; margin-bottom: 20px;">Real-time Fire Detection | Light Colors</p>
        
        <div class="section">
            <h3>⚙️ Detection Settings</h3>
            <div class="sensitivity-info">
                <strong>🎯 LANCE C2 Detection:</strong><br>
                • FRP threshold (>0.001 MW)<br>
                • Brightness temperature (>310K)<br>
                • Quality flag analysis<br>
                • Triple sampling rounds<br>
                • Agricultural area buffering<br>
                • 15,000+ pixel sampling<br>
                • Multi-scale analysis (250m-500m)<br>
                • Real-time data access
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
            <input type="date" id="startDate" class="form-control" value="2025-07-01">
            <input type="date" id="endDate" class="form-control" value="2025-07-13">
            <button onclick="setCurrentWeek()" class="btn btn-primary" style="width:100%;">
                Set Current Week (Real-time)
            </button>
        </div>

        <div class="section">
            <h3>🔍 Fire Detection</h3>
            <button onclick="testLANCE()" class="btn btn-primary" style="width:100%;">
                Test LANCE C2 Access
            </button>
            <button id="analyzeButton" onclick="analyzeFireDetection()" class="btn btn-success" style="width:100%;" disabled>
                Ultra-Aggressive Detection
            </button>
            <div id="loadingIndicator" class="hidden" style="text-align:center; margin-top:10px; color:#f39c12;">
                🔄 Scanning with LANCE C2 real-time data...
            </div>
        </div>

        <div class="section">
            <h3>🎯 Detection Results</h3>
            <div id="resultsContainer" class="hidden">
                <div class="result-item"><span>Fire Pixels:</span> <span id="firePixels">-</span></div>
                <div class="result-item"><span>Fire Area:</span> <span id="fireArea">-</span></div>
                <div class="result-item"><span>Fire Spots:</span> <span id="fireHotspots">-</span></div>
                <div class="result-item"><span>Max FRP:</span> <span id="maxFRP">-</span></div>
                <div class="result-item"><span>Images Used:</span> <span id="viirsImages">-</span></div>
            </div>
            <div id="hotspotInfo" class="hotspot-info hidden">
                <strong>🔥 Fire Spots:</strong><br>
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
                <p style="font-size: 12px; margin-bottom: 10px;">Export fire coordinates</p>
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
            <span>LANCE C2 Detection | Real-time | Multi-Round</span>
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
            attribution: '© OpenStreetMap | VIIRS: NASA LANCE C2 | Real-time Fire Detection'
        }).addTo(map);

        // Load states on page load
        loadStates();

        function setCurrentWeek() {
            const today = new Date();
            const lastWeek = new Date(today.getTime() - 7 * 24 * 60 * 60 * 1000);
            
            document.getElementById('startDate').value = lastWeek.toISOString().split('T')[0];
            document.getElementById('endDate').value = today.toISOString().split('T')[0];
            alert('📅 Set to current week for real-time fire detection\\n\\n🔥 LANCE C2 provides near real-time data\\n🎨 Using lighter color palette for better visibility');
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

        async function testLANCE() {
            try {
                const response = await fetch('/api/lance/test');
                const result = await response.json();
                
                if (result.status === 'success') {
                    alert(`✅ LANCE C2 accessible!\\n\\nImages available: ${result.images_available}\\nBands: ${result.bands.join(', ')}\\nResolution: ${result.resolution}\\n\\n🔥 Ready for real-time fire detection!`);
                } else {
                    alert('⚠️ LANCE C2 access issue: ' + result.message);
                }
            } catch (error) {
                alert('❌ LANCE C2 test failed: ' + error.message);
            }
        }

        async function analyzeFireDetection() {
            const state = document.getElementById('stateSelect').value;
            const district = document.getElementById('districtSelect').value;
            const startDate = document.getElementById('startDate').value;
            const endDate = document.getElementById('endDate').value;

            document.getElementById('loadingIndicator').classList.remove('hidden');
            document.getElementById('analyzeButton').disabled = true;

            try {
                const response = await fetch('/api/detect-lance-fires', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        state: state,
                        district: district,
                        start_date: startDate,
                        end_date: endDate
                    })
                });

                if (!response.ok) {
                    const errorText = await response.text();
                    throw new Error(`Server error (${response.status}): ${errorText}`);
                }

                let data;
                try {
                    data = await response.json();
                } catch (jsonError) {
                    const responseText = await response.text();
                    throw new Error(`Invalid JSON response: ${responseText.substring(0, 100)}...`);
                }

                console.log('🎯 LANCE C2 Fire Detection Results:', data);
                
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
                            
                            // Dynamic radius based on FRP
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
                                color: color,
                                weight: 0,
                                opacity: 0.7,
                                fillOpacity: 0.6
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
                                <strong>🌡️ Brightness Temp:</strong> ${props.brightness_temp}K<br>
                                <strong>📊 Intensity Level:</strong> ${props.intensity}<br>
                                <strong>✅ Confidence:</strong> ${props.confidence}<br>
                                <strong>🛰️ Source:</strong> ${props.source}<br>
                                <strong>📡 Dataset:</strong> LANCE C2
                            `);
                            
                            layer.on('mouseover', function() {
                                document.getElementById('coordinateText').innerHTML = 
                                    `<strong>Spot #${props.id}</strong><br>` +
                                    `Lat: ${props.latitude.toFixed(6)}<br>` +
                                    `Lng: ${props.longitude.toFixed(6)}<br>` +
                                    `FRP: ${props.frp} MW<br>` +
                                    `Temp: ${props.brightness_temp}K<br>` +
                                    `Intensity: ${props.intensity}`;
                                document.getElementById('coordinateDisplay').classList.add('active');
                                document.getElementById('coordinateDisplay').classList.remove('hidden');
                            });
                            
                            layer.on('mouseout', function() {
                                document.getElementById('coordinateDisplay').classList.remove('active');
                            });
                        }
                    }).addTo(map);
                    
                    document.getElementById('hotspotStatus').textContent = 
                        `${data.fire_hotspots} fire spots detected using ${data.detection_method || 'LANCE C2 detection'}`;
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
                
                alert(`🎯 LANCE C2 Fire Detection Complete!\\n\\nFire Spots Detected: ${data.fire_hotspots}\\nTotal Fire Area: ${data.fire_area_hectares} hectares\\nMax FRP: ${data.max_frp} MW\\nDetection Method: ${data.detection_method}\\nLANCE C2 Images Used: ${data.viirs_images_used}\\n\\n📍 Real-time data with light color palette!\\n🔥 Multi-round detection with ${data.sensitivity || 'ultra-low thresholds'}`);

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
                a.download = `LANCE_C2_Fire_Detection_${currentState}_${currentDistrict}_${new Date().toISOString().split('T')[0]}.csv`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                window.URL.revokeObjectURL(url);
                
                alert(`📊 CSV exported successfully!\\n\\nFile: LANCE_C2_Fire_Detection_${currentState}_${currentDistrict}.csv\\nContains: ${fireData.fire_hotspots} spots with coordinates and properties`);
                
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

@app.get("/api/lance/test")
async def test_lance_c2():
    """Test VIIRS LANCE C2 dataset access"""
    try:
        viirs = ee.ImageCollection('NASA/LANCE/SNPP_VIIRS/C2')
        recent = viirs.filterDate('2025-07-01', '2025-07-13')
        count = recent.size().getInfo()
        
        if count > 0:
            bands = recent.first().bandNames().getInfo()
            return {
                'status': 'success',
                'dataset': 'NASA/LANCE/SNPP_VIIRS/C2',
                'images_available': count,
                'bands': bands,
                'resolution': '375m',
                'optimization': 'Real-time fire detection with LANCE C2'
            }
        else:
            return {
                'status': 'warning',
                'message': 'LANCE C2 dataset accessible but no data for current period'
            }
    except Exception as e:
        return {
            'status': 'error',
            'error': str(e)
        }

@app.post("/api/detect-lance-fires")
async def detect_lance_fires(request: FireDetectionRequest):
    """Enhanced fire detection using LANCE C2"""
    
    if request.state not in STATES_DISTRICTS:
        raise HTTPException(status_code=400, detail="Invalid state")
    
    if request.district not in STATES_DISTRICTS[request.state]:
        raise HTTPException(status_code=400, detail="Invalid district")
    
    return detect_viirs_lance_c2_fires(request.state, request.district, request.start_date, request.end_date)

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
        'Brightness_Temperature_K',
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
            hotspot.get('brightness_temp', ''),
            hotspot.get('confidence', ''),
            hotspot.get('intensity', ''),
            current_fire_data['state'],
            current_fire_data['district'],
            current_fire_data['date_range'],
            hotspot.get('source', 'LANCE C2 Detection'),
            '375m',
            'Enhanced LANCE C2 Detection'
        ])
    
    csv_content = output.getvalue()
    output.close()
    
    filename = f"LANCE_C2_Fire_Detection_{current_fire_data['state']}_{current_fire_data['district']}_{datetime.now().strftime('%Y%m%d')}.csv"
    
    return StreamingResponse(
        io.BytesIO(csv_content.encode('utf-8')),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

if __name__ == "__main__":
    print("=" * 60)
    print("ULTRA-AGGRESSIVE VIIRS LANCE C2 FIRE DETECTION")
    print("Real-time Maximum Sensitivity Multi-Round Detection System")
    print("Features: FRP + Temperature + Quality Flags + Light Colors")
    print("=" * 60)
    
    # Test LANCE C2 access
    try:
        viirs_dataset = ee.ImageCollection('NASA/LANCE/SNPP_VIIRS/C2')
        test_count = viirs_dataset.limit(1).size().getInfo()
        print("✅ LANCE C2 dataset accessible")
        
        # Check available bands
        first_image = viirs_dataset.first()
        bands = first_image.bandNames().getInfo()
        print(f"✅ Available bands: {bands}")
        
        current_week = viirs_dataset.filterDate('2025-07-01', '2025-07-13')
        week_count = current_week.size().getInfo()
        print(f"✅ Current week LANCE C2 images: {week_count}")
        
        gaul = ee.FeatureCollection("FAO/GAUL/2015/level2")
        boundary_count = gaul.limit(1).size().getInfo()
        print(f"✅ FAO/GAUL boundaries accessible")
        
        print("\n🚀 Starting LANCE C2 fire detection server")
        print("🔥 Real-time data with maximum sensitivity")
        print("⚙️ FRP threshold: >0.001 MW | Temperature: >310K | Sampling: 15,000 pixels")
        print("🎨 Light color palette with enhanced transparency")
        print("🔄 Triple-round detection with temporal compositing")
        uvicorn.run(app, host="localhost", port=8080)
        
    except Exception as e:
        print(f"❌ Initialization failed: {e}")
        print("Check your Google Earth Engine permissions")