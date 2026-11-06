import pandas as pd
import geopandas as gpd
from sqlalchemy import create_engine
import gc  # Garbage Collector pour vider la RAM
from sqlalchemy.engine import make_url

# 1. Configuration de la base de données PostGIS
db_user = "postgres"
db_password = "maranatha"
db_host = "172.21.240.1"
db_port = "5432"
db_name = "anser_route"
#db_url = make_url("postgresql://postgres:password@172.21.240.1:5432/anser_route")
db_url = "postgresql+psycopg://postgres:password@172.21.240.1:5432/anser_route"
engine = create_engine(db_url)

#engine = create_engine(db_url)

print("# Db connect ")
print(db_url)
# 2. Liste de tous tes fichiers .csv.gz
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

# 3. Traitement bloc par bloc
first_file = True

for fichier in fichiers_csv_gz:
    print(f"--- Traitement de {fichier} ---")
    print("# Beg file " + fichier)
    
    # Lecture d'un seul fichier à la fois
    df = pd.read_csv(fichier, compression='gzip')
    print(f"Bâtiments lus : {len(df)}")
    
    # Conversion en GeoDataFrame (WGS84 EPSG:4326)
    if 'latitude' in df.columns and 'longitude' in df.columns:
        gdf = gpd.GeoDataFrame(
            df,
            geometry=gpd.points_from_xy(df.longitude, df.latitude),
            crs="EPSG:4326"
        )
    elif 'geometry' in df.columns:
        from shapely import wkt
        df['geometry'] = df['geometry'].apply(wkt.loads)
        gdf = gpd.GeoDataFrame(df, geometry='geometry', crs="EPSG:4326")

    # Reprojection immédiate en MÈTRES (UTM 33S pour RDC) & calcul du centroïde
    gdf = gdf.to_crs(epsg=4326)
    gdf['geometry'] = gdf.geometry.centroid  # Remplace le polygone par son centroïde

    # Mode d'insertion : 'replace' pour le 1er fichier, 'append' pour les suivants
    if_exists_mode = 'replace' if first_file else 'append'
    
    print(f"Insertion dans PostGIS ({if_exists_mode})...")

    #gdf.head(100).to_csv("echantilloh.csv")
    #exit()
    gdf.to_postgis(
        name='batiments_rdc', 
        con=engine, 
        if_exists=if_exists_mode, 
        index=False,
        chunksize=20000  # Insertion par paquets légers
    )
    
    first_file = False
    
    # NETTOYAGE MÉMOIRE RAM STRICT
    del df
    del gdf
    gc.collect() # Force la libération de la RAM
    print(f"Fichier {fichier} inséré avec succès !\n")
    #exit()


  
print("RÉUSSITE : Tous les blocs ont été combinés et insérés dans la table PostGIS 'batiments_rdc' !")