#!/usr/bin/env python3
"""ETL Anser : bâtiments Google Open Buildings + routes OSM → PostGIS.

Sous-commandes :
  buildings  : 18 fichiers bloc*.csv.gz → centroïdes en EPSG:32733 → table batiments_rdc
  roads      : GPKG Geofabrik (couche gis_osm_roads_free) → table gis_osm_roads_free

La configuration de la base provient du fichier .env (voir .env.example).
"""

import argparse
import gc
import logging
import os
import re
import time
import urllib.parse
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import InterfaceError, OperationalError

BASE_DIR = Path(__file__).resolve().parent

# Bâtiments (Google Open Buildings, 18 blocs compressés)
BUILDINGS_GLOB = "bloc*.csv.gz"
BUILDINGS_TABLE = "batiments_rdc"
CHUNK_BUILDINGS = 500_000

# Routes OSM (Geofabrik, archive GPKG)
GPKG_ZIP = BASE_DIR / "congo-democratic-republic-260801-free.gpkg.zip"
GPKG_EXTRACT_DIR = BASE_DIR / "data_gpkg"
GPKG_PATH = GPKG_EXTRACT_DIR / "congo-democratic-republic.gpkg"
ROADS_LAYER = "gis_osm_roads_free"
ROADS_TABLE = "gis_osm_roads_free"
CHUNK_ROADS = 50_000

# SCR : WGS84 (source) puis UTM 33S / mètres (cible des centroïdes bâtiments)
CRS_WGS84 = "EPSG:4326"
CRS_METRIC = "EPSG:32733"

# Reprise après coupure de connexion (ex. reset TCP du NAT WSL2) :
# délais de reconnexion cumulés en secondes (3 essais + tentative initiale)
RETRY_DELAYS = (5, 15, 30)

log = logging.getLogger("etl")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


# --------------------------------------------------------------------------
# Configuration base de données
# --------------------------------------------------------------------------

def build_db_url() -> tuple[str, dict]:
    load_dotenv(BASE_DIR / ".env")
    cfg = {
        "user": os.getenv("DB_USER", "postgres"),
        "password": os.getenv("DB_PASSWORD", ""),
        "host": os.getenv("DB_HOST", "localhost"),
        "port": os.getenv("DB_PORT", "5432"),
        "name": os.getenv("DB_NAME", "anser_route"),
    }
    url = (
        f"postgresql+psycopg://{urllib.parse.quote_plus(cfg['user'])}"
        f":{urllib.parse.quote_plus(cfg['password'])}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['name']}"
    )
    return url, cfg


def get_engine():
    url, cfg = build_db_url()
    log.info(
        "Connexion PostgreSQL : %s@%s:%s/%s (mot de passe masqué)",
        cfg["user"], cfg["host"], cfg["port"], cfg["name"],
    )
    engine = create_engine(
        url,
        pool_pre_ping=True,
        # Keepalive TCP : détecte les sockets mortes du réseau WSL2 (reset 10054)
        connect_args={
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 6,
        },
    )
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    log.info("Connexion réussie.")
    return engine


def to_postgis_retry(gdf, table: str, if_exists: str, engine, chunksize: int):
    """Insère un chunk via to_postgis ; reconnexion + retry si la connexion tombe.

    Un chunk dont l'insertion échoue est annulé (rollback) côté serveur,
    donc le retry du même chunk ne produit pas de doublon.
    Retourne l'éventuellement nouvel engine (reconnecté).
    """
    essais = len(RETRY_DELAYS) + 1
    for tent, delay in enumerate((0, *RETRY_DELAYS), start=1):
        if delay:
            log.warning(
                "Connexion perdue — reconnexion dans %ds (essai %d/%d) …",
                delay, tent, essais,
            )
            time.sleep(delay)
            engine.dispose()
            engine = get_engine()
        try:
            gdf.to_postgis(
                table, con=engine, if_exists=if_exists, index=False, chunksize=chunksize
            )
            return engine
        except (OperationalError, InterfaceError) as exc:
            log.warning(
                "Échec d'insertion (connexion) : %s",
                str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__,
            )
    raise RuntimeError(
        f"Insertion dans '{table}' impossible après {essais} essais (connexion perdue)."
    )


# --------------------------------------------------------------------------
# Bâtiments
# --------------------------------------------------------------------------

def transform_buildings_chunk(df: pd.DataFrame) -> gpd.GeoDataFrame:
    """Convertit un chunk CSV en GeoDataFrame de centroïdes en EPSG:32733."""
    if {"latitude", "longitude"}.issubset(df.columns):
        df = df.dropna(subset=["latitude", "longitude"])
        gdf = gpd.GeoDataFrame(
            df,
            geometry=gpd.points_from_xy(df["longitude"], df["latitude"]),
            crs=CRS_WGS84,
        )
    elif "geometry" in df.columns:
        from shapely import wkt

        df = df.dropna(subset=["geometry"]).copy()
        df["geometry"] = df["geometry"].map(wkt.loads)
        gdf = gpd.GeoDataFrame(df, geometry="geometry", crs=CRS_WGS84)
    else:
        raise KeyError(
            "Colonnes 'latitude'/'longitude' ou 'geometry' (WKT) introuvables. "
            f"Colonnes présentes : {list(df.columns)}"
        )

    gdf = gdf.to_crs(CRS_METRIC)
    gdf["geometry"] = gdf.geometry.centroid
    return gdf


def list_building_files() -> list[Path]:
    files = [
        p for p in BASE_DIR.glob(BUILDINGS_GLOB)
        if (m := re.fullmatch(r"bloc(\d+)\.csv\.gz", p.name))
    ]
    files.sort(key=lambda p: int(re.fullmatch(r"bloc(\d+)\.csv\.gz", p.name).group(1)))
    return files


def cmd_buildings(args) -> int:
    if (args.start_file > 1 or args.skip_chunks) and args.if_exists != "append":
        log.error("La reprise (--start-file / --skip-chunks) nécessite --if-exists append.")
        return 1

    files = list_building_files()
    if not files:
        log.error("Aucun fichier %s trouvé dans %s", BUILDINGS_GLOB, BASE_DIR)
        return 1
    if args.start_file > 1:
        files = [
            f for f in files
            if int(re.fullmatch(r"bloc(\d+)\.csv\.gz", f.name).group(1)) >= args.start_file
        ]
        log.info("Reprise : départ au fichier bloc%d (%d fichier(s) restant(s)).",
                 args.start_file, len(files))
    if args.limit_files:
        files = files[: args.limit_files]
    log.info("%d fichier(s) bâtiments : %s …", len(files), ", ".join(f.name for f in files[:3])
             + (" …" if len(files) > 3 else ""))

    engine = None if args.dry_run else get_engine()
    table = args.table or BUILDINGS_TABLE

    read_kwargs: dict = {"compression": "gzip", "chunksize": CHUNK_BUILDINGS}
    if args.dry_run:
        read_kwargs["nrows"] = 20_000

    total = 0
    first_chunk = True
    stop = False
    skip_remaining = args.skip_chunks

    for fichier in files:
        if stop:
            break
        log.info("Lecture de %s …", fichier.name)
        reader = pd.read_csv(fichier, **read_kwargs)
        for i, chunk in enumerate(reader):
            if skip_remaining > 0:
                log.info("chunk %d de %s ignoré (--skip-chunks %d restant).",
                         i + 1, fichier.name, skip_remaining)
                skip_remaining -= 1
                del chunk
                continue

            t0 = time.perf_counter()
            gdf = transform_buildings_chunk(chunk)

            if args.dry_run:
                log.info(
                    "[dry-run] %s chunk %d : %d lignes → %d centroïdes (CRS cible %s)",
                    fichier.name, i + 1, len(chunk), len(gdf), gdf.crs,
                )
                log.info("[dry-run] colonnes : %s", list(gdf.columns))
                log.info("[dry-run] aperçu géométries :\n%s", gdf.geometry.head(3).to_string())
                return 0

            mode = args.if_exists if first_chunk else "append"
            engine = to_postgis_retry(gdf, table, mode, engine, chunksize=50_000)
            total += len(gdf)
            first_chunk = False
            log.info(
                "chunk %d inséré (%s, %d lignes, total %d, %.1fs)",
                i + 1, mode, len(gdf), total, time.perf_counter() - t0,
            )
            del chunk, gdf
            gc.collect()

            if args.limit and total >= args.limit:
                log.info("--limit %d atteint, arrêt.", args.limit)
                stop = True
                break

    if not args.dry_run:
        log.info("RÉUSSITE : %d bâtiments insérés dans '%s'.", total, table)
    return 0


# --------------------------------------------------------------------------
# Routes OSM
# --------------------------------------------------------------------------

def ensure_gpkg(force: bool = False) -> None:
    if GPKG_PATH.exists() and not force:
        log.info("GPKG déjà extrait : %s", GPKG_PATH)
        return
    if not GPKG_ZIP.exists():
        raise FileNotFoundError(f"Archive introuvable : {GPKG_ZIP}")
    log.info("Extraction de %s vers %s …", GPKG_ZIP.name, GPKG_EXTRACT_DIR)
    with zipfile.ZipFile(GPKG_ZIP) as zf:
        zf.extractall(GPKG_EXTRACT_DIR)
    if not GPKG_PATH.exists():
        raise FileNotFoundError(
            f"{GPKG_PATH.name} absent après extraction ; couches trouvées : "
            f"{[p.name for p in GPKG_EXTRACT_DIR.glob('*.gpkg')]}"
        )


def transform_roads_chunk(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    # Le GPKG Geofabrik déclare CRS84 (équivalent WGS84) que geopandas ne
    # convertit pas toujours en code EPSG : on force EPSG:4326 (SRID 4326).
    gdf = gdf.set_crs(CRS_WGS84, allow_override=True)
    if "osm_id" in gdf.columns:
        gdf["osm_id"] = pd.to_numeric(gdf["osm_id"], errors="coerce").astype("Int64")
        nb_null = int(gdf["osm_id"].isna().sum())
        if nb_null:
            log.warning("%d osm_id non convertibles en BIGINT (NULL).", nb_null)
    return gdf


def cmd_roads(args) -> int:
    ensure_gpkg(force=args.force)

    couches = gpd.list_layers(GPKG_PATH)
    log.info("%d couche(s) dans le GPKG :", len(couches))
    for nom, geom_type in zip(couches["name"], couches["geometry_type"]):
        log.info("  - %s (%s)", nom, geom_type)

    layer = args.layer
    table = args.table or layer.lower().replace("-", "_")
    engine = None if args.dry_run else get_engine()

    offset = CHUNK_ROADS if args.dry_run else 0
    read_rows = 5_000 if args.dry_run else CHUNK_ROADS
    first_batch = True
    total = 0

    if args.dry_run:
        gdf = gpd.read_file(GPKG_PATH, layer=layer, rows=slice(0, read_rows), engine="pyogrio")
        gdf = transform_roads_chunk(gdf)
        log.info(
            "[dry-run] couche %s : %d lignes lues, CRS=%s, colonnes=%s",
            layer, len(gdf), gdf.crs, list(gdf.columns),
        )
        log.info("[dry-run] aperçu géométries :\n%s", gdf.geometry.head(3).to_string())
        return 0

    while True:
        t0 = time.perf_counter()
        gdf = gpd.read_file(
            GPKG_PATH, layer=layer, rows=slice(offset, offset + CHUNK_ROADS), engine="pyogrio"
        )
        if gdf.empty:
            break
        gdf = transform_roads_chunk(gdf)

        mode = args.if_exists if first_batch else "append"
        engine = to_postgis_retry(gdf, table, mode, engine, chunksize=10_000)

        total += len(gdf)
        first_batch = False
        log.info(
            "paquet %d inséré (%s, %d lignes, total %d, %.1fs)",
            offset // CHUNK_ROADS + 1, mode, len(gdf), total, time.perf_counter() - t0,
        )
        offset += CHUNK_ROADS
        del gdf
        gc.collect()

        if args.limit and total >= args.limit:
            log.info("--limit %d atteint, arrêt.", args.limit)
            break

    if total == 0:
        log.error("Aucune ligne lue dans la couche '%s'.", layer)
        return 1
    log.info("RÉUSSITE : %d routes insérées dans '%s' (couche %s).", total, table, layer)
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="ETL Anser : bâtiments + routes OSM → PostGIS (config via .env)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    commun = argparse.ArgumentParser(add_help=False)
    commun.add_argument(
        "--dry-run", action="store_true",
        help="Traite un seul petit chunk et affiche le résultat, sans toucher à la base",
    )
    commun.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Nombre max de lignes à traiter (test rapide)",
    )
    commun.add_argument(
        "--if-exists", choices=("replace", "append"), default="replace",
        help="Mode d'écriture du premier chunk (défaut : replace)",
    )
    commun.add_argument(
        "--table", default=None, metavar="NAME",
        help="Table cible (défaut : batiments_rdc / gis_osm_roads_free)",
    )

    p_b = sub.add_parser(
        "buildings", parents=[commun],
        help="18 blocs .csv.gz → centroïdes EPSG:32733 → batiments_rdc",
    )
    p_b.add_argument(
        "--limit-files", type=int, default=None, metavar="N",
        help="Ne traiter que les N premiers fichiers bloc",
    )
    p_b.add_argument(
        "--start-file", type=int, default=1, metavar="N",
        help="Reprise : commencer au fichier bloc N (avec --if-exists append)",
    )
    p_b.add_argument(
        "--skip-chunks", type=int, default=0, metavar="K",
        help="Reprise : ignorer les K premiers chunks du premier fichier traité",
    )
    p_b.set_defaults(func=cmd_buildings)

    p_r = sub.add_parser(
        "roads", parents=[commun],
        help="GPKG Geofabrik → gis_osm_roads_free (osm_id en BIGINT)",
    )
    p_r.add_argument("--layer", default=ROADS_LAYER, help=f"Couche à importer (défaut : {ROADS_LAYER})")
    p_r.add_argument(
        "--force", action="store_true",
        help="Ré-extraire le ZIP même si le GPKG est déjà présent",
    )
    p_r.set_defaults(func=cmd_roads)

    args = parser.parse_args()
    setup_logging()
    try:
        return args.func(args) or 0
    except Exception:
        log.exception("Échec de l'ETL")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
