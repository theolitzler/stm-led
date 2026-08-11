#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stm_phase0.py — Phase 0 du panneau LED du metro de Montreal.

Objectif : savoir exactement de quelles donnees on dispose AVANT d'acheter
une seule LED.

Commandes
---------
  probe     Interroge le GTFS-realtime de la STM et cherche du metro dedans.
  etat      Recupere l'etat de service (bus / metro / ascenseurs) via l'API i3.
  build     Compile le GTFS statique en horaire compact (timetable.json).
  live      Affiche en continu la position simulee des rames.
  diagnose  Mesure la plausibilite des donnees (dwell, intervalles).
  snapshot  Sort l'etat des LED en JSON (une entree par station).
  export-stations  CSV station -> index de LED, pour le cablage.

Prerequis
---------
  pip install requests gtfs-realtime-bindings
  export STM_API_KEY="votre_cle"

Exemples
--------
  python3 stm_phase0.py probe
  python3 stm_phase0.py etat
  python3 stm_phase0.py build gtfs_stm.zip
  python3 stm_phase0.py live
  python3 stm_phase0.py snapshot --at 08:45
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import zipfile
import zlib
from collections import defaultdict

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

API_RT = "https://api.stm.info/pub/od/gtfs-rt/ic/v2"
API_I3 = "https://api.stm.info/pub/od/i3/v2/messages"

# route_type GTFS : 1 = metro / subway. On s'appuie sur ca plutot que sur des
# route_id codes en dur, c'est plus robuste aux changements de la STM.
ROUTE_TYPE_METRO = "1"

# Utilise seulement par `probe` quand aucun timetable.json n'est disponible.
METRO_ROUTE_IDS_FALLBACK = {"1", "2", "4", "5"}

LINE_NAMES = {"1": "Verte", "2": "Orange", "4": "Jaune", "5": "Bleue"}
LINE_COLORS = {  # approximations des couleurs officielles, en RGB
    "1": (0, 142, 79),
    "2": (239, 129, 34),
    "4": (255, 210, 0),
    "5": (0, 131, 202),
}

# Couleurs de repli pour les lignes hors metro STM (REM, exo...).
# Palette de repli pour les reseaux hors STM. Choisie a l'ecart du vert /
# orange / jaune / bleu du metro : sur un panneau physique, deux lignes de
# teinte voisine sont indiscernables a un metre de distance.
# Le rouge est volontairement absent : il est reserve a la signalisation des
# interruptions de service. Un reseau rouge en permanence rendrait l'alerte
# illisible sur le panneau.
FALLBACK_COLORS = [(0, 200, 190),    # turquoise (couleur de marque du REM)
                   (200, 40, 150),   # magenta
                   (140, 70, 220),   # violet
                   (255, 120, 180),  # rose
                   (235, 235, 235),  # blanc
                   (120, 200, 255)]  # bleu pale

ALERT_COLOR = (220, 50, 50)   # reserve : interruption de service


def assign_line_colours(line_keys):
    """(voir docstring ci-dessous)"""
    """Attribue une couleur par ligne, une fois pour toutes, a la compilation.

    Assigner a la volee par hachage ne garantit pas que deux lignes different :
    on parcourt donc la palette dans l'ordre des cles triees, ce qui est a la
    fois deterministe et sans collision.
    """
    colours, spare = {}, 0
    for key in sorted(line_keys):
        route_id = key.split("~")[-1]
        if key.startswith("STM~") and route_id in LINE_COLORS:
            colours[key] = list(LINE_COLORS[route_id])
        else:
            colours[key] = list(FALLBACK_COLORS[spare % len(FALLBACK_COLORS)])
            spare += 1
    return colours


def _line_colour(line_key: str):
    """Repli si le timetable ne porte pas encore de palette (ancien fichier).

    crc32 et non hash() : hash() est randomise a chaque processus Python,
    les couleurs changeraient donc a chaque execution.
    """
    route_id = line_key.split("~")[-1]
    if route_id in LINE_COLORS and line_key.startswith(("STM~", route_id)):
        return LINE_COLORS[route_id]
    seed = zlib.crc32(line_key.encode("utf-8"))
    return FALLBACK_COLORS[seed % len(FALLBACK_COLORS)]


DEFAULT_TIMETABLE = "timetable.json"
WEEKDAY_KEYS = ["monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday"]


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def die(msg: str, code: int = 1):
    print(f"\n  ERREUR : {msg}\n", file=sys.stderr)
    sys.exit(code)


def api_key(args) -> str:
    key = getattr(args, "api_key", None) or os.environ.get("STM_API_KEY", "")
    if not key:
        die("aucune cle d'API. Utilisez --api-key ou export STM_API_KEY=...")
    return key.strip()


def parse_gtfs_time(value: str) -> int:
    """'25:03:00' -> secondes depuis minuit du jour de service (peut depasser 86400)."""
    parts = value.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"heure GTFS invalide : {value!r}")
    h, m, s = (int(p) for p in parts)
    return h * 3600 + m * 60 + s


def fmt_secs(total: int) -> str:
    total %= 86400
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def section(title: str):
    print()
    print(title)
    print("-" * len(title))


# ---------------------------------------------------------------------------
# 1. probe : le GTFS-realtime contient-il du metro ?
# ---------------------------------------------------------------------------

def cmd_probe(args):
    import requests
    from google.transit import gtfs_realtime_pb2

    key = api_key(args)
    metro_ids = _metro_ids_from_timetable(args.timetable) or METRO_ROUTE_IDS_FALLBACK

    print("Sonde du GTFS-realtime de la STM")
    print(f"route_id consideres comme metro : {sorted(metro_ids)}")

    found_any_metro = False
    feeds_read = 0          # nombre de flux effectivement decodes
    failures = []           # raisons d'echec, pour un verdict honnete

    for endpoint in ("tripUpdates", "vehiclePositions"):
        section(f"GET /{endpoint}")
        url = f"{API_RT}/{endpoint}"
        try:
            resp = requests.get(url, headers={"apikey": key}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            print(f"  echec reseau : {exc}")
            failures.append(f"{endpoint} : reseau injoignable ({exc})")
            continue

        print(f"  HTTP {resp.status_code}  ({len(resp.content)} octets)")

        if resp.status_code != 200:
            body = resp.text[:300]
            # La STM renvoie 400 + "Invalid API Key" plutot qu'un 401.
            if resp.status_code in (400, 401, 403) or "api key" in body.lower():
                print(f"  -> cle d'API refusee. Reponse : {body}")
                print("     Verifiez sur developpeurs.stm.info que l'application existe")
                print("     et qu'elle est abonnee au produit GTFS-realtime.")
                failures.append(f"{endpoint} : cle refusee (HTTP {resp.status_code})")
            else:
                print(f"  -> reponse inattendue. Corps : {body}")
                failures.append(f"{endpoint} : HTTP {resp.status_code}")
            continue

        feed = gtfs_realtime_pb2.FeedMessage()
        try:
            feed.ParseFromString(resp.content)
        except Exception as exc:  # noqa: BLE001
            print(f"  -> protobuf illisible : {exc}")
            print(f"     debut de la reponse : {resp.content[:200]!r}")
            failures.append(f"{endpoint} : protobuf illisible")
            continue

        feeds_read += 1
        stats = summarize_feed(feed, metro_ids)
        _print_feed_stats(stats, endpoint)
        if stats["metro_entities"]:
            found_any_metro = True
            _print_metro_samples(feed, metro_ids, endpoint)

    section("Verdict")
    if feeds_read == 0:
        # Aucun flux lu : le test n'a rien prouve. Ne rien conclure.
        print("  TEST NON CONCLUANT — aucun flux n'a pu etre lu.")
        for reason in failures:
            print(f"    - {reason}")
        print("\n  Ce resultat ne dit RIEN sur la presence du metro dans le flux.")
        print("  Corrigez l'acces, puis relancez.")
        sys.exit(2)
    elif found_any_metro:
        print(f"  {feeds_read}/2 flux lus. Du metro est present dans le temps reel.")
        print("  Excellente nouvelle : le panneau peut afficher des positions reelles")
        print("  (ou au moins des retards reels) plutot qu'une pure simulation.")
    else:
        print(f"  {feeds_read}/2 flux lus, aucune entite metro dedans.")
        print("  Conforme a la documentation de la STM.")
        print("  Le panneau reposera sur la simulation horaire (commandes build + live),")
        print("  avec l'API i3 pour signaler les interruptions.")
        if failures:
            print("\n  Note : un flux n'a pas pu etre lu, le verdict est donc partiel :")
            for reason in failures:
                print(f"    - {reason}")


def summarize_feed(feed, metro_ids) -> dict:
    """Statistiques agnostiques sur un FeedMessage GTFS-RT."""
    route_counter = defaultdict(int)
    total = 0
    with_position = 0
    metro_entities = 0

    for entity in feed.entity:
        total += 1
        trip = None
        if entity.HasField("trip_update"):
            trip = entity.trip_update.trip
        elif entity.HasField("vehicle"):
            trip = entity.vehicle.trip
            if entity.vehicle.HasField("position"):
                with_position += 1
        if trip is None:
            continue
        route_id = trip.route_id or "(vide)"
        route_counter[route_id] += 1
        if route_id in metro_ids:
            metro_entities += 1

    return {
        "timestamp": feed.header.timestamp,
        "total": total,
        "with_position": with_position,
        "routes": dict(route_counter),
        "metro_entities": metro_entities,
    }


def _print_feed_stats(stats: dict, endpoint: str):
    ts = stats["timestamp"]
    if ts:
        age = int(time.time()) - ts
        print(f"  horodatage du flux : {dt.datetime.fromtimestamp(ts)}  (il y a {age} s)")
    print(f"  entites : {stats['total']}")
    if endpoint == "vehiclePositions":
        print(f"  entites avec coordonnees GPS : {stats['with_position']}")
    print(f"  route_id distincts : {len(stats['routes'])}")
    top = sorted(stats["routes"].items(), key=lambda kv: -kv[1])[:12]
    print("  plus frequents : " + ", ".join(f"{r} ({n})" for r, n in top))
    print(f"  entites metro : {stats['metro_entities']}")


def _print_metro_samples(feed, metro_ids, endpoint: str, limit: int = 3):
    print("\n  Echantillons metro :")
    shown = 0
    for entity in feed.entity:
        if entity.HasField("trip_update"):
            trip = entity.trip_update.trip
        elif entity.HasField("vehicle"):
            trip = entity.vehicle.trip
        else:
            continue
        if trip.route_id not in metro_ids:
            continue
        print("  " + "-" * 40)
        print("  " + str(entity).replace("\n", "\n  ").rstrip())
        shown += 1
        if shown >= limit:
            break


def _metro_ids_from_timetable(path: str | None):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return set(data["meta"]["metro_route_ids"])
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 2. etat : API i3, etat de service
# ---------------------------------------------------------------------------

def cmd_etat(args):
    import requests

    key = api_key(args)
    url = f"{API_I3}/etatservice"
    headers = {
        "apikey": key,
        "origin": args.origin,
        "Accept": "application/json",
    }

    section(f"GET {url}")
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except Exception as exc:
        die(f"echec reseau : {exc}")

    print(f"  HTTP {resp.status_code}")

    if resp.status_code != 200:
        body = resp.text[:600]
        if "api key" in body.lower() or resp.status_code in (400, 401, 403):
            print(f"  -> cle refusee. Reponse : {body}")
            print("     Verifiez que l'application est abonnee au produit")
            print("     'iBUS - Etat du Service' sur le portail developpeurs.")
        else:
            print(f"  -> reponse inattendue. Corps : {body}")
        die("etat de service inaccessible.")

    try:
        payload = resp.json()
    except Exception:
        print(resp.text[:2000])
        die("reponse non-JSON")

    if args.raw:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    alerts = payload.get("alerts", [])
    print(f"  {len(alerts)} alertes dans le flux")

    metro_alerts = _extract_metro_alerts(alerts)
    section("Etat des lignes de metro")
    if not metro_alerts:
        print("  OK  Aucune alerte metro — service normal sur les 4 lignes.")
        return

    for route_id, cause, effect, header, desc in metro_alerts:
        line_name = LINE_NAMES.get(route_id, route_id)
        marker = "!! " if effect != "NO_SERVICE" else "XX "
        print(f"  {marker}Ligne {route_id} ({line_name})")
        print(f"      {header}")
        if desc:
            print(f"      {desc[:150]}")
        print(f"      cause: {cause}  effet: {effect}")
        print()


def _extract_metro_alerts(alerts):
    """Filtre les alertes qui concernent le metro."""
    metro_routes = {"1", "2", "4", "5"}
    results = []
    for alert in alerts:
        entities = alert.get("informed_entities", [])
        route_ids = {str(e.get("route_id", "")) for e in entities}
        matching = route_ids & metro_routes
        if not matching:
            continue
        cause = alert.get("cause", "")
        effect = alert.get("effect", "")
        header = ""
        for ht in alert.get("header_texts", []):
            if ht.get("language") == "fr":
                header = ht.get("text", "")
                break
        if not header:
            for ht in alert.get("header_texts", []):
                header = ht.get("text", "")
                break
        desc = ""
        for dt_item in alert.get("description_texts", []):
            if dt_item.get("language") == "fr":
                desc = dt_item.get("text", "")
                break
        for rid in sorted(matching):
            results.append((rid, cause, effect, header, desc))
    return results


METRO_LABELS = {"verte", "orange", "jaune", "bleue",
                "green", "yellow", "blue", "1", "2", "4", "5"}


def _is_metro_label(name) -> bool:
    """Vrai si l'etiquette designe une ligne de metro plutot qu'un bus.

    On compare des mots entiers : sinon un bus nomme 'Orange Express' ou la
    ligne '10' passeraient pour du metro.
    """
    if name is None:
        return False
    label = str(name).strip().lower()
    if label in METRO_LABELS:
        return True
    words = {word.strip("()-–—,.") for word in label.replace("/", " ").split()}
    return bool(words & {"verte", "orange", "jaune", "bleue",
                         "green", "yellow", "blue"})


# ---------------------------------------------------------------------------
# 3. build : GTFS statique -> horaire compact
# ---------------------------------------------------------------------------

def cmd_build(args):
    route_types = {t.strip() for t in args.route_types.split(",") if t.strip()}
    merge_stations = {s.strip() for s in (args.merge_stations or "").split(",") if s.strip()}
    per_direction = args.per_direction
    print(f"route_type retenus : {sorted(route_types)}")
    if per_direction:
        print("une LED par sens de circulation")
    if merge_stations:
        print(f"une seule LED par station physique pour : {sorted(merge_stations)}")

    feeds = []
    for path in args.gtfs:
        if not os.path.exists(path):
            die(f"fichier introuvable : {path}")
        feeds.append(_load_feed(path, route_types))

    if len(feeds) > 1:
        section("Fusion")
        for feed in feeds:
            print(f"  {feed['label']:<12} {len(feed['routes'])} ligne(s), "
                  f"{len(feed['seq'])} trajet(s)")

    stations, station_index = [], {}
    patterns, pattern_index = [], {}
    runs, services, line_names = [], {}, {}

    for feed in feeds:
        label = feed["label"]

        for service_id, svc in feed["services"].items():
            services[f"{label}~{service_id}"] = svc

        for route_id, route_name in feed["routes"].items():
            line_names[f"{label}~{route_id}"] = route_name
        if label in merge_stations:
            # Les stations fusionnees portent la cle "<reseau>~*" : sans entree
            # correspondante, la cle interne se retrouvait dans le CSV.
            line_names[f"{label}~*"] = label

        # Une LED par paire station x ligne (defaut), ou une seule LED par
        # station physique pour les reseaux a tronc commun comme le REM, ou
        # un meme quai est desservi par plusieurs services.
        merged = label in merge_stations

        def station_key(route_id, stop_id, direction="", _feed=feed, _merged=merged):
            base = _feed["stops"].get(stop_id, {}).get("parent") or stop_id
            line_part = "*" if _merged else route_id
            # Une LED par sens : la cle porte alors le direction_id. Le sens est
            # une propriete du train, mais chaque patron en a deja un, donc la
            # correspondance train -> LED se fait sans ambiguite.
            if per_direction:
                return f"{_feed['label']}~{line_part}~{direction}~{base}"
            return f"{_feed['label']}~{line_part}~{base}"

        # --- stations ---
        for trip_id, stoplist in feed["seq"].items():
            meta = feed["trips"][trip_id]
            for _, stop_id, _, _ in stoplist:
                key = station_key(meta["route"], stop_id, meta["direction"])
                if key in station_index:
                    continue
                info = feed["stops"].get(stop_id, {})
                base = info.get("parent") or stop_id
                name = feed["stops"].get(base, info).get("name") or info.get("name", base)
                station_index[key] = len(stations)
                stations.append({
                    "key": key,
                    "name": name,
                    "line": f"{label}~*" if merged else f"{label}~{meta['route']}",
                    "feed": label,
                    "direction": meta["direction"] if per_direction else "",
                    "headsign": meta["headsign"] if per_direction else "",
                    "lat": info.get("lat"),
                    "lon": info.get("lon"),
                })

        # --- patrons et passages ---
        for trip_id, stoplist in feed["seq"].items():
            meta = feed["trips"][trip_id]
            base_time = stoplist[0][3]
            shape = tuple(
                (station_index[station_key(meta["route"], stop_id, meta["direction"])],
                 arr - base_time, dep - base_time)
                for _, stop_id, arr, dep in stoplist
            )
            line = f"{label}~{meta['route']}"
            signature = (line, meta["direction"], meta["headsign"], shape)
            pid = pattern_index.get(signature)
            if pid is None:
                pid = len(patterns)
                pattern_index[signature] = pid
                patterns.append({
                    "line": line,
                    "direction": meta["direction"],
                    "headsign": meta["headsign"],
                    "stops": [list(item) for item in shape],
                })
            for departure in _departures_for(trip_id, base_time, feed["freqs"]):
                runs.append([pid, f"{label}~{meta['service']}", departure])

    runs.sort(key=lambda r: r[2])
    print(f"\n  stations x lignes : {len(stations)}")
    print(f"  patrons distincts : {len(patterns)}")
    print(f"  passages generes  : {len(runs)}")

    # --- ordre des LED : deterministe, suivant le trace de chaque ligne ---
    order = _wiring_order(stations, patterns)
    stations, patterns, runs = _reindex(stations, patterns, runs, order)
    print(f"  ordre des LED     : par ligne, puis le long du trace")

    payload = {
        "meta": {
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "sources": [feed["source"] for feed in feeds],
            "metro_route_ids": sorted(line_names),
            "line_names": line_names,
            "line_colours": assign_line_colours(line_names),
            "route_types": sorted(route_types),
            "frequency_based": any(feed["freqs"] for feed in feeds),
        },
        "stations": stations,
        "patterns": patterns,
        "services": services,
        "runs": runs,
    }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"\n  Ecrit : {args.out}  ({os.path.getsize(args.out) / 1024:.0f} Ko)")
    physical = len({s["key"].rsplit("~", 1)[-1] for s in stations})
    print(f"  {len(stations)} LED necessaires "
          f"({physical} stations physiques distinctes).")
    if per_direction:
        watts = len(stations) * 0.06
        print(f"  Alimentation : {watts:.1f} A au pire cas (blanc plein sur tout).")
        print(f"  Prevoyez un 5 V {max(3, int(watts * 0.6) + 1)} A et plafonnez la")
        print("  luminosite dans le firmware ; en usage reel seule une centaine de")
        print("  pixels est allumee, a intensite partielle.")
    print("  Exportez la correspondance station -> LED avec : export-stations")


def _load_feed(path: str, route_types: set) -> dict:
    """Lit un zip GTFS et n'en garde que les lignes des route_type demandes."""
    section(f"Lecture de {os.path.basename(path)}")
    zf = zipfile.ZipFile(path)
    names = set(zf.namelist())

    def rows(member, required=True):
        if member not in names:
            if required:
                die(f"{member} absent de {path}")
            return
        with zf.open(member) as raw:
            for row in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig")):
                yield {(k or "").strip(): (v or "").strip() for k, v in row.items()}

    # Etiquette du flux : nom de l'agence, sinon nom du fichier.
    label = os.path.splitext(os.path.basename(path))[0]
    for row in rows("agency.txt", required=False):
        raw_name = row.get("agency_name", "").strip()
        if raw_name:
            label = _short_label(raw_name)
            break
    print(f"  etiquette : {label}")

    routes, all_types = {}, defaultdict(int)
    for row in rows("routes.txt"):
        all_types[row.get("route_type", "?")] += 1
        if row.get("route_type") in route_types:
            rid = row["route_id"]
            routes[rid] = (row.get("route_long_name")
                           or row.get("route_short_name")
                           or LINE_NAMES.get(rid, rid))
    print("  route_type presents : " + ", ".join(
        f"{t} ({n} ligne{'s' if n > 1 else ''})" for t, n in sorted(all_types.items())))
    if not routes:
        die(f"aucune ligne de type {sorted(route_types)} dans {path}.\n"
            f"  Types disponibles : {sorted(all_types)}.\n"
            f"  Relancez avec --route-types suivi du ou des types voulus.\n"
            f"  Rappel GTFS : 0 = tram, 1 = metro, 2 = train, 3 = bus.")
    print(f"  lignes retenues : {len(routes)} -> {sorted(routes)}")

    stops = {}
    for row in rows("stops.txt"):
        stops[row["stop_id"]] = {"name": row.get("stop_name", ""),
                                 "parent": row.get("parent_station", ""),
                                 "lat": _float(row.get("stop_lat")),
                                 "lon": _float(row.get("stop_lon"))}

    trips = {}
    for row in rows("trips.txt"):
        if row["route_id"] in routes:
            trips[row["trip_id"]] = {"route": row["route_id"],
                                     "service": row["service_id"],
                                     "direction": row.get("direction_id", "0"),
                                     "headsign": row.get("trip_headsign", "")}
    print(f"  trajets : {len(trips)}")

    seq = defaultdict(list)
    for row in rows("stop_times.txt"):
        trip_id = row["trip_id"]
        if trip_id not in trips:
            continue
        arr = row.get("arrival_time") or row.get("departure_time")
        dep = row.get("departure_time") or row.get("arrival_time")
        if not arr or not dep:
            continue
        seq[trip_id].append((int(row["stop_sequence"]), row["stop_id"],
                             parse_gtfs_time(arr), parse_gtfs_time(dep)))
    for trip_id in seq:
        seq[trip_id].sort()
    print(f"  trajets avec horaires : {len(seq)}")

    freqs = defaultdict(list)
    for row in rows("frequencies.txt", required=False):
        if row["trip_id"] in seq:
            freqs[row["trip_id"]].append((parse_gtfs_time(row["start_time"]),
                                          parse_gtfs_time(row["end_time"]),
                                          int(row["headway_secs"])))
    print(f"  frequencies.txt : {'oui, ' + str(len(freqs)) + ' trajets' if freqs else 'absent'}")

    services = {}
    for row in rows("calendar.txt", required=False):
        services[row["service_id"]] = {
            "days": [int(row.get(day, "0") or 0) for day in WEEKDAY_KEYS],
            "start": row.get("start_date", ""), "end": row.get("end_date", ""),
            "add": [], "remove": []}
    for row in rows("calendar_dates.txt", required=False):
        entry = services.setdefault(row["service_id"],
                                    {"days": [0] * 7, "start": "", "end": "",
                                     "add": [], "remove": []})
        entry["add" if row.get("exception_type") == "1" else "remove"].append(row["date"])
    print(f"  service_id : {len(services)}")

    return {"label": label, "source": os.path.basename(path), "routes": routes,
            "stops": stops, "trips": trips, "seq": dict(seq),
            "freqs": dict(freqs), "services": services}


def _short_label(agency_name: str) -> str:
    """'Societe de transport de Montreal' -> 'STM'."""
    known = {"societe de transport de montreal": "STM",
             "reseau express metropolitain": "REM",
             "exo": "exo"}
    flat = agency_name.lower()
    for needle, short in known.items():
        if needle in flat.replace("é", "e").replace("è", "e"):
            return short
    initials = "".join(word[0] for word in agency_name.split() if word[0].isupper())
    return initials or agency_name[:8]


def _wiring_order(stations, patterns):
    """Ordre de cablage : ligne par ligne, en suivant le trace.

    L'index de LED doit etre stable et physiquement sensé : on cable en
    serpentin le long de chaque ligne, donc on ordonne les stations dans
    l'ordre ou le trace les traverse. Les branches (le REM en a trois) sont
    traitees en prenant d'abord le patron le plus long, puis en ajoutant les
    stations encore absentes.
    """
    by_line = defaultdict(list)
    for pattern in patterns:
        by_line[pattern["line"]].append(pattern)

    order = []
    seen = set()
    for line in sorted(by_line):
        candidates = sorted(by_line[line],
                            key=lambda p: (-len(p["stops"]), p["direction"]))
        for pattern in candidates:
            for station_id, _, _ in pattern["stops"]:
                if station_id not in seen:
                    seen.add(station_id)
                    order.append(station_id)
    # Filet de securite : une station orpheline ne doit pas disparaitre.
    for index in range(len(stations)):
        if index not in seen:
            order.append(index)
    return order


def _reindex(stations, patterns, runs, order):
    """Applique un nouvel ordre de stations sans casser les references."""
    remap = {old: new for new, old in enumerate(order)}
    new_stations = [stations[old] for old in order]
    for pattern in patterns:
        pattern["stops"] = [[remap[s], a, d] for s, a, d in pattern["stops"]]
    return new_stations, patterns, runs


LOWERCASE_WORDS = {"de", "du", "des", "la", "le", "les", "et", "aux", "au"}
ACRONYMS = {"OACI", "UQAM", "IX", "U", "UDEM"}

# Noms officiels des stations dont la forme brute du GTFS est fautive ou
# ambigue. Table volontairement explicite et facile a completer : pour 96
# etiquettes ecrites a la main sur un panneau, une exception verifiee vaut
# mieux qu'une regle astucieuse. Cle = nom brut, espaces normalises.
NAME_OVERRIDES = {
    "STATION PIE IX": "Pie-IX",
    "STATION CHAMPS DE MARS": "Champ-de-Mars",
    "STATION SQUARE VICTORIA-OACI": "Square-Victoria–OACI",
    "STATION LONGUEUIL- U. de SHERBROOKE": "Longueuil–Université-de-Sherbrooke",
    "STATION UNIVERSITÉ DE MONTREAL": "Université-de-Montréal",
    "STATION BERRI-UQAM": "Berri-UQAM",
    "STATION ST-LAURENT": "Saint-Laurent",
    "STATION PLACE ST-HENRI": "Place-Saint-Henri",
    "STATION CÔTE-STE-CATHERINE": "Côte-Sainte-Catherine",
    "STATION PLACE DES ARTS": "Place-des-Arts",
    "STATION PLACE D'ARMES": "Place-d'Armes",
    "STATION DE L'ASSOMPTION": "L'Assomption",
    "STATION JEAN-DRAPEAU": "Jean-Drapeau",
    "STATION LASALLE": "LaSalle",
    # Article elide : minuscule ici (preposition), mais majuscule dans
    # Lucien-L'Allier, qui est un patronyme. Aucune regle ne les distingue.
    "STATION DE L'ÉGLISE": "De l'Église",
}


def pretty_station_name(raw: str) -> str:
    """'STATION  DU PARC' -> 'Du Parc'. Pour les etiquettes du panneau.

    Les noms de la STM arrivent en capitales, prefixes de 'STATION', parfois
    avec des espaces doubles. str.title() ne convient pas en francais : il
    casse les apostrophes et les traits d'union, et ecrase les acronymes.
    """
    text = " ".join(raw.split())
    if text in NAME_OVERRIDES:
        return NAME_OVERRIDES[text]

    for prefix in ("STATION ", "Station "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if not text:
        return raw.strip()

    # Ne retoucher que ce qui est majoritairement en capitales : les noms du
    # REM arrivent deja correctement casses.
    letters = [c for c in text if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) / len(letters) < 0.8:
        return text

    def cap(token: str) -> str:
        if token.upper() in ACRONYMS:
            return token.upper()
        return token[0].upper() + token[1:].lower() if token else token

    words = []
    for index, word in enumerate(text.split(" ")):
        # Un mot de liaison reste minuscule, sauf en tete de nom.
        if index > 0 and word.lower() in LOWERCASE_WORDS:
            words.append(word.lower())
            continue
        # A l'interieur d'un compose (trait d'union, apostrophe), chaque
        # element garde sa majuscule : Lucien-L'Allier, et non Lucien-l'Allier.
        pieces, buffer = [], ""
        for char in word:
            if char in "-'\u2019":
                pieces.extend([buffer, char])
                buffer = ""
            else:
                buffer += char
        pieces.append(buffer)
        rebuilt, rank = "", 0
        for piece in pieces:
            if piece in "-'\u2019":
                rebuilt += piece
                continue
            # Cote-des-Neiges, Place-des-Arts : liaison interne en minuscule.
            if rank > 0 and piece.lower() in LOWERCASE_WORDS:
                rebuilt += piece.lower()
            else:
                rebuilt += cap(piece)
            rank += 1
        words.append(rebuilt)
    return " ".join(words)


def cmd_export_stations(args):
    """Sort la correspondance station -> index de LED, pour le cablage."""
    tt = Timetable.load(args.timetable)
    writer = csv.writer(sys.stdout)
    writer.writerow(["led_index", "feed", "line", "line_name", "station",
                     "direction", "vers", "latitude", "longitude", "rgb_hex"])
    for index, station in enumerate(tt.stations):
        colour = tt.colour(station["line"])
        writer.writerow([index,
                         station.get("feed", ""),
                         station["line"].split("~")[-1],
                         tt.line_name(station["line"]),
                         pretty_station_name(station["name"])
                         if args.clean_names else station["name"],
                         station.get("direction", ""),
                         pretty_station_name(station.get("headsign", ""))
                         if args.clean_names else station.get("headsign", ""),
                         station.get("lat", ""),
                         station.get("lon", ""),
                         "#%02X%02X%02X" % colour])


def _departures_for(trip_id, base_time, freqs):
    """Liste des heures de premier depart pour un trajet."""
    windows = freqs.get(trip_id)
    if not windows:
        return [base_time]
    out = []
    for start, end, headway in windows:
        if headway <= 0:
            continue
        t = start
        while t < end:
            out.append(t)
            t += headway
    return out or [base_time]


def _float(value):
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 4. Moteur de simulation
# ---------------------------------------------------------------------------

class Timetable:
    def __init__(self, data: dict, dwell_pad: int = 20):
        # dwell_pad : le GTFS du metro peut avoir arrival_time == departure_time
        # (aucun temps d'arret modelise). Sans marge, une rame ne serait "a quai"
        # qu'a un instant infinitesimal, jamais capture par un rafraichissement
        # aux 5 secondes. On considere donc la rame en station si elle est a
        # moins de dwell_pad secondes de l'heure d'arrivee ou de depart.
        self.dwell_pad = max(0, dwell_pad)
        self.meta = data["meta"]
        self.stations = data["stations"]
        self.patterns = data["patterns"]
        self.services = data["services"]
        self.runs_by_service = defaultdict(list)
        for pattern_id, service_id, departure in data["runs"]:
            self.runs_by_service[service_id].append((pattern_id, departure))
        # duree totale de chaque patron, pour ecarter vite les passages inactifs
        self.pattern_span = [p["stops"][-1][1] for p in self.patterns]

    @classmethod
    def load(cls, path: str, dwell_pad: int = 20):
        if not os.path.exists(path):
            die(f"{path} introuvable. Lancez d'abord : "
                f"python3 stm_phase0.py build gtfs_stm.zip")
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh), dwell_pad=dwell_pad)

    def line_name(self, line_key: str) -> str:
        return self.meta.get("line_names", {}).get(line_key, line_key)

    def colour(self, line_key: str):
        stored = self.meta.get("line_colours", {}).get(line_key)
        return tuple(stored) if stored else _line_colour(line_key)

    def line_label(self, line_key: str) -> str:
        """'STM~2' -> 'STM ligne 2 (Orange)'."""
        parts = line_key.split("~")
        feed, route_id = (parts[0], parts[-1]) if len(parts) > 1 else ("", line_key)
        pretty = LINE_NAMES.get(route_id)
        suffix = f" ({pretty})" if pretty and feed == "STM" else ""
        return f"{feed} ligne {route_id}{suffix}".strip()

    def active_services(self, date: dt.date):
        stamp = date.strftime("%Y%m%d")
        weekday = date.weekday()  # 0 = lundi, comme WEEKDAY_KEYS
        active = set()
        for sid, svc in self.services.items():
            if stamp in svc.get("remove", []):
                continue
            if stamp in svc.get("add", []):
                active.add(sid)
                continue
            start, end = svc.get("start", ""), svc.get("end", "")
            if start and stamp < start:
                continue
            if end and stamp > end:
                continue
            days = svc.get("days") or [0] * 7
            if len(days) == 7 and days[weekday]:
                active.add(sid)
        return active

    def trains_at(self, moment: dt.datetime):
        """Positions des rames a un instant donne.

        On evalue le jour courant et le jour precedent : le metro roule apres
        minuit, et le GTFS exprime ces passages en heures >= 24:00:00.
        """
        seconds = moment.hour * 3600 + moment.minute * 60 + moment.second
        trains = []
        for day_offset in (0, 1):
            date = moment.date() - dt.timedelta(days=day_offset)
            t = seconds + day_offset * 86400
            for service_id in self.active_services(date):
                for pattern_id, departure in self.runs_by_service.get(service_id, ()):
                    if departure > t:
                        continue  # les passages sont tries : on pourrait couper ici
                    if departure + self.pattern_span[pattern_id] < t:
                        continue
                    train = self._locate(pattern_id, departure, t)
                    if train:
                        trains.append(train)
        return trains

    def _locate(self, pattern_id: int, departure: int, t: int):
        pattern = self.patterns[pattern_id]
        stops = pattern["stops"]
        rel = t - departure
        if rel < stops[0][1] - self.dwell_pad or rel > stops[-1][2] + self.dwell_pad:
            return None

        pad = self.dwell_pad

        def result(index, to_index, progress, state):
            return {"line": pattern["line"],
                    "direction": pattern["direction"],
                    "headsign": pattern["headsign"],
                    "from": stops[index][0],
                    "to": stops[to_index][0],
                    "progress": round(progress, 3),
                    "at_station": state != "between",
                    "state": state}

        for i, (_, arr, dep) in enumerate(stops):
            # A quai : entre l'arrivee et le depart, elargi de pad de chaque cote.
            if arr - pad <= rel <= dep + pad:
                return result(i, i, 0.0, "at")
            if i + 1 < len(stops):
                next_arr = stops[i + 1][1]
                if dep < rel < next_arr:
                    # A l'approche : on entre dans la fenetre de la station suivante.
                    if next_arr - rel <= pad:
                        return result(i + 1, i + 1, 1.0, "arriving")
                    span = next_arr - pad - dep
                    progress = (rel - dep) / span if span > 0 else 0.0
                    return result(i, i + 1, min(1.0, progress), "between")
        return None

    def led_state(self, moment: dt.datetime):
        """Couleur RGB de chaque station : ce que le panneau afficherait.

        Chaque LED a une couleur physique fixe (celle de sa ligne / son reseau).
        L'intensite varie selon la proximite d'un train. On utilise la couleur
        de la station, pas du train : un train REM-A3 (magenta dans la palette
        interne) allume du turquoise sur une LED REM~*.
        """
        pixels = [[0, 0, 0] for _ in self.stations]
        for train in self.trains_at(moment):
            if train["at_station"]:
                _add(pixels[train["from"]], (255, 255, 255), 1.0)
            else:
                progress = train["progress"]
                _add(pixels[train["from"]], (255, 255, 255), 1.0 - progress)
                _add(pixels[train["to"]], (255, 255, 255), progress)

        # Appliquer la couleur de la station, plafonnee a 100 % d'intensite
        result = []
        for i, pixel in enumerate(pixels):
            intensity = min(255, max(pixel))
            colour = self.colour(self.stations[i]["line"])
            result.append([min(255, int(c * intensity / 255)) for c in colour])
        return result


def _add(pixel, colour, weight):
    for i in range(3):
        pixel[i] += colour[i] * weight


def resolve_moment(spec: str | None) -> dt.datetime:
    now = dt.datetime.now()
    if not spec:
        return now
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
        try:
            parsed = dt.datetime.strptime(spec, fmt)
        except ValueError:
            continue
        if fmt.startswith("%H"):
            return now.replace(hour=parsed.hour, minute=parsed.minute,
                               second=parsed.second, microsecond=0)
        return parsed
    die(f"format de date non reconnu : {spec!r} (essayez 08:45 ou 2026-07-28T08:45)")


# ---------------------------------------------------------------------------
# 5. live / snapshot
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Compilation du firmware pour Raspberry Pi Pico
# ---------------------------------------------------------------------------

FIRMWARE_MAGIC = b"MTLM"  # Montreal Metro LED Map
FIRMWARE_VERSION = 1

def cmd_compile_firmware(args):
    """Precalcule l'etat des LED minute par minute, pour le Pico.

    Le Pico n'a ni la RAM ni le CPU pour charger le timetable et interpoler
    en temps reel. On fait donc tout le travail ici, et le Pico ne fait que
    lire un offset et pousser les octets vers la chaine de LED.

    Format binaire (little-endian) :
      4 octets  magic "MTLM"
      1 octet   version (1)
      1 octet   nombre de types de jour (3 = sem/sam/dim)
      2 octets  nombre de minutes (1440)
      2 octets  nombre de LED (N)
      N x 3     palette : couleur RGB de chaque LED (de sa ligne)
      3 x 1440 x N octets : intensite de chaque LED, par type de jour
                             puis par minute (0 = eteint, 255 = plein)

    Taille pour 192 LED : 10 + 576 + 829440 = ~811 Ko (tient dans 2 Mo de flash).
    """
    import struct

    tt = Timetable.load(args.timetable, dwell_pad=args.dwell_pad)
    n_leds = len(tt.stations)

    # Couleurs de ligne par LED
    palette = []
    for station in tt.stations:
        colour = tt.colour(station["line"])
        palette.append(colour)

    # Types de jour : 0 = semaine, 1 = samedi, 2 = dimanche
    # On prend un representant de chaque depuis une semaine type.
    day_samples = _pick_day_samples(tt)
    n_days = len(day_samples)
    print(f"Compilation du firmware")
    print(f"  LED        : {n_leds}")
    print(f"  types de jour : {n_days} ({', '.join(d.strftime('%A %Y-%m-%d') for d in day_samples)})")

    frames = bytearray()
    for day_idx, sample_date in enumerate(day_samples):
        label = ("semaine", "samedi", "dimanche")[day_idx]
        lit_max = 0
        for minute in range(1440):
            moment = dt.datetime.combine(sample_date,
                                         dt.time(minute // 60, minute % 60))
            pixels = tt.led_state(moment)
            for led_idx in range(n_leds):
                r, g, b = pixels[led_idx]
                line_r, line_g, line_b = palette[led_idx]
                # L'intensite est le rapport entre la couleur calculee et la
                # couleur de la ligne. On prend le canal le plus significatif.
                if max(line_r, line_g, line_b) == 0:
                    intensity = 0
                else:
                    ratios = []
                    for ch, line_ch in zip((r, g, b), (line_r, line_g, line_b)):
                        if line_ch > 0:
                            ratios.append(min(255, int(ch / line_ch * 255)))
                    intensity = max(ratios) if ratios else 0
                frames.append(min(255, intensity))
            lit = sum(1 for p in pixels if any(p))
            if lit > lit_max:
                lit_max = lit
        print(f"  {label:<10} : {lit_max} LED allumees au pic")

    # Assemblage
    header = struct.pack("<4sBBHH",
                         FIRMWARE_MAGIC, FIRMWARE_VERSION,
                         n_days, 1440, n_leds)
    pal = bytearray()
    for r, g, b in palette:
        pal.extend([r, g, b])

    blob = header + bytes(pal) + bytes(frames)
    with open(args.out, "wb") as fh:
        fh.write(blob)

    size_kb = len(blob) / 1024
    print(f"\n  Ecrit : {args.out}  ({size_kb:.0f} Ko)")
    print(f"  Copiez ce fichier sur le Pico (dans le systeme de fichiers CircuitPython).")

    # Resume pour le firmware
    meta = {"n_leds": n_leds, "n_days": n_days, "n_minutes": 1440,
            "palette_offset": 10, "data_offset": 10 + n_leds * 3,
            "frame_size": n_leds, "file_size": len(blob)}
    meta_path = args.out.rsplit(".", 1)[0] + "_meta.json"
    with open(meta_path, "w") as fh:
        json.dump(meta, fh, indent=2)
    print(f"  Metadonnees : {meta_path}")


def _pick_day_samples(tt):
    """Trouve une date representative pour semaine, samedi, dimanche.

    On cherche dans les 30 prochains jours pour maximiser les chances de
    tomber dans la periode de validite du GTFS.
    """
    today = dt.date.today()
    samples = [None, None, None]
    for offset in range(30):
        day = today + dt.timedelta(days=offset)
        weekday = day.weekday()
        active = tt.active_services(day)
        if not active:
            continue
        if weekday < 5 and samples[0] is None:
            samples[0] = day
        elif weekday == 5 and samples[1] is None:
            samples[1] = day
        elif weekday == 6 and samples[2] is None:
            samples[2] = day
        if all(samples):
            break

    # Repli si on n'a pas trouve les trois
    for i, fallback_offset in enumerate([0, 5, 6]):
        if samples[i] is None:
            samples[i] = today + dt.timedelta(days=fallback_offset)
    return samples


def cmd_diagnose(args):
    """Mesure les proprietes du GTFS reel, pour valider la plausibilite
    de la simulation avant de percer quoi que ce soit."""
    tt = Timetable.load(args.timetable, dwell_pad=args.dwell_pad)

    section("1. Temps d'arret en station (dwell)")
    dwells = []
    runs = []
    for pattern in tt.patterns:
        stops = pattern["stops"]
        for i, (_, arr, dep) in enumerate(stops):
            dwells.append(dep - arr)
            if i + 1 < len(stops):
                runs.append(stops[i + 1][1] - dep)

    zero = sum(1 for d in dwells if d == 0)
    print(f"  arrets analyses : {len(dwells)}")
    print(f"  avec dwell = 0  : {zero} ({100 * zero / len(dwells):.1f} %)")
    if zero == len(dwells):
        print("  -> La STM ne modelise AUCUN temps d'arret : arrival == departure.")
        print(f"     La marge --dwell-pad (actuellement {tt.dwell_pad} s) est donc")
        print("     indispensable pour que les stations s'allument sur le panneau.")
    else:
        print(f"  dwell median    : {_median(dwells)} s")

    section("2. Temps de parcours entre stations")
    print(f"  segments analyses : {len(runs)}")
    print(f"  min / median / max : {min(runs)} / {_median(runs)} / {max(runs)} s")
    distinct = sorted(set(runs))
    print(f"  valeurs distinctes : {len(distinct)}")
    print(f"  les plus courantes : {_top_values(runs, 8)}")
    if all(r % 60 == 0 for r in runs):
        print("  -> Tous les temps sont des multiples de 60 s : horaires arrondis")
        print("     a la minute. C'est ce qui met les rames en phase dans `live`.")
        print("     Artefact de donnees, pas un bug de calcul.")
    elif all(r % 30 == 0 for r in runs):
        print("  -> Tous les temps sont des multiples de 30 s (arrondi a la demie).")
    else:
        print("  -> Temps de parcours a la seconde : les rames ne devraient PAS")
        print("     etre en phase. Si elles le sont dans `live`, c'est suspect.")

    section("3. Rames simultanees, heure par heure")
    print("  Comparez la colonne 'intervalle' avec la realite : la ligne verte")
    print("  tourne autour de 3-5 min en pointe, 8-10 min en soiree.")
    base = dt.datetime.now().replace(minute=0, second=0, microsecond=0)
    header = "  heure  " + "".join(f"{l.split('~')[0][:3]}-{l.split('~')[-1]:<4}"
                                   for l in sorted(tt.meta["metro_route_ids"]))
    print(header)
    for hour in range(6, 24):
        moment = base.replace(hour=hour)
        per_line = defaultdict(int)
        for train in tt.trains_at(moment):
            per_line[train["line"]] += 1
        cells = "".join(f"{per_line.get(l, 0):>8}" for l in
                        sorted(tt.meta["metro_route_ids"]))
        print(f"  {hour:02d}:00 {cells}")

    section("4. Intervalle implicite par ligne (a l'heure demandee)")
    moment = resolve_moment(args.at)
    print(f"  instant : {moment:%Y-%m-%d %H:%M}")
    trains = tt.trains_at(moment)
    by_dir = defaultdict(list)
    for train in trains:
        by_dir[(train["line"], train["direction"])].append(train)

    for (line, direction) in sorted(by_dir):
        count = len(by_dir[(line, direction)])
        span = _line_travel_time(tt, line, direction)
        if span and count:
            headway = span / count / 60
            print(f"  {tt.line_label(line):<22} dir {direction} : {count:>3} rames, "
                  f"parcours {span // 60:>3} min -> intervalle ~{headway:.1f} min")
        else:
            print(f"  {tt.line_label(line):<22} dir {direction} : {count:>3} rames")

    print("\n  Un intervalle implausible (> 10 min en journee) signale un probleme")
    print("  de selection des services actifs plutot qu'un probleme de geometrie.")


def _median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    if not ordered:
        return 0
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def _top_values(values, limit):
    counter = defaultdict(int)
    for value in values:
        counter[value] += 1
    top = sorted(counter.items(), key=lambda kv: -kv[1])[:limit]
    return ", ".join(f"{v}s x{n}" for v, n in top)


def _line_travel_time(tt, line, direction):
    """Duree du plus long patron de la ligne : approxime le temps de parcours."""
    spans = [p["stops"][-1][1] - p["stops"][0][1]
             for p in tt.patterns
             if p["line"] == line and p["direction"] == direction]
    return max(spans) if spans else None


def cmd_live(args):
    tt = Timetable.load(args.timetable, dwell_pad=args.dwell_pad)
    try:
        while True:
            moment = resolve_moment(args.at)
            render_console(tt, moment)
            if args.once or args.at:
                return
            time.sleep(args.interval)
            print("\033[2J\033[H", end="")  # efface l'ecran
    except KeyboardInterrupt:
        print("\nArret.")


def render_console(tt: Timetable, moment: dt.datetime):
    trains = tt.trains_at(moment)
    print(f"=== {moment:%Y-%m-%d %H:%M:%S} — {len(trains)} rames en service ===")

    if not trains:
        print("\n  Aucune rame. Le metro est ferme, ou le GTFS ne couvre pas cette date.")
        print(f"  (validite des services : voir meta.source = {tt.meta.get('source')})")
        return

    grouped = defaultdict(list)
    for train in trains:
        grouped[(train["line"], train["direction"])].append(train)

    for (line, direction) in sorted(grouped):
        batch = grouped[(line, direction)]
        headsign = batch[0]["headsign"] or f"direction {direction}"
        print(f"\n{tt.line_label(line)} -> {headsign}   [{len(batch)} rames]")
        batch.sort(key=lambda tr: (tr["from"], tr["progress"]))
        for train in batch:
            name_from = tt.stations[train["from"]]["name"]
            state = train.get("state", "between")
            if state == "at":
                print(f"   @  {name_from}  (a quai)")
            elif state == "arriving":
                print(f"   >| {name_from}  (a l'approche)")
            else:
                name_to = tt.stations[train["to"]]["name"]
                bar = _progress_bar(train["progress"])
                pct = int(train["progress"] * 100)
                print(f"   >  {name_from} {bar} {pct:3d}%  {name_to}")

    counts = defaultdict(int)
    for train in trains:
        counts[train.get("state", "between")] += 1
    print(f"\n  Etats : {counts['at']} a quai, {counts['arriving']} a l'approche, "
          f"{counts['between']} entre deux stations")


def _progress_bar(progress: float, width: int = 10) -> str:
    filled = max(0, min(width, int(round(progress * width))))
    return "[" + "=" * filled + "." * (width - filled) + "]"


def cmd_snapshot(args):
    tt = Timetable.load(args.timetable, dwell_pad=args.dwell_pad)
    moment = resolve_moment(args.at)
    pixels = tt.led_state(moment)
    output = {
        "moment": moment.isoformat(timespec="seconds"),
        "train_count": len(tt.trains_at(moment)),
        "pixels": [
            {"index": i,
             "station": station["name"],
             "line": station["line"],
             "rgb": pixels[i]}
            for i, station in enumerate(tt.stations)
        ],
    }
    if args.only_lit:
        output["pixels"] = [p for p in output["pixels"] if any(p["rgb"])]
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Phase 0 du panneau LED du metro de Montreal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("--api-key", help="cle STM (sinon variable STM_API_KEY)")
    parser.add_argument("--timetable", default=DEFAULT_TIMETABLE,
                        help=f"horaire compile (defaut : {DEFAULT_TIMETABLE})")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="chercher du metro dans le GTFS-realtime")

    p_etat = sub.add_parser("etat", help="etat de service via l'API i3")
    p_etat.add_argument("--raw", action="store_true", help="afficher le JSON brut")
    p_etat.add_argument("--origin", default="https://localhost",
                        help="valeur de l'entete Origin exige par l'API i3")

    p_build = sub.add_parser("build", help="compiler un ou plusieurs GTFS")
    p_build.add_argument("gtfs", nargs="+",
                         help="un ou plusieurs zips GTFS (ex. gtfs_stm.zip gtfs_rem.zip)")
    p_build.add_argument("--per-direction", action="store_true",
                         help="une LED par sens de circulation : Berri-UQAM en "
                              "recoit 6 (3 lignes x 2 sens)")
    p_build.add_argument("--merge-stations", default="",
                         help="reseaux ou une station physique ne recoit qu'une "
                              "seule LED meme si plusieurs lignes la desservent "
                              "(ex. REM, dont le tronc est partage)")
    p_build.add_argument("--route-types", default="1",
                         help="route_type a retenir, separes par des virgules "
                              "(0=tram, 1=metro, 2=train). Defaut : 1")
    p_build.add_argument("--out", default=DEFAULT_TIMETABLE)

    p_fw = sub.add_parser("compile-firmware",
                          help="precalculer l'etat des LED pour le Pico")
    p_fw.add_argument("--out", default="metro.bin",
                      help="fichier binaire de sortie (defaut : metro.bin)")
    p_fw.add_argument("--dwell-pad", type=int, default=20)

    p_export = sub.add_parser("export-stations",
                              help="CSV station -> index de LED, pour le cablage")
    p_export.add_argument("--clean-names", action="store_true",
                          help="normaliser les noms pour les etiquettes du "
                               "panneau (retire 'STATION', corrige la casse)")

    p_diag = sub.add_parser("diagnose", help="mesurer la plausibilite des donnees")
    p_diag.add_argument("--at", help="instant a analyser, ex. 08:45")
    p_diag.add_argument("--dwell-pad", type=int, default=20)

    p_live = sub.add_parser("live", help="positions simulees en continu")
    p_live.add_argument("--dwell-pad", type=int, default=20,
                        help="secondes de tolerance pour considerer une rame a quai")
    p_live.add_argument("--interval", type=float, default=5.0)
    p_live.add_argument("--once", action="store_true")
    p_live.add_argument("--at", help="instant a simuler, ex. 08:45")

    p_snap = sub.add_parser("snapshot", help="etat des LED en JSON")
    p_snap.add_argument("--at", help="instant a simuler, ex. 08:45")
    p_snap.add_argument("--only-lit", action="store_true",
                        help="omettre les stations eteintes")
    p_snap.add_argument("--dwell-pad", type=int, default=20)

    args = parser.parse_args()
    handlers = {"probe": cmd_probe, "etat": cmd_etat, "build": cmd_build,
                "live": cmd_live, "snapshot": cmd_snapshot,
                "diagnose": cmd_diagnose,
                "compile-firmware": cmd_compile_firmware,
                "export-stations": cmd_export_stations}
    handlers[args.command](args)


if __name__ == "__main__":
    main()