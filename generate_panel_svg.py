#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_panel_svg.py — Genere le SVG schematique du panneau LED.

Lit layout.json (positions manuelles sur une grille octilineaire) et produit :
  - Le dessin du panneau (fond noir, lignes colorees, stations, etiquettes)
  - Un gabarit de percage (cercles cotes, indexation LED)
  - Un diagnostic des collisions (stations trop proches)

Usage :
  python3 generate_panel_svg.py                       # panneau complet
  python3 generate_panel_svg.py --drill-template      # gabarit de percage seul
  python3 generate_panel_svg.py --check-spacing       # diagnostic sans SVG
"""

from __future__ import annotations
import argparse
import json
import math
import os
import sys

# ---------------------------------------------------------------------------
# Chargement du layout
# ---------------------------------------------------------------------------

def load_layout(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def collect_stations(layout: dict):
    """Extrait toutes les stations avec leurs positions en unites de grille."""
    stations = []  # [{name, x, y, line, color, is_interchange}]
    seen_positions = {}  # (x,y) -> first station name

    for line_id, line_data in layout["lines"].items():
        if line_id == "REM":
            color = line_data["color"]
            for branch_name, branch_stations in line_data["branches"].items():
                for s in branch_stations:
                    _add_station(stations, seen_positions, s, "REM", color)
        else:
            color = line_data["color"]
            for s in line_data["stations"]:
                _add_station(stations, seen_positions, s, line_id, color)

    # Marquer les correspondances
    interchange_names = set()
    for ic in layout.get("interchanges", []):
        interchange_names.add(ic["station"])
    for s in stations:
        s["is_interchange"] = s["name"] in interchange_names

    return stations


def _add_station(stations, seen, s, line_id, color):
    if s.get("_alias"):
        return  # doublon de correspondance, deja ajoute par sa ligne d'origine
    name = s["name"]
    x, y = s["x"], s["y"]
    key = (x, y)
    stations.append({
        "name": name, "x": x, "y": y,
        "line": line_id, "color": color,
        "collision_with": seen.get(key),
    })
    if key not in seen:
        seen[key] = name


def collect_paths(layout: dict):
    """Extrait les polylignes de chaque ligne, pour le trace."""
    paths = []
    for line_id, line_data in layout["lines"].items():
        if line_id == "REM":
            color = line_data["color"]
            trunk = line_data["branches"]["trunk"]
            for branch_name in ("deux_montagnes", "anse_a_lorme"):
                branch = line_data["branches"][branch_name]
                full = trunk + branch
                paths.append({
                    "line": f"REM-{branch_name}",
                    "color": color,
                    "points": [(s["x"], s["y"]) for s in full],
                })
        else:
            color = line_data["color"]
            paths.append({
                "line": line_id,
                "color": color,
                "points": [(s["x"], s["y"]) for s in line_data["stations"]],
            })
    return paths


# ---------------------------------------------------------------------------
# Diagnostic
# ---------------------------------------------------------------------------

def check_spacing(stations, unit_mm, min_mm=9):
    """Verifie que les stations ne sont pas trop proches pour le percage."""
    issues = []
    for i, a in enumerate(stations):
        for b in stations[i+1:]:
            if a["name"] == b["name"]:
                continue
            dx = (a["x"] - b["x"]) * unit_mm
            dy = (a["y"] - b["y"]) * unit_mm
            dist = math.sqrt(dx*dx + dy*dy)
            if dist < min_mm:
                issues.append((a["name"], b["name"], dist, a["line"], b["line"]))
    return sorted(issues, key=lambda t: t[2])


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

DARK_BG = "#1C1C1E"
LABEL_COLOR = "#E0E0E0"
INTERCHANGE_COLOR = "#FFFFFF"
DRILL_COLOR = "#FF3366"

SVG_HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="{width_mm}mm" height="{height_mm}mm"
     viewBox="{vb_x} {vb_y} {vb_w} {vb_h}">
<defs>
  <style>
    .label {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
              font-size: {font_size}px; fill: {label_color}; }}
    .label-left {{ text-anchor: end; }}
    .label-right {{ text-anchor: start; }}
    .label-center {{ text-anchor: middle; }}
    .title {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
              font-size: {title_size}px; fill: {label_color};
              font-weight: 700; letter-spacing: 0.5px; }}
    .legend {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
               font-size: {legend_size}px; fill: {label_color}; }}
    .drill-idx {{ font-family: monospace; font-size: {idx_size}px;
                  fill: {drill_color}; text-anchor: middle; }}
  </style>
</defs>
<rect x="{vb_x}" y="{vb_y}" width="{vb_w}" height="{vb_h}" fill="{bg}"/>
"""

def generate_svg(layout, stations, paths, mode="panel"):
    unit = layout["grid_unit_mm"]
    line_w = layout["line_width_mm"]
    radius = layout["station_radius_mm"]
    rail_gap = layout["rail_gap_mm"]
    font_size = layout["font_size_mm"]

    # Bornes
    all_x = [s["x"] for s in stations]
    all_y = [s["y"] for s in stations]
    margin = 8  # grid units
    min_x = min(all_x) - margin
    max_x = max(all_x) + margin
    min_y = min(all_y) - margin
    max_y = max(all_y) + margin

    vb_x = min_x * unit
    vb_y = min_y * unit
    vb_w = (max_x - min_x) * unit
    vb_h = (max_y - min_y) * unit

    width_mm = vb_w
    height_mm = vb_h

    parts = [SVG_HEADER.format(
        width_mm=width_mm, height_mm=height_mm,
        vb_x=vb_x, vb_y=vb_y, vb_w=vb_w, vb_h=vb_h,
        font_size=font_size, label_color=LABEL_COLOR,
        title_size=font_size * 2, legend_size=font_size * 1.2,
        idx_size=font_size * 0.8, drill_color=DRILL_COLOR,
        bg=DARK_BG,
    )]

    # --- Lignes (double trait pour les deux rails) ---
    for path in paths:
        points = path["points"]
        color = path["color"]
        if len(points) < 2:
            continue

        # Calculer les normales pour le decalage des deux rails
        for offset_sign in (-1, 1):
            poly = []
            for i, (px, py) in enumerate(points):
                # Direction du segment
                if i == 0:
                    dx, dy = points[1][0]-px, points[1][1]-py
                elif i == len(points)-1:
                    dx, dy = px-points[-2][0], py-points[-2][1]
                else:
                    dx, dy = points[i+1][0]-points[i-1][0], points[i+1][1]-points[i-1][1]
                length = math.sqrt(dx*dx + dy*dy) or 1
                nx, ny = -dy/length, dx/length
                off = offset_sign * rail_gap / 2 / unit
                poly.append(f"{(px+nx*off)*unit},{(py+ny*off)*unit}")

            pts_str = " ".join(poly)
            parts.append(
                f'<polyline points="{pts_str}" fill="none" '
                f'stroke="{color}" stroke-width="{line_w}" '
                f'stroke-linecap="round" stroke-linejoin="round" '
                f'opacity="0.85"/>'
            )

    # --- Stations ---
    for s in stations:
        cx, cy = s["x"] * unit, s["y"] * unit
        color = s["color"]

        if s["is_interchange"]:
            # Correspondance : cercle blanc plus grand
            parts.append(
                f'<circle cx="{cx}" cy="{cy}" r="{radius*1.8}" '
                f'fill="{DARK_BG}" stroke="{INTERCHANGE_COLOR}" '
                f'stroke-width="1.5"/>'
            )
        else:
            # Station normale : deux petits cercles (un par rail)
            # Mais ici on trace juste le point central pour la lisibilite
            parts.append(
                f'<circle cx="{cx}" cy="{cy}" r="{radius}" '
                f'fill="{color}" stroke="{DARK_BG}" stroke-width="0.5"/>'
            )

    # --- Etiquettes ---
    for s in stations:
        cx, cy = s["x"] * unit, s["y"] * unit
        name = s["name"]
        # Placement : a droite par defaut, a gauche si la station est a droite du centre
        anchor = "start"
        tx = cx + radius * 2.5
        ty = cy + font_size * 0.35

        # Pour les lignes verticales (orange nord), mettre le label a droite
        # Pour le bas de la carte, a gauche ou en dessous

        parts.append(
            f'<text x="{tx}" y="{ty}" class="label label-right">'
            f'{_svg_escape(name)}</text>'
        )

    # --- Titre ---
    tx = (min_x + 2) * unit
    ty = (min_y + 3) * unit
    parts.append(f'<text x="{tx}" y="{ty}" class="title">MÉTRO + REM</text>')
    parts.append(f'<text x="{tx}" y="{ty + font_size * 2.5}" class="title" '
                 f'style="font-weight:400; font-size:{font_size*1.4}px">'
                 f'Montréal — 192 LED — panneau v1</text>')

    # --- Legende ---
    ly = (min_y + 6) * unit
    line_defs = [
        ("1", "Ligne 1 - Verte", "#008E4F"),
        ("2", "Ligne 2 - Orange", "#EF8122"),
        ("4", "Ligne 4 - Jaune", "#FFD200"),
        ("5", "Ligne 5 - Bleue", "#0083CA"),
        ("REM", "REM", "#00C8BE"),
    ]
    for i, (lid, lname, lcolor) in enumerate(line_defs):
        lx = tx
        ly_i = ly + i * font_size * 2
        parts.append(f'<rect x="{lx}" y="{ly_i - font_size*0.6}" '
                     f'width="{font_size*2}" height="{font_size*0.8}" '
                     f'rx="1" fill="{lcolor}"/>')
        parts.append(f'<text x="{lx + font_size*2.5}" y="{ly_i}" '
                     f'class="legend">{lname}</text>')

    # --- Gabarit de percage (optionnel) ---
    if mode == "drill":
        led_idx = 0
        for s in stations:
            cx, cy = s["x"] * unit, s["y"] * unit
            # Deux trous par station (un par direction)
            for offset in (-rail_gap/2, rail_gap/2):
                hx = cx + offset
                parts.append(
                    f'<circle cx="{hx}" cy="{cy}" r="2.5" '
                    f'fill="none" stroke="{DRILL_COLOR}" '
                    f'stroke-width="0.3" stroke-dasharray="1,1"/>')
                parts.append(
                    f'<text x="{hx}" y="{cy - 3.5}" class="drill-idx">'
                    f'{led_idx}</text>')
                led_idx += 1

    parts.append("</svg>")
    return "\n".join(parts)


def _svg_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Genere le SVG du panneau")
    parser.add_argument("--layout", default="layout.json")
    parser.add_argument("--out", default="panel.svg")
    parser.add_argument("--drill-template", action="store_true",
                        help="ajouter le gabarit de percage")
    parser.add_argument("--check-spacing", action="store_true",
                        help="diagnostic des collisions uniquement")
    parser.add_argument("--min-spacing", type=float, default=9,
                        help="espacement minimum en mm (defaut 9)")
    args = parser.parse_args()

    layout = load_layout(args.layout)
    stations = collect_stations(layout)
    paths = collect_paths(layout)
    unit = layout["grid_unit_mm"]

    print(f"Layout charge : {len(stations)} stations physiques")

    # Diagnostic
    issues = check_spacing(stations, unit, args.min_spacing)
    if issues:
        print(f"\n  COLLISIONS ({len(issues)} paires a moins de {args.min_spacing} mm) :")
        for a, b, dist, la, lb in issues[:20]:
            print(f"    {dist:5.1f} mm  {a} ({la}) <-> {b} ({lb})")
        print(f"\n  Corrigez dans layout.json puis relancez.")
    else:
        print(f"  Espacement OK : toutes les stations a >= {args.min_spacing} mm")

    if args.check_spacing:
        return

    mode = "drill" if args.drill_template else "panel"
    svg = generate_svg(layout, stations, paths, mode)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(svg)
    print(f"\n  {args.out} ecrit ({len(svg) // 1024} Ko)")

    # Dimensions
    all_x = [s["x"] for s in stations]
    all_y = [s["y"] for s in stations]
    w = (max(all_x) - min(all_x) + 16) * unit
    h = (max(all_y) - min(all_y) + 16) * unit
    print(f"  Dimensions du panneau : {w} x {h} mm")
    for name, lmm, hmm in [("A2", 420, 594), ("A1", 594, 841)]:
        fits = "TIENT" if w <= lmm and h <= hmm else "trop grand"
        print(f"    {name} ({lmm}x{hmm} mm) : {fits}")


if __name__ == "__main__":
    main()
