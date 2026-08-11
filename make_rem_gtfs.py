#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_rem_gtfs.py — Fabrique un GTFS statique pour le REM.

Pourquoi fabriquer plutot que telecharger : le REM ne publie aucun GTFS
reference dans les catalogues ouverts (verifie dans le catalogue MobilityData
et sur Donnees Quebec). Mais il n'en a pas vraiment besoin : le REM ne roule
pas a l'horaire nominatif, il roule a la cadence. Un GTFS base sur
frequencies.txt est donc une representation fidele, pas un bricolage.

Source des donnees : rem.info/fr/se-deplacer/stations-du-reseau et
rem.info/fr/se-deplacer/horaire-de-service.

CE QUI EST VERIFIE (rem.info)          CE QUI EST ESTIME (a calibrer)
-------------------------------------  -------------------------------------
la liste des 23 stations en service    les temps de parcours entre stations
la scission du tronc a Bois-Franc      les bornes des periodes de pointe
les cadences par troncon                le temps d'arret en station
les premiers et derniers departs        les coordonnees (absentes ici)
les jours feries

Usage :
  python3 make_rem_gtfs.py                      # 23 stations en service
  python3 make_rem_gtfs.py --include-future     # + les 3 stations a venir
  python3 make_rem_gtfs.py --out gtfs_rem.zip

Puis :
  python3 stm_phase0.py build gtfs_stm.zip gtfs_rem.zip
"""

from __future__ import annotations

import argparse
import datetime as dt
import zipfile

# ---------------------------------------------------------------------------
# Topologie. Verifiee sur rem.info : le tronc va de Brossard a Bois-Franc,
# puis se scinde vers Deux-Montagnes et vers Anse-a-l'Orme.
#
# Chaque entree : (nom, en_service)
# ---------------------------------------------------------------------------

TRUNK = [
    ("Brossard", True),
    ("Du Quartier", True),
    ("Panama", True),
    ("Île-des-Sœurs", True),
    ("Griffintown – Bernard-Landry", False),   # station projetee
    ("Gare Centrale", True),
    ("McGill", True),
    ("Édouard-Montpetit", True),
    ("Canora", True),
    ("Ville-de-Mont-Royal", True),
    ("Côte-de-Liesse", True),
    ("Montpellier", True),
    ("Du Ruisseau", True),
    ("Bois-Franc", True),
]

BRANCH_DEUX_MONTAGNES = [
    ("Sunnybrooke", True),
    ("Pierrefonds-Roxboro", True),
    ("Île-Bigras", True),
    ("Sainte-Dorothée", True),
    ("Grand-Moulin", True),
    ("Deux-Montagnes", True),
]

BRANCH_ANSE_A_LORME = [
    ("Des Sources", True),
    ("Fairview–Pointe-Claire", True),
    ("Kirkland", True),
    ("Marie-Curie", False),                    # en construction
    ("YUL-Aéroport-Montréal-Trudeau", False),  # en construction, 2027
    ("Anse-à-l'Orme", True),
]

# ---------------------------------------------------------------------------
# Temps de parcours, en secondes. ESTIMES — a calibrer.
#
# Le script affiche les durees bout-a-bout obtenues : comparez-les avec Chrono
# ou Transit et ajustez. Une erreur de 30 s sur un segment est invisible sur un
# panneau ou une station occupe 12 mm.
#
# Cle : nom de la station de depart du segment.
# ---------------------------------------------------------------------------

RUN_TIMES = {
    # Tronc sud : deux longues traversees (le fleuve, puis le centre-ville)
    "Brossard": 90, "Du Quartier": 90, "Panama": 240, "Île-des-Sœurs": 300,
    "Griffintown – Bernard-Landry": 90,
    # Tronc nord : le tunnel du mont Royal puis la remontee vers Bois-Franc
    "Gare Centrale": 120, "McGill": 150, "Édouard-Montpetit": 120,
    "Canora": 90, "Ville-de-Mont-Royal": 150, "Côte-de-Liesse": 120,
    "Montpellier": 90, "Du Ruisseau": 120,
    # Branche Deux-Montagnes (ancien trace du train de banlieue)
    "Bois-Franc": 150, "Sunnybrooke": 150, "Pierrefonds-Roxboro": 180,
    "Île-Bigras": 150, "Sainte-Dorothée": 150, "Grand-Moulin": 150,
    # Branche Anse-a-l'Orme (Ouest-de-l'Île)
    "Des Sources": 180, "Fairview–Pointe-Claire": 180, "Kirkland": 150,
    "Marie-Curie": 150, "YUL-Aéroport-Montréal-Trudeau": 210,
}

DEFAULT_RUN_TIME = 180
SCALE = 1.0   # facteur d'etalonnage global, pilote par --scale

# --- Modele cinematique (option --derive-run-times) -----------------------
# Une fois les coordonnees connues, les temps de parcours n'ont plus besoin
# d'etre devines : ils se deduisent de la distance et d'un profil de vitesse.
# Un train qui accelere a `a` jusqu'a `v` puis freine met, sur une distance d,
# le temps d/v + v/a  (le second terme est la penalite d'arret). Si le segment
# est trop court pour atteindre v, il met 2*racine(d/a).
V_MAX = 27.8        # m/s, soit 100 km/h : vitesse de pointe annoncee du REM
ACCEL = 1.1         # m/s^2, typique d'un metro leger
TORTUOSITY = 1.05   # la voie est plus longue que la distance a vol d'oiseau
DWELL = 25   # ESTIME. Le REM est automatise, les arrets sont courts et reguliers.

# ---------------------------------------------------------------------------
# Cadences, verifiees sur rem.info. Les bornes de pointe sont estimees.
# La cadence du tronc n'est PAS declaree ici : elle emerge de la superposition
# des deux lignes, ce qui est exactement ainsi que le reseau fonctionne.
# ---------------------------------------------------------------------------

PEAK_WINDOWS = [("06:30:00", "09:30:00"), ("15:30:00", "18:30:00")]

HEADWAYS = {
    #  ligne              pointe  hors pointe   (secondes)
    "DM":  {"peak": 420, "off": 900},   # 7 min / 15 min
    "AAO": {"peak": 840, "off": 900},   # 14 min / 15 min
}

# Premiers et derniers departs, verifies sur rem.info.
# {type_de_jour: {ligne: {direction: (premier, dernier)}}}
SERVICE_SPAN = {
    "SEM": {"DM":  {"0": ("05:37:00", "24:51:00"), "1": ("05:30:00", "24:32:00")},
            "AAO": {"0": ("05:30:00", "24:36:00"), "1": ("05:36:00", "24:34:00")}},
    "SAM": {"DM":  {"0": ("05:30:00", "25:21:00"), "1": ("05:30:00", "24:58:00")},
            "AAO": {"0": ("05:37:00", "25:06:00"), "1": ("05:37:00", "25:06:00")}},
    "DIM": {"DM":  {"0": ("05:30:00", "24:51:00"), "1": ("05:30:00", "24:28:00")},
            "AAO": {"0": ("05:37:00", "24:36:00"), "1": ("05:37:00", "24:36:00")}},
}

# direction_id 0 = depuis Brossard ; 1 = vers Brossard.
ROUTES = {
    "DM":  ("A4", "REM – Brossard / Deux-Montagnes", BRANCH_DEUX_MONTAGNES),
    "AAO": ("A3", "REM – Brossard / Anse-à-l'Orme", BRANCH_ANSE_A_LORME),
}

HOLIDAYS_2026 = ["20260403", "20260406", "20260624", "20260701",
                 "20260907", "20261012", "20261225", "20261226"]

FEED_START, FEED_END = "20260101", "20270101"


COORDS = {}


def load_coords(path: str) -> dict:
    """Lit un CSV station,latitude,longitude (voir osm_to_coords.py)."""
    import csv
    table = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("station") or "").strip()
            lat = (row.get("latitude") or "").strip()
            lon = (row.get("longitude") or "").strip()
            if name and lat and lon:
                try:
                    table[name] = (float(lat), float(lon))
                except ValueError:
                    print(f"  coordonnees illisibles pour {name!r}, ignorees")
    return table


def write_coords_template(path: str, include_future: bool) -> None:
    """Sort un squelette a remplir a la main, si Overpass n'est pas une option."""
    import csv
    names = [n for n, live in TRUNK + BRANCH_DEUX_MONTAGNES + BRANCH_ANSE_A_LORME
             if live or include_future]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["station", "latitude", "longitude"])
        for name in names:
            writer.writerow([name, "", ""])
    print(f"{path} ecrit : {len(names)} lignes a completer.")
    print("Puis : python3 make_rem_gtfs.py --coords " + path)


def haversine(a: tuple, b: tuple) -> float:
    """Distance en metres entre deux couples (latitude, longitude)."""
    import math
    radius = 6371000.0
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    inner = (math.sin((lat2 - lat1) / 2) ** 2
             + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(inner))


def kinematic_time(distance: float) -> int:
    """Temps de parcours d'un segment, en secondes, depuis sa longueur."""
    import math
    effective = distance * TORTUOSITY
    threshold = V_MAX ** 2 / ACCEL      # distance pour atteindre V_MAX et freiner
    if effective < threshold:
        seconds = 2 * math.sqrt(effective / ACCEL)
    else:
        seconds = effective / V_MAX + V_MAX / ACCEL
    return max(30, int(round(seconds)))


def derive_run_times(order: list) -> dict:
    """Temps de parcours deduits des coordonnees, pour une suite de stations."""
    table = {}
    for origin, target in zip(order, order[1:]):
        if origin in COORDS and target in COORDS:
            table[origin] = kinematic_time(haversine(COORDS[origin], COORDS[target]))
    return table


def secs(value: str) -> int:
    h, m, s = (int(p) for p in value.split(":"))
    return h * 3600 + m * 60 + s


def hms(total: int) -> str:
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


TRANSLIT = {"à": "a", "â": "a", "ä": "a", "é": "e", "è": "e", "ê": "e",
            "ë": "e", "î": "i", "ï": "i", "ô": "o", "ö": "o", "ù": "u",
            "û": "u", "ü": "u", "ç": "c", "œ": "oe",
            "'": "-", "’": "-", "–": "-", "—": "-", " ": "-"}


def station_id(name: str) -> str:
    """Identifiant stable et lisible, sans accent ni espace."""
    flat = "".join(TRANSLIT.get(char, char) for char in name.lower())
    while "--" in flat:
        flat = flat.replace("--", "-")
    return "REM-" + flat.strip("-").upper()


def build(out_path: str, include_future: bool, scale: float = 1.0,
          coords_path: str | None = None, derive: bool = False) -> None:
    global SCALE, COORDS
    SCALE = scale
    if coords_path:
        COORDS = load_coords(coords_path)
        print(f"Coordonnees chargees : {len(COORDS)} stations")
    if scale != 1.0:
        print(f"Facteur d'etalonnage : x{scale}")

    def keep(entry):
        return entry[1] or include_future

    trunk = [n for n, live in TRUNK if keep((n, live))]
    branches = {code: [n for n, live in stops if keep((n, live))]
                for code, (_, _, stops) in
                ((c, ROUTES[c]) for c in ROUTES)}

    all_stations = list(dict.fromkeys(trunk + branches["DM"] + branches["AAO"]))

    if derive:
        if not COORDS:
            print("  --derive-run-times exige --coords : ignore.")
        else:
            derived = {}
            for code in ROUTES:
                derived.update(derive_run_times(trunk + branches[code]))
            changed = sum(1 for k, v in derived.items()
                          if abs(v - RUN_TIMES.get(k, DEFAULT_RUN_TIME)) > 20)
            RUN_TIMES.update(derived)
            print(f"  temps de parcours deduits des distances : {len(derived)} segments"
                  f" ({changed} nettement differents de l'estimation)")

    print(f"Stations retenues : {len(all_stations)}"
          f"{' (stations futures incluses)' if include_future else ''}")

    agency = ["agency_id,agency_name,agency_url,agency_timezone,agency_lang",
              "REM,Réseau express métropolitain,https://rem.info,America/Montreal,fr"]

    # route_type = 1 (metro) : le REM est un metro leger automatise, et ca permet
    # de le fusionner avec le GTFS de la STM sans changer --route-types.
    routes = ["route_id,route_short_name,route_long_name,route_type,route_color"]
    for code, (short, long_name, _) in ROUTES.items():
        colour = "C82896" if code == "DM" else "8C46DC"
        routes.append(f"{short},{short},{long_name},1,{colour}")

    stops = ["stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station"]
    missing_coords = []
    for name in all_stations:
        sid = station_id(name)
        # Les coordonnees ne sont pas publiees en lot par le REM. Elles ne
        # servent qu'au dessin du panneau, pas a la simulation : on accepte
        # 0,0 en signalant le manque. Voir osm_to_coords.py.
        lat, lon = COORDS.get(name, (0.0, 0.0))
        if name not in COORDS:
            missing_coords.append(name)
        stops.append(f'{sid},"{name}",{lat},{lon},1,')
        for code in ROUTES:
            if name in trunk or name in branches[code]:
                stops.append(f'{sid}-{ROUTES[code][0]},"{name}",{lat},{lon},0,{sid}')

    trips = ["trip_id,route_id,service_id,direction_id,trip_headsign"]
    stop_times = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]
    frequencies = ["trip_id,start_time,end_time,headway_secs,exact_times"]

    journey_times = {}

    for code, (short, _, _) in ROUTES.items():
        full_line = trunk + branches[code]
        for day_type in SERVICE_SPAN:
            for direction in ("0", "1"):
                ordered = full_line if direction == "0" else list(reversed(full_line))
                trip_id = f"{short}-{day_type}-{direction}"
                trips.append(f'{trip_id},{short},{day_type},{direction},"{ordered[-1]}"')

                first, last = SERVICE_SPAN[day_type][code][direction]
                clock = secs(first)
                for seq, name in enumerate(ordered, start=1):
                    sid = f"{station_id(name)}-{short}"
                    stop_times.append(f"{trip_id},{hms(clock)},{hms(clock + DWELL)},"
                                      f"{sid},{seq}")
                    clock += DWELL
                    if seq < len(ordered):
                        origin = name if direction == "0" else ordered[seq]
                        base = RUN_TIMES.get(origin, DEFAULT_RUN_TIME)
                        clock += int(round(base * SCALE))
                journey_times[(short, direction)] = clock - DWELL - secs(first)

                for window_start, window_end in _service_windows(first, last):
                    headway = HEADWAYS[code]["peak" if _is_peak(window_start) else "off"]
                    frequencies.append(f"{trip_id},{window_start},{window_end},"
                                       f"{headway},0")

    calendar = ["service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
                "sunday,start_date,end_date",
                f"SEM,1,1,1,1,1,0,0,{FEED_START},{FEED_END}",
                f"SAM,0,0,0,0,0,1,0,{FEED_START},{FEED_END}",
                f"DIM,0,0,0,0,0,0,1,{FEED_START},{FEED_END}"]

    # Les jours feries suivent l'horaire du jour avec une cadence hors pointe.
    # On les rattache donc au service dominical.
    calendar_dates = ["service_id,date,exception_type"]
    for date in HOLIDAYS_2026:
        weekday = dt.datetime.strptime(date, "%Y%m%d").weekday()
        regular = "SEM" if weekday < 5 else ("SAM" if weekday == 5 else "DIM")
        if regular != "DIM":
            calendar_dates.append(f"{regular},{date},2")
            calendar_dates.append(f"DIM,{date},1")

    files = {"agency.txt": agency, "routes.txt": routes, "stops.txt": stops,
             "trips.txt": trips, "stop_times.txt": stop_times,
             "frequencies.txt": frequencies, "calendar.txt": calendar,
             "calendar_dates.txt": calendar_dates}
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, lines in files.items():
            zf.writestr(name, "\n".join(lines) + "\n")

    _report(out_path, journey_times, missing_coords)


def _service_windows(first: str, last: str):
    """Decoupe l'amplitude de service en fenetres pointe / hors pointe."""
    start, end = secs(first), secs(last)
    cuts = {start, end}
    for peak_start, peak_end in PEAK_WINDOWS:
        for boundary in (secs(peak_start), secs(peak_end)):
            if start < boundary < end:
                cuts.add(boundary)
    ordered = sorted(cuts)
    return [(hms(a), hms(b)) for a, b in zip(ordered, ordered[1:]) if b > a]


def _is_peak(window_start: str) -> bool:
    moment = secs(window_start)
    return any(secs(a) <= moment < secs(b) for a, b in PEAK_WINDOWS)


def _report(out_path, journey_times, missing_coords):
    print(f"\n{out_path} ecrit.\n")

    print("Durees bout-a-bout obtenues (a comparer avec Chrono ou Transit) :")
    for (line, direction), duration in sorted(journey_times.items()):
        sens = "depuis Brossard" if direction == "0" else "vers Brossard"
        print(f"  {line} {sens:<16} {duration // 60} min {duration % 60:02d} s")
    print("  Trop long ou trop court ? Etalonnez d'un coup avec --scale :")
    print("    --scale 0.9  raccourcit tous les segments de 10 %")
    print("  Ajustez RUN_TIMES station par station seulement si un segment")
    print("  precis est manifestement faux.")

    peak = 1 / (HEADWAYS["DM"]["peak"] / 60) + 1 / (HEADWAYS["AAO"]["peak"] / 60)
    off = 1 / (HEADWAYS["DM"]["off"] / 60) + 1 / (HEADWAYS["AAO"]["off"] / 60)
    print("\nControle de coherence — cadence du tronc (Brossard <-> Bois-Franc) :")
    print(f"  en pointe     : {1 / peak:.1f} min   (rem.info annonce 3 min 30 a 6 min)")
    print(f"  hors pointe   : {1 / off:.1f} min   (rem.info annonce 7 min)")
    print("  Elle n'est pas codee : elle emerge de la superposition des deux lignes.")

    if missing_coords:
        print(f"\nAttention : {len(missing_coords)} stations sans coordonnees (0,0).")
        print("  Sans effet sur la simulation, mais indispensable pour dessiner")
        print("  le panneau. Deux facons de les obtenir :")
        print("    depuis OpenStreetMap : voir osm_to_coords.py")
        print("    a la main            : --coords-template rem_coords.csv")
        if len(missing_coords) <= 8:
            for name in missing_coords:
                print(f"      - {name}")
    else:
        print("\nToutes les stations ont des coordonnees.")


def main():
    parser = argparse.ArgumentParser(description="Fabrique un GTFS pour le REM")
    parser.add_argument("--out", default="gtfs_rem.zip")
    parser.add_argument("--include-future", action="store_true",
                        help="inclure Griffintown, Marie-Curie et YUL "
                             "(utile pour reserver des LED)")
    parser.add_argument("--coords", help="CSV station,latitude,longitude")
    parser.add_argument("--coords-template", metavar="FICHIER",
                        help="ecrire un squelette de coordonnees a remplir, "
                             "puis quitter")
    parser.add_argument("--derive-run-times", action="store_true",
                        help="deduire les temps de parcours des distances reelles "
                             "plutot que de la table estimee (exige --coords)")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="facteur applique a tous les temps de parcours, "
                             "pour etalonner d'un seul chiffre")
    args = parser.parse_args()
    if args.coords_template:
        write_coords_template(args.coords_template, args.include_future)
        return
    build(args.out, args.include_future, args.scale, args.coords,
          args.derive_run_times)


if __name__ == "__main__":
    main()
