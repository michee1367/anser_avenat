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
# 2. Paramètres du fichier CSV compressé
csv_gz_path = "bloc_9.csv.gz"   # Votre fichier .csv.gz
table_dest = "batiments_rdc"
CHUNK_SIZE = 1000000             # 100 000 bâtiments par morceau (Ajustable)

for fichier in fichiers_csv_gz:
    print(f"--- Traitement de {fichier} ---")
    print("# Beg file " + fichier)
    
    # Lecture d'un seul fichier à la fois
    #df = pd.read_csv(fichier, compression='gzip')
    #print(f"Bâtiments lus : {len(df)}")

    csv_reader = pd.read_csv(
        fichier, 
        compression='gzip', 
        chunksize=CHUNK_SIZE,
        low_memory=False
    )

    first_batch = False
    total_insere = 0

    for i, df_chunk in enumerate(csv_reader):
        print(f"Traitement du bloc n°{i+1} ({len(df_chunk)} lignes)...")

        # --- ÉTAPE A : CONVERTIR EN GEOPANDAS ---
        # CAS A1 : Si votre CSV contient des colonnes de coordonnées (ex: 'longitude', 'latitude')
        gdf_chunk = gpd.GeoDataFrame(
            df_chunk, 
            geometry=gpd.points_from_xy(df_chunk['longitude'], df_chunk['latitude']),
            crs="EPSG:4326"
        )

        print(f"Bâtiments lus : {len(df_chunk)}")

        # Conversion en GeoDataFrame (WGS84 EPSG:4326)
        if 'latitude' in df_chunk.columns and 'longitude' in df_chunk.columns:
            gdf = gpd.GeoDataFrame(
                df_chunk,
                geometry=gpd.points_from_xy(df_chunk.longitude, df_chunk.latitude),
                crs="EPSG:4326"
            )
        elif 'geometry' in df_chunk.columns:
            from shapely import wkt
            df_chunk['geometry'] = df_chunk['geometry'].apply(wkt.loads)
            gdf = gpd.GeoDataFrame(df_chunk, geometry='geometry', crs="EPSG:4326")

        # Reprojection immédiate en MÈTRES (UTM 33S pour RDC) & calcul du centroïde
        gdf = gdf.to_crs(epsg=4326)
        gdf['geometry'] = gdf.geometry.centroid  # Remplace le polygone par son centroïde

        # Mode d'insertion : 'replace' pour le 1er fichier, 'append' pour les suivants
        if_exists_mode = 'replace' if first_file else 'append'

        # CAS A2 : Si la géométrie est au format texte WKT (ex: dans une colonne nommée 'geometry' ou 'wkt')
        # Décommentez les 2 lignes ci-dessous si vous êtes dans le CAS A2 :
        # df_chunk['geometry'] = df_chunk['geometry'].apply(loads)
        # gdf_chunk = gpd.GeoDataFrame(df_chunk, geometry='geometry', crs="EPSG:4326")

        # --- ÉTAPE B : INSERTION DANS POSTGIS ---
        #if_exists_mode = 'replace' if first_batch else 'append'
        
        gdf_chunk.to_postgis(
            name=table_dest,
            con=engine,
            if_exists=if_exists_mode,
            index=False,
            chunksize=20000
        )

        total_insere += len(gdf_chunk)
        print(f"✅ Bloc n°{i+1} inséré ! (Total cumulé : {total_insere}")
        
        first_batch = False
        first_file = False
        del df_chunk
        del gdf_chunk
        gc.collect() # Force la libération de la RAM
        
    

    #gdf.head(100).to_csv("echantilloh.csv")
    #exit()
    """gdf.to_postgis(
        name='batiments_rdc', 
        con=engine, 
        if_exists=if_exists_mode, 
        index=False,
        chunksize=20000  # Insertion par paquets légers
    )"""
    
    
    # NETTOYAGE MÉMOIRE RAM STRICT
    print(f"Fichier {fichier} inséré avec succès !\n")
    #exit()


    
print("RÉUSSITE : Tous les blocs ont été combinés et insérés dans la table PostGIS 'batiments_rdc' !")