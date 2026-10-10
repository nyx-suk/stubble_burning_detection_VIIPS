import ee
import pandas as pd
import numpy as np

# Initialize Earth Engine
ee.Initialize()

# Select the region - Cerro Gordo County, Iowa
counties = ee.FeatureCollection('TIGER/2018/Counties')
selected = counties.filter(ee.Filter.eq('GEOID', '19033'))
geometry = selected.geometry()

print("Region selected: Cerro Gordo County, Iowa")

# Prepare the Satellite Embedding dataset
embeddings = ee.ImageCollection('GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL')

year = 2022
start_date = ee.Date.fromYMD(year, 1, 1)
end_date = start_date.advance(1, 'year')

filtered_embeddings = embeddings.filter(ee.Filter.date(start_date, end_date)).filter(ee.Filter.bounds(geometry))
embeddings_image = filtered_embeddings.mosaic()

print(f"Satellite Embedding data loaded for year: {year}")

# Create a cropland mask using USDA NASS CDL
cdl = ee.ImageCollection('USDA/NASS/CDL').filter(ee.Filter.date(start_date, end_date)).first()
crop_landcover = cdl.select('cropland')
cropland_mask = cdl.select('cultivated').eq(2).rename('cropmask')

print("Cropland mask created using USDA NASS CDL")

# Mask all non-cropland pixels
cluster_image = embeddings_image.updateMask(cropland_mask)

# Extract training samples using stratified sampling
training = cluster_image.addBands(cropland_mask).stratifiedSample(
    numPoints=1000,
    classBand='cropmask',
    region=geometry,
    scale=10,
    tileScale=16,
    seed=100,
    dropNulls=True,
    geometries=True
)

print(f"Training samples extracted: {training.size().getInfo()} points")

# Optional: Export samples to asset (uncomment if needed for larger regions)
"""
export_folder = 'projects/your-project/assets/satellite_embedding/'
samples_export_fc = 'cluster_training_samples'
samples_export_fc_path = export_folder + samples_export_fc

export_task = ee.batch.Export.table.toAsset(
    collection=training,
    description='Cluster_Training_Samples',
    assetId=samples_export_fc_path
)
export_task.start()
print("Sample export task started. Wait for completion before proceeding.")

# Use exported asset instead
# training = ee.FeatureCollection(samples_export_fc_path)
"""

# Perform unsupervised clustering
min_clusters = 4
max_clusters = 5

clusterer = ee.Clusterer.wekaCascadeKMeans(
    minClusters=min_clusters,
    maxClusters=max_clusters
).train(
    features=training,
    inputProperties=cluster_image.bandNames()
)

clustered = cluster_image.cluster(clusterer)
print(f"Clustering completed with {min_clusters}-{max_clusters} clusters")

# Calculate cluster areas
area_image = ee.Image.pixelArea().divide(4046.86).addBands(clustered)  # Convert to acres

areas = area_image.reduceRegion(
    reducer=ee.Reducer.sum().group(
        groupField=1,
        groupName='cluster'
    ),
    geometry=geometry,
    scale=10,
    maxPixels=1e10
)

cluster_areas_list = ee.List(areas.get('groups'))

# Process results to extract areas
def process_cluster_area(item):
    area_dict = ee.Dictionary(item)
    cluster_number = area_dict.getNumber('cluster').format()
    area = area_dict.getNumber('sum')
    return ee.Feature(None, {'cluster': cluster_number, 'area': area})

cluster_areas = cluster_areas_list.map(process_cluster_area)
cluster_area_fc = ee.FeatureCollection(cluster_areas)

# Get cluster areas as Python dictionary
cluster_data = cluster_area_fc.getInfo()
print("Cluster Areas:")
for feature in cluster_data['features']:
    props = feature['properties']
    print(f"Cluster {props['cluster']}: {props['area']:.0f} acres")

# Select top 2 clusters by area
selected_fc = cluster_area_fc.sort('area', False).limit(2)
selected_data = selected_fc.getInfo()

# Assign labels based on area (largest = corn, second largest = soybean)
corn_cluster = selected_data['features'][0]['properties']['cluster']
soybean_cluster = selected_data['features'][1]['properties']['cluster']

corn_area = selected_data['features'][0]['properties']['area']
soybean_area = selected_data['features'][1]['properties']['area']

print(f"\nCrop Assignment:")
print(f"Corn Cluster: {corn_cluster}, Area: {corn_area:.0f} acres")
print(f"Soybean Cluster: {soybean_cluster}, Area: {soybean_area:.0f} acres")

# Reference statistics from NASS
print(f"\nReference Statistics (NASS 2022):")
print(f"Corn Area: 163,500 acres")
print(f"Soybean Area: 110,500 acres")

# Create final crop map
corn_pixels = clustered.eq(ee.Number.parse(corn_cluster))
soybean_pixels = clustered.eq(ee.Number.parse(soybean_cluster))

# Create final classification: 0=other, 1=corn, 2=soybean
crop_map = corn_pixels.add(soybean_pixels.multiply(2))

print("\nCrop map created successfully!")

# Validation against CDL
cdl_validation = ee.ImageCollection('USDA/NASS/CDL').filter(ee.Filter.date(start_date, end_date)).first()
crop_landcover_cdl = cdl_validation.select('cropland')
crop_map_cdl = crop_landcover_cdl.updateMask(cropland_mask).rename('crops')

# Remap CDL values: Corn=1, Soybean=5, Others=0
crop_classes = ee.List.sequence(0, 254)
target_classes = ee.List.repeat(0, 255).set(1, 1).set(5, 2)
crop_map_reclass = crop_map_cdl.remap(crop_classes, target_classes).rename('crops')

print("Validation data prepared from CDL")

# Define visualization parameters
crop_vis = {'min': 0, 'max': 2, 'palette': ['#bdbdbd', '#ffd400', '#267300']}

# Display results (for interactive environments like Colab with geemap)
try:
    import geemap
    
    # Create interactive map
    Map = geemap.Map(center=[43.3, -93.6], zoom=12)
    
    # Add layers
    Map.addLayer(geometry, {'color': 'red'}, 'Study Area', False)
    Map.addLayer(cropland_mask.clip(geometry), {'min': 0, 'max': 1, 'palette': ['white', 'green']}, 'Crop Mask', False)
    Map.addLayer(training, {'color': 'blue'}, 'Training Samples', False)
    Map.addLayer(clustered.randomVisualizer().clip(geometry), {}, 'Clusters', False)
    Map.addLayer(crop_map.clip(geometry), crop_vis, 'Detected Crop Map')
    Map.addLayer(crop_map_reclass.clip(geometry), crop_vis, 'CDL Reference Map', False)
    
    # Add legend
    legend_dict = {
        'Other Crops': '#bdbdbd',
        'Corn': '#ffd400',
        'Soybean': '#267300'
    }
    Map.add_legend(legend_dict=legend_dict, title='Crop Types')
    
    # Display map
    Map
    
except ImportError:
    print("Install geemap for interactive visualization: pip install geemap")
    print("Alternatively, use the Earth Engine Code Editor for visualization")

# Export results (optional)
def export_crop_map():
    """Export the final crop map"""
    export_task = ee.batch.Export.image.toDrive(
        image=crop_map.clip(geometry),
        description='crop_map_cerro_gordo_2022',
        folder='earth_engine_exports',
        scale=10,
        region=geometry,
        maxPixels=1e10
    )
    export_task.start()
    print("Export task started for crop map")

# Uncomment to export
# export_crop_map()

# Calculate accuracy metrics (simplified)
def calculate_basic_stats():
    """Calculate basic comparison statistics"""
    # This is a simplified comparison - in practice you'd want more sophisticated metrics
    difference = crop_map.subtract(crop_map_reclass).abs()
    
    stats = difference.reduceRegion(
        reducer=ee.Reducer.mean().combine(
            reducer2=ee.Reducer.stdDev(),
            sharedInputs=True
        ),
        geometry=geometry,
        scale=30,
        maxPixels=1e10
    )
    
    return stats.getInfo()

# Uncomment to calculate stats
stats = calculate_basic_stats()
print(f"Basic comparison stats: {stats}")

print("\nScript completed successfully!")
print("\nKey Results:")
print(f"- Successfully classified crops using unsupervised learning")
print(f"- Detected corn area: {corn_area:.0f} acres (Reference: 163,500 acres)")
print(f"- Detected soybean area: {soybean_area:.0f} acres (Reference: 110,500 acres)")
print(f"- Used Google Satellite Embedding dataset with 64-dimensional feature vectors")
print(f"- Leveraged local knowledge and aggregate crop statistics for label assignment")