# AGENTS.md — Modèle de distribution MT/BT (`plan_electricite.py`)

Document de reprise. **Chantier terminé le 22/10/2026** : les 11 agglomérations
ont été recalculées avec le modèle hiérarchique validé. Ce fichier sert de
mémoire (modèle, résultats, pièges) pour toute reprise ou extension.

## Objectif atteint

Modèle de distribution en schéma `plan` uniquement :

* zones DBSCAN/KMeans → **postes HTA/BT** plafonnés à `--max-menages-poste`
  (200, ratio de sécurité 0,85 → max observé 372 ménages) ;
* **réseau MT** = arbre de plus courts chemins depuis la source, coupé aux
  postes et aux jonctions (`troncon_id`, `feeder_id`, `parent_noeud_id`) ;
* **desserte BT** = un arbre SPT par poste le long de la voirie
  (`MultiLineString`) + **épis** (`plan.derive`) pour les bâtiments à plus de
  `--bt-seuil-epi` (150 m) du tronc.

Contraintes respectées : écriture limitée au schéma `plan`, `etl.py` et tables
applicatives intacts, aucune dépendance Python ajoutée, jamais de jointure
globale sur `batiments_rdc` (84,9 M lignes — a déjà fait redémarrer PostgreSQL).

## Résultats (validation du 22/10/2026)

| ID | Nom | Zones | Postes | Jonct. | Tr.MT | km MT | km BT | Épis | Ménages | max/poste | m BT/bât |
|----|-----|------:|-------:|-------:|------:|------:|------:|-----:|--------:|----------:|---------:|
| 22 | Kikwit | 171 | 582 | 142 | 679 | 326,7 | 2 418,9 | 4 528 | 83 840 | 372 | 28,9 |
| 1087 | Bandundu | 98 | 329 | 74 | 362 | 201,7 | 1 620,1 | 5 065 | 47 226 | 235 | 34,3 |
| 88 | Idiofa | 66 | 206 | 46 | 229 | 144,4 | 1 132,5 | 5 839 | 29 897 | 227 | 37,9 |
| 3664 | Idiofa | 20 | 65 | 9 | 45 | 25,1 | 151,3 | 3 091 | 9 378 | 220 | 16,1 |
| 3817 | Gungu | 24 | 74 | 0 | 48 | 138,7 | 896,8 | 4 605 | 10 348 | 285 | 86,7 |
| 87 | Bulungu | 30 | 96 | 42 | 113 | 55,2 | 233,3 | 1 188 | 13 176 | 215 | 17,7 |
| 3624 | Idiofa | 14 | 47 | 0 | 27 | 1,0 | 33,8 | 2 055 | 6 804 | 199 | 5,0 |
| 3783 | Gungu | 26 | 81 | 18 | 83 | 71,2 | 675,3 | 2 920 | 11 110 | 338 | 60,8 |
| 3520 | Masi-Manimba | 26 | 76 | 1 | 22 | 18,3 | 101,7 | 5 448 | 10 736 | 244 | 9,5 |
| 3719 | Idiofa | 30 | 99 | 12 | 90 | 50,2 | 579,5 | 3 001 | 14 071 | 250 | 41,2 |
| 32 | Kisangani | 386 | 1 236 | 180 | 1 176 | 723,7 | 2 994,2 | 26 307 | 176 769 | 319 | 16,9 |

TOTAL : 891 zones, 2 891 postes, 524 jonctions, 2 874 tronçons MT (1 756 km),
2 891 troncs BT (10 837 km), 64 047 épis, 413 355 ménages.

Contrôles passés :

* `sum(n_menages)` = nombre de bâtiments de l'agglomération (exact sur 87, 22,
  32, 88) et `plan.agg_statut` cohérent (mêmes totaux que `plan.ligne`) ;
* charge par poste équilibrée (moyenne 137–145, p90 ≈ 180, max 372) ;
* **MT = arbre parfait dans les 11 agglomérations** : nombre de sommets
  géométriques distincts = nombre de tronçons routables + 1 ;
* BT : `liaison_directe` uniquement pour les postes dont la composante routière
  est isolée (0 à 10 % des postes en ville, jusqu'à ~55 % en zone rurale non
  cartographiée) ;
* carte autonome : `visu/plan_kwilu.html` = **23,4 Mo** (< 25 Mo).

## Modèle tel qu'implémenté

1. **Postes** : découpage en sous-groupes ≤ 200 ménages, centroïde KMeans par
   sous-groupe. Accroche au sommet routier le plus proche, puis recalage dans la
   **composante de service** (celle qui porte le plus de postes) si la distance
   ≤ `--max-snap-m` (1 500 m). Pas de fusion : **plusieurs postes peuvent
   partager un sommet** (MT mutualisé, BT propre à chaque poste).
2. **Affectation des bâtiments** : KNN sur **`noeud.zone_centroid`** (et non sur
   le sommet routier) → charge équilibrée ; les postes restent affichés sur la
   voirie.
3. **BT** : les bâtiments s'accrochent à un sommet de la **composante de leur
   poste** (`_bat_vert`, calcul composante par composante via une table `_cv`
   indexée GIST — sinon le filtre force un balayage complet et 87 passait de
   1 min à 6 min). SPT `pgr_dijkstra(src → vids)` élagué aux cibles, agrégé en
   `MultiLineString`.
4. **Épis** : distance au tronc BT, `COALESCE(l.geom, n.geom)` + `ST_ClosestPoint`
   quand le poste n'a pas de tronc (tous ses bâtiments sur son propre sommet) :
   la desserte passe alors entièrement par les épis.
5. **MT** : un seul `pgr_dijkstra` multi-cibles depuis la source, `lead()` +
   `DISTINCT ON (child)` pour l'arbre, chaînage `ST_MakeLine` ordonné par `seq`,
   extrémités recalées sur les géométries de sommets. Postes hors d'atteinte :
   **liaison MT directe** (droite du centroïde du poste vers le nœud de l'arbre
   le plus proche, statut `liaison_directe`).

## Limites connues

* Voirie OSM très incomplète en zone rurale : dans les zones non couvertes, les
  bâtiments s'accrochent au même sommet que leur poste ⇒ tronc BT vide et
  desserte uniquement par épis (`liaison_directe` côté BT, normal) ; les postes
  sur composantes isolées reçoivent une liaison MT directe.
* Chaque poste tire son propre arbre BT : deux postes voisins peuvent emprunter
  les mêmes rues, donc `km BT` additionne des tronçons partagés (un tronc commun
  par feeder serait plus réaliste mais n'est pas modélisé).
* Ratio `m BT/bâtiment` très variable (5 m à 87 m) : il reflète la densité et la
  longueur des rues OSM, pas une erreur de calcul.
* Aucun calcul électrique (chute de tension, foisonnement, court-circuit).

## Reproduction / extension

```bash
export MSYS_NO_PATHCONV=1
cd /c/Users/PC/Documents/Anser_avenat/data
wsl.exe --cd /mnt/c/Users/PC/Documents/Anser_avenat/data ./venv/bin/python \
  plan_electricite.py nodes --only <id>
wsl.exe --cd /mnt/c/Users/PC/Documents/Anser_avenat/data ./venv/bin/python \
  plan_electricite.py lines --only <id>
```

`cmd_agg` / `cmd_lines` / `cmd_report` appellent `_migrate_schema` (colonne
`geom` générique, `derive`, `agg_statut`, index dont
`idx_plan_noeud_centroid` GIST sur `zone_centroid`).

La table `plan.ligne` fait ~5 800 lignes ; la carte :

```bash
wsl.exe ... visu_plan.py export   # → visu/plan_kwilu.html
```

Le tracé BT de la carte est simplifié (`ST_SimplifyPreserveTopology(geom,
0.00002)` ≈ 2 m, GeoJSON précision 5) pour rester sous 25 Mo.

Base : PostgreSQL 18 (172.21.240.1, `anser_route`), pgRouting 4.0.1,
PostGIS 3.6.2.

## Pièges déjà rencontrés

* `round(double precision, integer)` n'existe pas → caster `::numeric`.
* `CASE WHEN :param IS NULL` → `AmbiguousParameter` : utiliser
  `CAST(:param AS text) IS NULL` (et `CAST(:dst AS bigint[])` pour les tableaux).
* Heredocs bash > ~8 Ko tronqués → écrire les scripts dans
  `C:\Users\PC\AppData\Local\Temp\opencode\*.py` via l'outil `write`.
* `plan.ligne.geom` = `geometry(Geometry,4326)` (BT = `MultiLineString`),
  `plan.road.geom` = `LINESTRING`, `plan.road_vertex.geom` = `GEOMETRY`.
* `pgr_dijkstra(src → {src})` renvoie **0 ligne** ⇒ arbre vide : c'est le cas
  normal d'un poste dont tous les bâtiments sont sur son propre sommet (ne pas
  le traiter comme une erreur).
* `ST_Distance` sur des géométries en degrés donne des valeurs inutilisables :
  toujours `::geography` pour les mètres.
* Filtre sur la composante dans un KNN : sans table par composante indexée,
  le planificateur balaie tous les sommets (17 min pour 84 k bâtiments).

## Fichiers

* `plan_electricite.py` — code principal (`_compute_nodes_agg`,
  `_compute_lines_agg`, `_bat_poste`, `_bat_vert`, `_migrate_schema`, DDL,
  `cmd_agg/lines/report`).
* `patch_balance.py`, `patch_batvert.py` — correctifs appliqués (historique,
  idempotents par assertions ; ne plus relancer sur le code courant).
* `visu_plan.py`, `visu/template.html`, `visu/app.js`, `visu/style.css`,
  `visu/plan_kwilu.html` — carte Leaflet autonome.
* `README.md` — documentation utilisateur (modèle, options, tables, limites).
* `docs/` — deux documentations « offre », rendues en PDF par Chrome headless
  (`python docs/make_pdf.py`) : `ingenieur_logiciel.html/pdf` (8 p.) et
  `ingenieur_electricien.html/pdf` (7 p.), charte commune `docs/style.css`.
  Pied de page = **boîtes de marge `@page`** (Chrome ≥ 131) avec
  `counter(page)`/`counter(pages)` ; `@page :first` les masque sur la
  couverture. Un `position: fixed; bottom: 0` se cale sur la **boîte de
  contenu** et chevauche le texte : ne pas y revenir.
* `Offre_Technique_et_Financiere_ANSER.pdf` — cahier des charges.
* `AGENTS.md` — ce fichier.
