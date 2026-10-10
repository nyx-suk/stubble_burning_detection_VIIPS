"""Daily VIIRS product processing."""

import ee
from fastapi import HTTPException

from app.services.gee_analysis import get_agricultural_mask, get_district_boundary, nominal_pixel_footprint_hectares

current_fire_data = {'hotspots': [], 'state': '', 'district': '', 'date_range': '', 'fusion_info': {}}


def is_viirs_fire_mask(value) -> bool:
    """True for documented VNP14A1 active-fire classes (7, 8, or 9)."""
    try:
        return int(round(float(value))) in (7, 8, 9)
    except (TypeError, ValueError):
        return False

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
                        if mask_val is not None and not is_viirs_fire_mask(mask_val):
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
                'nominal_footprint_hectares': 0.0,
                'fire_area_hectares': 0.0,
                'area_disclaimer': 'Nominal footprint represents aggregate spatial extent of 1km pixels containing active thermal detections, NOT verified burned area. Agricultural fires are typically sub-pixel. Verified burned area requires burn-scar analysis (e.g. Sentinel-2 dNBR).',
                'legacy_fire_area_field_note': 'Deprecated compatibility alias for nominal_footprint_hectares; this is not verified burned area.',
                'pixel_resolution_meters': 1000,
                'fire_hotspots': 0,
                'max_frp': 0.0,
                'hotspots_coordinates': [],
                'hotspots_geojson': {'type': 'FeatureCollection', 'features': []},
                'district_boundary': None,
                'map_bounds': None,
                'viirs_images_used': 0,
                'detection_method': 'VNP14A1 FireMask Detection',
                'dataset': 'NASA/VIIRS/002/VNP14A1',
                'resolution': '1km',
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
                'nominal_footprint_hectares': 0.0,
                'fire_area_hectares': 0.0,
                'area_disclaimer': 'Nominal footprint represents aggregate spatial extent of 1km pixels containing active thermal detections, NOT verified burned area. Agricultural fires are typically sub-pixel. Verified burned area requires burn-scar analysis (e.g. Sentinel-2 dNBR).',
                'legacy_fire_area_field_note': 'Deprecated compatibility alias for nominal_footprint_hectares; this is not verified burned area.',
                'pixel_resolution_meters': 1000,
                'fire_hotspots': 0,
                'max_frp': 0.0,
                'hotspots_coordinates': [],
                'hotspots_geojson': {'type': 'FeatureCollection', 'features': []},
                'district_boundary': boundary_info['boundary_geojson'],
                'map_bounds': boundary_info['bounds'],
                'viirs_images_used': 0,
                'detection_method': 'VNP14A1 FireMask Detection',
                'dataset': 'NASA/VIIRS/002/VNP14A1',
                'resolution': '1km',
                'sensitivity': 'No VIIRS data available',
                'status': 'No VIIRS data found for this period'
            }
        
        # Extract fire spots using VNP14A1 detection
        hotspots = extract_stubble_burning_spots(viirs, district_geom)
        
        # Calculate statistics
        fire_pixels = len(hotspots)
        # 1km x 1km = 1,000,000 m² = 100.0 hectares nominal footprint per pixel
        nominal_footprint_hectares = nominal_pixel_footprint_hectares(fire_pixels, 1000)
        max_frp = max([h['frp'] for h in hotspots], default=0.0)
        area_disclaimer = (
            "Nominal hotspot footprint represents aggregate spatial extent of 1km pixels containing active "
            "thermal detections, NOT verified burned area. Agricultural fires are typically sub-pixel in extent. "
            "Verified burned area requires high-resolution burn scar analysis (e.g. Sentinel-2 dNBR)."
        )
        
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
            'nominal_footprint_hectares': round(nominal_footprint_hectares, 2),
            'fire_area_hectares': round(nominal_footprint_hectares, 2),  # Deprecated compatibility alias
            'legacy_fire_area_field_note': 'Deprecated compatibility alias for nominal_footprint_hectares; this is not verified burned area.',
            'area_disclaimer': area_disclaimer,
            'pixel_resolution_meters': 1000,
            'fire_hotspots': len(hotspots),
            'max_frp': round(max_frp, 3),
            'hotspots_coordinates': hotspots,
            'hotspots_geojson': hotspots_geojson,
            'district_boundary': boundary_info['boundary_geojson'],
            'map_bounds': boundary_info['bounds'],
            'viirs_images_used': image_count,
            'detection_method': 'VNP14A1 FireMask Detection',
            'dataset': 'NASA/VIIRS/002/VNP14A1',
            'resolution': '1km',
            'sensitivity': 'FireMask classes 7-9 (Low, Nominal, High confidence)',
            'status': 'Success'
        }
        
    except Exception as e:
        print(f"❌ VIIRS fire detection error: {e}")
        # Return a proper error response instead of raising exception
        return {
            'fire_pixels': 0,
            'nominal_footprint_hectares': 0.0,
            'fire_area_hectares': 0.0,
            'area_disclaimer': 'Nominal footprint represents aggregate spatial extent of 1km pixels containing active thermal detections, NOT verified burned area. Agricultural fires are typically sub-pixel. Verified burned area requires burn-scar analysis (e.g. Sentinel-2 dNBR).',
            'legacy_fire_area_field_note': 'Deprecated compatibility alias for nominal_footprint_hectares; this is not verified burned area.',
            'pixel_resolution_meters': 1000,
            'fire_hotspots': 0,
            'max_frp': 0.0,
            'hotspots_coordinates': [],
            'hotspots_geojson': {'type': 'FeatureCollection', 'features': []},
            'district_boundary': None,
            'map_bounds': None,
            'viirs_images_used': 0,
            'detection_method': 'VNP14A1 FireMask Detection',
            'dataset': 'NASA/VIIRS/002/VNP14A1',
            'resolution': '1km',
            'sensitivity': 'Error occurred during detection',
            'status': f'Error: {str(e)}'
        }
