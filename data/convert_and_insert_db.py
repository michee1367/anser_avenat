import pandas as pd
import geopandas as gpd
from sqlalchemy import create_engine
from shapely import wkt

# 1. Liste de vos 4 fichiers .csv.gz téléchargés
fichiers_csv_gz = [
    'bloc1.csv.gz',
    'bloc2.csv.gz',
    'bloc3.csv.gz',
    'bloc4.csv.gz',
    'bloc5.csv.gz',
    'bloc6.csv.gz',
    'bloc7.csv.gz',
    'bloc8.csv.gz',
    'bloc9.csv.gz',
    'bloc10.csv.gz',
    'bloc11.csv.gz',
    'bloc12.csv.gz',
    'bloc13.csv.gz',
    'bloc14.csv.gz',
    'bloc15.csv.gz',
    'bloc16.csv.gz',
    'bloc17.csv.gz',
    'bloc18.csv.gz'
]

print("1. Lecture et réunion des 18 fichiers...")
liste_df = []
for fichier in fichiers_csv_gz:
    print("#################")
    print(fichier)
    df_temp = pd.read_csv(fichier, compression='gzip')
    liste_df.append(df_temp)
    print("#################")


# Concaténation des 4 DataFrames en un seul
df_combined = pd.concat(liste_df, ignore_index=True)
print(f"Total de bâtiments fusionnés : {len(df_combined)}")
# ['latitude', 'longitude', 'area_in_meters', 'confidence', 'geometry', 'full_plus_code']
# 2. Conversion en GeoDataFrame
# Selon la structure du CSV de Google Open Buildings :
if 'latitude' in df_combined.columns and 'longitude' in df_combined.columns:
    gdf = gpd.GeoDataFrame(
        df_combined,
        geometry=gpd.points_from_xy(df_combined.longitude, df_combined.latitude),
        crs="EPSG:4326"
    )
elif 'geometry' in df_combined.columns:
    # Si la colonne contient des polygones au format WKT
    df_combined['geometry'] = df_combined['geometry'].apply(wkt.loads)
    gdf = gpd.GeoDataFrame(df_combined, geometry='geometry', crs="EPSG:4326")

# 3. Reprojection en MÈTRES (EPSG:32733) & calcul des centroïdes pour le clustering
print("2. Reprojection en mètres (UTM 33S) et calcul des centroïdes...")
gdf = gdf.to_crs(epsg=32733)
gdf['geometry'] = gdf.geometry.centroid  # Remplace le polygone par son centroïde

exit()

# 4. Connexion et insertion dans PostgreSQL / PostGIS
# Modifiez les identifiants selon votre configuration PostgreSQL
db_user = "postgres"
db_password = "mon_mot_de_passe"
db_host = "localhost"
db_port = "5432"
db_name = "anser_ple_db"

engine = create_engine(f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}")

print("3. Insertion dans la base de données PostGIS...")
gdf.to_postgis(
    name='batiments_rdc', 
    con=engine, 
    if_exists='replace', # ou 'append' 
    index=False,
    chunksize=50000     # Insertion par paquets de 50 000 pour gérer la mémoire
)

print("Insertion terminée avec succès dans la table 'batiments_rdc' !")

