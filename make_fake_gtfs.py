#!/usr/bin/env python3
"""Fabrique un GTFS synthetique qui imite la structure de celui de la STM."""
import zipfile

LINES = {
    "1": ("Verte", ["Angrignon", "Monk", "Jolicoeur", "Verdun", "De l'Eglise",
                    "LaSalle", "Charlevoix", "Lionel-Groulx", "Atwater",
                    "Guy-Concordia", "Peel", "McGill", "Place-des-Arts",
                    "Saint-Laurent", "Berri-UQAM"]),
    "2": ("Orange", ["Cote-Vertu", "Du College", "De la Savane", "Namur",
                     "Plamondon", "Cote-Sainte-Catherine", "Snowdon",
                     "Villa-Maria", "Vendome", "Place-Saint-Henri",
                     "Lionel-Groulx", "Georges-Vanier", "Lucien-L'Allier",
                     "Bonaventure", "Square-Victoria", "Place-d'Armes",
                     "Champ-de-Mars", "Berri-UQAM"]),
    "4": ("Jaune", ["Berri-UQAM", "Jean-Drapeau", "Longueuil"]),
}

DWELL = 25          # secondes a quai
RUN_TIME = 105      # secondes entre deux stations
HEADWAYS = [("05:30:00", "06:30:00", 480),
            ("06:30:00", "09:30:00", 210),
            ("09:30:00", "15:30:00", 330),
            ("15:30:00", "18:30:00", 210),
            ("18:30:00", "25:00:00", 420)]


def hms(total):
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def build(path="fake_gtfs.zip"):
    agency = ["agency_id,agency_name,agency_url,agency_timezone",
              "STM,Societe de transport de Montreal,https://www.stm.info,America/Montreal"]

    routes = ["route_id,route_short_name,route_long_name,route_type"]
    stops = ["stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station"]
    trips = ["trip_id,route_id,service_id,direction_id,trip_headsign"]
    stop_times = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]
    frequencies = ["trip_id,start_time,end_time,headway_secs,exact_times"]
    calendar = ["service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
                "sunday,start_date,end_date",
                "SEM,1,1,1,1,1,0,0,20260101,20261231",
                "SAM,0,0,0,0,0,1,0,20260101,20261231",
                "DIM,0,0,0,0,0,0,1,20260101,20261231"]
    calendar_dates = ["service_id,date,exception_type"]

    seen_stations = set()
    lat, lon = 45.50, -73.58

    for route_id, (colour, station_names) in LINES.items():
        routes.append(f"{route_id},{route_id},Ligne {route_id} - {colour},1")

        for offset, name in enumerate(station_names):
            station_id = f"ST{abs(hash(name)) % 100000:05d}"
            platform_id = f"{station_id}_{route_id}"
            if station_id not in seen_stations:
                stops.append(f"{station_id},{name},"
                             f"{lat + offset * 0.004:.6f},{lon + offset * 0.004:.6f},1,")
                seen_stations.add(station_id)
            stops.append(f"{platform_id},{name},"
                         f"{lat + offset * 0.004:.6f},{lon + offset * 0.004:.6f},0,"
                         f"{station_id}")

        for direction in ("0", "1"):
            ordered = station_names if direction == "0" else list(reversed(station_names))
            trip_id = f"T{route_id}D{direction}"
            trips.append(f"{trip_id},{route_id},SEM,{direction},{ordered[-1]}")

            clock = 5 * 3600 + 30 * 60  # 05:30:00, reference de frequences.txt
            for seq, name in enumerate(ordered, start=1):
                station_id = f"ST{abs(hash(name)) % 100000:05d}"
                platform_id = f"{station_id}_{route_id}"
                arrive = clock
                depart = clock + DWELL
                stop_times.append(f"{trip_id},{hms(arrive)},{hms(depart)},"
                                  f"{platform_id},{seq}")
                clock = depart + RUN_TIME

            for start, end, headway in HEADWAYS:
                frequencies.append(f"{trip_id},{start},{end},{headway},0")

    files = {
        "agency.txt": agency, "routes.txt": routes, "stops.txt": stops,
        "trips.txt": trips, "stop_times.txt": stop_times,
        "frequencies.txt": frequencies, "calendar.txt": calendar,
        "calendar_dates.txt": calendar_dates,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, lines in files.items():
            zf.writestr(name, "\n".join(lines) + "\n")
    print(f"{path} ecrit ({len(stop_times) - 1} lignes de stop_times)")


if __name__ == "__main__":
    build()
