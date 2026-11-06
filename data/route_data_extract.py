import os
import urllib.parse
import geopandas as gpd
import pyogrio
from sqlalchemy import create_engine
import zipfile

# ==========================================
# 1. CONFIGURATION DE LA BASE DE DONNÉES
# ==========================================
DB_USER = "postgres"
DB_PASS = "maranatha"
DB_HOST = "172.21.240.1"  # IP de votre serveur Windows/PostgreSQL
DB_PORT = "5432"
DB_NAME = "anser_route"

# Sécurisation du mot de passe pour l'URL de connexion
safe_password = urllib.parse.quote_plus(DB_PASS)
db_url = f"postgresql+psycopg://{DB_USER}:{safe_password}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Création de l'engine SQLAlchemy
engine = create_engine(db_url)

# ==========================================
# 2. PARAMÈTRES DU FICHIER ET DE L'IMPORT
# ==========================================
ZIP_FILE_PATH = "congo-democratic-republic-260801-free.gpkg.zip"  # Chemin vers votre fichier .gpkg.zip
CHUNK_SIZE = 50000                          # Nombre d'éléments traités par paquet (ajustable)

# ==========================================
# 3. EXTRACTION ET BOUCLE SUR LES COUCHES
# ==========================================
if not os.path.exists(ZIP_FILE_PATH):
    raise FileNotFoundError(f"Le fichier '{ZIP_FILE_PATH}' est introuvable.")

print(f"📦 Analyse du fichier : {ZIP_FILE_PATH}")


# Récupération de la liste des couches via pyogrio
layers_info = pyogrio.list_layers(ZIP_FILE_PATH)
print(f"🔍 {len(layers_info)} couche(s) trouvée(s) :")
for name, geom_type in layers_info:
    print(f"  - {name} ({geom_type})")

#exit()
print("\n" + "=" * 50)
print("🚀 Début de l'importation dans PostGIS")
print("=" * 50)

layer_name = "gis_osm_roads_free"
# Utilisation du préfixe zip:// pour GeoPandas
#layer_uri = f"zip://{ZIP_FILE_PATH}"
zip_path = f"{ZIP_FILE_PATH}"
extract_dir = "./data_gpkg"

# 1. Décompression du ZIP
with zipfile.ZipFile(zip_path, 'r') as zip_ref:
    zip_ref.extractall(extract_dir)

# 2. Lecture du fichier GPKG extrait
gpkg_path = f"{extract_dir}/congo-democratic-republic.gpkg"

offset = 0
first_batch = True
total_layer_rows = 0

while True:
    # Lecture par morceaux (chunks) pour éviter la saturation de la mémoire RAM
    gdf_chunk = gpd.read_file(
        gpkg_path,
        layer=layer_name,
        rows=slice(offset, offset + CHUNK_SIZE)
    )

    #print(gdf_chunk.columns.to_list())

    #exit()

    # Arrêt de la boucle si le morceau est vide
    if gdf_chunk.empty:
        break

    # Remplacement de la table pour le premier paquet, puis ajout pour les suivants
    if_exists_mode = 'replace' if first_batch else 'append'

    # Définition du nom de la table PostGIS (nom de la couche nettoyé)
    table_name = layer_name.lower().replace("-", "_")

    # Insertion dans PostGIS
    gdf_chunk.to_postgis(
        name=table_name,
        con=engine,
        if_exists=if_exists_mode,
        index=False,
        chunksize=10000
    )

    total_layer_rows += len(gdf_chunk)
    print(f"  └─ Paquet de {len(gdf_chunk):,} éléments inséré (Total cumulé : {total_layer_rows:,})")

    offset += CHUNK_SIZE
    first_batch = False

print(f"✅ Couche '{layer_name}' insérée avec succès dans la table '{table_name}' ({total_layer_rows:,} lignes).")
print("\n🎉 Toutes les couches ont été importées dans PostGIS avec succès !")