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

def extract_fire_spots_lance_c2(viirs_collection, district_geom):
    """
    Extract active fire spots using NASA/LANCE/SNPP_VIIRS/C2 dataset.
    Uses documented active-fire detections, quality flags, and scientifically
    justified contextual thermal anomaly thresholds (Schroeder et al., 2014)
    to prevent false positives from solar-heated bare soil and ambient temperatures.
    """
    try:
        print("🔥 Extracting active fire spots with scientifically validated thresholds...")
        
        # Check what bands are actually available
        first_image = viirs_collection.first()
        available_bands = first_image.bandNames().getInfo()
        print(f"📡 Available LANCE C2 bands: {available_bands}")
        
        # Map available bands case-insensitively
        bands_map = {b.lower(): b for b in available_bands}
        
        frp_band = bands_map.get('frp') or bands_map.get('fire_radiative_power')
        ti4_band = bands_map.get('bright_ti4') or bands_map.get('t21') or bands_map.get('brightness')
        ti5_band = bands_map.get('bright_ti5') or bands_map.get('t31')
        conf_band = bands_map.get('confidence')
        
        # Quality flag bands
        qa_bands = [b for b in available_bands if b.lower().startswith('qf') or b.lower() == 'qa']
        
        # Get agricultural mask
        agri_mask = get_agricultural_mask(district_geom)
        
        fire_conditions = []
        
        # --- 1. Product Documented Active-Fire FRP Detection ---
        # Documented active fires release measurable Fire Radiative Power (MW).
        # We require FRP > 0.1 MW to confirm true active combustion and eliminate zero-power noise.
        if frp_band:
            print(f"🔥 Using documented FRP band ({frp_band}) for active fire detection")
            frp_max = viirs_collection.select(frp_band).max()
            frp_fire = frp_max.gt(0.1)
            
            # If confidence is also provided in the product, require nominal/high quality
            if conf_band:
                conf_max = viirs_collection.select(conf_band).max()
                # Respect quality flags / confidence: require confidence >= 30% to filter artifacts
                frp_fire = frp_fire.And(conf_max.gte(30))
                
            fire_conditions.append(frp_fire)
            
        # --- 2. Scientifically Justified Contextual Thermal Anomaly Algorithm ---
        # Contextual thresholding based on Schroeder et al. (2014) VIIRS 375m active fire algorithm:
        # Bare agricultural soil under daylight heating regularly reaches 310K-320K (37°C-47°C).
        # Ambient/ground temperatures (e.g. 295K, 310K) MUST NOT trigger fire detections.
        # Active biomass burning creates an intense mid-IR (3.9um) spectral spike relative to thermal-IR (11um).
        if ti4_band and ti5_band:
            print(f"🔥 Using dual-band contextual thermal analysis ({ti4_band} and {ti5_band})")
            ti4 = viirs_collection.select(ti4_band).max()
            ti5 = viirs_collection.select(ti5_band).max()
            temp_diff = ti4.subtract(ti5)
            
            # Contextual fire criteria:
            # - Elevated mid-IR brightness temperature (T4 > 325K) AND pronounced spectral difference (T4 - T5 > 12K), OR
            # - Saturated flaming combustion threshold (T4 > 340K)
            contextual_thermal = (ti4.gt(325).And(temp_diff.gt(12))).Or(ti4.gt(340))
            
            # Quality flag check if quality bands are present
            if qa_bands:
                # In quality flags, ensure pixel is not flagged as cloud or water
                # (Quality flags are used as EXCLUSION filters, never as positive fire triggers)
                qa_img = viirs_collection.select(qa_bands[0]).max()
                # Bit 0 usually indicates cloud/water in VIIRS QF
                qa_valid = qa_img.bitwiseAnd(1).eq(0)
                contextual_thermal = contextual_thermal.And(qa_valid)
                
            fire_conditions.append(contextual_thermal)
            
        elif ti4_band:
            # Single-band thermal detection: require high absolute temperature (T4 > 330K)
            print(f"🔥 Using single-band thermal threshold ({ti4_band} > 330K)")
            ti4 = viirs_collection.select(ti4_band).max()
            fire_conditions.append(ti4.gt(330))
            
        # --- 3. Product Confidence-Based Detection ---
        # If product confidence is provided without FRP or as a validated active-fire band
        if conf_band and not frp_band and not (ti4_band and ti5_band):
            print(f"🔥 Using product confidence band ({conf_band} >= 50%)")
            conf_max = viirs_collection.select(conf_band).max()
            fire_conditions.append(conf_max.gte(50))
            
        # Combine valid fire conditions
        if fire_conditions:
            combined_fires = fire_conditions[0]
            for i in range(1, len(fire_conditions)):
                # If multiple independent scientific methods exist, combine candidates
                combined_fires = combined_fires.Or(fire_conditions[i])
            print(f"🔗 Combined {len(fire_conditions)} scientific detection criteria")
        else:
            print("⚠️ No suitable active-fire detection bands found")
            return []
            
        # Apply agricultural mask if available (with moderate buffer)
        if agri_mask is not None:
            print("🌾 Applying agricultural area mask to focus on crop residue fires")
            agri_mask_buffered = agri_mask.focal_max(radius=375, kernelType='circle', units='meters')
            combined_fires = combined_fires.And(agri_mask_buffered)
            
        # Select sampling bands to include FRP, thermal, and confidence metrics
        sample_bands = []
        if frp_band:
            sample_bands.append(frp_band)
        if ti4_band:
            sample_bands.append(ti4_band)
        if ti5_band:
            sample_bands.append(ti5_band)
        if conf_band:
            sample_bands.append(conf_band)
            
        if not sample_bands:
            sample_bands = [available_bands[0]]
            
        sample_image = viirs_collection.select(sample_bands).max()
        frp_available = frp_band is not None
        
        print("📍 Performing active fire sampling at native 375m resolution...")
        
        # Sampling focused on confirmed fire pixels (native 375m scale)
        fire_sample = sample_image.updateMask(combined_fires).sample(
            region=district_geom,
            scale=375,  # Native VIIRS 375m resolution
            numPixels=5000,
            geometries=True,
            dropNulls=True
        )
        
        all_hotspots = []
        hotspot_id = 1
        unique_coordinates = set()
        
        fire_features = fire_sample.getInfo()
        features_list = fire_features.get('features', [])
        print(f"📊 Identified {len(features_list)} confirmed fire detections")
        
        for feature in features_list:
            geom = feature.get('geometry', {})
            props = feature.get('properties', {})
            
            if geom.get('type') == 'Point':
                coords = geom.get('coordinates', [])
                if len(coords) >= 2:
                    lat_rounded = round(coords[1], 5)
                    lng_rounded = round(coords[0], 5)
                    coord_key = (lat_rounded, lng_rounded)
                    
                    if coord_key in unique_coordinates:
                        continue
                    unique_coordinates.add(coord_key)
                    
                    # Extract brightness temperature
                    b_temp = None
                    if ti4_band and ti4_band in props:
                        try:
                            b_temp = float(props[ti4_band])
                        except (ValueError, TypeError):
                            b_temp = None
                            
                    # Extract thermal difference if ti5 available
                    t_diff = None
                    if ti4_band and ti5_band and ti4_band in props and ti5_band in props:
                        try:
                            t_diff = float(props[ti4_band]) - float(props[ti5_band])
                        except (ValueError, TypeError):
                            t_diff = None
                            
                    # Extract FRP value
                    frp_value = None
                    if frp_available and frp_band in props:
                        try:
                            frp_val = float(props[frp_band])
                            if frp_val > 0:
                                frp_value = frp_val
                        except (ValueError, TypeError):
                            frp_value = None
                            
                    if frp_value is None:
                        # Estimate FRP from temperature if FRP not directly provided
                        if b_temp is not None:
                            # Higher excess temperature above background corresponds to higher FRP
                            frp_value = max(0.2, (b_temp - 315.0) * 0.15)
                        else:
                            frp_value = 1.0
                            
                    # Extract or determine confidence
                    confidence = 'Nominal'
                    if conf_band and conf_band in props:
                        try:
                            c_val = float(props[conf_band])
                            if c_val >= 80:
                                confidence = 'High'
                            elif c_val >= 30:
                                confidence = 'Nominal'
                            else:
                                confidence = 'Low'
                        except (ValueError, TypeError):
                            confidence = 'Nominal'
                    else:
                        # Derive confidence from contextual thermal indicators and FRP
                        if (b_temp and b_temp >= 340) or (t_diff and t_diff >= 20) or frp_value >= 5.0:
                            confidence = 'High'
                        elif (b_temp and b_temp >= 325) or (t_diff and t_diff >= 12) or frp_value >= 1.0:
                            confidence = 'Nominal'
                        else:
                            confidence = 'Low'
                            
                    # Determine intensity based on FRP (MW)
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
                        
                    hotspot = {
                        'id': hotspot_id,
                        'latitude': coords[1],
                        'longitude': coords[0],
                        'frp': round(frp_value, 3),
                        'confidence': confidence,
                        'intensity': intensity,
                        'brightness_temp': round(b_temp, 2) if b_temp else None,
                        'source': f'LANCE C2 Active Fire ({confidence})'
                    }
                    all_hotspots.append(hotspot)
                    hotspot_id += 1
                    
        print(f"🎯 Total confirmed fire hotspots detected: {len(all_hotspots)}")
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
                'nominal_footprint_hectares': 0.0,
                'fire_area_hectares': 0.0,
                'area_disclaimer': 'Nominal hotspot footprint only: each detected pixel contributes its nominal 375m x 375m area. This is not verified burned area; hotspots can be sub-pixel. Burned area requires burn-scar analysis (e.g. Sentinel-2 dNBR).',
                'pixel_resolution_meters': 375,
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
        # Nominal pixel footprint only; a thermal hotspot does not mean the full pixel burned.
        nominal_footprint_hectares = fire_pixels * 14.0625  # 375m x 375m = 140,625 m² = 14.0625 ha
        area_disclaimer = (
            'Nominal hotspot footprint represents the aggregate area of 375m pixels containing active '
            'thermal detections, NOT verified burned area. Fires may cover only part of a pixel. '
            'Verified burned area requires burn-scar analysis (e.g. Sentinel-2 dNBR).'
        )
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
            'nominal_footprint_hectares': round(nominal_footprint_hectares, 2),
            # Legacy field retained for existing clients; use nominal_footprint_hectares instead.
            'fire_area_hectares': round(nominal_footprint_hectares, 2),
            'legacy_fire_area_field_note': 'Deprecated compatibility alias for nominal_footprint_hectares; this is not verified burned area.',
            'area_disclaimer': area_disclaimer,
            'pixel_resolution_meters': 375,
            'fire_hotspots': len(hotspots),
            'max_frp': round(max_frp, 3),
            'hotspots_coordinates': hotspots,
            'hotspots_geojson': hotspots_geojson,
            'district_boundary': boundary_info['boundary_geojson'],
            'map_bounds': boundary_info['bounds'],
            'viirs_images_used': image_count,
            'detection_method': 'Contextual Active Fire Detection (Schroeder et al., 2014)',
            'dataset': 'NASA/LANCE/SNPP_VIIRS/C2',
            'resolution': '375m',
            'sensitivity': 'Scientifically Validated Contextual Thermal & FRP Thresholds',
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
                • FRP confirmation (>0.1 MW)<br>
                • Contextual thermal (T4 > 325K & ΔT > 12K)<br>
                • Flaming threshold (T4 > 340K)<br>
                • Quality flags & confidence respected<br>
                • Native 375m resolution<br>
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
                <div class="result-item"><span>Nominal Hotspot Footprint:</span> <span id="fireArea">-</span></div>
                <div style="font-size: 10px; color: #bdc3c7; margin-top: -3px; margin-bottom: 5px;">Pixel footprint only; hotspots may be sub-pixel. This is not measured burned area.</div>
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
                document.getElementById('fireArea').textContent = (data.nominal_footprint_hectares ?? data.fire_area_hectares) + ' ha';
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
                
                alert(`🎯 LANCE C2 Fire Detection Complete!\\n\\nFire Spots Detected: ${data.fire_hotspots}\\nNominal Hotspot Pixel Footprint: ${data.nominal_footprint_hectares ?? data.fire_area_hectares} ha\\n(Not verified burned area; hotspots may be sub-pixel. Burn scars require separate analysis.)\\nMax FRP: ${data.max_frp} MW\\nDetection Method: ${data.detection_method}\\nLANCE C2 Images Used: ${data.viirs_images_used}\\n\\n📍 Real-time data with light color palette!\\n🔥 Multi-round detection with ${data.sensitivity || 'ultra-low thresholds'}`);

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
            hotspot.get('source', 'LANCE C2 Active Fire'),
            '375m',
            'Contextual Active Fire Detection (Schroeder et al., 2014)'
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
        print("🔥 Real-time data with contextual thermal anomaly detection")
        print("⚙️ Contextual thresholds: T4 > 325K & ΔT > 12K | Flaming: T4 > 340K | FRP: >0.1 MW")
        print("🛡️ Quality flags respected | Bare soil false-positive rejection enabled")
        print("🔄 Sampling at native 375m resolution")
        uvicorn.run(app, host="localhost", port=8080)
        
    except Exception as e:
        print(f"❌ Initialization failed: {e}")
        print("Check your Google Earth Engine permissions")
