"""
Enhanced VIIRS Data Fusion Fire Detection System
Combines NASA/LANCE/SNPP_VIIRS/C2 + NASA/VIIRS/002/VNP14A1 for Precise Detection
Data Fusion Approach: Fuse datasets first, then detect fire spots using combined properties
"""

import ee
import uvicorn
import csv
import io
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from datetime import datetime, timedelta

# Initialize Google Earth Engine
try:
    ee.Initialize(project='ee-deepeshy')
    print("✅ Google Earth Engine initialized")
except Exception as e:
    print(f"❌ GEE Error: {e}")

# Create FastAPI app
app = FastAPI(title="VIIRS Data Fusion Fire Detection System", description="LANCE C2 + VNP14A1 Data Fusion")

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
    'date_range': '',
    'fusion_info': {}
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
        landcover = ee.ImageCollection("MODIS/061/MCD12Q1") \
                     .filterDate('2020-01-01', '2020-12-31') \
                     .first() \
                     .select('LC_Type1')
        
        # Agricultural land cover types (12: Croplands, 14: Cropland/Natural vegetation mosaic)
        agri_mask = landcover.eq(12).Or(landcover.eq(14))
        
        return agri_mask.clip(district_geom)
    except Exception as e:
        print(f"⚠️ Agricultural mask creation failed: {e}")
        return None

def perform_viirs_data_fusion(lance_collection, vnp14a1_collection, district_geom, start_date, end_date):
    """
    Perform data fusion between LANCE C2 and VNP14A1 datasets
    Combines real-time and processed data for enhanced fire detection
    """
    try:
        print("🔄 Performing VIIRS data fusion...")
        
        # Check available bands in both collections
        lance_bands = []
        vnp14a1_bands = []
        
        if lance_collection.size().getInfo() > 0:
            lance_bands = lance_collection.first().bandNames().getInfo()
            print(f"📡 LANCE C2 bands: {lance_bands}")
        
        if vnp14a1_collection.size().getInfo() > 0:
            vnp14a1_bands = vnp14a1_collection.first().bandNames().getInfo()
            print(f"📡 VNP14A1 bands: {vnp14a1_bands}")
        
        # Create temporal composites for fusion
        fusion_result = {}
        
        # 1. LANCE C2 Processing (Real-time data)
        if lance_collection.size().getInfo() > 0:
            print("🛰️ Processing LANCE C2 real-time data...")
            
            # FRP composite from LANCE
            if 'frp' in lance_bands:  # Note: LANCE bands are lowercase
                lance_frp_max = lance_collection.select('frp').max().rename('LANCE_FRP_MAX')
                lance_frp_mean = lance_collection.select('frp').mean().rename('LANCE_FRP_MEAN')
                lance_frp_count = lance_collection.select('frp').count().rename('LANCE_FRP_COUNT')
                fusion_result['lance_frp_max'] = lance_frp_max
                fusion_result['lance_frp_mean'] = lance_frp_mean
                fusion_result['lance_frp_count'] = lance_frp_count
            
            # Temperature composites from LANCE
            if 'Bright_ti4' in lance_bands:  # Note: LANCE bands have this exact case
                lance_ti4_max = lance_collection.select('Bright_ti4').max().rename('LANCE_TI4_MAX')
                lance_ti4_mean = lance_collection.select('Bright_ti4').mean().rename('LANCE_TI4_MEAN')
                fusion_result['lance_ti4_max'] = lance_ti4_max
                fusion_result['lance_ti4_mean'] = lance_ti4_mean
            
            if 'Bright_ti5' in lance_bands:  # Note: LANCE bands have this exact case
                lance_ti5_max = lance_collection.select('Bright_ti5').max().rename('LANCE_TI5_MAX')
                lance_ti5_mean = lance_collection.select('Bright_ti5').mean().rename('LANCE_TI5_MEAN')
                fusion_result['lance_ti5_max'] = lance_ti5_max
                fusion_result['lance_ti5_mean'] = lance_ti5_mean
            
            # Quality flags from LANCE (use actual available bands)
            quality_bands = ['confidence']  # This is the actual quality band in LANCE
            for qf_band in quality_bands:
                if qf_band in lance_bands:
                    qf_max = lance_collection.select(qf_band).max().rename(f'LANCE_{qf_band.upper()}_MAX')
                    qf_mean = lance_collection.select(qf_band).mean().rename(f'LANCE_{qf_band.upper()}_MEAN')
                    fusion_result[f'lance_{qf_band.lower()}_max'] = qf_max
                    fusion_result[f'lance_{qf_band.lower()}_mean'] = qf_mean
        
        # 2. VNP14A1 Processing (Daily processed fire product)
        if vnp14a1_collection.size().getInfo() > 0:
            print("🛰️ Processing VNP14A1 fire product data...")
            
            # Fire detection from VNP14A1
            if 'FireMask' in vnp14a1_bands:  # Use exact case from logs
                vnp_fire_mask = vnp14a1_collection.select('FireMask').max().rename('VNP_FIRE_MASK')
                vnp_fire_count = vnp14a1_collection.select('FireMask').count().rename('VNP_FIRE_COUNT')
                fusion_result['vnp_fire_mask'] = vnp_fire_mask
                fusion_result['vnp_fire_count'] = vnp_fire_count
            
            # FRP from VNP14A1 if available
            if 'MaxFRP' in vnp14a1_bands:  # Use exact case from logs
                vnp_max_frp = vnp14a1_collection.select('MaxFRP').max().rename('VNP_MAX_FRP')
                vnp_mean_frp = vnp14a1_collection.select('MaxFRP').mean().rename('VNP_MEAN_FRP')
                fusion_result['vnp_max_frp'] = vnp_max_frp
                fusion_result['vnp_mean_frp'] = vnp_mean_frp
            
            # Quality assessment bands
            if 'QA' in vnp14a1_bands:  # Use exact case from logs
                vnp_qa = vnp14a1_collection.select('QA').max().rename('VNP_QA')
                fusion_result['vnp_qa'] = vnp_qa
        
        # 3. Create Fusion Composite Image
        print("🔗 Creating fusion composite...")
        
        fusion_bands = []
        band_names = []
        
        # Add all fusion components to a single image
        for key, image in fusion_result.items():
            fusion_bands.append(image)
            band_names.append(key)
        
        if fusion_bands:
            # Create multi-band fusion image
            fusion_image = ee.Image.cat(fusion_bands).clip(district_geom)
            
            # 4. Create Enhanced Fire Detection Indices
            print("🎯 Creating enhanced fire detection indices...")
            
            enhanced_indices = {}
            
            # Multi-source FRP fusion
            if 'lance_frp_max' in fusion_result and 'vnp_max_frp' in fusion_result:
                # Combined FRP confidence (where both datasets agree)
                frp_consensus = fusion_result['lance_frp_max'].multiply(fusion_result['vnp_max_frp']).sqrt().rename('FRP_CONSENSUS')
                enhanced_indices['frp_consensus'] = frp_consensus
                
                # FRP enhancement (maximum of both sources)
                frp_enhanced = fusion_result['lance_frp_max'].max(fusion_result['vnp_max_frp']).rename('FRP_ENHANCED')
                enhanced_indices['frp_enhanced'] = frp_enhanced
            
            # Temperature-Fire Mask Fusion
            if 'lance_ti4_max' in fusion_result and 'vnp_fire_mask' in fusion_result:
                # Temperature-confirmed fires
                temp_fire_fusion = fusion_result['lance_ti4_max'].multiply(fusion_result['vnp_fire_mask']).rename('TEMP_FIRE_FUSION')
                enhanced_indices['temp_fire_fusion'] = temp_fire_fusion
            
            # Multi-temporal fire persistence
            if 'lance_frp_count' in fusion_result and 'vnp_fire_count' in fusion_result:
                # Fire persistence index
                fire_persistence = fusion_result['lance_frp_count'].add(fusion_result['vnp_fire_count']).rename('FIRE_PERSISTENCE')
                enhanced_indices['fire_persistence'] = fire_persistence
            
            # Quality-weighted fire detection
            quality_bands = [key for key in fusion_result.keys() if 'confidence' in key.lower() or 'qa' in key.lower()]
            if quality_bands:
                # Combine quality indicators
                quality_composite = ee.Image(0)
                for qb in quality_bands:
                    if qb in fusion_result:
                        quality_composite = quality_composite.add(fusion_result[qb])
                
                quality_weighted = quality_composite.divide(len(quality_bands)).rename('QUALITY_WEIGHTED')
                enhanced_indices['quality_weighted'] = quality_weighted
            
            # Add enhanced indices to fusion image
            for key, image in enhanced_indices.items():
                fusion_bands.append(image)
                # Get the actual band name from the image (which will be uppercase)
                actual_band_name = image.bandNames().getInfo()[0]
                band_names.append(actual_band_name)
            
            # Final fusion image with all bands
            final_fusion_image = ee.Image.cat(fusion_bands).clip(district_geom)
            
            fusion_info = {
                'lance_images': lance_collection.size().getInfo(),
                'vnp14a1_images': vnp14a1_collection.size().getInfo(),
                'lance_bands': lance_bands,
                'vnp14a1_bands': vnp14a1_bands,
                'fusion_bands': band_names,
                'enhanced_indices': list(enhanced_indices.keys()),
                'fusion_success': True
            }
            
            return final_fusion_image, fusion_info
        
        else:
            print("⚠️ No data available for fusion")
            return None, {
                'lance_images': lance_collection.size().getInfo(),
                'vnp14a1_images': vnp14a1_collection.size().getInfo(),
                'fusion_success': False,
                'error': 'No data available for fusion'
            }
            
    except Exception as e:
        print(f"❌ Data fusion error: {e}")
        return None, {
            'fusion_success': False,
            'error': str(e)
        }

def extract_fire_spots_from_fusion(fusion_image, fusion_info, district_geom):
    """Extract fire spots using the fused VIIRS data"""
    try:
        print("🔥 Extracting fire spots from fused VIIRS data...")
        
        if fusion_image is None:
            return []
        
        # Get available bands from fusion
        fusion_bands = fusion_info.get('fusion_bands', [])
        print(f"🔗 Available fusion bands: {fusion_bands}")
        
        # Create comprehensive fire detection using fused data
        fire_detection_layers = []
        
        # 1. Enhanced FRP Detection
        if 'FRP_ENHANCED' in fusion_bands:
            print("🔥 Using enhanced FRP for primary detection")
            frp_enhanced = fusion_image.select('FRP_ENHANCED')
            fire_detection_layers.append(frp_enhanced.gt(0.001))
            fire_detection_layers.append(frp_enhanced.gt(0.01))
            fire_detection_layers.append(frp_enhanced.gt(0.1))
        
        # 2. FRP Consensus Detection (high confidence)
        if 'FRP_CONSENSUS' in fusion_bands:
            print("🔥 Using FRP consensus for high-confidence detection")
            frp_consensus = fusion_image.select('FRP_CONSENSUS')
            fire_detection_layers.append(frp_consensus.gt(0.005))
            fire_detection_layers.append(frp_consensus.gt(0.05))
        
        # 3. Temperature-Fire Fusion Detection
        if 'TEMP_FIRE_FUSION' in fusion_bands:
            print("🔥 Using temperature-fire fusion detection")
            temp_fire = fusion_image.select('TEMP_FIRE_FUSION')
            fire_detection_layers.append(temp_fire.gt(0))
            fire_detection_layers.append(temp_fire.gt(300))
        
        # 4. Fire Persistence Detection
        if 'FIRE_PERSISTENCE' in fusion_bands:
            print("🔥 Using fire persistence for temporal validation")
            persistence = fusion_image.select('FIRE_PERSISTENCE')
            fire_detection_layers.append(persistence.gt(1))  # At least 2 detections
            fire_detection_layers.append(persistence.gt(2))  # High persistence
        
        # 5. Individual LANCE bands if enhanced not available
        lance_frp_bands = [b for b in fusion_bands if 'LANCE_FRP' in b]
        for lance_band in lance_frp_bands:
            print(f"🔥 Using LANCE {lance_band}")
            lance_data = fusion_image.select(lance_band)
            fire_detection_layers.append(lance_data.gt(0.001))
        
        # 6. Individual VNP14A1 bands
        vnp_bands = [b for b in fusion_bands if 'VNP_' in b and 'MASK' in b]
        for vnp_band in vnp_bands:
            print(f"🔥 Using VNP14A1 {vnp_band}")
            vnp_data = fusion_image.select(vnp_band)
            fire_detection_layers.append(vnp_data.gt(0))
        
        # 7. Temperature-based detection from LANCE
        temp_bands = [b for b in fusion_bands if 'TI4' in b or 'TI5' in b]
        for temp_band in temp_bands:
            print(f"🔥 Using temperature {temp_band}")
            temp_data = fusion_image.select(temp_band)
            fire_detection_layers.append(temp_data.gt(310))
            fire_detection_layers.append(temp_data.gt(320))
        
        # 8. Quality-weighted detection
        if 'QUALITY_WEIGHTED' in fusion_bands:
            print("🔥 Using quality-weighted detection")
            quality_weighted = fusion_image.select('QUALITY_WEIGHTED')
            fire_detection_layers.append(quality_weighted.gt(0.1))
        
        # Combine all detection layers
        if fire_detection_layers:
            print(f"🔗 Combining {len(fire_detection_layers)} detection layers")
            combined_fire_detection = fire_detection_layers[0]
            for i in range(1, len(fire_detection_layers)):
                combined_fire_detection = combined_fire_detection.Or(fire_detection_layers[i])
        else:
            print("⚠️ No fire detection layers available")
            return []
        
        # Apply agricultural mask
        agri_mask = get_agricultural_mask(district_geom)
        if agri_mask is not None:
            print("🌾 Applying agricultural area mask to fused data")
            agri_mask_buffered = agri_mask.focal_max(radius=500, kernelType='circle', units='meters')
            combined_fire_detection = combined_fire_detection.And(agri_mask_buffered)
        
        # Prepare sampling image with key bands
        sample_bands = []
        sample_names = []
        
        # Priority sampling bands (using uppercase names)
        priority_bands = ['FRP_ENHANCED', 'FRP_CONSENSUS', 'TEMP_FIRE_FUSION', 'FIRE_PERSISTENCE']
        for pb in priority_bands:
            if pb in fusion_bands:
                sample_bands.append(pb)
                sample_names.append(pb)
        
        # Add LANCE FRP if available
        lance_frp_bands = [b for b in fusion_bands if 'LANCE_FRP_MAX' in b]
        for lfb in lance_frp_bands:
            sample_bands.append(lfb)
            sample_names.append(lfb)
        
        # Add temperature bands
        temp_bands = [b for b in fusion_bands if 'TI4_MAX' in b]
        for tb in temp_bands:
            sample_bands.append(tb)
            sample_names.append(tb)
        
        if not sample_bands:
            # Fallback to any available bands
            sample_bands = fusion_bands[:5]  # Take first 5 bands
            sample_names = sample_bands
        
        print(f"📍 Sampling with bands: {sample_names}")
        
        # Create sampling image
        sampling_image = fusion_image.select(sample_bands[:5])  # Limit to 5 bands for sampling
        
        # Multi-scale sampling from fused data
        print("📍 Performing comprehensive sampling on fused data...")
        
        # Three rounds of sampling
        fire_samples = []
        
        # Round 1: High resolution
        fire_sample_1 = sampling_image.updateMask(combined_fire_detection).sample(
            region=district_geom,
            scale=375,
            numPixels=20000,  # Increased for fusion data
            geometries=True,
            dropNulls=True
        )
        fire_samples.append(('High-res', fire_sample_1))
        
        # Round 2: Medium resolution
        fire_sample_2 = sampling_image.updateMask(combined_fire_detection).sample(
            region=district_geom,
            scale=500,
            numPixels=10000,
            geometries=True,
            dropNulls=True
        )
        fire_samples.append(('Medium-res', fire_sample_2))
        
        # Round 3: Coarse resolution for broader coverage
        fire_sample_3 = sampling_image.updateMask(combined_fire_detection).sample(
            region=district_geom,
            scale=750,
            numPixels=5000,
            geometries=True,
            dropNulls=True
        )
        fire_samples.append(('Coarse-res', fire_sample_3))
        
        # Process samples
        hotspots = []
        hotspot_id = 1
        unique_coordinates = set()
        
        for round_name, fire_sample in fire_samples:
            fire_features = fire_sample.getInfo()
            round_features = len(fire_features.get('features', []))
            print(f"📊 Fusion {round_name}: {round_features} potential fire points")
            
            for feature in fire_features.get('features', []):
                geom = feature.get('geometry', {})
                props = feature.get('properties', {})
                
                if geom.get('type') == 'Point':
                    coords = geom.get('coordinates', [])
                    if len(coords) >= 2:
                        # Check for spatial duplicates
                        lat_rounded = round(coords[1], 5)
                        lng_rounded = round(coords[0], 5)
                        coord_key = (lat_rounded, lng_rounded)
                        
                        if coord_key in unique_coordinates:
                            continue
                        unique_coordinates.add(coord_key)
                        
                        # Extract fused properties
                        fusion_props = {}
                        for band in sample_names:
                            if band in props:
                                fusion_props[band] = props[band]
                        
                        # Calculate composite FRP from fusion
                        frp_value = 0.1  # Default
                        
                        # Priority: Enhanced FRP
                        if 'FRP_ENHANCED' in fusion_props:
                            frp_value = max(0.1, float(fusion_props['FRP_ENHANCED']))
                        elif 'FRP_CONSENSUS' in fusion_props:
                            frp_value = max(0.1, float(fusion_props['FRP_CONSENSUS']))
                        elif any('LANCE_FRP' in k for k in fusion_props.keys()):
                            # Use LANCE FRP
                            lance_frp_keys = [k for k in fusion_props.keys() if 'LANCE_FRP' in k]
                            frp_value = max(0.1, float(fusion_props[lance_frp_keys[0]]))
                        elif any('TI4' in k for k in fusion_props.keys()):
                            # Convert temperature to pseudo-FRP
                            temp_keys = [k for k in fusion_props.keys() if 'TI4' in k]
                            temp_value = float(fusion_props[temp_keys[0]])
                            frp_value = max(0.1, (temp_value - 300) * 0.1)
                        
                        # Get fire persistence score
                        persistence_score = fusion_props.get('FIRE_PERSISTENCE', 1)
                        
                        # Get temperature info
                        temp_keys = [k for k in fusion_props.keys() if 'TI4' in k]
                        brightness_temp = float(fusion_props[temp_keys[0]]) if temp_keys else 0
                        
                        # Enhanced confidence based on fusion
                        confidence_factors = []
                        
                        # FRP-based confidence
                        if frp_value >= 15.0:
                            confidence_factors.append('Very High FRP')
                        elif frp_value >= 5.0:
                            confidence_factors.append('High FRP')
                        elif frp_value >= 1.0:
                            confidence_factors.append('Medium FRP')
                        else:
                            confidence_factors.append('Low FRP')
                        
                        # Persistence-based confidence
                        if persistence_score >= 3:
                            confidence_factors.append('High Persistence')
                        elif persistence_score >= 2:
                            confidence_factors.append('Medium Persistence')
                        
                        # Fusion-based confidence
                        if 'FRP_CONSENSUS' in fusion_props and fusion_props['FRP_CONSENSUS'] > 0:
                            confidence_factors.append('Multi-Source Consensus')
                        
                        if 'TEMP_FIRE_FUSION' in fusion_props and fusion_props['TEMP_FIRE_FUSION'] > 0:
                            confidence_factors.append('Temperature Validated')
                        
                        # Overall confidence
                        if len(confidence_factors) >= 3:
                            overall_confidence = 'Very High'
                            overall_intensity = 'Very High'
                        elif len(confidence_factors) >= 2:
                            overall_confidence = 'High'
                            overall_intensity = 'High'
                        elif any('High' in cf for cf in confidence_factors):
                            overall_confidence = 'Medium'
                            overall_intensity = 'Medium'
                        else:
                            overall_confidence = 'Low'
                            overall_intensity = 'Low'
                        
                        hotspot = {
                            'id': hotspot_id,
                            'latitude': coords[1],
                            'longitude': coords[0],
                            'frp': round(frp_value, 3),
                            'confidence': overall_confidence,
                            'intensity': overall_intensity,
                            'brightness_temp': brightness_temp,
                            'persistence_score': persistence_score,
                            'confidence_factors': confidence_factors,
                            'fusion_properties': fusion_props,
                            'source': f'Data Fusion {round_name}',
                            'detection_method': 'VIIRS Data Fusion'
                        }
                        hotspots.append(hotspot)
                        hotspot_id += 1
        
        print(f"🎯 Total fusion-based fire hotspots: {len(hotspots)}")
        return hotspots
        
    except Exception as e:
        print(f"❌ Fusion fire extraction error: {e}")
        return []

def detect_fusion_viirs_fires(state: str, district: str, start_date: str, end_date: str):
    """Enhanced fire detection using VIIRS data fusion"""
    global current_fire_data
    
    try:
        print(f"🔗 VIIRS Data Fusion Fire Detection: {district}, {state} ({start_date} to {end_date})")
        
        # Get district boundary
        boundary_info = get_district_boundary(state, district)
        
        if boundary_info['status'] != 'success':
            raise HTTPException(status_code=404, detail=f"District {district} not found in {state}")
        
        district_geom = boundary_info['geometry']
        
        # Load both VIIRS datasets
        print("📡 Loading VIIRS datasets for fusion...")
        
        # LANCE C2 (Real-time)
        lance_viirs = ee.ImageCollection('NASA/LANCE/SNPP_VIIRS/C2') \
                        .filterDate(start_date, end_date) \
                        .filterBounds(district_geom)
        
        # VNP14A1 (Daily fire product) - Note: Adjust date range for daily product
        vnp14a1 = ee.ImageCollection('NASA/VIIRS/002/VNP14A1') \
                     .filterDate(start_date, end_date) \
                     .filterBounds(district_geom)
        
        lance_count = lance_viirs.size().getInfo()
        vnp14a1_count = vnp14a1.size().getInfo()
        total_images = lance_count + vnp14a1_count
        
        print(f"📡 LANCE C2 images: {lance_count}")
        print(f"📡 VNP14A1 images: {vnp14a1_count}")
        print(f"📡 Total images for fusion: {total_images}")
        
        if total_images == 0:
            current_fire_data = {
                'hotspots': [],
                'state': state,
                'district': district,
                'date_range': f"{start_date} to {end_date}",
                'fusion_info': {'fusion_success': False, 'error': 'No data available'}
            }
            
            return {
                'fire_pixels': 0,
                'fire_area_hectares': 0.0,
                'fire_hotspots': 0,
                'max_frp': 0.0,
                'hotspots_coordinates': [],
                'district_boundary': boundary_info['boundary_geojson'],
                'map_bounds': boundary_info['bounds'],
                'lance_images': 0,
                'vnp14a1_images': 0,
                'total_images': 0,
                'fusion_info': {'fusion_success': False, 'error': 'No data available'},
                'status': 'No VIIRS data found for fusion'
            }
        
        # Perform data fusion
        fusion_image, fusion_info = perform_viirs_data_fusion(
            lance_viirs, vnp14a1, district_geom, start_date, end_date
        )
        
        if not fusion_info.get('fusion_success', False):
            raise Exception(f"Data fusion failed: {fusion_info.get('error', 'Unknown error')}")
        
        # Extract fire spots from fused data
        hotspots = extract_fire_spots_from_fusion(fusion_image, fusion_info, district_geom)
        
        # Calculate statistics
        fire_pixels = len(hotspots)
        fire_area_hectares = fire_pixels * 0.140625  # 375m x 375m
        max_frp = max([h['frp'] for h in hotspots], default=0.0)
        
        # Store current fire data
        current_fire_data = {
            'hotspots': hotspots,
            'state': state,
            'district': district,
            'date_range': f"{start_date} to {end_date}",
            'fusion_info': fusion_info
        }
        
        # Create GeoJSON for hotspots
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
                    'persistence_score': hotspot.get('persistence_score', 1),
                    'confidence_factors': hotspot.get('confidence_factors', []),
                    'latitude': hotspot['latitude'],
                    'longitude': hotspot['longitude'],
                    'source': hotspot['source'],
                    'detection_method': hotspot['detection_method']
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
            'lance_images': lance_count,
            'vnp14a1_images': vnp14a1_count,
            'total_images': total_images,
            'fusion_info': fusion_info,
            'detection_method': 'VIIRS Data Fusion (LANCE C2 + VNP14A1)',
            'dataset': 'Fused NASA VIIRS Datasets',
            'resolution': '375m (fusion composite)',
            'sensitivity': 'Multi-Source Data Fusion Enhanced',
            'status': 'Success'
        }
        
    except Exception as e:
        print(f"❌ VIIRS fusion fire detection error: {e}")
        raise HTTPException(status_code=500, detail=f"VIIRS fusion fire detection failed: {str(e)}")

# API Endpoints
@app.get("/")
async def dashboard():
    """Enhanced dashboard for VIIRS data fusion fire detection"""
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>VIIRS Data Fusion Fire Detection System</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: Arial, sans-serif; background: #f5f5f5; height: 100vh; display: flex; }
        .sidebar { width: 380px; background: #2c3e50; color: white; padding: 20px; overflow-y: auto; }
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
        .fusion-info { background: rgba(155, 89, 182, 0.1); padding: 8px; border-radius: 4px; margin-top: 10px; font-size: 11px; }
        .fusion-badge { background: linear-gradient(45deg, #9b59b6, #3498db); color: white; padding: 4px 8px; border-radius: 12px; font-size: 10px; font-weight: bold; margin: 2px 0; }
        .dataset-info { background: rgba(52, 152, 219, 0.1); padding: 8px; border-radius: 4px; margin-top: 10px; font-size: 11px; }
    </style>
</head>
<body>
    <div class="sidebar">
        <h2>🔗 VIIRS Data Fusion</h2>
        <div class="fusion-badge">LANCE C2 + VNP14A1 Fusion</div>
        <p style="font-size: 12px; color: #bdc3c7; margin-bottom: 20px;">Multi-Source Data Fusion | Enhanced Precision</p>
        
        <div class="section">
            <h3>🔗 Data Fusion Configuration</h3>
            <div class="fusion-info">
                <strong>📊 Fusion Components:</strong><br>
                • <strong>LANCE C2:</strong> Real-time fire data<br>
                • <strong>VNP14A1:</strong> Daily fire product<br>
                • <strong>Enhanced FRP:</strong> Combined power estimation<br>
                • <strong>FRP Consensus:</strong> Multi-source agreement<br>
                • <strong>Temp-Fire Fusion:</strong> Temperature validation<br>
                • <strong>Fire Persistence:</strong> Temporal consistency<br>
                • <strong>Quality Weighting:</strong> Confidence scoring<br>
                • <strong>Agricultural Masking:</strong> Focused detection<br>
                • <strong>Spatial Resolution:</strong> 375m fusion composite
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
                🕒 Set Current Week (Fusion Ready)
            </button>
        </div>

        <div class="section">
            <h3>🔍 Data Fusion Detection</h3>
            <button onclick="testFusionAccess()" class="btn btn-primary" style="width:100%;">
                Test Fusion Data Access
            </button>
            <button id="analyzeButton" onclick="analyzeFusionFireDetection()" class="btn btn-success" style="width:100%;" disabled>
                🔗 Fusion Fire Detection
            </button>
            <div id="loadingIndicator" class="hidden" style="text-align:center; margin-top:10px; color:#f39c12;">
                🔄 Performing data fusion and fire detection...
            </div>
        </div>

        <div class="section">
            <h3>🎯 Fusion Results</h3>
            <div id="resultsContainer" class="hidden">
                <div class="result-item"><span>Fire Pixels:</span> <span id="firePixels">-</span></div>
                <div class="result-item"><span>Fire Area:</span> <span id="fireArea">-</span></div>
                <div class="result-item"><span>Fire Spots:</span> <span id="fireHotspots">-</span></div>
                <div class="result-item"><span>Max FRP:</span> <span id="maxFRP">-</span></div>
                <div class="result-item"><span>LANCE Images:</span> <span id="lanceImages">-</span></div>
                <div class="result-item"><span>VNP14A1 Images:</span> <span id="vnpImages">-</span></div>
                <div class="result-item"><span>Total Images:</span> <span id="totalImages">-</span></div>
            </div>
            <div id="fusionInfo" class="dataset-info hidden">
                <strong>🔗 Fusion Status:</strong><br>
                <span id="fusionStatus">No fusion performed</span>
            </div>
            <div id="hotspotInfo" class="hotspot-info hidden">
                <strong>🔥 Fusion Fire Spots:</strong><br>
                <span id="hotspotStatus">No spots detected</span>
            </div>
            <div id="coordinateDisplay" class="coordinate-display hidden">
                <strong>📍 Spot Details:</strong><br>
                <span id="coordinateText">Hover over spots to see fusion details</span>
            </div>
        </div>

        <div class="section">
            <h3>📁 Export Fusion Data</h3>
            <div class="export-section">
                <p style="font-size: 12px; margin-bottom: 10px;">Export fusion fire coordinates</p>
                <button id="exportButton" onclick="exportCSV()" class="btn btn-warning" style="width:100%;" disabled>
                    📊 Export Fusion CSV
                </button>
                <div id="exportInfo" style="font-size: 11px; margin-top: 5px; color: #bdc3c7;">
                    No fusion data to export
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
                    <div class="legend-color" style="background: #8B0000; border: 3px solid gold;"></div>
                    <span>Very High (≥15 MW)</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #FF4500; border: 2px solid gold;"></div>
                    <span>High (≥5 MW)</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #FF8C00; border: 2px solid silver;"></div>
                    <span>Medium (≥1 MW)</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #FFD700; border: 1px solid gray;"></div>
                    <span>Low (≥0.1 MW)</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #FFFFE0; border: 1px solid lightgray;"></div>
                    <span>Very Low (<0.1 MW)</span>
                </div>
                <div style="border-top: 1px solid rgba(255,255,255,0.2); margin-top: 8px; padding-top: 8px;">
                    <div class="legend-item">
                        <div style="width: 20px; height: 15px; margin-right: 8px; background: linear-gradient(45deg, #9b59b6, #3498db); border-radius: 50%;"></div>
                        <span>Data Fusion Enhanced</span>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div class="main-content">
        <div id="map"></div>
        <div class="status-bar">
            <span id="mapCoordinates">Lat: -, Lon: -</span>
            <span>VIIRS Data Fusion | LANCE C2 + VNP14A1 | Enhanced Detection</span>
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
            attribution: '© OpenStreetMap | VIIRS: NASA LANCE C2 + VNP14A1 | Data Fusion Fire Detection'
        }).addTo(map);

        // Load states on page load
        loadStates();

        function setCurrentWeek() {
            const today = new Date();
            const lastWeek = new Date(today.getTime() - 7 * 24 * 60 * 60 * 1000);
            
            document.getElementById('startDate').value = lastWeek.toISOString().split('T')[0];
            document.getElementById('endDate').value = today.toISOString().split('T')[0];
            alert('📅 Set to current week for data fusion\\n\\n🔗 LANCE C2 + VNP14A1 fusion ready\\n📊 Enhanced multi-source detection');
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

        async function testFusionAccess() {
            try {
                const response = await fetch('/api/fusion/test');
                const result = await response.json();
                
                if (result.status === 'success') {
                    alert(`✅ Data Fusion System Ready!\\n\\nLANCE C2 Images: ${result.lance_images}\\nVNP14A1 Images: ${result.vnp14a1_images}\\nTotal for Fusion: ${result.total_images}\\nLANCE Bands: ${result.lance_bands.join(', ')}\\nVNP14A1 Bands: ${result.vnp14a1_bands.join(', ')}\\nFusion Capabilities: ${result.fusion_capabilities.join(', ')}\\n\\n🔗 Ready for multi-source data fusion!`);
                } else {
                    alert('⚠️ Data fusion access issue: ' + result.message);
                }
            } catch (error) {
                alert('❌ Fusion test failed: ' + error.message);
            }
        }

        async function analyzeFusionFireDetection() {
            const state = document.getElementById('stateSelect').value;
            const district = document.getElementById('districtSelect').value;
            const startDate = document.getElementById('startDate').value;
            const endDate = document.getElementById('endDate').value;

            document.getElementById('loadingIndicator').classList.remove('hidden');
            document.getElementById('analyzeButton').disabled = true;

            try {
                const response = await fetch('/api/detect-fusion-fires', {
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

                console.log('🔗 VIIRS Data Fusion Results:', data);
                
                if (data.status && data.status.includes('Error')) {
                    throw new Error(`Detection failed: ${data.status}`);
                }
                
                fireData = data;
                
                // Update results
                document.getElementById('firePixels').textContent = data.fire_pixels.toLocaleString();
                document.getElementById('fireArea').textContent = data.fire_area_hectares + ' hectares';
                document.getElementById('fireHotspots').textContent = data.fire_hotspots.toLocaleString();
                document.getElementById('maxFRP').textContent = data.max_frp + ' MW';
                document.getElementById('lanceImages').textContent = data.lance_images;
                document.getElementById('vnpImages').textContent = data.vnp14a1_images;
                document.getElementById('totalImages').textContent = data.total_images;
                document.getElementById('resultsContainer').classList.remove('hidden');
                
                // Update fusion info
                const fusionInfo = data.fusion_info;
                let fusionStatusText = '';
                if (fusionInfo.fusion_success) {
                    fusionStatusText = `✅ Fusion successful\\n`;
                    fusionStatusText += `Enhanced indices: ${fusionInfo.enhanced_indices.join(', ')}\\n`;
                    fusionStatusText += `Fusion bands: ${fusionInfo.fusion_bands.length} total`;
                } else {
                    fusionStatusText = `❌ Fusion failed: ${fusionInfo.error}`;
                }
                document.getElementById('fusionStatus').textContent = fusionStatusText;
                document.getElementById('fusionInfo').classList.remove('hidden');
                
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
                
                // Add fire hotspots with fusion styling
                if (hotspotsLayer) map.removeLayer(hotspotsLayer);
                if (data.hotspots_geojson && data.hotspots_geojson.features && data.hotspots_geojson.features.length > 0) {
                    hotspotsLayer = L.geoJSON(data.hotspots_geojson, {
                        pointToLayer: function(feature, latlng) {
                            const frp = feature.properties.frp;
                            const confidence = feature.properties.confidence;
                            const persistenceScore = feature.properties.persistence_score || 1;
                            
                            // Enhanced color scheme for fusion data
                            let color;
                            let borderColor = '#9b59b6'; // Fusion indicator
                            let borderWidth = 2;
                            
                            if (frp >= 15.0) {
                                color = '#8B0000'; // Dark red
                                borderColor = '#FFD700'; // Gold border for very high
                                borderWidth = 3;
                            } else if (frp >= 5.0) {
                                color = '#FF4500'; // Orange red
                                borderColor = '#FFD700'; // Gold border for high
                                borderWidth = 2;
                            } else if (frp >= 1.0) {
                                color = '#FF8C00'; // Dark orange
                                borderColor = '#C0C0C0'; // Silver border for medium
                            } else if (frp >= 0.1) {
                                color = '#FFD700'; // Gold
                                borderColor = '#808080'; // Gray border for low
                            } else {
                                color = '#FFFFE0'; // Light yellow
                                borderColor = '#D3D3D3'; // Light gray border
                            }
                            
                            // Adjust size based on persistence and confidence
                            let radius = 6;
                            if (persistenceScore >= 3) radius += 3;
                            else if (persistenceScore >= 2) radius += 2;
                            
                            if (confidence === 'Very High') radius += 2;
                            else if (confidence === 'High') radius += 1;
                            
                            return L.circleMarker(latlng, {
                                radius: Math.max(4, Math.min(radius, 16)),
                                fillColor: color,
                                color: color,
                                weight: 0,
                                opacity: 0,
                                fillOpacity: 0.7
                            });
                        },
                        onEachFeature: function(feature, layer) {
                            const props = feature.properties;
                            
                            layer.bindPopup(`
                                <strong>🔗 Fusion Fire Spot #${props.id}</strong><br>
                                <strong>📍 Location:</strong><br>
                                Lat: ${props.latitude.toFixed(6)}<br>
                                Lng: ${props.longitude.toFixed(6)}<br>
                                <strong>🔥 Fire Radiative Power:</strong> ${props.frp} MW<br>
                                <strong>🌡️ Brightness Temp:</strong> ${props.brightness_temp}K<br>
                                <strong>📊 Confidence:</strong> ${props.confidence}<br>
                                <strong>⏱️ Persistence Score:</strong> ${props.persistence_score}<br>
                                <strong>✅ Confidence Factors:</strong><br>
                                ${props.confidence_factors.join('<br>')}<br>
                                <strong>🛰️ Source:</strong> ${props.source}<br>
                                <strong>🔗 Method:</strong> ${props.detection_method}
                            `);
                            
                            layer.on('mouseover', function() {
                                document.getElementById('coordinateText').innerHTML = 
                                    `<strong>Fusion Spot #${props.id}</strong><br>` +
                                    `Lat: ${props.latitude.toFixed(6)}<br>` +
                                    `Lng: ${props.longitude.toFixed(6)}<br>` +
                                    `FRP: ${props.frp} MW<br>` +
                                    `Confidence: ${props.confidence}<br>` +
                                    `Persistence: ${props.persistence_score}<br>` +
                                    `Factors: ${props.confidence_factors.length}`;
                                document.getElementById('coordinateDisplay').classList.add('active');
                                document.getElementById('coordinateDisplay').classList.remove('hidden');
                            });
                            
                            layer.on('mouseout', function() {
                                document.getElementById('coordinateDisplay').classList.remove('active');
                            });
                        }
                    }).addTo(map);
                    
                    document.getElementById('hotspotStatus').textContent = 
                        `${data.fire_hotspots} fusion-enhanced fire spots detected`;
                    document.getElementById('hotspotInfo').classList.remove('hidden');
                    document.getElementById('coordinateDisplay').classList.remove('hidden');
                    
                    document.getElementById('exportButton').disabled = false;
                    document.getElementById('exportInfo').textContent = 
                        `${data.fire_hotspots} fusion spots ready for export`;
                    
                } else {
                    document.getElementById('hotspotStatus').textContent = 'No fire spots detected with fusion parameters';
                    document.getElementById('hotspotInfo').classList.remove('hidden');
                    document.getElementById('exportButton').disabled = true;
                    document.getElementById('exportInfo').textContent = 'No fusion data to export';
                }
                
                if (data.map_bounds) {
                    const bounds = [[data.map_bounds[1], data.map_bounds[0]], [data.map_bounds[3], data.map_bounds[2]]];
                    map.fitBounds(bounds, { padding: [20, 20] });
                }
                
                document.getElementById('lastUpdate').textContent = 'Updated: ' + new Date().toLocaleTimeString();
                
                alert(`🔗 VIIRS Data Fusion Complete!\\n\\nFusion Fire Spots: ${data.fire_hotspots}\\nTotal Area: ${data.fire_area_hectares} hectares\\nMax FRP: ${data.max_frp} MW\\nMethod: ${data.detection_method}\\nLANCE Images: ${data.lance_images}\\nVNP14A1 Images: ${data.vnp14a1_images}\\nTotal Images: ${data.total_images}\\n\\n📊 Enhanced detection through data fusion!`);

            } catch (error) {
                console.error('❌ Fusion fire detection error:', error);
                alert('❌ Fusion detection failed: ' + error.message);
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
                a.download = `VIIRS_Data_Fusion_Fire_Detection_${currentState}_${currentDistrict}_${new Date().toISOString().split('T')[0]}.csv`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                window.URL.revokeObjectURL(url);
                
                alert(`📊 Fusion CSV exported successfully!\\n\\nFile: VIIRS_Data_Fusion_Fire_Detection_${currentState}_${currentDistrict}.csv\\nContains: ${fireData.fire_hotspots} spots with fusion metadata`);
                
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
                document.getElementById('fusionInfo').classList.add('hidden');
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

@app.get("/api/fusion/test")
async def test_fusion_access():
    """Test both VIIRS datasets for fusion capability"""
    try:
        # Test LANCE C2
        lance_viirs = ee.ImageCollection('NASA/LANCE/SNPP_VIIRS/C2')
        lance_recent = lance_viirs.filterDate('2025-07-01', '2025-07-13')
        lance_count = lance_recent.size().getInfo()
        
        # Test VNP14A1
        vnp14a1 = ee.ImageCollection('NASA/VIIRS/002/VNP14A1')
        vnp14a1_recent = vnp14a1.filterDate('2025-07-01', '2025-07-13')
        vnp14a1_count = vnp14a1_recent.size().getInfo()
        
        total_count = lance_count + vnp14a1_count
        
        if total_count > 0:
            # Get bands for fusion compatibility
            lance_bands = lance_recent.first().bandNames().getInfo() if lance_count > 0 else []
            vnp14a1_bands = vnp14a1_recent.first().bandNames().getInfo() if vnp14a1_count > 0 else []
            
            # Determine fusion capabilities
            fusion_capabilities = []
            if lance_bands and any('FRP' in band for band in lance_bands):
                fusion_capabilities.append('FRP Fusion')
            if lance_bands and any('Bright_ti' in band for band in lance_bands):
                fusion_capabilities.append('Temperature Fusion')
            if vnp14a1_bands and any('Fire' in band for band in vnp14a1_bands):
                fusion_capabilities.append('Fire Mask Fusion')
            if lance_bands and vnp14a1_bands:
                fusion_capabilities.append('Quality Cross-Validation')
                fusion_capabilities.append('Temporal Persistence')
            
            return {
                'status': 'success',
                'datasets': ['NASA/LANCE/SNPP_VIIRS/C2', 'NASA/VIIRS/002/VNP14A1'],
                'lance_images': lance_count,
                'vnp14a1_images': vnp14a1_count,
                'total_images': total_count,
                'lance_bands': lance_bands,
                'vnp14a1_bands': vnp14a1_bands,
                'fusion_capabilities': fusion_capabilities,
                'resolution': '375m (fusion composite)',
                'optimization': 'Multi-source data fusion for enhanced fire detection'
            }
        else:
            return {
                'status': 'warning',
                'message': 'VIIRS datasets accessible but no data for current period'
            }
    except Exception as e:
        return {
            'status': 'error',
            'error': str(e)
        }

@app.post("/api/detect-fusion-fires")
async def detect_fusion_fires(request: FireDetectionRequest):
    """Enhanced fire detection using VIIRS data fusion"""
    
    if request.state not in STATES_DISTRICTS:
        raise HTTPException(status_code=400, detail="Invalid state")
    
    if request.district not in STATES_DISTRICTS[request.state]:
        raise HTTPException(status_code=400, detail="Invalid district")
    
    return detect_fusion_viirs_fires(request.state, request.district, request.start_date, request.end_date)

@app.get("/api/export-csv")
async def export_csv():
    """Export detected fire spots as CSV with fusion information"""
    global current_fire_data
    
    if not current_fire_data['hotspots']:
        raise HTTPException(status_code=404, detail="No fire detection data available for export")
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write header with fusion information
    writer.writerow([
        'Spot_ID',
        'Latitude',
        'Longitude', 
        'Fire_Radiative_Power_MW',
        'Brightness_Temperature_K',
        'Confidence_Level',
        'Intensity_Level',
        'Persistence_Score',
        'Confidence_Factors',
        'State',
        'District',
        'Date_Range',
        'Detection_Source',
        'Detection_Method',
        'Resolution',
        'System_Type',
        'Fusion_Enhanced'
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
            hotspot.get('persistence_score', 1),
            '; '.join(hotspot.get('confidence_factors', [])),
            current_fire_data['state'],
            current_fire_data['district'],
            current_fire_data['date_range'],
            hotspot.get('source', 'Data Fusion Detection'),
            hotspot.get('detection_method', 'VIIRS Data Fusion'),
            '375m (fusion composite)',
            'VIIRS Data Fusion System',
            'Yes'
        ])
    
    csv_content = output.getvalue()
    output.close()
    
    filename = f"VIIRS_Data_Fusion_Fire_Detection_{current_fire_data['state']}_{current_fire_data['district']}_{datetime.now().strftime('%Y%m%d')}.csv"
    
    return StreamingResponse(
        io.BytesIO(csv_content.encode('utf-8')),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

if __name__ == "__main__":
    print("=" * 80)
    print("VIIRS DATA FUSION FIRE DETECTION SYSTEM")
    print("LANCE C2 + VNP14A1 Multi-Source Data Fusion for Enhanced Precision")
    print("Features: Real-time + Processed Data Fusion | Enhanced Indices | Quality Weighting")
    print("=" * 80)
    
    # Test both VIIRS datasets for fusion
    try:
        # Test LANCE C2
        lance_dataset = ee.ImageCollection('NASA/LANCE/SNPP_VIIRS/C2')
        lance_count = lance_dataset.limit(1).size().getInfo()
        print("✅ LANCE C2 dataset accessible")
        
        # Test VNP14A1
        vnp14a1_dataset = ee.ImageCollection('NASA/VIIRS/002/VNP14A1')
        vnp14a1_count = vnp14a1_dataset.limit(1).size().getInfo()
        print("✅ VNP14A1 dataset accessible")
        
        # Check current data for fusion
        current_week_lance = lance_dataset.filterDate('2025-07-01', '2025-07-13')
        current_week_vnp = vnp14a1_dataset.filterDate('2025-07-01', '2025-07-13')
        
        lance_week_count = current_week_lance.size().getInfo()
        vnp_week_count = current_week_vnp.size().getInfo()
        total_fusion_images = lance_week_count + vnp_week_count
        
        print(f"✅ Current week LANCE C2 images: {lance_week_count}")
        print(f"✅ Current week VNP14A1 images: {vnp_week_count}")
        print(f"✅ Total images for fusion: {total_fusion_images}")
        
        # Check fusion compatibility
        if lance_week_count > 0 and vnp_week_count > 0:
            lance_bands = current_week_lance.first().bandNames().getInfo()
            vnp_bands = current_week_vnp.first().bandNames().getInfo()
            print(f"✅ LANCE C2 bands: {lance_bands}")
            print(f"✅ VNP14A1 bands: {vnp_bands}")
            print("✅ Datasets ready for fusion")
        elif lance_week_count > 0:
            print("⚠️ Only LANCE C2 data available (partial fusion possible)")
        elif vnp_week_count > 0:
            print("⚠️ Only VNP14A1 data available (partial fusion possible)")
        else:
            print("⚠️ Limited data for current week fusion")
        
        # Test boundaries
        gaul = ee.FeatureCollection("FAO/GAUL/2015/level2")
        boundary_count = gaul.limit(1).size().getInfo()
        print(f"✅ FAO/GAUL boundaries accessible")
        
        print("\n🚀 Starting VIIRS Data Fusion fire detection server")
        print("🔗 Data Fusion: LANCE C2 (real-time) + VNP14A1 (processed)")
        print("📊 Enhanced Indices: FRP fusion, temperature validation, persistence scoring")
        print("⚙️ Multi-threshold detection with quality weighting")
        print("🎨 Fusion-enhanced markers with confidence indicators")
        print("📈 Comprehensive fire detection through data integration")
        uvicorn.run(app, host="localhost", port=7000)
        
    except Exception as e:
        print(f"❌ Initialization failed: {e}")
        print("Check your Google Earth Engine permissions and dataset access")
