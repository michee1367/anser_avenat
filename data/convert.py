import gzip
import pandas as pd
import geopandas as gpd

# 1. Lecture du CSV.GZ
df = pd.read_csv('10b_buildings.csv.gz', compression='gzip')

# 2. Si le CSV contient latitude et longitude :
gdf = gpd.GeoDataFrame(
    df, 
    geometry=gpd.points_from_xy(df.longitude, df.latitude),
    crs="EPSG:4326"
)

# --- NOTE : Si ton CSV contenait plutôt des polygones WKT (ex: colonne 'geometry') :
# from shapely import wkt
# df['geometry'] = df['geometry'].apply(wkt.loads)
# gdf = gpd.GeoDataFrame(df, geometry='geometry', crs="EPSG:4326")

# 3. REPROJECTION D'ABORD EN MÈTRES (ex: EPSG:32733 - UTM Zone 33S pour RDC)
# Cela règle le problème d'avertissement sur le calcul de centroid
gdf_metric = gdf.to_crs(epsg=32733)

# 4. CALCUL DU CENTROÏDE ET REMPLACEMENT DE LA GÉOMÉTRIE UNIQUE
# En remplaçant directement .geometry, on évite d'avoir 2 colonnes géométriques
gdf_metric['geometry'] = gdf_metric.geometry.centroid

# 5. EXPORT SANS ERREUR VERS GEOPACKAGE
gdf_metric.to_file("batiments_rdc_metrique.gpkg", driver="GPKG")

print("Conversion et centroïdes calculés avec succès ! Prêt pour DBSCAN/K-Means.")