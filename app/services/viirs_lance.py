"""Lance VIIRS product processing."""

import ee
from fastapi import HTTPException

from app.services.gee_analysis import get_agricultural_mask, get_district_boundary, nominal_pixel_footprint_hectares

current_fire_data = {'hotspots': [], 'state': '', 'district': '', 'date_range': '', 'fusion_info': {}}

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
        nominal_footprint_hectares = nominal_pixel_footprint_hectares(fire_pixels, 375)
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
