#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
osm_to_coords.py — Convertit un export Overpass en table de coordonnees REM.

Le REM ne publie pas ses coordonnees en lot, mais il est entierement
cartographie dans OpenStreetMap. Ce script prend l'export brut d'Overpass et
produit le CSV que make_rem_gtfs.py sait lire.

Marche a suivre
---------------
1. Aller sur https://overpass-turbo.eu
2. Coller cette requete et lancer :

     [out:json][timeout:180];
     rel(6169927);
     rel(r);
     node(r);
     out body;

3. Exporter le resultat : GeoJSON ou JSON brut, les deux sont acceptes
4. python3 osm_to_coords.py overpass.json

Si la requete echoue ou ne renvoie rien (serveur charge, ou la relation a change
d'identifiant), essayer la variante par zone geographique, sans recursion :

     [out:json][timeout:180];
     (
       node["railway"="station"]["station"="light_rail"]
         (45.35,-74.05,45.65,-73.40);
       node["railway"="station"]["network"~"REM",i]
         (45.35,-74.05,45.65,-73.40);
       node["railway"="station"]["operator"~"REM|métropolitain|CDPQ|Pulsar",i]
         (45.35,-74.05,45.65,-73.40);
     );
     out body;

En cas d'erreur « the server is probably too busy », changer de miroir dans les
reglages d'overpass-turbo (engrenage) :
https://overpass.kumi.systems/api/interpreter

Licence : les donnees OSM sont sous ODbL. Usage personnel sans souci ;
mentionner "© les contributeurs d'OpenStreetMap" si tu publies le panneau.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import unicodedata

# Les 23 stations en service, plus les 3 a venir. Doit rester aligne sur
# make_rem_gtfs.py.
EXPECTED = [
    "Brossard", "Du Quartier", "Panama", "Île-des-Sœurs",
    "Griffintown – Bernard-Landry", "Gare Centrale", "McGill",
    "Édouard-Montpetit", "Canora", "Ville-de-Mont-Royal", "Côte-de-Liesse",
    "Montpellier", "Du Ruisseau", "Bois-Franc", "Sunnybrooke",
    "Pierrefonds-Roxboro", "Île-Bigras", "Sainte-Dorothée", "Grand-Moulin",
    "Deux-Montagnes", "Des Sources", "Fairview–Pointe-Claire", "Kirkland",
    "Marie-Curie", "YUL-Aéroport-Montréal-Trudeau", "Anse-à-l'Orme",
]

# Formes rencontrees dans OSM qui ne correspondent pas mot pour mot.
ALIASES = {
    "gare centrale": "Gare Centrale",
    "central station": "Gare Centrale",
    "roxboro-pierrefonds": "Pierrefonds-Roxboro",
    "fairview pointe-claire": "Fairview–Pointe-Claire",
    "fairview-pointe-claire": "Fairview–Pointe-Claire",
    "ile-des-soeurs": "Île-des-Sœurs",
    "ile-bigras": "Île-Bigras",
    "yul-aeroport-montreal-trudeau": "YUL-Aéroport-Montréal-Trudeau",
    "aeroport-montreal-trudeau": "YUL-Aéroport-Montréal-Trudeau",
    "griffintown-bernard-landry": "Griffintown – Bernard-Landry",
    "griffintown bernard-landry": "Griffintown – Bernard-Landry",
    "mont-royal": "Ville-de-Mont-Royal",
    "ville-de-mont-royal": "Ville-de-Mont-Royal",
}


def fold(text: str) -> str:
    """Normalise pour comparaison : sans accent, sans ponctuation, minuscule."""
    stripped = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in stripped if unicodedata.category(c) != "Mn")
    stripped = stripped.replace("œ", "oe").replace("Œ", "OE")
    keep = [c.lower() if c.isalnum() else " " for c in stripped]
    return " ".join("".join(keep).split())


FOLDED_EXPECTED = {fold(name): name for name in EXPECTED}
FOLDED_ALIASES = {fold(k): v for k, v in ALIASES.items()}


def match_station(osm_name: str):
    key = fold(osm_name)
    if key in FOLDED_EXPECTED:
        return FOLDED_EXPECTED[key]
    if key in FOLDED_ALIASES:
        return FOLDED_ALIASES[key]
    # Dernier recours : correspondance par prefixe, souvent suffisante
    # ("Station Brossard", "Brossard REM").
    for folded, official in FOLDED_EXPECTED.items():
        if key.startswith(folded) or folded.startswith(key):
            return official
    return None


def extract(payload: dict):
    """Sort {nom_officiel: (lat, lon)} depuis un export Overpass.

    Accepte les deux formats que propose overpass-turbo : le JSON brut
    (cle "elements") et le GeoJSON (cle "features"). Le bouton d'export met le
    GeoJSON en avant, autant lire les deux.
    """
    if "features" in payload and "elements" not in payload:
        return _extract_geojson(payload)
    return _extract_overpass(payload)


def _extract_geojson(payload: dict):
    found, unknown = {}, []
    for feature in payload.get("features", []):
        props = feature.get("properties") or {}
        # overpass-turbo range parfois les etiquettes sous "tags"
        tags = props.get("tags") if isinstance(props.get("tags"), dict) else props
        name = tags.get("name") or tags.get("name:fr") or ""
        if not name:
            continue
        lat, lon = _geojson_point(feature.get("geometry") or {})
        if lat is None:
            continue
        official = match_station(name)
        if official is None:
            unknown.append(name)
            continue
        found.setdefault(official, (round(lat, 6), round(lon, 6)))
    return found, unknown


def _geojson_point(geometry: dict):
    """Centre d'une geometrie GeoJSON, quel que soit son type."""
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return None, None
    # GeoJSON stocke [longitude, latitude], dans cet ordre.
    if kind == "Point":
        return float(coords[1]), float(coords[0])
    # Pour une ligne ou un polygone, on prend le barycentre des sommets.
    points = []

    def flatten(node):
        if (isinstance(node, (list, tuple)) and len(node) == 2
                and all(isinstance(v, (int, float)) for v in node)):
            points.append(node)
        elif isinstance(node, (list, tuple)):
            for child in node:
                flatten(child)

    flatten(coords)
    if not points:
        return None, None
    return (sum(p[1] for p in points) / len(points),
            sum(p[0] for p in points) / len(points))


def _extract_overpass(payload: dict):
    found, unknown = {}, []
    for element in payload.get("elements", []):
        tags = element.get("tags") or {}
        name = tags.get("name") or tags.get("name:fr") or ""
        if not name:
            continue
        lat = element.get("lat")
        lon = element.get("lon")
        if lat is None and "center" in element:      # way ou relation
            lat = element["center"].get("lat")
            lon = element["center"].get("lon")
        if lat is None or lon is None:
            continue

        official = match_station(name)
        if official is None:
            unknown.append(name)
            continue
        # Plusieurs noeuds peuvent porter le meme nom (quais, entrees) :
        # on garde le premier, l'ecart est de quelques dizaines de metres.
        found.setdefault(official, (round(float(lat), 6), round(float(lon), 6)))
    return found, unknown


def main():
    parser = argparse.ArgumentParser(
        description="Convertit un export Overpass en coordonnees REM")
    parser.add_argument("overpass_json", help="fichier exporte depuis Overpass")
    parser.add_argument("--out", default="rem_coords.csv")
    parser.add_argument("--include-future", action="store_true",
                        help="exiger aussi les 3 stations a venir")
    args = parser.parse_args()

    try:
        with open(args.overpass_json, encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"lecture impossible : {exc}")

    found, unknown = extract(payload)

    future = {"Griffintown – Bernard-Landry", "Marie-Curie",
              "YUL-Aéroport-Montréal-Trudeau"}
    wanted = EXPECTED if args.include_future else [n for n in EXPECTED
                                                   if n not in future]

    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["station", "latitude", "longitude"])
        for name in wanted:
            lat, lon = found.get(name, ("", ""))
            writer.writerow([name, lat, lon])

    filled = sum(1 for n in wanted if n in found)
    print(f"{args.out} ecrit : {filled}/{len(wanted)} stations renseignees")

    missing = [n for n in wanted if n not in found]
    if missing:
        print("\nA completer a la main dans le CSV :")
        for name in missing:
            print(f"  - {name}")
        print("\n  Cherchez la station sur openstreetmap.org, clic droit sur le")
        print("  point, « Afficher l'adresse » donne les coordonnees.")

    if unknown:
        print(f"\n{len(unknown)} nom(s) OSM non reconnu(s), ignore(s) :")
        for name in sorted(set(unknown))[:15]:
            print(f"  ? {name}")
        print("  Si l'un d'eux est bien une station REM, ajoutez-le a ALIASES.")

    if filled == len(wanted):
        print("\nComplet. Ensuite :")
        print(f"  python3 make_rem_gtfs.py --coords {args.out}")


if __name__ == "__main__":
    main()
