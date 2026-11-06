"""Vérification de la table routes après ETL."""
from sqlalchemy import text

from etl import get_engine, setup_logging

setup_logging()
engine = get_engine()
with engine.connect() as conn:
    row = conn.execute(text(
        "SELECT count(*), ST_SRID(geometry), "
        "postgis_typmod_srid(a.atttypmod), "
        "(SELECT data_type FROM information_schema.columns "
        " WHERE table_name = 'gis_osm_roads_free' AND column_name = 'osm_id') "
        "FROM gis_osm_roads_free, pg_attribute a "
        "WHERE a.attrelid = 'gis_osm_roads_free'::regclass AND a.attname = 'geometry' "
        "GROUP BY 2, 3, 4"
    )).first()
    print(f"Lignes: {row[0]:,} | SRID géométrie: {row[1]} (typmod: {row[2]}) | osm_id: {row[3]}")
    extent = conn.execute(text(
        "SELECT round(ST_XMin(e)::numeric,4), round(ST_YMin(e)::numeric,4), "
        "round(ST_XMax(e)::numeric,4), round(ST_YMax(e)::numeric,4) "
        "FROM (SELECT ST_Extent(geometry) AS e FROM gis_osm_roads_free) s"
    )).first()
    print(f"Emprise WGS84: ({extent[0]}, {extent[1]}) → ({extent[2]}, {extent[3]})")
    sample = conn.execute(text(
        "SELECT osm_id, code, fclass, name FROM gis_osm_roads_free "
        "WHERE name IS NOT NULL LIMIT 3"
    )).fetchall()
    for r in sample:
        print(" ", tuple(r))
    conn.execute(text("DROP TABLE IF EXISTS batiments_rdc_test"))
    print("Table de test batiments_rdc_test supprimée.")
