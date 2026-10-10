"""Fusion VIIRS product processing."""

import ee
from fastapi import HTTPException

from app.services.gee_analysis import get_agricultural_mask, get_district_boundary, nominal_pixel_footprint_hectares

current_fire_data = {'hotspots': [], 'state': '', 'district': '', 'date_range': '', 'fusion_info': {}}


def fusion_confidence(frp, persistence, has_consensus, temperature_validated):
    """Classify a hotspot using its FRP, repeated detections, and source agreement."""
    factors = []
    if frp >= 15:
        factors.append('Very High FRP')
    elif frp >= 5:
        factors.append('High FRP')
    elif frp >= 1:
        factors.append('Medium FRP')
    else:
        factors.append('Low FRP')
    if persistence >= 3:
        factors.append('High Persistence')
    elif persistence >= 2:
        factors.append('Medium Persistence')
    if has_consensus:
        factors.append('Multi-Source Consensus')
    if temperature_validated:
        factors.append('Temperature Validated')
    if len(factors) >= 3:
        level = 'Very High'
    elif len(factors) >= 2:
        level = 'High'
    elif any('High' in factor for factor in factors):
        level = 'Medium'
    else:
        level = 'Low'
    return factors, level

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
                        
                        has_consensus = fusion_props.get('FRP_CONSENSUS', 0) > 0
                        temperature_validated = fusion_props.get('TEMP_FIRE_FUSION', 0) > 0
                        confidence_factors, overall_confidence = fusion_confidence(
                            frp_value, persistence_score, has_consensus, temperature_validated
                        )
                        overall_intensity = overall_confidence
                        
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
                'nominal_footprint_hectares': 0.0,
                'fire_area_hectares': 0.0,
                'area_disclaimer': 'Nominal hotspot pixel footprint only, not verified burned area. Hotspots may be sub-pixel; burn scars require separate analysis.',
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
        nominal_footprint_hectares = nominal_pixel_footprint_hectares(fire_pixels, 375)
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
            'nominal_footprint_hectares': round(nominal_footprint_hectares, 2),
            'fire_area_hectares': round(nominal_footprint_hectares, 2),
            'legacy_fire_area_field_note': 'Deprecated compatibility alias for nominal_footprint_hectares; this is not verified burned area.',
            'area_disclaimer': 'Nominal footprint of 375m pixels containing active thermal detections, not verified burned area. Hotspots may be sub-pixel; burn scars require separate analysis.',
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
