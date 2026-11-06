#!/usr/bin/env python3
"""Plan de distribution électrique par agglomération (province du Kwilu).

Principe : pour chaque agglomération cible, on segmente les bâtiments en zones
de distribution (DBSCAN puis KMeans de raffinement), on place un poste source
HTB et une sous-station MT par zone, puis on relie la source à chaque
sous-station par le plus court chemin le long du réseau routier OSM (pgRouting).

La commande « agg » applique ce calcul à une agglomération quelconque du
pays : si le calcul existe déjà il est affiché tel quel, sinon il est réalisé
(périmètre, zones, postes, lignes) sans modifier les autres agglomérations.

Sorties : schéma PostgreSQL `plan` uniquement.
  plan.province, plan.territoire, plan.agglomeration, plan.agglomeration_pop
      → tables de référence rechargées depuis le backup pg_dump
  plan.perimetre          → les N plus grandes agglomérations de la province
  plan.road / _vertex / _component → graphe de routage (fusion des sommets)
  plan.zone               → zones de distribution (clusters)
  plan.noeud              → poste source + sous-stations
  plan.ligne              → lignes de distribution calculées
  plan.parametres         → paramètres techniques du graphe (maille, classes)

Aucune table applicative n'est modifiée (ligne_electrique, noeud,
centrale_electrique, agglomerations).

Sous-commandes :
  agglomerations  : backup pg_dump → plan.province/territoire/agglomeration/pop
  perimetre       : province + top N par population → plan.perimetre
  roads-graph     : gis_osm_roads_free → plan.road / plan.road_vertex / plan.road_component
  cluster         : bâtiments → plan.zone (DBSCAN + raffinement KMeans)
  nodes           : poste source + sous-stations → plan.noeud
  lines           : pgr_dijkstra → plan.ligne
  agg <id>        : calcule UNE agglomération (n'importe laquelle, tout le pays)
                    si le calcul n'existe pas encore
  report          : synthèse console (+ export GeoJSON/GPKG optionnel)
  plan            : enchaîne toutes les étapes

La configuration de la base provient du fichier .env (voir .env.example).
"""

import argparse
import logging
import os
import re
import time
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import InterfaceError, OperationalError

BASE_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SCHEMA = "plan"
BACKUP_DEFAULT = BASE_DIR / "backup_20261021_110312.sql"

ROADS_TABLE = "gis_osm_roads_free"
BUILDINGS_TABLE = "batiments_rdc"

CRS_WGS84 = "EPSG:4326"
CRS_METRIC = "EPSG:32733"

# Fusion des sommets routiers : grille de ~222 m (les tronçons OSM se
# chevauchent de quelques centimètres, d'où 2 269 composantes à 1 cm contre
# 42 à 222 m sur Kikwit).
SNAP_TOLERANCE_M = 150.0
SNAP_MARGIN_M = 2000.0

# Segmentation
DBSCAN_EPS_M = 200.0
DBSCAN_MINPOINTS = 10
MAX_BAT_PER_ZONE = 500
NOISE_ATTACH_M = 500.0

# Postes de transformation : nombre maximal de ménages (bâtiments) desservis
# par une sous-station. Un poste HTA/BT de 100 kVA alimente ~200 ménages
# (0,5 kVA/ménage) ; les zones trop peuplées sont donc fractionnées en
# plusieurs postes.
MAX_MENAGES_POSTE = 200
# Facteur de sécurité appliqué au KMeans de fractionnement (les sous-groupes
# ne sont pas parfaitement équilibrés : on vise 85 % du plafond).
POSTE_SPLIT_RATIO = 0.85

# Tension (kV)
TENSION_SOURCE_KV = 220.0
TENSION_DISTRIB_KV = 30.0
TENSION_MT_KV = TENSION_DISTRIB_KV
TENSION_BT_KV = 0.4

# Desserte basse tension : au-delà de ce seuil, un épi est tiré du tronc BT
# vers le bâtiment (branchement direct sinon).
BT_SEUIL_EPI_M = 150.0
# Garde-fou sur la longueur d'un tronçon MT (un détour routier absurde est
# refusé et journalisé).
MAX_TRONCON_KM = 15.0

# Repli de routage : rayon de recherche de sommets atteignables
FALLBACK_RADIUS_M = 800.0

# Plafond de longueur d'une ligne de distribution depuis le poste source.
# Au-delà, la liaison n'est pas réaliste le long du réseau routier (le tracé
# part sur une route interurbaine de plusieurs centaines de kilomètres) :
# elle est marquée « à construire » (liaison directe) au lieu de suivre le
# détour. Le ratio détour / distance directe reste stocké à titre d'information.
MAX_LINE_KM = 25.0

# Classes de routes retenues pour le tracé. Les pistes et chemins ruraux
# (track, path, track_grade*) représentent 80 % du linéaire du périmètre et
# provoquent des détours de plus de 100 km pour 8 km de distance réelle :
# on ne conserve que le réseau urbain.
ROAD_CLASSES = (
    "residential", "unclassified", "living_street", "tertiary", "secondary",
    "primary", "trunk", "service", "primary_link", "secondary_link",
    "tertiary_link",
)

RETRY_DELAYS = (5, 15, 30)

log = logging.getLogger("plan")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


# --------------------------------------------------------------------------
# Connexion base de données
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


def _raw_connection(conn):
    """Retourne la connexion psycopg brute (nécessaire pour COPY)."""
    candidate = conn.connection
    return getattr(candidate, "dbapi_connection", candidate)


def _ewkb(value) -> str | None:
    """Normalise une géométrie PostGIS (hex EWKB) en chaîne hexadécimale."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return value.hex()


def run(conn, sql: str, params: dict | None = None, label: str | None = None):
    """Exécute une requête SQL en journalisant sa durée."""
    t0 = time.perf_counter()
    res = conn.execute(text(sql), params or {})
    if label:
        log.info("%s (%.1fs)", label, time.perf_counter() - t0)
    return res


# --------------------------------------------------------------------------
# Lecture du backup pg_dump (blocs COPY)
# --------------------------------------------------------------------------

COPY_RE = re.compile(rb"^COPY\s+(?:public\.)?([A-Za-z0-9_]+)\s*\(([^)]*)\)\s+FROM stdin;$")


def iter_dump_copies(path: Path, wanted: set[str]):
    """Parcourt le dump une fois et renvoie (table, colonnes, bloc texte)."""
    with open(path, "rb") as fh:
        current = None
        cols: list[str] = []
        buf: list[bytes] = []
        for line in fh:
            m = COPY_RE.match(line.rstrip(b"\r\n"))
            if m:
                name = m.group(1).decode()
                current = name if name in wanted else None
                cols = [c.strip() for c in m.group(2).decode().split(",")] if current else []
                buf = []
                continue
            if current is None:
                continue
            if line.rstrip(b"\r\n") == b"\\.":
                yield current, cols, b"".join(buf)
                current = None
                buf = []
            else:
                buf.append(line)


# --------------------------------------------------------------------------
# Schéma SQL
# --------------------------------------------------------------------------

DDL_PROVINCE = f"""
CREATE TABLE {SCHEMA}.province (
    id integer PRIMARY KEY,
    superficie double precision,
    nom varchar(80) NOT NULL,
    geom geometry(MultiPolygon,4326),
    pg_id varchar(80) NOT NULL
)"""

DDL_TERRITOIRE = f"""
CREATE TABLE {SCHEMA}.territoire (
    id integer PRIMARY KEY,
    superficie double precision,
    nom varchar(80) NOT NULL,
    ins_code varchar(80),
    geom geometry(MultiPolygon,4326),
    autres_proprietes json,
    province_id integer,
    pg_id varchar(80) NOT NULL,
    ensoleillement double precision,
    vitesse_vend double precision
)"""

DDL_AGGLOMERATION = f"""
CREATE TABLE {SCHEMA}.agglomeration (
    id integer PRIMARY KEY,
    altitude double precision NOT NULL,
    superficie double precision,
    pente_terrain double precision,
    nom varchar(80),
    pg_id varchar(80) NOT NULL,
    geom geometry(MultiPolygon,4326),
    centroid geometry(Point,4326),
    autres_proprietes json,
    territoire_id integer
)"""

DDL_AGGLOMERATION_POP = f"""
CREATE TABLE {SCHEMA}.agglomeration_pop (
    id integer,
    annee integer NOT NULL,
    autres_proprietes json,
    population integer NOT NULL,
    population_electrifiee integer NOT NULL,
    histogram_occupation_sol json,
    demande_sante double precision,
    demande_en_education double precision,
    demande_agricole double precision,
    demande_commerciale double precision,
    demande_residentielle_niveau_tier integer,
    aggromeration_id integer,
    night_lights double precision
)"""

DDL_PERIMETRE = f"""
CREATE TABLE {SCHEMA}.perimetre (
    agglomeration_id integer PRIMARY KEY,
    rang integer,
    nom varchar(80),
    population integer,
    superficie double precision,
    geom geometry(MultiPolygon,4326),
    centroid geometry(Point,4326)
)"""

DDL_ROAD = f"""
CREATE TABLE {SCHEMA}.road (
    id bigint PRIMARY KEY,
    source bigint,
    target bigint,
    cost double precision,
    reverse_cost double precision,
    geom geometry(LineString,4326),
    fclass varchar(80),
    name varchar(200),
    oneway varchar(10)
)"""

DDL_ROAD_VERTEX = f"""
CREATE TABLE {SCHEMA}.road_vertex (
    id bigint PRIMARY KEY,
    kx bigint,
    ky bigint,
    geom geometry(Point,4326)
)"""

DDL_ROAD_COMPONENT = f"""
CREATE TABLE {SCHEMA}.road_component (
    node bigint PRIMARY KEY,
    component bigint
)"""

DDL_ZONE = f"""
CREATE TABLE {SCHEMA}.zone (
    agglomeration_id integer NOT NULL,
    zone_id integer NOT NULL,
    methode varchar(20),
    nb_batiments integer,
    surface_batie_m2 double precision,
    centroid geometry(Point,4326),
    geom geometry(Polygon,4326),
    PRIMARY KEY (agglomeration_id, zone_id)
)"""

DDL_NOEUD = f"""
CREATE TABLE {SCHEMA}.noeud (
    id bigserial PRIMARY KEY,
    agglomeration_id integer NOT NULL,
    role varchar(20),
    type_poste varchar(20),
    tension_kv double precision,
    zone_id integer,
    parent_noeud_id integer,
    nb_batiments integer,
    n_menages integer,
    surface_batie_m2 double precision,
    vertex_id bigint,
    snap_distance_m double precision,
    geom geometry(Point,4326),
    zone_centroid geometry(Point,4326)
)"""

DDL_LIGNE = f"""
CREATE TABLE {SCHEMA}.ligne (
    id bigserial PRIMARY KEY,
    agglomeration_id integer NOT NULL,
    noeud_depart integer,
    noeud_arrivee integer,
    zone_id integer,
    type_ligne varchar(20),
    troncon_id integer,
    feeder_id integer,
    tension_kv double precision,
    longueur_m double precision,
    longueur_geom_m double precision,
    nb_aretes integer,
    n_menages integer,
    statut varchar(20),
    longueur_refusee_m double precision,
    detour_ratio double precision,
    geom geometry(Geometry,4326)
)"""

DDL_DERIVE = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.derive (
    id bigserial PRIMARY KEY,
    agglomeration_id integer NOT NULL,
    noeud_id integer,
    zone_id integer,
    longueur_m double precision,
    d_tronc_m double precision,
    geom geometry(LineString,4326)
)"""

DDL_AGG_STATUT = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.agg_statut (
    agglomeration_id integer PRIMARY KEY,
    zones integer,
    postes integer,
    jonctions integer,
    troncons_mt integer,
    troncs_bt integer,
    epis integer,
    km_mt double precision,
    km_bt double precision,
    menages integer,
    menages_raccordes integer,
    maj timestamp DEFAULT now()
)"""

DDL_PARAMETRES = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.parametres (
    parametre text PRIMARY KEY,
    valeur text,
    maj timestamp DEFAULT now()
)"""

DUMP_TABLES = {
    "provinces": (f"{SCHEMA}.province", DDL_PROVINCE),
    "territoire_ville": (f"{SCHEMA}.territoire", DDL_TERRITOIRE),
    "agglomerations": (f"{SCHEMA}.agglomeration", DDL_AGGLOMERATION),
    "agglomerations_hist": (f"{SCHEMA}.agglomeration_pop", DDL_AGGLOMERATION_POP),
}

EDGES_SQL = f"SELECT id, source, target, cost, reverse_cost FROM {SCHEMA}.road"


# --------------------------------------------------------------------------
# Étape 1 : chargement du backup
# --------------------------------------------------------------------------

DDL_SNAP_LIGNE = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.snap_ligne(g geometry, x1 double precision,
                                                y1 double precision, x2 double precision,
                                                y2 double precision)
RETURNS geometry LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    g2 geometry;
    mid geometry[];
BEGIN
    IF g IS NULL THEN
        RETURN NULL;
    END IF;
    IF ST_NPoints(g) <= 2 THEN
        RETURN ST_MakeLine(ARRAY[ST_SetSRID(ST_MakePoint(x1, y1), 4326),
                                ST_SetSRID(ST_MakePoint(x2, y2), 4326)]);
    END IF;
    g2 := ST_RemovePoint(ST_RemovePoint(g, 0), ST_NPoints(g) - 2);
    SELECT array_agg(d.geom) INTO mid FROM ST_DumpPoints(g2) d;
    RETURN ST_MakeLine(ARRAY[ST_SetSRID(ST_MakePoint(x1, y1), 4326)] || mid ||
                       ARRAY[ST_SetSRID(ST_MakePoint(x2, y2), 4326)]);
END
$fn$
"""


def _ensure_functions(conn) -> None:
    """Crée la fonction de raccordement des lignes aux postes."""
    conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    conn.execute(text(DDL_SNAP_LIGNE))


def _set_param(conn, nom: str, valeur) -> None:
    """Mémorise un paramètre de calcul (tolérance, classes de routes, marge)."""
    conn.execute(text(f"""
        INSERT INTO {SCHEMA}.parametres (parametre, valeur) VALUES (:k, :v)
        ON CONFLICT (parametre) DO UPDATE SET valeur = EXCLUDED.valeur, maj = now()
    """), {"k": nom, "v": str(valeur)})


def _get_param(conn, nom: str, default=None):
    """Relit un paramètre de calcul mémorisé."""
    conn.execute(text(DDL_PARAMETRES))
    val = conn.execute(
        text(f"SELECT valeur FROM {SCHEMA}.parametres WHERE parametre = :k"), {"k": nom}
    ).scalar()
    return default if val is None else val


def _classes_routieres(conn, args) -> list[str] | None:
    """Classes de routes retenues pour le tracé.

    Ordre de priorité : option explicite, puis valeur mémorisée lors du
    dernier calcul (le graphe reste ainsi reproductible), puis liste par
    défaut. Une liste vide signifie « toutes les classes ».
    """
    explicite = getattr(args, "road_classes", None)
    if explicite is not None:
        return [c.strip() for c in explicite.split(",") if c.strip()]
    memorise = _get_param(conn, "road_classes")
    if memorise is not None:
        classes = [c.strip() for c in memorise.split(",") if c.strip()]
        return classes
    return list(ROAD_CLASSES)


def _table_columns(conn, table: str) -> set[str]:
    """Ensemble des colonnes d'une table du schéma plan."""
    schema, name = table.split(".", 1) if "." in table else (SCHEMA, table)
    return {
        r[0] for r in conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = :s AND table_name = :t
        """), {"s": schema, "t": name})
    }


def _ensure_table(conn, table: str, ddl: str, colonnes: list[str], recréer: bool) -> bool:
    """Garantit l'existence de la table avec le bon schéma.

    `recréer=True`  : on repart d'une table vide (comportement historique).
    `recréer=False` : on ne touche à rien si le schéma est déjà conforme
                       (mode ciblé : les autres agglomérations sont conservées).
    """
    exists = conn.execute(
        text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}
    ).scalar()
    if exists and not recréer:
        manquantes = set(colonnes) - _table_columns(conn, table)
        if not manquantes:
            return False
        n = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
        if n:
            # Mode ciblé : on ne détruit jamais les données des autres
            # agglomérations pour un simple problème de schéma.
            raise RuntimeError(
                f"Table {table} : colonnes manquantes "
                f"({', '.join(sorted(manquantes))}) et {n} lignes existantes — "
                "recréation refusée en mode ciblé.")
        log.warning("Table %s : colonnes manquantes (%s), recréation nécessaire.",
                    table, ", ".join(sorted(manquantes)))
    conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    conn.execute(text(ddl))
    return True


def cmd_agglomerations(args) -> int:
    backup = Path(args.backup) if args.backup else BACKUP_DEFAULT
    if not backup.exists():
        log.error("Backup introuvable : %s", backup)
        return 1
    log.info("Backup : %s (%.0f Mo)", backup.name, backup.stat().st_size / 1e6)

    engine = get_engine()
    wanted = set(DUMP_TABLES)
    trouves: dict[str, tuple[list[str], bytes]] = {}

    t0 = time.perf_counter()
    for name, cols, block in iter_dump_copies(backup, wanted):
        trouves[name] = (cols, block)
        log.info("Bloc COPY %s : %d lignes", name, block.count(b"\n"))
    log.info("Lecture du dump terminée (%.1fs).", time.perf_counter() - t0)

    manquants = wanted - set(trouves)
    if manquants:
        log.error("Tables absentes du backup : %s", ", ".join(sorted(manquants)))
        return 1

    if args.dry_run:
        for name, (cols, block) in trouves.items():
            log.info("[dry-run] %s → %s, colonnes : %s", name, DUMP_TABLES[name][0], cols)
        return 0

    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        _ensure_functions(conn)
        raw = _raw_connection(conn)
        with raw.cursor() as cur:
            for name, (dst, ddl) in DUMP_TABLES.items():
                cols, block = trouves[name]
                log.info("Chargement de %s → %s …", name, dst)
                conn.execute(text(f"DROP TABLE IF EXISTS {dst} CASCADE"))
                conn.execute(text(ddl))
                with cur.copy(
                    f'COPY {dst} ({", ".join(cols)}) FROM STDIN WITH (FORMAT text)'
                ) as cp:
                    cp.write(block.decode("utf-8", errors="replace"))
                n = conn.execute(text(f"SELECT count(*) FROM {dst}")).scalar()
                log.info("  %s : %d lignes chargées.", dst, n)
        for idx in ("agglomeration", "territoire", "province"):
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_{idx}_geom "
                              f"ON {SCHEMA}.{idx} USING gist (geom)"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_pop_agg "
                          f"ON {SCHEMA}.agglomeration_pop (aggromeration_id, annee DESC)"))

    log.info("RÉUSSITE : tables de référence rechargées dans le schéma '%s'.", SCHEMA)
    return 0


# --------------------------------------------------------------------------
# Étape 2 : périmètre (top N agglomérations par population)
# --------------------------------------------------------------------------

COLONNES_PERIMETRE = ["agglomeration_id", "rang", "nom", "population",
                      "superficie", "geom", "centroid"]


def _insert_perimetre(conn, r, rang: int) -> None:
    """Insère (ou remplace) une agglomération dans plan.perimetre."""
    conn.execute(
        text(f"""
        INSERT INTO {SCHEMA}.perimetre
            (agglomeration_id, rang, nom, population, superficie, geom, centroid)
        VALUES (:id, :rang, :nom, :pop, :sup,
                ST_GeomFromEWKB(decode(:geom, 'hex')),
                ST_GeomFromEWKB(decode(:centroid, 'hex')))
        """),
        {
            "id": r["id"], "rang": rang, "nom": r["nom"], "pop": r["population"],
            "sup": r["superficie"],
            "geom": _ewkb(r["geom"]),
            "centroid": _ewkb(r["centroid"]),
        },
    )


def cmd_perimetre(args) -> int:
    engine = get_engine()
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"{SCHEMA}.agglomeration"}
        ).scalar()
        if not exists:
            log.error("Tables de référence absentes : lancez d'abord « agglomerations ».")
            return 1

        if args.only:
            # Mode ciblé : les ids demandés sont retenus quelle que soit la
            # province, les autres périmètres sont conservés.
            sql = f"""
            WITH pop AS (
                SELECT DISTINCT ON (aggromeration_id) aggromeration_id, population, annee
                FROM {SCHEMA}.agglomeration_pop
                ORDER BY aggromeration_id, annee DESC
            )
            SELECT a.id, COALESCE(NULLIF(a.nom, ''), t.nom) AS nom, a.superficie,
                   a.geom, a.centroid, pop.population, pop.annee
            FROM {SCHEMA}.agglomeration a
            JOIN {SCHEMA}.territoire t ON t.id = a.territoire_id
            JOIN pop ON pop.aggromeration_id = a.id
            WHERE a.geom IS NOT NULL AND a.id = ANY(:ids)
            """
            rows = run(conn, sql, {"ids": list(args.only)},
                       label="Sélection ciblée").mappings().all()
        else:
            sql = f"""
            WITH prov AS (
                SELECT id FROM {SCHEMA}.province WHERE upper(nom) = upper(:province)
            ),
            terr AS (
                SELECT t.id FROM {SCHEMA}.territoire t
                JOIN prov p ON p.id = t.province_id
            ),
            pop AS (
                SELECT DISTINCT ON (aggromeration_id) aggromeration_id, population, annee
                FROM {SCHEMA}.agglomeration_pop
                ORDER BY aggromeration_id, annee DESC
            )
            SELECT a.id, COALESCE(NULLIF(a.nom, ''), t.nom) AS nom, a.superficie,
                   a.geom, a.centroid, pop.population, pop.annee
            FROM {SCHEMA}.agglomeration a
            JOIN terr ON terr.id = a.territoire_id
            JOIN {SCHEMA}.territoire t ON t.id = a.territoire_id
            JOIN pop ON pop.aggromeration_id = a.id
            WHERE a.geom IS NOT NULL
            ORDER BY pop.population DESC, a.superficie DESC NULLS LAST
            LIMIT :top
            """
            rows = run(conn, sql, {"province": args.province, "top": args.top},
                       label=f"Sélection du top {args.top} de la province {args.province}"
                       ).mappings().all()

        if not rows:
            if args.only:
                log.error("Aucune agglomération ne correspond à --only %s.",
                          " ".join(str(i) for i in args.only))
            else:
                log.error("Aucune agglomération trouvée pour la province '%s'.",
                          args.province)
            return 1

        for r in rows:
            log.info("  #%d %-14s population=%-8s superficie=%-8s annee=%s",
                     rows.index(r) + 1, (r["nom"] or "?"), r["population"],
                     round(r["superficie"], 2) if r["superficie"] else None, r["annee"])

        if args.dry_run:
            log.info("[dry-run] %d agglomérations sélectionnées, rien n'est écrit.", len(rows))
            return 0

        if args.only:
            # Mode ciblé : seules les agglomérations demandées sont remplacées,
            # les autres périmètres sont conservés.
            _ensure_table(conn, f"{SCHEMA}.perimetre", DDL_PERIMETRE,
                          COLONNES_PERIMETRE, recréer=False)
            for r in rows:
                rang = conn.execute(text(
                    f"SELECT rang FROM {SCHEMA}.perimetre WHERE agglomeration_id = :id"
                ), {"id": r["id"]}).scalar()
                conn.execute(
                    text(f"DELETE FROM {SCHEMA}.perimetre WHERE agglomeration_id = :id"),
                    {"id": r["id"]},
                )
                if rang is None:
                    rang = conn.execute(text(
                        f"SELECT COALESCE(max(rang), 0) + 1 FROM {SCHEMA}.perimetre"
                    )).scalar()
                _insert_perimetre(conn, r, rang)
        else:
            conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.perimetre CASCADE"))
            conn.execute(text(DDL_PERIMETRE))
            for rang, r in enumerate(rows, start=1):
                _insert_perimetre(conn, r, rang)
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_perimetre_geom "
                          f"ON {SCHEMA}.perimetre USING gist (geom)"))

    log.info("RÉUSSITE : %d agglomérations retenues dans %s.perimetre.", len(rows), SCHEMA)
    return 0


# --------------------------------------------------------------------------
# Étape 3 : graphe de routage
# --------------------------------------------------------------------------

def cmd_roads_graph(args) -> int:
    engine = get_engine()
    with engine.begin() as conn:
        has = conn.execute(
            text("SELECT count(*) FROM information_schema.tables "
                 "WHERE table_schema = :s AND table_name = 'perimetre'"),
            {"s": SCHEMA},
        ).scalar()
        if not has:
            log.error("Aucun périmètre : lancez d'abord « perimetre ».")
            return 1

        # Grille de fusion des sommets : pas d'environ tolerance_m degrés.
        div = max(1, int(round(args.snap_tolerance / 111320.0 * 1e7)))
        log.info("Tolérance de fusion : %.0f m (diviseur %d ≈ %.0f m de pas)",
                 args.snap_tolerance, div, div * 1e-7 * 111320.0)

        classes = _classes_routieres(conn, args)
        log.info("Classes routables retenues : %s", ", ".join(classes) if classes else "toutes")

        if not args.dry_run:
            # Mémorise l'état des sommets : après reconstruction, les identifiants
            # changent (ils sont recalculés) mais la grille (kx,ky) reste la
            # même : on remappe donc les postes sur les nouveaux sommets.
            conn.execute(text("DROP TABLE IF EXISTS _vert_map"))
            conn.execute(text(f"""
                CREATE TEMP TABLE _vert_map AS
                SELECT id AS old_id, kx, ky FROM {SCHEMA}.road_vertex
            """))
            conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.road CASCADE"))
            conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.road_vertex CASCADE"))
            conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.road_component CASCADE"))

        # 1. Arêtes du périmètre (+ marge) avec les clés de sommet fusionnées.
        run(conn, f"""
        CREATE TEMP TABLE _stage AS
        SELECT r.osm_id::bigint AS id, r.geometry, r.fclass, r.name, r.oneway,
               round(ST_X(ST_StartPoint(r.geometry)) * 1e7 / {div})::bigint AS kx_src,
               round(ST_Y(ST_StartPoint(r.geometry)) * 1e7 / {div})::bigint AS ky_src,
               round(ST_X(ST_EndPoint(r.geometry)) * 1e7 / {div})::bigint AS kx_tgt,
               round(ST_Y(ST_EndPoint(r.geometry)) * 1e7 / {div})::bigint AS ky_tgt
        FROM {ROADS_TABLE} r
        CROSS JOIN (
            SELECT ST_Expand(ST_Collect(geom), {args.margin / 111320.0}) AS b
            FROM {SCHEMA}.perimetre
        ) p
        WHERE r.geometry && p.b
          AND (CAST(:classes AS text[]) IS NULL OR r.fclass = ANY(CAST(:classes AS text[])))
        """, {"classes": classes}, label="Extraction des arêtes routières du périmètre")

        n_edges = conn.execute(text("SELECT count(*) FROM _stage")).scalar()
        log.info("Arêtes retenues : %d", n_edges)

        # 2. Sommets : une ligne par cellule de la grille.
        #    La position retenue est le médoïde (extrémité de route la plus
        #    proche du centroïde de la cellule) : le sommet reste ainsi sur une
        #    route réelle et le déplacement des arêtes est minimal.
        run(conn, f"""
        CREATE TEMP TABLE _verts AS
        WITH ends AS (
            SELECT kx_src AS kx, ky_src AS ky, ST_StartPoint(geometry) AS p FROM _stage
            UNION ALL
            SELECT kx_tgt, ky_tgt, ST_EndPoint(geometry) FROM _stage
        ),
        cells AS (
            SELECT kx, ky, avg(ST_X(p)) AS cx, avg(ST_Y(p)) AS cy
            FROM ends GROUP BY kx, ky
        )
        SELECT row_number() OVER (ORDER BY c.kx, c.ky)::bigint AS id, c.kx, c.ky,
               (SELECT e.p FROM ends e
                WHERE e.kx = c.kx AND e.ky = c.ky
                ORDER BY ST_Distance(e.p::geography,
                                    ST_SetSRID(ST_MakePoint(c.cx, c.cy), 4326)::geography)
                LIMIT 1) AS geom
        FROM cells c
        """, label="Construction des sommets fusionnés")

        n_verts = conn.execute(text("SELECT count(*) FROM _verts")).scalar()
        log.info("Sommets : %d", n_verts)

        # 3. Table des arêtes routables (sans boucle, coût > 0).
        #    La géométrie OSM d'origine est conservée ; c'est la ligne tracée
        #    qui est ensuite raccordée aux postes (fonction plan.snap_ligne).
        run(conn, f"""
        CREATE TABLE {SCHEMA}.road AS
        SELECT s.id, vs.id AS source, vt.id AS target,
               ST_Length(s.geometry::geography) AS cost,
               CASE WHEN s.oneway = 'F' THEN -1
                    ELSE ST_Length(s.geometry::geography) END AS reverse_cost,
               s.geometry AS geom, s.fclass, s.name, s.oneway
        FROM _stage s
        JOIN _verts vs ON vs.kx = s.kx_src AND vs.ky = s.ky_src
        JOIN _verts vt ON vt.kx = s.kx_tgt AND vt.ky = s.ky_tgt
        WHERE vs.id <> vt.id AND ST_Length(s.geometry::geography) > 0
        """, label="Écriture de plan.road")

        conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_road_geom "
                          f"ON {SCHEMA}.road USING gist (geom)"))

        if args.dry_run:
            n = conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.road")).scalar()
            log.info("[dry-run] %d arêtes routables prêtes.", n)
            conn.execute(text("DROP TABLE IF EXISTS _stage"))
            conn.execute(text("DROP TABLE IF EXISTS _verts"))
            return 0

        # 4. Sommets et composantes connexes.
        run(conn, f"""
        CREATE TABLE {SCHEMA}.road_vertex AS
        SELECT id, kx, ky, geom FROM _verts
        """, label="Écriture de plan.road_vertex")
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_road_vertex_geom "
                          f"ON {SCHEMA}.road_vertex USING gist (geom)"))
        # Index d'unicité sur la cellule de grille : il identifie les sommets
        # déjà connus lors d'une extension ciblée du graphe.
        conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{SCHEMA}_road_vertex_cell "
                          f"ON {SCHEMA}.road_vertex (kx, ky)"))

        run(conn, f"""
        CREATE TABLE {SCHEMA}.road_component AS
        SELECT node::bigint AS node, component::bigint AS component
        FROM pgr_connectedComponents(:edges)
        """, {"edges": EDGES_SQL}, label="pgr_connectedComponents")

        conn.execute(text("DROP TABLE IF EXISTS _stage"))
        conn.execute(text("DROP TABLE IF EXISTS _verts"))

        # 5. Remappage des postes sur les nouveaux identifiants de sommets.
        n_remap = conn.execute(text(f"""
            UPDATE {SCHEMA}.noeud n SET vertex_id = v.id
            FROM {SCHEMA}.road_vertex v
            JOIN _vert_map om ON om.kx = v.kx AND om.ky = v.ky
            WHERE n.vertex_id = om.old_id
        """)).rowcount
        n_orphelins = conn.execute(text(f"""
            UPDATE {SCHEMA}.noeud SET vertex_id = NULL
            WHERE vertex_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM _vert_map om WHERE om.old_id = vertex_id)
        """)).rowcount
        conn.execute(text("DROP TABLE IF EXISTS _vert_map"))
        if n_remap or n_orphelins:
            log.info("Postes remappés sur le nouveau graphe : %d, sans sommet : %d.",
                     n_remap, n_orphelins)

        # 5 bis. Les postes sans sommet sont réaccrochés sur le nouveau graphe
        #       (leur cellule de grille a disparu : le tracé est réécrit).
        n_resnap = 0
        orphelins = conn.execute(text(f"""
            SELECT id, ST_X(zone_centroid) AS cx, ST_Y(zone_centroid) AS cy
            FROM {SCHEMA}.noeud WHERE vertex_id IS NULL AND zone_centroid IS NOT NULL
        """)).mappings().all()
        for o in orphelins:
            vid, dist, vxy = _nearest_vertex(conn, o["cx"], o["cy"])
            if vid is not None and dist is not None and dist <= args.max_snap_m:
                conn.execute(text(f"""
                    UPDATE {SCHEMA}.noeud SET vertex_id = :vid, snap_distance_m = :d,
                           geom = ST_SetSRID(ST_MakePoint(:x, :y), 4326)
                    WHERE id = :id
                """), {"vid": vid, "d": dist, "x": vxy[0], "y": vxy[1], "id": o["id"]})
                n_resnap += 1
        if n_resnap:
            log.info("Postes réaccrochés sur le nouveau graphe : %d.", n_resnap)

        # 6. Paramètres de construction mémorisés (extension ciblée du graphe).
        _set_param(conn, "snap_tolerance_m", args.snap_tolerance)
        _set_param(conn, "snap_div", div)
        _set_param(conn, "margin_m", args.margin)
        _set_param(conn, "road_classes", ",".join(classes) if classes else "")
        comp = conn.execute(text(f"""
            SELECT count(DISTINCT component) AS nb, max(n) AS max_n FROM (
                SELECT component, count(*) n FROM {SCHEMA}.road_component GROUP BY component
            ) s
        """)).mappings().first()
        log.info("Composantes connexes : %d (plus grande : %s sommets)",
                 comp["nb"], comp["max_n"])

    log.info("RÉUSSITE : graphe de routage écrit dans %s.road / _vertex / _component.", SCHEMA)
    return 0


# --------------------------------------------------------------------------
# Étape 4 : segmentation des bâtiments (DBSCAN puis KMeans)
# --------------------------------------------------------------------------

BUILDINGS_CTE = f"""
WITH base AS (
    SELECT geom, centroid FROM {SCHEMA}.perimetre WHERE agglomeration_id = :agg
),
zone AS MATERIALIZED (
    -- UTM locale (hémisphère sud) déduite de la longitude du centroïde :
    -- les métriques de clustering sont justes jusqu'à ~20°E.
    SELECT 32700 + (floor((ST_X(centroid) + 180) / 6) + 1)::int AS sr FROM base
),
poly AS MATERIALIZED (
    SELECT ST_Transform(b.geom, z.sr) AS g,
           ST_Transform(b.geom, 32733) AS g32733,
           z.sr
    FROM base b CROSS JOIN zone z
),
bat AS (
    -- Les bâtiments sont stockés en EPSG:32733 : on filtre avec l'index GIST
    -- dans ce CRS, puis on reprojette dans la zone UTM locale.
    SELECT ST_Transform(r.geometry, p.sr) AS geometry, r.area_in_meters
    FROM poly p
    JOIN {BUILDINGS_TABLE} r ON r.geometry && p.g32733
    WHERE ST_Contains(p.g32733, r.geometry)
      AND (:min_area = 0 OR r.area_in_meters >= :min_area)
)
"""


COLONNES_ZONE = ["agglomeration_id", "zone_id", "methode", "nb_batiments",
                 "surface_batie_m2", "centroid", "geom"]
COLONNES_NOEUD = ["agglomeration_id", "role", "type_poste", "tension_kv",
                  "zone_id", "parent_noeud_id", "nb_batiments", "n_menages",
                  "surface_batie_m2", "vertex_id", "snap_distance_m", "geom",
                  "zone_centroid"]
COLONNES_LIGNE = ["agglomeration_id", "noeud_depart", "noeud_arrivee", "zone_id",
                  "type_ligne", "troncon_id", "feeder_id", "tension_kv",
                  "longueur_m", "longueur_geom_m", "nb_aretes", "n_menages",
                  "statut", "longueur_refusee_m", "detour_ratio", "geom"]


def _migrate_schema(conn) -> None:
    """Migration additive du modèle réseau MT/BT.

    Ajoute les colonnes aux tables existantes sans les recréer (les données
    des autres agglomérations sont conservées) et crée les nouvelles tables
    `derive` (épis BT) et `agg_statut` (synthèse par agglomération).
    """
    conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    ajouts = (
        ("noeud", "type_poste", "varchar(20)"),
        ("noeud", "parent_noeud_id", "integer"),
        ("noeud", "n_menages", "integer"),
        ("ligne", "zone_id", "integer"),
        ("ligne", "troncon_id", "integer"),
        ("ligne", "feeder_id", "integer"),
        ("ligne", "n_menages", "integer"),
    )
    for table, colonne, typ in ajouts:
        existe = conn.execute(
            text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"{SCHEMA}.{table}"}
        ).scalar()
        if existe:
            conn.execute(text(f"ALTER TABLE {SCHEMA}.{table} "
                              f"ADD COLUMN IF NOT EXISTS {colonne} {typ}"))
    # Le tronc BT est un arbre (MultiLineString) : la colonne geom doit
    # accepter tout type de géométrie (les tronçons MT restent des lignes).
    if conn.execute(text(f"SELECT to_regclass('{SCHEMA}.ligne')")).scalar():
        typ = conn.execute(text(f"""
            SELECT format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            WHERE a.attrelid = '{SCHEMA}.ligne'::regclass AND a.attname = 'geom'
        """)).scalar()
        if typ and "LineString" in typ:
            conn.execute(text(f"ALTER TABLE {SCHEMA}.ligne "
                              f"ALTER COLUMN geom TYPE geometry(Geometry,4326) "
                              f"USING geom::geometry"))
    conn.execute(text(DDL_DERIVE))
    conn.execute(text(DDL_AGG_STATUT))
    conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_ligne_type "
                      f"ON {SCHEMA}.ligne (agglomeration_id, type_ligne)"))
    conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_noeud_role "
                      f"ON {SCHEMA}.noeud (agglomeration_id, role)"))
    conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_noeud_centroid "
                      f"ON {SCHEMA}.noeud USING gist (zone_centroid)"))
    conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_derive_agg "
                      f"ON {SCHEMA}.derive (agglomeration_id, zone_id)"))


def _compute_zone_agg(conn, agg, args) -> None:
    """Calcule les zones de distribution d'une agglomération (les siennes sont remplacées)."""
    aid = agg["agglomeration_id"]
    t0 = time.perf_counter()
    conn.execute(text(f"DELETE FROM {SCHEMA}.zone WHERE agglomeration_id = :agg"),
                 {"agg": aid})
    params = {"agg": aid, "min_area": args.min_area,
              "eps": args.eps, "minpoints": args.minpoints}

    # 4.1 Clustering DBSCAN, calculé une seule fois par agglomération.
    conn.execute(text("DROP TABLE IF EXISTS _cl"))
    run(conn, "CREATE TEMP TABLE _cl AS " + BUILDINGS_CTE + """
    SELECT geometry, area_in_meters,
           ST_ClusterDBSCAN(geometry, :eps, :minpoints) OVER () AS cid
    FROM bat
    """, params, label=f"  DBSCAN (agg. {aid})")

    n_bat = conn.execute(text("SELECT count(*) FROM _cl")).scalar()
    n_noise = conn.execute(text("SELECT count(*) FROM _cl WHERE cid < 0")).scalar()

    zones = conn.execute(text("""
        SELECT cid, count(*)::int AS nb, sum(area_in_meters) AS surf,
               ST_X(ST_Transform(ST_Centroid(ST_Collect(geometry)), 4326)) AS cx,
               ST_Y(ST_Transform(ST_Centroid(ST_Collect(geometry)), 4326)) AS cy,
               ST_Transform(ST_ConvexHull(ST_Collect(geometry)), 4326) AS hull
        FROM _cl WHERE cid >= 0 GROUP BY cid ORDER BY cid
    """)).mappings().all()

    # 4.2 Raffinement KMeans des zones trop grosses.
    final: list[dict] = []
    for z in zones:
        if z["nb"] > args.max_bat_per_zone:
            k = max(2, -(-z["nb"] // args.max_bat_per_zone))
            parts = [dict(p) for p in conn.execute(text("""
                WITH k AS (
                    SELECT ST_ClusterKMeans(geometry, :k) OVER () AS kid,
                           geometry, area_in_meters
                    FROM _cl WHERE cid = :cid
                )
                SELECT kid, count(*)::int AS nb, sum(area_in_meters) AS surf,
                       ST_X(ST_Transform(ST_Centroid(ST_Collect(geometry)), 4326)) AS cx,
                       ST_Y(ST_Transform(ST_Centroid(ST_Collect(geometry)), 4326)) AS cy,
                       ST_Transform(ST_ConvexHull(ST_Collect(geometry)), 4326) AS hull
                FROM k GROUP BY kid ORDER BY kid
            """), {"k": k, "cid": z["cid"]}).mappings().all()]
            for p in parts:
                p["methode"] = "kmeans"
            final.extend(parts)
            log.info("  zone %d (%d bât.) → KMeans k=%d → %d zones",
                     z["cid"], z["nb"], k, len(parts))
        else:
            z = dict(z)
            z["methode"] = "dbscan"
            final.append(z)

    # 4.3 Bruit DBSCAN : rattachement à la zone la plus proche (< 500 m).
    n_attaches = 0
    if final and n_noise:
        n_attaches = conn.execute(text("""
            WITH zs AS (
                SELECT ST_Centroid(ST_Collect(geometry)) AS c
                FROM _cl WHERE cid >= 0 GROUP BY cid
            )
            SELECT count(*) FROM _cl d
            WHERE d.cid < 0 AND EXISTS (
                SELECT 1 FROM zs
                WHERE ST_DWithin(zs.c::geography, d.geometry::geography, :rad)
            )
        """), {"rad": NOISE_ATTACH_M}).scalar()

    # 4.4 Écriture des zones.
    for i, z in enumerate(final, start=1):
        conn.execute(text(f"""
            INSERT INTO {SCHEMA}.zone
                (agglomeration_id, zone_id, methode, nb_batiments, surface_batie_m2,
                 centroid, geom)
            VALUES (:agg, :zid, :meth, :nb, :surf,
                    ST_SetSRID(ST_MakePoint(:cx, :cy), 4326),
                    ST_GeomFromEWKB(decode(:hull, 'hex')))
        """), {
            "agg": aid, "zid": i, "meth": z["methode"], "nb": z["nb"],
            "surf": z["surf"] or 0.0,
            "cx": z["cx"], "cy": z["cy"],
            "hull": _ewkb(z["hull"]),
        })

    log.info("Agglomération %d (%s) : %d bâtiments, %d zones (%d bruit dont %d rattachés) "
             "en %.1fs", aid, agg["nom"], n_bat, len(final), n_noise, n_attaches,
             time.perf_counter() - t0)


def cmd_cluster(args) -> int:
    engine = get_engine()
    with engine.begin() as conn:
        aggs = conn.execute(text(
            f"SELECT agglomeration_id, nom FROM {SCHEMA}.perimetre ORDER BY rang"
        )).mappings().all()
        if not aggs:
            log.error("Aucun périmètre : lancez d'abord « perimetre ».")
            return 1
        if args.only:
            retenus = [a for a in aggs if a["agglomeration_id"] in args.only]
            if not retenus:
                log.error("Aucune agglomération ne correspond à --only %s.",
                          " ".join(str(i) for i in args.only))
                return 1
            aggs = retenus

        # Sans --only la table est reconstruite (comportement historique) ;
        # avec --only elle est conservée pour les autres agglomérations.
        _ensure_table(conn, f"{SCHEMA}.zone", DDL_ZONE, COLONNES_ZONE,
                      recréer=not args.only)

    # Une transaction par agglomération : une coupure de connexion ne perd
    # que le calcul en cours, qui est rejoué automatiquement.
    for agg in aggs:
        _etape_resiliente(engine, _compute_zone_agg, agg, args)

    log.info("RÉUSSITE : zones écrites dans %s.zone.", SCHEMA)
    return 0


# --------------------------------------------------------------------------
# Étape 5 : nœuds (poste source + sous-stations)
# --------------------------------------------------------------------------

def _snap(conn, x: float, y: float):
    """Accroche un point sur l'arête la plus proche puis renvoie le sommet le plus proche."""
    row = conn.execute(text(f"""
        SELECT r.source, r.target,
               ST_Distance(vs.geom::geography, ST_SetSRID(ST_MakePoint(:x,:y),4326)::geography) AS ds,
               ST_Distance(vt.geom::geography, ST_SetSRID(ST_MakePoint(:x,:y),4326)::geography) AS dt
        FROM {SCHEMA}.road r
        JOIN {SCHEMA}.road_vertex vs ON vs.id = r.source
        JOIN {SCHEMA}.road_vertex vt ON vt.id = r.target
        ORDER BY r.geom <-> ST_SetSRID(ST_MakePoint(:x,:y),4326)
        LIMIT 1
    """), {"x": x, "y": y}).mappings().first()
    if row is None:
        return None
    if row["ds"] <= row["dt"]:
        return row["source"], float(row["ds"])
    return row["target"], float(row["dt"])


def _nearest_vertex(conn, x: float, y: float, component: int | None = None):
    """Sommet du graphe le plus proche de (x, y), éventuellement dans une composante donnée."""
    joint = ""
    params = {"x": x, "y": y}
    if component is not None:
        joint = f"JOIN {SCHEMA}.road_component c ON c.node = v.id"
        params["comp"] = component
    row = conn.execute(text(f"""
        SELECT v.id, ST_X(v.geom) AS vx, ST_Y(v.geom) AS vy,
               ST_Distance(v.geom::geography,
                           ST_SetSRID(ST_MakePoint(:x,:y),4326)::geography) AS d
        FROM {SCHEMA}.road_vertex v
        {joint}
        {"WHERE c.component = :comp" if component is not None else ""}
        ORDER BY v.geom <-> ST_SetSRID(ST_MakePoint(:x,:y),4326)
        LIMIT 1
    """), params).mappings().first()
    if row is None:
        return None, None, None
    return row["id"], float(row["d"]), (row["vx"], row["vy"])


def _modal_component(conn, vertex_ids: list[int]) -> int | None:
    """Composante connexe contenant le plus de sous-stations (réseau de service)."""
    ids = [v for v in vertex_ids if v is not None]
    if not ids:
        return None
    row = conn.execute(text(f"""
        SELECT c.component FROM unnest(CAST(:vids AS bigint[])) v
        JOIN {SCHEMA}.road_component c ON c.node = v
        GROUP BY c.component ORDER BY count(*) DESC LIMIT 1
    """), {"vids": ids}).mappings().first()
    return row["component"] if row else None


def _compute_nodes_agg(conn, agg, args) -> int:
    """Calcule le poste source et les sous-stations d'une agglomération.

    Les sous-stations ne sont plus « une par zone » : chaque zone est
    fractionnée en autant de postes que nécessaire pour respecter
    `--max-menages-poste` (200 ménages ≈ poste 100 kVA). Les nœuds, lignes et
    dérivations de l'agglomération sont remplacés ; les autres sont conservés.
    Renvoie le nombre de sous-stations créées.
    """
    aid = agg["agglomeration_id"]
    cx, cy = agg["cx"], agg["cy"]
    for table in ("derive", "ligne", "noeud", "agg_statut"):
        conn.execute(text(f"DELETE FROM {SCHEMA}.{table} WHERE agglomeration_id = :agg"),
                     {"agg": aid})
    # Index KNN indispensable à l'affectation des bâtiments aux zones.
    conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_zone_centroid "
                      f"ON {SCHEMA}.zone USING gist (centroid)"))

    zones = conn.execute(text(f"""
        SELECT zone_id, nb_batiments, surface_batie_m2,
               ST_X(centroid) AS cx, ST_Y(centroid) AS cy
        FROM {SCHEMA}.zone WHERE agglomeration_id = :agg ORDER BY zone_id
    """), {"agg": aid}).mappings().all()
    if not zones:
        log.warning("Agglomération %d : aucune zone, postes ignorés.", aid)
        return 0

    # 1. Bâtiments du périmètre, lus UNE fois via l'index GIST (jamais de
    #    balayage global de batiments_rdc : 84,9 M de lignes).
    conn.execute(text("DROP TABLE IF EXISTS _bat"))
    conn.execute(text(f"""
        CREATE TEMP TABLE _bat AS
        SELECT ST_Transform(r.geometry, 4326) AS g,
               r.geometry AS g33,
               COALESCE(r.area_in_meters, 0) AS area
        FROM {SCHEMA}.perimetre p
        JOIN {BUILDINGS_TABLE} r
          ON r.geometry && ST_Transform(p.geom, 32733)
         AND ST_Contains(ST_Transform(p.geom, 32733), r.geometry)
        WHERE p.agglomeration_id = :agg
    """), {"agg": aid})
    n_bat = conn.execute(text("SELECT count(*) FROM _bat")).scalar() or 0
    if not n_bat:
        log.warning("Agglomération %d : aucun bâtiment dans le périmètre.", aid)
        return 0

    # 2. Affectation de chaque bâtiment à la zone la plus proche (partition).
    conn.execute(text("DROP TABLE IF EXISTS _bat_zone"))
    conn.execute(text(f"""
        CREATE TEMP TABLE _bat_zone AS
        SELECT z.zone_id, b.g, b.g33, b.area
        FROM _bat b
        CROSS JOIN LATERAL (
            SELECT zone_id FROM {SCHEMA}.zone
            WHERE agglomeration_id = :agg
            ORDER BY centroid <-> b.g LIMIT 1
        ) z
    """), {"agg": aid})
    conn.execute(text("CREATE INDEX ON _bat_zone (zone_id)"))

    # 3. Fractionnement de chaque zone en sous-groupes <= max_menages_poste.
    #    Facteur de sécurité : le KMeans n'équilibre pas parfaitement les
    #    effectifs, on vise 85 % du plafond.
    cible = max(1.0, float(args.max_menages_poste) * POSTE_SPLIT_RATIO)
    counts = dict(conn.execute(text(
        "SELECT zone_id, count(*) FROM _bat_zone GROUP BY zone_id")).all())
    parts: list[dict] = []
    for z in zones:
        n = int(counts.get(z["zone_id"], 0))
        if n == 0:
            log.warning("  zone %d : aucun bâtiment rattaché, poste ignoré.", z["zone_id"])
            continue
        k = max(1, min(n, int(-(-n // cible))))
        rows = conn.execute(text("""
            SELECT pid, count(*)::int AS nb, COALESCE(sum(area), 0) AS surf,
                   ST_X(ST_Transform(ST_Centroid(ST_Collect(g)), 4326)) AS cx,
                   ST_Y(ST_Transform(ST_Centroid(ST_Collect(g)), 4326)) AS cy
            FROM (SELECT ST_ClusterKMeans(g33, :k) OVER () AS pid, g, area
                  FROM _bat_zone WHERE zone_id = :zid) s
            GROUP BY pid ORDER BY pid
        """), {"k": k, "zid": z["zone_id"]}).mappings().all()
        for p in rows:
            parts.append({"zone_id": z["zone_id"], "cx": float(p["cx"]),
                          "cy": float(p["cy"]), "nb": int(p["nb"]),
                          "surf": float(p["surf"] or 0.0)})

    if not parts:
        log.warning("Agglomération %d : aucun sous-groupe exploitable.", aid)
        return 0

    # 4. Accrochage des postes sur le sommet routier le plus proche.
    snaps = []
    for p in parts:
        v, d, xy = _nearest_vertex(conn, p["cx"], p["cy"])
        if v is None:
            log.warning("  poste (zone %d) : aucune route à proximité, ignoré.",
                        p["zone_id"])
            continue
        snaps.append((p, v, d, xy))

    # 5. Composante de service : celle qui porte le plus de postes.
    comp = _modal_component(conn, [s[1] for s in snaps])

    # 6. Poste source HTB : sommet de la composante le plus proche du centroïde.
    vertex, dist, vxy = _nearest_vertex(conn, cx, cy, component=comp)
    if vertex is None:
        vertex, dist, vxy = _nearest_vertex(conn, cx, cy)
    if vertex is None:
        log.warning("Agglomération %d : aucune route à proximité, poste source ignoré.", aid)
        return 0

    # 7. Recalage des postes dans la composante de service. Plusieurs postes
    #    peuvent partager un même sommet routier : le réseau MT est alors
    #    mutualisé sur ce sommet, chaque poste gardant son propre tronc BT.
    final_snaps = []
    n_recalage = 0
    for p, v, d, xy in snaps:
        if comp is not None:
            v2, d2, xy2 = _nearest_vertex(conn, p["cx"], p["cy"], component=comp)
            if v2 is not None and d2 is not None and d2 <= args.max_snap_m:
                if v2 != v:
                    n_recalage += 1
                v, d, xy = v2, d2, xy2
        final_snaps.append((p, v, d, xy))
    conn.execute(text(f"""
        INSERT INTO {SCHEMA}.noeud
            (agglomeration_id, role, type_poste, tension_kv, zone_id,
             nb_batiments, n_menages, surface_batie_m2, vertex_id,
             snap_distance_m, geom, zone_centroid)
        VALUES (:agg, 'source', 'source', :tension, NULL, NULL, NULL, NULL, :vid,
                :dist, ST_SetSRID(ST_MakePoint(:x,:y), 4326),
                ST_SetSRID(ST_MakePoint(:zcx,:zcy), 4326))
    """), {"agg": aid, "tension": args.tension_source, "vid": vertex,
           "dist": dist, "x": vxy[0], "y": vxy[1], "zcx": cx, "zcy": cy})

    # 8. Sous-stations (postes HTA/BT), une par sous-groupe de ménages.
    for p, v, d, xy in final_snaps:
        conn.execute(text(f"""
            INSERT INTO {SCHEMA}.noeud
                (agglomeration_id, role, type_poste, tension_kv, zone_id,
                 nb_batiments, n_menages, surface_batie_m2, vertex_id,
                 snap_distance_m, geom, zone_centroid)
            VALUES (:agg, 'sous_station', 'poste_hta', :tension, :zid,
                    :nb, :nb, :surf, :vid, :dist,
                    ST_SetSRID(ST_MakePoint(:x,:y), 4326),
                    ST_SetSRID(ST_MakePoint(:zcx,:zcy), 4326))
        """), {"agg": aid, "tension": _tension_mt(args), "zid": p["zone_id"],
               "nb": p["nb"], "surf": p["surf"], "vid": v, "dist": d,
               "x": xy[0], "y": xy[1], "zcx": p["cx"], "zcy": p["cy"]})

    log.info("Agglomération %d (%s) : poste source %.0f kV sur le sommet %d "
             "(composante %s, accroche %.0f m), %d sous-stations pour %d zones "
             "et %d bâtiments (%d recalées, plafond %d ménages/poste, "
             "MT %.1f kV)",
             aid, agg["nom"], args.tension_source, vertex, comp, dist,
             len(final_snaps), len(zones), n_bat, n_recalage,
             args.max_menages_poste, _tension_mt(args))
    return len(final_snaps)


def cmd_nodes(args) -> int:
    engine = get_engine()
    with engine.begin() as conn:
        aggs = conn.execute(text(
            f"SELECT agglomeration_id, nom, ST_X(centroid) AS cx, ST_Y(centroid) AS cy "
            f"FROM {SCHEMA}.perimetre ORDER BY rang"
        )).mappings().all()
        if not aggs:
            log.error("Aucun périmètre : lancez d'abord « perimetre ».")
            return 1
        if args.only:
            retenus = [a for a in aggs if a["agglomeration_id"] in args.only]
            if not retenus:
                log.error("Aucune agglomération ne correspond à --only %s.",
                          " ".join(str(i) for i in args.only))
                return 1
            aggs = retenus

        # Sans --only la table est reconstruite (comportement historique) ;
        # avec --only elle est conservée pour les autres agglomérations.
        _migrate_schema(conn)
        _ensure_table(conn, f"{SCHEMA}.noeud", DDL_NOEUD, COLONNES_NOEUD,
                      recréer=not args.only)

    # Une transaction par agglomération : une coupure de connexion ne perd
    # que le calcul en cours, qui est rejoué automatiquement.
    for agg in aggs:
        _etape_resiliente(engine, _compute_nodes_agg, agg, args)

    with engine.connect() as conn:
        n = conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.noeud")).scalar()
    log.info("RÉUSSITE : %d nœuds écrits dans %s.noeud.", n, SCHEMA)
    return 0


# --------------------------------------------------------------------------
# Étape 6 : lignes de distribution (pgr_dijkstra)
# --------------------------------------------------------------------------

DIJKSTRA_SQL = f"""
WITH path AS (
    SELECT path_seq, node, edge FROM pgr_dijkstra(:edges, :src, :dst)
),
edges AS (
    SELECT p.path_seq, p.node AS node_from, r.source, r.target, r.geom,
           vs.geom AS g_src, vt.geom AS g_tgt
    FROM path p
    JOIN {SCHEMA}.road r ON r.id = p.edge
    JOIN {SCHEMA}.road_vertex vs ON vs.id = r.source
    JOIN {SCHEMA}.road_vertex vt ON vt.id = r.target
),
snapped AS (
    SELECT e.path_seq, e.node_from, e.source, e.target,
           CASE WHEN e.source = e.node_from
                THEN ST_SetPoint(ST_SetPoint(e.geom, 0, e.g_src),
                                 ST_NPoints(e.geom) - 1, e.g_tgt)
                ELSE ST_SetPoint(ST_SetPoint(ST_Reverse(e.geom), 0, e.g_tgt),
                                 ST_NPoints(e.geom) - 1, e.g_src)
           END AS geom,
           e.geom AS geom_brut
    FROM edges e
)
SELECT ST_MakeLine(s.geom ORDER BY s.path_seq) AS geom,
       count(*)::int AS nb_aretes,
       sum(ST_Length(s.geom_brut::geography)) AS longueur_m
FROM snapped s
"""


def _route(conn, src: int, dst: int):
    row = conn.execute(text(DIJKSTRA_SQL), {
        "edges": EDGES_SQL, "src": src, "dst": dst,
    }).mappings().first()
    return row


def _too_long(longueur_m: float, args) -> bool:
    """Vrai si le tracé routier dépasse la longueur maximale admissible."""
    return longueur_m > args.max_line_km * 1000.0


def _route_fallback(conn, src: int, x: float, y: float):
    """Cherche un sommet atteignable proche de (x, y) et trace le chemin jusqu'à lui."""
    cands = conn.execute(text(f"""
        SELECT array_agg(id) AS ids FROM {SCHEMA}.road_vertex
        WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(:x,:y),4326)::geography, :rad)
    """), {"x": x, "y": y, "rad": FALLBACK_RADIUS_M}).scalar()
    if not cands:
        return None, None
    best = conn.execute(text(f"""
        SELECT end_vid, agg_cost FROM pgr_dijkstra(:edges, :src, :cands)
        WHERE agg_cost = (SELECT min(agg_cost) FROM pgr_dijkstra(:edges, :src, :cands))
    """), {"edges": EDGES_SQL, "src": src, "cands": cands}).mappings().first()
    if best is None:
        return None, None
    return _route(conn, src, best["end_vid"]), best["end_vid"]


def _compute_lines_agg(conn, agg, args) -> dict:
    """Calcule le réseau MT/BT d'une agglomération (ses lignes sont remplacées).

    * MT : plus court chemin arborescent (SPT) depuis le poste source vers
      toutes les sous-stations, découpé en tronçons aux points de branchement ;
      chaque tronçon devient une ligne MT, regroupée par « feeder ».
    * BT : un tronc par poste (vers le bâtiment le plus éloigné de son
      groupe), puis un épi vers chaque bâtiment situé à plus de
      `--bt-seuil-epi` mètres du tronc.

    Renvoie un récapitulatif des lignes et du linéaire.
    """
    aid = agg["agglomeration_id"]
    for table in ("derive", "ligne"):
        conn.execute(text(f"DELETE FROM {SCHEMA}.{table} WHERE agglomeration_id = :agg"),
                     {"agg": aid})
    # Index KNN indispensable à l'affectation des bâtiments aux postes.
    conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_noeud_geom "
                      f"ON {SCHEMA}.noeud USING gist (geom)"))

    source = conn.execute(text(f"""
        SELECT id, vertex_id, ST_X(geom) AS cx, ST_Y(geom) AS cy
        FROM {SCHEMA}.noeud
        WHERE agglomeration_id = :agg AND role = 'source'
        LIMIT 1
    """), {"agg": aid}).mappings().first()
    if source is None:
        log.warning("Agglomération %d : poste source absent, ignorée.", aid)
        return {"lignes": 0, "mt": 0, "bt": 0, "epis": 0, "lineaire": 0.0}

    postes = conn.execute(text(f"""
        SELECT id, vertex_id, zone_id, n_menages,
               ST_X(geom) AS cx, ST_Y(geom) AS cy
        FROM {SCHEMA}.noeud
        WHERE agglomeration_id = :agg AND role = 'sous_station'
          AND vertex_id IS NOT NULL
        ORDER BY id
    """), {"agg": aid}).mappings().all()
    if not postes:
        log.warning("Agglomération %d : aucune sous-station, ignorée.", aid)
        return {"lignes": 0, "mt": 0, "bt": 0, "epis": 0, "lineaire": 0.0}

    src_v = int(source["vertex_id"])
    dsts = "{" + ",".join(str(int(p["vertex_id"])) for p in postes) + "}"

    # ------------------------------------------------------------------
    # 1. Arbre MT : plus court chemin arborescent (SPT) depuis la source.
    # ------------------------------------------------------------------
    spt_rows = conn.execute(text(f"""
        WITH res AS (
            SELECT * FROM pgr_dijkstra(:edges, :src, CAST(:dst AS bigint[]))
        ),
        arr AS (
            SELECT end_vid, node, edge, agg_cost,
                   lead(node) OVER (PARTITION BY end_vid ORDER BY path_seq) AS child,
                   lead(agg_cost) OVER (PARTITION BY end_vid ORDER BY path_seq) AS child_cost
            FROM res
        )
        SELECT DISTINCT ON (child) child, node AS parent, edge, child_cost
        FROM arr
        WHERE child IS NOT NULL AND edge >= 0
        ORDER BY child, child_cost
    """), {"edges": EDGES_SQL, "src": src_v, "dst": dsts}).mappings().all()

    # parent / enfants de l'arbre (sommets routiers)
    parent: dict[int, tuple[int, int]] = {}
    children: dict[int, list[tuple[int, int]]] = {}
    for r in spt_rows:
        child, par, edge = int(r["child"]), int(r["parent"]), int(r["edge"])
        parent[child] = (par, edge)
        children.setdefault(par, []).append((child, edge))

    atteints = set(parent)
    absents = [p for p in postes if int(p["vertex_id"]) not in atteints]
    if absents:
        log.warning("Agglomération %d : %d sous-stations non atteignables depuis "
                    "la source (tronçon MT absent).", aid, len(absents))

    # Points de coupure : source, postes, et tout sommet de degré >= 3.
    poste_v = {int(p["vertex_id"]) for p in postes}
    cut = {src_v} | poste_v | {v for v, kids in children.items() if len(kids) >= 2}

    # Jonctions : nœuds électriques insérés (points de branchement MT).
    jonctions = sorted(v for v in cut if len(children.get(v, [])) >= 2
                       and v != src_v and v not in poste_v)
    if jonctions:
        conn.execute(text(f"""
            INSERT INTO {SCHEMA}.noeud
                (agglomeration_id, role, type_poste, tension_kv, vertex_id, geom)
            SELECT :agg, 'jonction', 'jonction', :tension, v.id, v.geom
            FROM {SCHEMA}.road_vertex v
            WHERE v.id = ANY(CAST(:ids AS bigint[]))
        """), {"agg": aid, "tension": _tension_mt(args),
               "ids": "{" + ",".join(str(v) for v in jonctions) + "}"})

    # Table de correspondance sommet → nœud électrique.
    noeud_par_sommet: dict[int, int] = {}
    noeud_xy: dict[int, tuple[float, float]] = {}
    for r in conn.execute(text(f"""
            SELECT id, vertex_id, ST_X(geom) AS x, ST_Y(geom) AS y
            FROM {SCHEMA}.noeud
            WHERE agglomeration_id = :agg AND vertex_id IS NOT NULL
              AND role IN ('source', 'sous_station', 'jonction')
            ORDER BY CASE role WHEN 'source' THEN 0
                               WHEN 'sous_station' THEN 1 ELSE 2 END, id
        """), {"agg": aid}).mappings().all():
        vid = int(r["vertex_id"])
        noeud_par_sommet.setdefault(vid, int(r["id"]))
        noeud_xy.setdefault(vid, (float(r["x"]), float(r["y"])))

    # Chaînes (tronçons) entre deux points de coupure : on descend de chaque
    # point de coupure jusqu'au suivant.
    chaines: list[dict] = []
    for c in cut:
        for child, edge in children.get(c, []):
            aretes = [(c, child, edge)]
            cur = child
            while cur not in cut:
                kids = children.get(cur, [])
                if len(kids) != 1:
                    break
                child, edge = kids[0]
                aretes.append((cur, child, edge))
                cur = child
            if c in noeud_par_sommet and cur in noeud_par_sommet:
                chaines.append({"start": c, "end": cur, "aretes": aretes})

    # Chaque arête appartient à un seul tronçon : index inverse.
    chaine_par_arete = {a[2]: i for i, ch in enumerate(chaines) for a in ch["aretes"]}

    # Ménages desservis en aval de chaque tronçon (somme des postes du sous-arbre).
    menages_aval = [0] * len(chaines)
    for p in postes:
        v = int(p["vertex_id"])
        while v != src_v and v in parent:
            par, edge = parent[v]
            i = chaine_par_arete.get(edge)
            if i is not None:
                menages_aval[i] += int(p["n_menages"] or 0)
            v = par

    # Feeder : branche de premier niveau issue de la source (1, 2, 3, ...).
    def source_child(v: int) -> int:
        while v in parent and parent[v][0] != src_v:
            v = parent[v][0]
        return v

    feeder_index: dict[int, int] = {}
    rows = []
    for i, ch in enumerate(chaines):
        fc = source_child(ch["aretes"][0][1])
        if fc not in feeder_index:
            feeder_index[fc] = len(feeder_index) + 1
        for seq, (par_v, child_v, edge) in enumerate(ch["aretes"]):
            rows.append({
                "cid": i, "seq": seq, "e": edge, "pv": par_v, "cv": child_v,
                "sn": noeud_par_sommet[ch["start"]],
                "en": noeud_par_sommet[ch["end"]],
                "feeder": feeder_index[fc],
                "menages": menages_aval[i],
            })

    conn.execute(text("DROP TABLE IF EXISTS _chaine"))
    conn.execute(text("""
        CREATE TEMP TABLE _chaine (
            chain_id integer, seq integer, edge_id bigint,
            parent_v bigint, child_v bigint,
            start_noeud integer, end_noeud integer,
            feeder_id integer, menages integer
        )
    """))
    if rows:
        conn.execute(text(
            "INSERT INTO _chaine VALUES "
            "(:cid,:seq,:e,:pv,:cv,:sn,:en,:feeder,:menages)"), rows)
        conn.execute(text(f"""
            INSERT INTO {SCHEMA}.ligne
                (agglomeration_id, noeud_depart, noeud_arrivee, zone_id,
                 type_ligne, troncon_id, feeder_id, tension_kv, longueur_m,
                 longueur_geom_m, nb_aretes, n_menages, statut,
                 longueur_refusee_m, detour_ratio, geom)
            SELECT :agg, s.start_noeud, s.end_noeud, NULL, 'MT',
                   s.chain_id, s.feeder_id, :tension, s.len_raw,
                   ST_Length(s.geom::geography), s.nb, s.menages,
                   'routable', 0, NULL, s.geom
            FROM (
                SELECT c.chain_id, c.start_noeud, c.end_noeud, c.feeder_id,
                       c.menages,
                       sum(ST_Length(r.geom::geography)) AS len_raw,
                       count(*)::int AS nb,
                       ST_MakeLine(
                           CASE WHEN r.source = c.parent_v
                                THEN ST_SetPoint(
                                        ST_SetPoint(r.geom, 0, vs.geom),
                                        ST_NPoints(r.geom) - 1, vt.geom)
                                ELSE ST_SetPoint(
                                        ST_SetPoint(ST_Reverse(r.geom), 0, vt.geom),
                                        ST_NPoints(r.geom) - 1, vs.geom)
                           END ORDER BY c.seq) AS geom
                FROM _chaine c
                JOIN {SCHEMA}.road r ON r.id = c.edge_id
                JOIN {SCHEMA}.road_vertex vs ON vs.id = r.source
                JOIN {SCHEMA}.road_vertex vt ON vt.id = r.target
                GROUP BY c.chain_id, c.start_noeud, c.end_noeud,
                         c.feeder_id, c.menages
            ) s
        """), {"agg": aid, "tension": _tension_mt(args)})

    # Postes hors d'atteinte : liaison MT directe depuis le nœud du réseau le
    # plus proche (tracé à construire, signalé comme tel).
    liens_directs: list[dict] = []
    if absents:
        arbre_v = [v for v in noeud_xy if v == src_v or v in parent]
        n_directs = 0
        for p in absents:
            vid = int(p["vertex_id"])
            if vid not in noeud_par_sommet or not arbre_v:
                continue
            near = min(arbre_v, key=lambda v: (noeud_xy[v][0] - p["cx"]) ** 2
                                            + (noeud_xy[v][1] - p["cy"]) ** 2)
            conn.execute(text(f"""
                INSERT INTO {SCHEMA}.ligne
                    (agglomeration_id, noeud_depart, noeud_arrivee, zone_id,
                     type_ligne, troncon_id, feeder_id, tension_kv, longueur_m,
                     longueur_geom_m, nb_aretes, n_menages, statut,
                     longueur_refusee_m, detour_ratio, geom)
                SELECT :agg, :dep, :arr, NULL, 'MT', :troncon, NULL, :tension,
                       ST_Length(g::geography), ST_Length(g::geography), 0, :n,
                       'liaison_directe', 0, NULL, g
                FROM (SELECT ST_MakeLine(
                          ST_SetSRID(ST_MakePoint(:x1,:y1),4326),
                          ST_SetSRID(ST_MakePoint(:x2,:y2),4326)) AS g) t
            """), {"agg": aid, "dep": noeud_par_sommet[near],
                   "arr": noeud_par_sommet[vid],
                   "troncon": len(chaines) + 1 + n_directs,
                   "tension": _tension_mt(args), "n": int(p["n_menages"] or 0),
                   "x1": noeud_xy[near][0], "y1": noeud_xy[near][1],
                   "x2": p["cx"], "y2": p["cy"]})
            liens_directs.append({"vid": vid, "pid": noeud_par_sommet[near]})
            n_directs += 1
        if n_directs:
            log.info("Agglomération %d : %d liaisons MT directes (postes "
                     "non atteignables par la voirie).", aid, n_directs)

    # parent_noeud_id : nœud amont de chaque poste / jonction.
    conn.execute(text("DROP TABLE IF EXISTS _parent_noeud"))
    conn.execute(text("CREATE TEMP TABLE _parent_noeud (vid bigint, parent_id integer)"))
    liens = list(liens_directs)
    for v in cut:
        if v == src_v or v not in noeud_par_sommet:
            continue
        cur = parent.get(v, (None, -1))[0]
        while cur is not None and cur not in cut:
            cur = parent.get(cur, (None, -1))[0]
        if cur is not None and cur in noeud_par_sommet:
            liens.append({"vid": v, "pid": noeud_par_sommet[cur]})
    if liens:
        conn.execute(text("INSERT INTO _parent_noeud VALUES (:vid, :pid)"), liens)
        conn.execute(text(f"""
            UPDATE {SCHEMA}.noeud n SET parent_noeud_id = p.parent_id
            FROM _parent_noeud p
            WHERE n.agglomeration_id = :agg AND n.vertex_id = p.vid
        """), {"agg": aid})

    # ------------------------------------------------------------------
    # 2. Desserte BT : arbre de plus courts chemins depuis chaque poste vers
    #    les sommets routiers de ses bâtiments (réseau BT le long des rues).
    # ------------------------------------------------------------------
    conn.execute(text("DROP TABLE IF EXISTS _bat"))
    conn.execute(text(f"""
        CREATE TEMP TABLE _bat AS
        SELECT ST_Transform(r.geometry, 4326) AS g,
               r.geometry AS g33,
               COALESCE(r.area_in_meters, 0) AS area
        FROM {SCHEMA}.perimetre p
        JOIN {BUILDINGS_TABLE} r
          ON r.geometry && ST_Transform(p.geom, 32733)
         AND ST_Contains(ST_Transform(p.geom, 32733), r.geometry)
        WHERE p.agglomeration_id = :agg
    """), {"agg": aid})
    conn.execute(text("DROP TABLE IF EXISTS _bat_poste"))
    conn.execute(text(f"""
        CREATE TEMP TABLE _bat_poste AS
        SELECT n.id AS noeud_id, n.zone_id, b.g, b.g33
        FROM _bat b
        CROSS JOIN LATERAL (
            SELECT id, zone_id FROM {SCHEMA}.noeud
            WHERE agglomeration_id = :agg AND role = 'sous_station'
            ORDER BY zone_centroid <-> b.g LIMIT 1
        ) n
    """), {"agg": aid})
    conn.execute(text("CREATE INDEX ON _bat_poste (noeud_id)"))
    # Les bâtiments sont accrochés au réseau de la composante de leur poste :
    # chaque tronc BT reste ainsi connecté à son poste. Le KNN est fait
    # composante par composante sur une petite table indexée (sinon le filtre
    # sur la composante force un balayage complet des sommets).
    conn.execute(text("DROP TABLE IF EXISTS _bat_vert"))
    conn.execute(text(f"""
        CREATE TEMP TABLE _bat_vert
            (noeud_id integer, zone_id integer, g geometry, vertex_id bigint)
    """))
    comps = conn.execute(text(f"""
        SELECT DISTINCT c.component
        FROM {SCHEMA}.noeud n
        JOIN {SCHEMA}.road_component c ON c.node = n.vertex_id
        WHERE n.agglomeration_id = :agg AND n.role = 'sous_station'
    """), {"agg": aid}).scalars().all()
    for comp_id in comps:
        conn.execute(text("DROP TABLE IF EXISTS _cv"))
        conn.execute(text(f"""
            CREATE TEMP TABLE _cv AS
            SELECT v.id, v.geom FROM {SCHEMA}.road_vertex v
            JOIN {SCHEMA}.road_component c ON c.node = v.id
            WHERE c.component = :c
        """), {"c": comp_id})
        conn.execute(text("CREATE INDEX ON _cv USING gist (geom)"))
        conn.execute(text(f"""
            INSERT INTO _bat_vert (noeud_id, zone_id, g, vertex_id)
            SELECT bp.noeud_id, bp.zone_id, bp.g, v.id
            FROM _bat_poste bp
            JOIN {SCHEMA}.noeud n ON n.id = bp.noeud_id
            JOIN {SCHEMA}.road_component pc ON pc.node = n.vertex_id
            CROSS JOIN LATERAL (
                SELECT id FROM _cv ORDER BY geom <-> bp.g LIMIT 1) v
            WHERE pc.component = :c
        """), {"c": comp_id})
    conn.execute(text(f"""
        INSERT INTO _bat_vert (noeud_id, zone_id, g, vertex_id)
        SELECT bp.noeud_id, bp.zone_id, bp.g, v.id
        FROM _bat_poste bp
        JOIN {SCHEMA}.noeud n ON n.id = bp.noeud_id
        LEFT JOIN {SCHEMA}.road_component pc ON pc.node = n.vertex_id
        CROSS JOIN LATERAL (
            SELECT id FROM {SCHEMA}.road_vertex ORDER BY geom <-> bp.g
            LIMIT 1) v
        WHERE pc.component IS NULL
    """), {"agg": aid})
    conn.execute(text("CREATE INDEX ON _bat_vert (noeud_id)"))

    groupes = conn.execute(text("""
        SELECT noeud_id, min(zone_id) AS zone_id, count(*)::int AS n,
               array_agg(DISTINCT vertex_id) AS vids
        FROM _bat_vert GROUP BY noeud_id
    """)).mappings().all()

    posts_xy = {int(p["id"]): p for p in postes}
    n_bt_route = n_bt_direct = 0
    for g in groupes:
        pid = int(g["noeud_id"])
        p = posts_xy.get(pid)
        if p is None:
            continue
        conn.execute(text(f"UPDATE {SCHEMA}.noeud SET n_menages = :n "
                          f"WHERE id = :pid"), {"n": int(g["n"]), "pid": pid})
        vids = "{" + ",".join(str(int(v)) for v in g["vids"]) + "}"
        arbre = conn.execute(text(f"""
            WITH res AS (
                SELECT * FROM pgr_dijkstra(:edges, :src, CAST(:dst AS bigint[]))
            ),
            arr AS (
                SELECT end_vid, node, edge, agg_cost,
                       lead(node) OVER (PARTITION BY end_vid ORDER BY path_seq) AS child,
                       lead(agg_cost) OVER (PARTITION BY end_vid ORDER BY path_seq)
                           AS child_cost
                FROM res
            ),
            spt AS (
                SELECT DISTINCT ON (child) child, node AS parent, edge
                FROM arr WHERE child IS NOT NULL AND edge >= 0
                ORDER BY child, child_cost
            )
            SELECT ST_Multi(ST_Collect(
                       CASE WHEN r.source = s.parent
                            THEN ST_SetPoint(
                                    ST_SetPoint(r.geom, 0, vs.geom),
                                    ST_NPoints(r.geom) - 1, vt.geom)
                            ELSE ST_SetPoint(
                                    ST_SetPoint(ST_Reverse(r.geom), 0, vt.geom),
                                    ST_NPoints(r.geom) - 1, vs.geom)
                       END)) AS geom,
                   count(*)::int AS nb,
                   sum(ST_Length(r.geom::geography)) AS longueur
            FROM spt s
            JOIN {SCHEMA}.road r ON r.id = s.edge
            JOIN {SCHEMA}.road_vertex vs ON vs.id = r.source
            JOIN {SCHEMA}.road_vertex vt ON vt.id = r.target
        """), {"edges": EDGES_SQL, "src": int(p["vertex_id"]), "dst": vids}
        ).mappings().first()

        geom_hex = arbre["geom"] if arbre else None
        statut = "routable"
        if geom_hex is None:
            # Repli : liaison directe du poste vers le centroïde de son groupe.
            statut = "liaison_directe"
            n_bt_direct += 1
        else:
            n_bt_route += 1
        conn.execute(text(f"""
            INSERT INTO {SCHEMA}.ligne
                (agglomeration_id, noeud_depart, noeud_arrivee, zone_id, type_ligne,
                 troncon_id, feeder_id, tension_kv, longueur_m, longueur_geom_m,
                 nb_aretes, n_menages, statut, longueur_refusee_m, detour_ratio, geom)
            SELECT :agg, :dep, NULL, :zid, 'BT', NULL, NULL, :tension,
                   COALESCE(CAST(:len AS double precision), ST_Length(g::geography)),
                   ST_Length(g::geography), :nb, :n, :stat, 0, NULL, g
            FROM (SELECT CASE WHEN CAST(:geomhex AS text) IS NULL
                     THEN ST_Multi(ST_MakeLine(
                              ST_SetSRID(ST_MakePoint(:x1,:y1),4326),
                              ST_SetSRID(ST_MakePoint(x2,y2),4326)))
                     ELSE ST_GeomFromEWKB(decode(:geomhex, 'hex'))
                END AS g
                FROM (SELECT ST_X(ST_Centroid(ST_Collect(g))) AS x2,
                             ST_Y(ST_Centroid(ST_Collect(g))) AS y2
                      FROM _bat_vert WHERE noeud_id = :dep) c) t
        """), {"agg": aid, "dep": pid, "zid": int(g["zone_id"]),
               "tension": args.tension_bt,
               "len": (float(arbre["longueur"]) if arbre
                        and arbre["longueur"] is not None else None),
               "nb": int(arbre["nb"] or 0) if arbre else 0,
               "n": int(g["n"]), "stat": statut, "geomhex": geom_hex,
               "x1": float(p["cx"]), "y1": float(p["cy"])})
    log.info("Agglomération %d : troncs BT routés %d, directs %d.",
             aid, n_bt_route, n_bt_direct)

    # ------------------------------------------------------------------
    # 3. Épis BT : bâtiments à plus de --bt-seuil-epi du tronc.
    # ------------------------------------------------------------------
    n_epis = conn.execute(text(f"""
        INSERT INTO {SCHEMA}.derive
            (agglomeration_id, noeud_id, zone_id, longueur_m, d_tronc_m, geom)
        SELECT :agg, s.noeud_id, s.zone_id, ST_Length(s.epi::geography), s.d, s.epi
        FROM (
            SELECT bp.noeud_id, bp.zone_id,
                   ST_Distance(bp.g::geography,
                               COALESCE(l.geom, n.geom)::geography) AS d,
                   ST_MakeLine(
                       ST_ClosestPoint(COALESCE(l.geom, n.geom), bp.g),
                       bp.g) AS epi
            FROM _bat_vert bp
            JOIN {SCHEMA}.noeud n ON n.id = bp.noeud_id
            LEFT JOIN {SCHEMA}.ligne l
              ON l.agglomeration_id = :agg AND l.type_ligne = 'BT'
             AND l.noeud_depart = bp.noeud_id
        ) s
        WHERE s.d > :seuil AND s.epi IS NOT NULL
    """), {"agg": aid, "seuil": args.bt_seuil_epi}).rowcount

    # ------------------------------------------------------------------
    # 4. Synthèse.
    # ------------------------------------------------------------------
    stats = conn.execute(text(f"""
        SELECT type_ligne, count(*) AS n, COALESCE(sum(longueur_m), 0) AS lin
        FROM {SCHEMA}.ligne WHERE agglomeration_id = :agg
        GROUP BY type_ligne
    """), {"agg": aid}).mappings().all()
    s = {r["type_ligne"]: r for r in stats}
    mt = s.get("MT")
    bt = s.get("BT")
    menages = conn.execute(text(f"""
        SELECT count(*) FROM _bat_poste
    """)).scalar() or 0
    conn.execute(text(f"""
        INSERT INTO {SCHEMA}.agg_statut
            (agglomeration_id, zones, postes, jonctions, troncons_mt, troncs_bt,
             epis, km_mt, km_bt, menages, menages_raccordes, maj)
        VALUES (:agg,
                (SELECT count(*) FROM {SCHEMA}.zone WHERE agglomeration_id = :agg),
                :postes,
                (SELECT count(*) FROM {SCHEMA}.noeud
                  WHERE agglomeration_id = :agg AND role = 'jonction'),
                :mt_n, :bt_n, :epis, :mt_km, :bt_km, :menages, :menages, now())
        ON CONFLICT (agglomeration_id) DO UPDATE SET
            zones = EXCLUDED.zones, postes = EXCLUDED.postes,
            jonctions = EXCLUDED.jonctions, troncons_mt = EXCLUDED.troncons_mt,
            troncs_bt = EXCLUDED.troncs_bt, epis = EXCLUDED.epis,
            km_mt = EXCLUDED.km_mt, km_bt = EXCLUDED.km_bt,
            menages = EXCLUDED.menages, menages_raccordes = EXCLUDED.menages_raccordes,
            maj = now()
    """), {"agg": aid, "postes": len(postes),
           "mt_n": int(mt["n"] if mt else 0), "bt_n": int(bt["n"] if bt else 0),
           "epis": n_epis, "mt_km": float(mt["lin"] if mt else 0) / 1000.0,
           "bt_km": float(bt["lin"] if bt else 0) / 1000.0,
           "menages": int(menages)})

    log.info("Agglomération %d (%s) : %d postes, %d tronçons MT (%.1f km), "
             "%d troncs BT (%.1f km), %d épis, %d ménages desservis",
             aid, agg["nom"], len(postes), int(mt["n"] if mt else 0),
             float(mt["lin"] if mt else 0) / 1000.0,
             int(bt["n"] if bt else 0), float(bt["lin"] if bt else 0) / 1000.0,
             n_epis, int(menages))
    return {"lignes": int(mt["n"] if mt else 0) + int(bt["n"] if bt else 0),
            "mt": int(mt["n"] if mt else 0), "bt": int(bt["n"] if bt else 0),
            "epis": n_epis,
            "lineaire": float((mt["lin"] if mt else 0) + (bt["lin"] if bt else 0))}


def cmd_lines(args) -> int:
    engine = get_engine()
    with engine.begin() as conn:
        aggs = conn.execute(text(
            f"SELECT agglomeration_id, nom FROM {SCHEMA}.perimetre ORDER BY rang"
        )).mappings().all()
        if not aggs:
            log.error("Aucun périmètre : lancez d'abord « perimetre ».")
            return 1
        if args.only:
            retenus = [a for a in aggs if a["agglomeration_id"] in args.only]
            if not retenus:
                log.error("Aucune agglomération ne correspond à --only %s.",
                          " ".join(str(i) for i in args.only))
                return 1
            aggs = retenus

        # Sans --only la table est reconstruite (comportement historique) ;
        # avec --only elle est conservée pour les autres agglomérations.
        _migrate_schema(conn)
        _ensure_table(conn, f"{SCHEMA}.ligne", DDL_LIGNE, COLONNES_LIGNE,
                      recréer=not args.only)
        _ensure_functions(conn)

    # Une transaction par agglomération : une coupure de connexion ne perd
    # que le calcul en cours, qui est rejoué automatiquement.
    for agg in aggs:
        _etape_resiliente(engine, _compute_lines_agg, agg, args)

    with engine.connect() as conn:
        n = conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.ligne")).scalar()
    log.info("RÉUSSITE : %d lignes écrites dans %s.ligne.", n, SCHEMA)
    return 0



# --------------------------------------------------------------------------
# Calcul ciblé d'une agglomération (n'importe laquelle, tout le pays)
# --------------------------------------------------------------------------

def _perimetre_row(conn, agg_id: int):
    """Ligne de plan.perimetre d'une agglomération (None si absente)."""
    return conn.execute(text(f"""
        SELECT agglomeration_id, rang, nom, population, superficie,
               ST_X(centroid) AS cx, ST_Y(centroid) AS cy
        FROM {SCHEMA}.perimetre WHERE agglomeration_id = :a
    """), {"a": agg_id}).mappings().first()


def _ensure_perimetre_agg(conn, agg_id: int):
    """Crée au besoin le périmètre d'une agglomération (toutes provinces confondues).

    Renvoie True si le périmètre a été créé, False s'il existait déjà,
    None si l'agglomération n'existe pas dans plan.agglomeration.
    """
    if _perimetre_row(conn, agg_id) is not None:
        return False
    row = conn.execute(text(f"""
        SELECT a.id, COALESCE(NULLIF(a.nom, ''), t.nom) AS nom, a.superficie,
               a.geom, a.centroid, pop.population
        FROM {SCHEMA}.agglomeration a
        JOIN {SCHEMA}.territoire t ON t.id = a.territoire_id
        LEFT JOIN LATERAL (
            SELECT population FROM {SCHEMA}.agglomeration_pop p
            WHERE p.aggromeration_id = a.id ORDER BY annee DESC LIMIT 1
        ) pop ON true
        WHERE a.id = :a AND a.geom IS NOT NULL
    """), {"a": agg_id}).mappings().first()
    if row is None:
        return None
    rang = conn.execute(text(f"SELECT COALESCE(max(rang), 0) + 1 FROM {SCHEMA}.perimetre")).scalar()
    _insert_perimetre(conn, row, rang)
    log.info("Périmètre créé pour l'agglomération %d (%s) — rang %s.",
             agg_id, row["nom"], rang)
    return True


def _graph_covers_agg(conn, agg_id: int) -> bool:
    """Vrai si le graphe de routage contient des routes à l'intérieur du périmètre."""
    n = conn.execute(text(f"""
        SELECT count(*) FROM {SCHEMA}.road r
        JOIN {SCHEMA}.perimetre p ON p.agglomeration_id = :a
        WHERE r.geom && p.geom AND ST_Intersects(r.geom, p.geom)
    """), {"a": agg_id}).scalar()
    return bool(n)


def _extend_road_graph(conn, agg_id: int, args) -> dict:
    """Étend le graphe de routage à une agglomération sans détruire l'existant.

    Les sommets sont identifiés par leur cellule de grille (kx,ky) : les
    cellules déjà connues sont réutilisées, les nouvelles sont ajoutées avec
    des identifiants frais. Les arêtes sont celles du périmètre élargi de la
    marge, filtrées sur les mêmes classes de routes que le graphe d'origine.
    """
    div = _get_param(conn, "snap_div")
    if div:
        div = int(div)
    else:
        div = max(1, int(round(args.snap_tolerance / 111320.0 * 1e7)))
        log.warning("Grille de fusion non mémorisée : %d (%.0f m) déduit de --snap-tolerance.",
                    div, div * 1e-7 * 111320.0)
    margin = args.margin
    classes = _classes_routieres(conn, args) or []
    log.info("Extension du graphe : maille %d (%.0f m), marge %d m, classes %s",
             div, div * 1e-7 * 111320.0, margin, ", ".join(classes) or "toutes")

    # La cellule de grille (kx,ky) identifie un sommet de façon stable :
    # index d'unicité pour les jointures de l'extension.
    conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{SCHEMA}_road_vertex_cell "
                      f"ON {SCHEMA}.road_vertex (kx, ky)"))

    conn.execute(text("DROP TABLE IF EXISTS _stage_new"))
    run(conn, f"""
    CREATE TEMP TABLE _stage_new AS
    SELECT r.osm_id::bigint AS id, r.geometry, r.fclass, r.name, r.oneway,
           round(ST_X(ST_StartPoint(r.geometry)) * 1e7 / {div})::bigint AS kx_src,
           round(ST_Y(ST_StartPoint(r.geometry)) * 1e7 / {div})::bigint AS ky_src,
           round(ST_X(ST_EndPoint(r.geometry)) * 1e7 / {div})::bigint AS kx_tgt,
           round(ST_Y(ST_EndPoint(r.geometry)) * 1e7 / {div})::bigint AS ky_tgt
    FROM {ROADS_TABLE} r
    JOIN {SCHEMA}.perimetre p ON p.agglomeration_id = :a
    WHERE r.geometry && ST_Expand(p.geom, {margin / 111320.0})
      AND (CAST(:classes AS text[]) IS NULL OR r.fclass = ANY(CAST(:classes AS text[])))
    """, {"a": agg_id, "classes": classes or None},
        label="  Extraction des arêtes routières de l'agglomération")

    n_stage = conn.execute(text("SELECT count(*) FROM _stage_new")).scalar()
    log.info("  Arêtes candidates : %d", n_stage)
    if not n_stage:
        conn.execute(text("DROP TABLE IF EXISTS _stage_new"))
        return {"arêtes": 0, "sommets": 0}

    # Cellules de grille inconnues : elles deviennent de nouveaux sommets.
    conn.execute(text("DROP TABLE IF EXISTS _cells_new"))
    run(conn, f"""
    CREATE TEMP TABLE _cells_new AS
    WITH ends AS (
        SELECT kx_src AS kx, ky_src AS ky, ST_StartPoint(geometry) AS p FROM _stage_new
        UNION ALL
        SELECT kx_tgt, ky_tgt, ST_EndPoint(geometry) FROM _stage_new
    ),
    cells AS (
        SELECT kx, ky, avg(ST_X(p)) AS cx, avg(ST_Y(p)) AS cy
        FROM ends GROUP BY kx, ky
    )
    SELECT c.kx, c.ky,
           (SELECT e.p FROM ends e
            WHERE e.kx = c.kx AND e.ky = c.ky
            ORDER BY ST_Distance(e.p::geography,
                                ST_SetSRID(ST_MakePoint(c.cx, c.cy), 4326)::geography)
            LIMIT 1) AS geom
    FROM cells c
    WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.road_vertex v
                      WHERE v.kx = c.kx AND v.ky = c.ky)
    """, label="  Cellules de grille nouvelles")

    base_id = conn.execute(text(f"SELECT COALESCE(max(id), 0) FROM {SCHEMA}.road_vertex")).scalar()
    n_cells = conn.execute(text("SELECT count(*) FROM _cells_new")).scalar()
    if n_cells:
        conn.execute(text(f"""
            INSERT INTO {SCHEMA}.road_vertex (id, kx, ky, geom)
            SELECT :base + row_number() OVER (ORDER BY kx, ky), kx, ky, geom
            FROM _cells_new
        """), {"base": base_id})
    log.info("  Sommets ajoutés : %d", n_cells)

    # Index sur l'identifiant OSM : évite les doublons lors de l'extension
    # (plan.road est créée par CREATE TABLE AS, sans contrainte unique).
    conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_road_id "
                      f"ON {SCHEMA}.road (id)"))

    n_ajout = conn.execute(text(f"""
        INSERT INTO {SCHEMA}.road (id, source, target, cost, reverse_cost,
                                   geom, fclass, name, oneway)
        SELECT s.id, vs.id, vt.id,
               ST_Length(s.geometry::geography),
               CASE WHEN s.oneway = 'F' THEN -1
                    ELSE ST_Length(s.geometry::geography) END,
               s.geometry, s.fclass, s.name, s.oneway
        FROM _stage_new s
        JOIN {SCHEMA}.road_vertex vs ON vs.kx = s.kx_src AND vs.ky = s.ky_src
        JOIN {SCHEMA}.road_vertex vt ON vt.kx = s.kx_tgt AND vt.ky = s.ky_tgt
        WHERE vs.id <> vt.id AND ST_Length(s.geometry::geography) > 0
          AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.road r WHERE r.id = s.id)
    """)).rowcount
    log.info("  Arêtes ajoutées : %d", n_ajout)

    # Composantes connexes : recalculées sur l'ensemble du graphe.
    conn.execute(text(f"DELETE FROM {SCHEMA}.road_component"))
    run(conn, f"""
    INSERT INTO {SCHEMA}.road_component (node, component)
    SELECT node::bigint, component::bigint FROM pgr_connectedComponents(:edges)
    """, {"edges": EDGES_SQL}, label="  pgr_connectedComponents")

    conn.execute(text("DROP TABLE IF EXISTS _stage_new"))
    conn.execute(text("DROP TABLE IF EXISTS _cells_new"))
    return {"arêtes": n_ajout, "sommets": n_cells}


def etat_agglomeration(conn, agg_id: int) -> dict:
    """État de calcul d'une agglomération : périmètre, zones, postes, réseau."""
    etat = {
        "agglomeration_id": agg_id, "nom": None, "rang": None, "population": None,
        "perimetre": False, "zones": 0, "batiments": 0, "source": 0, "postes": 0,
        "jonctions": 0, "troncons_mt": 0, "troncs_bt": 0, "epis": 0,
        "lignes": 0, "ok": 0, "direct": 0, "lineaire": 0.0,
        "km_mt": 0.0, "km_bt": 0.0, "menages": 0,
        "orphelins": 0, "complete": False, "manques": [], "etapes": [],
    }
    per = _perimetre_row(conn, agg_id)
    if per is not None:
        etat["perimetre"] = True
        etat["nom"] = per["nom"]
        etat["rang"] = per["rang"]
        etat["population"] = per["population"]

    row = conn.execute(text(f"""
        SELECT count(*) AS n, COALESCE(sum(nb_batiments), 0) AS b
        FROM {SCHEMA}.zone WHERE agglomeration_id = :a
    """), {"a": agg_id}).mappings().first()
    etat["zones"] = int(row["n"] or 0)
    etat["batiments"] = int(row["b"] or 0)

    row = conn.execute(text(f"""
        SELECT count(*) FILTER (WHERE role = 'source') AS src,
               count(*) FILTER (WHERE role = 'sous_station') AS sub,
               count(*) FILTER (WHERE role = 'jonction') AS jon,
               COALESCE(sum(n_menages) FILTER (WHERE role = 'sous_station'), 0) AS men
        FROM {SCHEMA}.noeud WHERE agglomeration_id = :a
    """), {"a": agg_id}).mappings().first()
    etat["source"] = int(row["src"] or 0)
    etat["postes"] = int(row["sub"] or 0)
    etat["jonctions"] = int(row["jon"] or 0)
    etat["menages"] = int(row["men"] or 0)

    row = conn.execute(text(f"""
        SELECT count(*) FILTER (WHERE type_ligne = 'MT') AS mt,
               count(*) FILTER (WHERE type_ligne = 'BT') AS bt,
               count(*) FILTER (WHERE statut = 'routable') AS ok,
               count(*) FILTER (WHERE statut = 'liaison_directe') AS direct,
               COALESCE(sum(longueur_m) FILTER (WHERE type_ligne = 'MT'), 0) AS lin_mt,
               COALESCE(sum(longueur_m) FILTER (WHERE type_ligne = 'BT'), 0) AS lin_bt
        FROM {SCHEMA}.ligne WHERE agglomeration_id = :a
    """), {"a": agg_id}).mappings().first()
    etat["troncons_mt"] = int(row["mt"] or 0)
    etat["troncs_bt"] = int(row["bt"] or 0)
    etat["lignes"] = etat["troncons_mt"] + etat["troncs_bt"]
    etat["ok"] = int(row["ok"] or 0)
    etat["direct"] = int(row["direct"] or 0)
    etat["km_mt"] = float(row["lin_mt"] or 0.0) / 1000.0
    etat["km_bt"] = float(row["lin_bt"] or 0.0) / 1000.0
    etat["lineaire"] = float((row["lin_mt"] or 0.0) + (row["lin_bt"] or 0.0))

    a_derive = conn.execute(
        text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"{SCHEMA}.derive"}
    ).scalar()
    if a_derive:
        etat["epis"] = int(conn.execute(text(f"""
            SELECT count(*) FROM {SCHEMA}.derive WHERE agglomeration_id = :a
        """), {"a": agg_id}).scalar() or 0)

    etat["orphelins"] = conn.execute(text(f"""
        SELECT count(*) FROM {SCHEMA}.noeud n
        WHERE n.agglomeration_id = :a AND n.vertex_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.road_vertex v WHERE v.id = n.vertex_id)
    """), {"a": agg_id}).scalar() or 0

    manques = []
    if not etat["perimetre"]:
        manques.append("périmètre")
    if etat["zones"] == 0:
        manques.append("zones")
    if etat["source"] != 1:
        manques.append("poste source")
    if etat["postes"] == 0:
        manques.append("sous-stations")
    if etat["troncons_mt"] == 0:
        manques.append("tronçons MT")
    if etat["troncs_bt"] == 0:
        manques.append("troncs BT")
    if etat["orphelins"]:
        manques.append("graphe obsolète")
    etat["manques"] = manques
    etat["complete"] = not manques
    return etat


def _resume_agglomeration(etat: dict) -> str:
    """Résumé lisible d'une agglomération."""
    nom = etat["nom"] or "?"
    return (f"agglomération {etat['agglomeration_id']} ({nom}) — "
            f"{etat['zones']} zones, {etat['postes']} postes, "
            f"{etat['jonctions']} jonctions, {etat['troncons_mt']} tronçons MT "
            f"({etat['km_mt']:.1f} km), {etat['troncs_bt']} troncs BT "
            f"({etat['km_bt']:.1f} km), {etat['epis']} épis, "
            f"{etat['menages']} ménages")


def _etape_resiliente(engine, fn, *args, **kwargs):
    """Exécute une étape dans sa propre transaction et la rejoue si la
    connexion tombe (chaque étape est idempotente : elle remplace ses
    propres lignes avant d'écrire)."""
    erreurs = (OperationalError, InterfaceError)
    for tentative in range(1, 4):
        try:
            with engine.begin() as conn:
                # Un verrou détenu par une connexion abandonnée ne doit pas
                # bloquer l'étape indéfiniment : on échoue pour rejouer.
                conn.execute(text("SET LOCAL lock_timeout = '60s'"))
                conn.execute(text("SET LOCAL statement_timeout = '1800s'"))
                return fn(conn, *args, **kwargs)
        except erreurs as e:
            if tentative == 3:
                raise
            log.warning("Connexion perdue pendant l'étape (%s) — nouvelle "
                        "tentative %d/3.", str(e).splitlines()[0][:120], tentative + 1)
            time.sleep(2 * tentative)
    return None


def _args_par_defaut():
    """Jeu d'options par défaut pour l'appel programmatique."""
    return argparse.Namespace(
        min_area=0.0, eps=DBSCAN_EPS_M, minpoints=DBSCAN_MINPOINTS,
        max_bat_per_zone=MAX_BAT_PER_ZONE, tension_source=TENSION_SOURCE_KV,
        tension_distrib=TENSION_DISTRIB_KV, tension_mt=None,
        tension_bt=TENSION_BT_KV, max_menages_poste=MAX_MENAGES_POSTE,
        bt_seuil_epi=BT_SEUIL_EPI_M, max_troncon_km=MAX_TRONCON_KM,
        bt_sur_route=False, max_snap_m=1500.0,
        max_line_km=MAX_LINE_KM, direct_fallback=True, snap_tolerance=SNAP_TOLERANCE_M,
        margin=SNAP_MARGIN_M, road_classes=None, force=False,
        only=None,
    )


def _tension_mt(args) -> float:
    """Tension des tronçons MT (retombe sur --tension-distrib si absente)."""
    return float(getattr(args, "tension_mt", None) or args.tension_distrib)


def calculer_agglomeration(agg_id: int, force: bool = False, args=None) -> dict:
    """Calcule (au besoin) une agglomération : périmètre, zones, postes, lignes.

    Les étapes déjà faites et valides sont conservées ; seules les étapes
    manquantes ou périmées sont refaites, et uniquement pour l'agglomération
    visée : les autres ne sont jamais modifiées.

    Renvoie l'état final (voir `etat_agglomeration`).
    """
    args = args or _args_par_defaut()
    engine = get_engine()
    etapes: list[str] = []

    # 1. Périmètre : créé à partir des tables de référence s'il manque.
    def _etape_perimetre(conn):
        if force or _perimetre_row(conn, agg_id) is None:
            if _ensure_perimetre_agg(conn, agg_id) is None:
                raise ValueError(
                    f"Agglomération {agg_id} introuvable dans plan.agglomeration.")
            return "périmètre"
        return None

    r = _etape_resiliente(engine, _etape_perimetre)
    if r:
        etapes.append(r)

    # 2. Graphe routier : étendu si la zone n'est pas couverte.
    def _etape_graphe(conn):
        if force or not _graph_covers_agg(conn, agg_id):
            _extend_road_graph(conn, agg_id, args)
            return "graphe"
        return None

    r = _etape_resiliente(engine, _etape_graphe)
    if r:
        etapes.append(r)

    # 3. Zones de distribution.
    def _etape_zones(conn):
        etat = etat_agglomeration(conn, agg_id)
        if force or etat["zones"] == 0:
            _compute_zone_agg(conn, _perimetre_row(conn, agg_id), args)
            return "zones"
        return None

    r = _etape_resiliente(engine, _etape_zones)
    if r:
        etapes.append(r)

    # 4. Poste source et sous-stations.
    def _etape_postes(conn):
        etat = etat_agglomeration(conn, agg_id)
        if force or etat["source"] != 1 or etat["postes"] == 0:
            _compute_nodes_agg(conn, _perimetre_row(conn, agg_id), args)
            return "postes"
        return None

    r = _etape_resiliente(engine, _etape_postes)
    if r:
        etapes.append(r)

    # 5. Lignes de distribution.
    def _etape_lignes(conn):
        etat = etat_agglomeration(conn, agg_id)
        if force or etat["troncons_mt"] == 0 or etat["troncs_bt"] == 0:
            _compute_lines_agg(conn, _perimetre_row(conn, agg_id), args)
            return "lignes"
        return None

    r = _etape_resiliente(engine, _etape_lignes)
    if r:
        etapes.append(r)

    with engine.connect() as conn:
        etat = etat_agglomeration(conn, agg_id)
    etat["etapes"] = etapes
    return etat


def cmd_agg(args) -> int:
    """Calcule une agglomération si le calcul n'existe pas encore."""
    agg_id = args.id if getattr(args, "id", None) is not None else getattr(args, "id_opt", None)
    if agg_id is None:
        log.error("Identifiant manquant : usage « %s agg <id> ».",
                  Path(__file__).name)
        return 2

    engine = get_engine()
    with engine.begin() as conn:
        _migrate_schema(conn)
        _ensure_table(conn, f"{SCHEMA}.noeud", DDL_NOEUD, COLONNES_NOEUD,
                      recréer=False)
        _ensure_table(conn, f"{SCHEMA}.ligne", DDL_LIGNE, COLONNES_LIGNE,
                      recréer=False)
    with engine.connect() as conn:
        etat = etat_agglomeration(conn, agg_id)
        if not etat["perimetre"]:
            existe = conn.execute(text(
                f"SELECT nom FROM {SCHEMA}.agglomeration WHERE id = :a"), {"a": agg_id}
            ).first()
            if existe is None:
                log.error("Agglomération %d introuvable (ni dans plan.agglomeration, "
                          "ni dans plan.perimetre).", agg_id)
                disponibles = conn.execute(text(
                    f"SELECT agglomeration_id, nom FROM {SCHEMA}.perimetre ORDER BY rang"
                )).mappings().all()
                if disponibles:
                    log.info("Agglomérations déjà calculées : %s",
                             ", ".join(f"{r['agglomeration_id']} ({r['nom']})"
                                       for r in disponibles))
                return 1

        if etat["complete"] and not args.force:
            log.info("EXISTE : %s", _resume_agglomeration(etat))
            return 0

    etat = calculer_agglomeration(agg_id, force=args.force, args=args)
    if etat["etapes"]:
        log.info("CALCUL (%s) : %s", ", ".join(etat["etapes"]),
                 _resume_agglomeration(etat))
    else:
        log.info("CALCUL : %s", _resume_agglomeration(etat))
    return 0


# --------------------------------------------------------------------------
# Rapport
# --------------------------------------------------------------------------

def cmd_report(args) -> int:
    engine = get_engine()
    with engine.begin() as conn:
        _migrate_schema(conn)
    with engine.connect() as conn:
        a_derive = conn.execute(
            text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"{SCHEMA}.derive"}
        ).scalar()
        epis_sql = (f"(SELECT count(*) FROM {SCHEMA}.derive d "
                    f"WHERE d.agglomeration_id = p.agglomeration_id)") if a_derive else "0"
        rows = conn.execute(text(f"""
            SELECT p.rang, p.agglomeration_id, p.nom, p.population,
                   z.nb_zones, n.nb_postes, n.nb_jonctions, n.menages,
                   COALESCE(m.mt, 0) AS mt, COALESCE(b.bt, 0) AS bt,
                   COALESCE(m.km, 0) AS km_mt, COALESCE(b.km, 0) AS km_bt,
                   {epis_sql} AS epis
            FROM {SCHEMA}.perimetre p
            LEFT JOIN (
                SELECT agglomeration_id, count(*) AS nb_zones,
                       COALESCE(sum(nb_batiments), 0) AS bat
                FROM {SCHEMA}.zone GROUP BY agglomeration_id
            ) z ON z.agglomeration_id = p.agglomeration_id
            LEFT JOIN (
                SELECT agglomeration_id,
                       count(*) FILTER (WHERE role = 'sous_station') AS nb_postes,
                       count(*) FILTER (WHERE role = 'jonction') AS nb_jonctions,
                       COALESCE(sum(n_menages) FILTER (WHERE role = 'sous_station'), 0)
                           AS menages
                FROM {SCHEMA}.noeud GROUP BY agglomeration_id
            ) n ON n.agglomeration_id = p.agglomeration_id
            LEFT JOIN (
                SELECT agglomeration_id, count(*) AS mt, sum(longueur_m) / 1000.0 AS km
                FROM {SCHEMA}.ligne WHERE type_ligne = 'MT'
                GROUP BY agglomeration_id
            ) m ON m.agglomeration_id = p.agglomeration_id
            LEFT JOIN (
                SELECT agglomeration_id, count(*) AS bt, sum(longueur_m) / 1000.0 AS km
                FROM {SCHEMA}.ligne WHERE type_ligne = 'BT'
                GROUP BY agglomeration_id
            ) b ON b.agglomeration_id = p.agglomeration_id
            ORDER BY p.rang
        """)).mappings().all()

        if not rows:
            log.error("Aucune donnée de plan à résumer.")
            return 1

        log.info("")
        log.info("%-4s %-6s %-14s %9s %6s %7s %6s %6s %6s %5s %8s %9s %7s",
                 "Rang", "ID", "Nom", "Population", "Zones", "Postes", "Jonct.",
                 "Tr.MT", "Tr.BT", "Épis", "km MT", "km BT", "Ménages")
        log.info("-" * 112)
        tot = {"zones": 0, "postes": 0, "jonctions": 0, "mt": 0, "bt": 0,
               "epis": 0, "km_mt": 0.0, "km_bt": 0.0, "menages": 0}
        for r in rows:
            log.info("%-4d %-6d %-14s %9s %6d %7d %6d %6d %6d %5d %8.1f %9.1f %7d",
                     r["rang"], r["agglomeration_id"], (r["nom"] or "?")[:14],
                     r["population"], r["nb_zones"] or 0, r["nb_postes"] or 0,
                     r["nb_jonctions"] or 0, r["mt"], r["bt"], r["epis"],
                     r["km_mt"], r["km_bt"], r["menages"] or 0)
            tot["zones"] += r["nb_zones"] or 0
            tot["postes"] += r["nb_postes"] or 0
            tot["jonctions"] += r["nb_jonctions"] or 0
            tot["mt"] += r["mt"]
            tot["bt"] += r["bt"]
            tot["epis"] += r["epis"]
            tot["km_mt"] += r["km_mt"]
            tot["km_bt"] += r["km_bt"]
            tot["menages"] += r["menages"] or 0
        log.info("-" * 112)
        log.info("TOTAL : %d agglomérations, %d zones, %d postes, %d jonctions, "
                 "%d tronçons MT (%.1f km), %d troncs BT (%.1f km), %d épis, "
                 "%d ménages",
                 len(rows), tot["zones"], tot["postes"], tot["jonctions"],
                 tot["mt"], tot["km_mt"], tot["bt"], tot["km_bt"],
                 tot["epis"], tot["menages"])

        if args.export:
            import geopandas as gpd

            out = Path(args.export)
            out.parent.mkdir(parents=True, exist_ok=True)
            couches = [("noeud", "noeuds"), ("ligne", "lignes"), ("zone", "zones")]
            if a_derive:
                couches.append(("derive", "epis"))
            for table, label in couches:
                gdf = gpd.read_postgis(
                    f"SELECT * FROM {SCHEMA}.{table}", conn, geom_col="geom", crs=CRS_WGS84
                )
                if gdf.empty:
                    continue
                fmt = "GPKG" if out.suffix.lower() in (".gpkg", ".geopackage") else "GeoJSON"
                cible = out if fmt == "GeoJSON" else out.with_name(f"{out.stem}_{label}.gpkg")
                gdf.to_file(cible, driver=fmt, layer=label)
                log.info("Export %s → %s (%d entités)", label, cible.name, len(gdf))
    return 0

def cmd_plan(args) -> int:
    etapes = [
        ("agglomerations", cmd_agglomerations),
        ("perimetre", cmd_perimetre),
        ("roads-graph", cmd_roads_graph),
        ("cluster", cmd_cluster),
        ("nodes", cmd_nodes),
        ("lines", cmd_lines),
        ("report", cmd_report),
    ]
    for nom, func in etapes:
        log.info("=== Étape : %s ===", nom)
        rc = func(args)
        if rc != 0:
            log.error("Étape '%s' en échec, arrêt.", nom)
            return rc
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan de distribution électrique (Kwilu) : bâtiments + routes OSM → pgRouting",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    commun = argparse.ArgumentParser(add_help=False)
    commun.add_argument("--dry-run", action="store_true",
                         help="N'écrit rien (aperçu du traitement)")
    commun.add_argument("--province", default="Kwilu", help="Nom de la province (défaut : Kwilu)")
    commun.add_argument("--top", type=int, default=10, help="Nombre d'agglomérations (défaut : 10)")
    commun.add_argument("--backup", default=None, metavar="FICHIER",
                        help="Dump pg_dump source (défaut : backup_20261021_110312.sql)")
    commun.add_argument("--min-area", type=float, default=0.0,
                         help="Surface minimale d'un bâtiment pris en compte (m²)")
    commun.add_argument("--only", type=int, nargs="*", default=None,
                         help="Limiter le traitement à ces ids d'agglomération")
    commun.add_argument("--tension-source", type=float, default=TENSION_SOURCE_KV,
                         help="Tension du poste source en kV (défaut : 220)")
    commun.add_argument("--tension-distrib", type=float, default=TENSION_DISTRIB_KV,
                         help="Tension de distribution en kV (défaut : 30)")
    commun.add_argument("--tension-mt", type=float, default=None,
                         help="Tension des tronçons MT en kV (défaut : --tension-distrib)")
    commun.add_argument("--tension-bt", type=float, default=TENSION_BT_KV,
                         help="Tension de la desserte BT en kV (défaut : 0.4)")
    commun.add_argument("--max-menages-poste", type=int, default=MAX_MENAGES_POSTE,
                         help="Ménages (bâtiments) max par sous-station (défaut : 200)")
    commun.add_argument("--bt-seuil-epi", type=float, default=BT_SEUIL_EPI_M,
                         help="Distance au tronc BT au-delà de laquelle un épi est "
                              "tiré vers un bâtiment (m, défaut : 150)")
    commun.add_argument("--max-troncon-km", type=float, default=MAX_TRONCON_KM,
                         help="Longueur maximale d'un tronçon MT (km, défaut : 15)")
    commun.add_argument("--bt-sur-route", dest="bt_sur_route", action="store_true",
                         help="Router les épis BT sur le réseau routier (défaut : segment direct)")
    commun.add_argument("--export", default=None, metavar="FICHIER",
                         help="Exporter noeuds/lignes/zones (GeoJSON ou GPKG)")

    p_agg = sub.add_parser("agglomerations", parents=[commun],
                            help="Backup pg_dump → plan.province/territoire/agglomeration/pop")
    p_agg.set_defaults(func=cmd_agglomerations)

    p_per = sub.add_parser("perimetre", parents=[commun],
                            help="Top N agglomérations de la province → plan.perimetre")
    p_per.set_defaults(func=cmd_perimetre)

    p_rg = sub.add_parser("roads-graph", parents=[commun],
                           help="Graphe de routage → plan.road / _vertex / _component")
    p_rg.add_argument("--snap-tolerance", type=float, default=SNAP_TOLERANCE_M,
                      help="Fusion des sommets routiers en mètres (défaut : 222)")
    p_rg.add_argument("--margin", type=float, default=SNAP_MARGIN_M,
                      help="Marge autour du périmètre en mètres (défaut : 2000)")
    p_rg.add_argument("--road-classes", default=None,
                      help="Classes de routes retenues pour le tracé "
                           "(défaut : celles du dernier calcul ; vide = toutes)")
    p_rg.set_defaults(func=cmd_roads_graph)

    p_cl = sub.add_parser("cluster", parents=[commun],
                           help="Bâtiments → zones de distribution (plan.zone)")
    p_cl.add_argument("--eps", type=float, default=DBSCAN_EPS_M,
                      help="Rayon DBSCAN en mètres (défaut : 200)")
    p_cl.add_argument("--minpoints", type=int, default=DBSCAN_MINPOINTS,
                      help="Points minimaux DBSCAN (défaut : 10)")
    p_cl.add_argument("--max-bat-per-zone", type=int, default=MAX_BAT_PER_ZONE,
                      help="Bâtiments max par zone avant raffinement KMeans (défaut : 500)")
    p_cl.set_defaults(func=cmd_cluster)

    p_no = sub.add_parser("nodes", parents=[commun],
                           help="Poste source + sous-stations → plan.noeud")
    p_no.add_argument("--max-snap-m", type=float, default=1500.0,
                      help="Distance max d'accrochage d'une sous-station sur le réseau (m, "
                           "défaut : 1500)")
    p_no.set_defaults(func=cmd_nodes)

    p_li = sub.add_parser("lines", parents=[commun],
                           help="pgr_dijkstra → plan.ligne")
    p_li.add_argument("--no-direct-fallback", dest="direct_fallback",
                      action="store_false", default=True,
                      help="Ne pas créer de liaison directe si le routage échoue")
    p_li.add_argument("--max-line-km", type=float, default=MAX_LINE_KM,
                      help="Longueur maximale d'une ligne depuis le poste source "
                           "(km, défaut : 25)")
    p_li.set_defaults(func=cmd_lines)

    p_re = sub.add_parser("report", parents=[commun],
                           help="Synthèse du plan (console + export optionnel)")
    p_re.set_defaults(func=cmd_report)

    p_ag = sub.add_parser("agg", parents=[commun],
                           help="Calcule une agglomération (sous-stations + lignes) "
                                "si le calcul n'existe pas encore")
    p_ag.add_argument("id", type=int, nargs="?", default=None,
                       help="Identifiant de l'agglomération")
    p_ag.add_argument("--id", dest="id_opt", type=int, default=None,
                      help="Identifiant de l'agglomération (alternative à la position)")
    p_ag.add_argument("--force", action="store_true",
                      help="Recalculer même si le calcul existe déjà")
    p_ag.add_argument("--eps", type=float, default=DBSCAN_EPS_M,
                      help="Rayon DBSCAN en mètres (défaut : 200)")
    p_ag.add_argument("--minpoints", type=int, default=DBSCAN_MINPOINTS,
                      help="Points minimaux DBSCAN (défaut : 10)")
    p_ag.add_argument("--max-bat-per-zone", type=int, default=MAX_BAT_PER_ZONE,
                      help="Bâtiments max par zone avant raffinement KMeans (défaut : 500)")
    p_ag.add_argument("--max-snap-m", type=float, default=1500.0,
                      help="Distance max d'accrochage d'une sous-station sur le réseau (m)")
    p_ag.add_argument("--max-line-km", type=float, default=MAX_LINE_KM,
                      help="Longueur maximale d'une ligne depuis le poste source (km)")
    p_ag.add_argument("--no-direct-fallback", dest="direct_fallback",
                      action="store_false", default=True,
                      help="Ne pas créer de liaison directe si le routage échoue")
    p_ag.add_argument("--snap-tolerance", type=float, default=SNAP_TOLERANCE_M,
                      help="Fusion des sommets routiers en mètres (défaut : 150)")
    p_ag.add_argument("--margin", type=float, default=SNAP_MARGIN_M,
                      help="Marge autour du périmètre en mètres (défaut : 2000)")
    p_ag.add_argument("--road-classes", default=None,
                      help="Classes de routes retenues pour le tracé "
                           "(défaut : celles du dernier calcul ; vide = toutes)")
    p_ag.set_defaults(func=cmd_agg)

    p_pl = sub.add_parser("plan", parents=[commun],
                           help="Enchaîne toutes les étapes")
    p_pl.add_argument("--snap-tolerance", type=float, default=SNAP_TOLERANCE_M)
    p_pl.add_argument("--margin", type=float, default=SNAP_MARGIN_M)
    p_pl.add_argument("--eps", type=float, default=DBSCAN_EPS_M)
    p_pl.add_argument("--minpoints", type=int, default=DBSCAN_MINPOINTS)
    p_pl.add_argument("--max-bat-per-zone", type=int, default=MAX_BAT_PER_ZONE)
    p_pl.add_argument("--max-snap-m", type=float, default=1500.0)
    p_pl.add_argument("--road-classes", default=None,
                      help="Classes de routes retenues pour le tracé "
                           "(défaut : celles du dernier calcul ; vide = toutes)")
    p_pl.add_argument("--no-direct-fallback", dest="direct_fallback",
                      action="store_false", default=True)
    p_pl.add_argument("--max-line-km", type=float, default=MAX_LINE_KM,
                      help="Longueur maximale d'une ligne depuis le poste source "
                           "(km, défaut : 25)")
    p_pl.set_defaults(func=cmd_plan)

    args = parser.parse_args()
    setup_logging()
    try:
        return args.func(args) or 0
    except Exception:
        log.exception("Échec du plan de distribution")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
