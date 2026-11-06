"""Vérification de l'environnement DB — usage : python check_db.py"""
from sqlalchemy import text

from etl import get_engine, setup_logging

setup_logging()
engine = get_engine()
with engine.connect() as conn:
    print("PG:", conn.execute(text("SELECT version()")).scalar()[:55])
    postgis = conn.execute(
        text("SELECT extversion FROM pg_extension WHERE extname = 'postgis'")
    ).scalar()
    pgr = conn.execute(
        text("SELECT extversion FROM pg_extension WHERE extname = 'pgrouting'")
    ).scalar()
    print(f"PostGIS: {postgis or 'ABSENT'} | pgRouting: {pgr or 'ABSENT'}")
    tables = conn.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
    ).fetchall()
    print("Tables existantes:", [t[0] for t in tables] or "aucune")
    for table in ("batiments_rdc", "gis_osm_roads_free"):
        exists = conn.execute(
            text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}
        ).scalar()
        if exists:
            n = conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar()
            print(f"  {table}: {n:,} lignes")
