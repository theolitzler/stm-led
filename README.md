# Panneau LED — Métro + REM de Montréal

Panneau mural affichant en temps simulé la position des rames de métro et du
REM sur 192 LED WS2812B, piloté par un ESP32.

## Architecture

```
  GTFS statique (STM)  ─┐
                        ├─▶  stm_phase0.py build  ─▶  timetable.json
  GTFS fabriqué (REM)  ─┘          │
                                   ▼
                          compile-firmware  ─▶  metro.bin (811 Ko)
                                                    │
                                                    ▼
                                            ESP32 + 192 WS2812B
```

Le calcul lourd (horaires, interpolation, précompilation minute par minute)
se fait sur un ordinateur. L'ESP32 ne fait que lire un offset dans un fichier
binaire et pousser les couleurs vers la chaîne de LED.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests gtfs-realtime-bindings
```

## Pipeline complet

```bash
# 1. Coordonnées du REM depuis OpenStreetMap
python3 osm_to_coords.py overpass.json

# 2. GTFS fabriqué pour le REM (calibré sur Chrono)
python3 make_rem_gtfs.py --coords rem_coords.csv --derive-run-times --scale 1.33

# 3. Compilation : fusion STM + REM, une LED par sens
python3 stm_phase0.py build gtfs_stm.zip gtfs_rem.zip \
        --merge-stations REM --per-direction

# 4. Diagnostic (optionnel mais recommandé)
python3 stm_phase0.py diagnose --at 08:00

# 5. Simulation en console
python3 stm_phase0.py live

# 6. Firmware pour l'ESP32
python3 stm_phase0.py compile-firmware

# 7. Document d'atelier pour le câblage
python3 stm_phase0.py export-stations --clean-names > stations.csv

# 8. SVG du panneau
python3 generate_panel_svg.py
```

## Déploiement sur l'ESP32

```bash
# Flasher MicroPython
esptool.py erase_flash
esptool.py write_flash -z 0x1000 ESP32_GENERIC-20241025-v1.24.0.bin

# Copier les fichiers
mpremote cp firmware_esp32/main.py :main.py
mpremote cp firmware_esp32/config.py :config.py
mpremote cp metro.bin :metro.bin
```

Éditer `config.py` avant de copier : SSID WiFi, mot de passe, fuseau horaire.

## Commandes disponibles

| Commande | Rôle |
|---|---|
| `probe` | Chercher du métro dans le flux GTFS-realtime |
| `etat` | État de service via l'API i3 |
| `build` | Compiler un ou plusieurs GTFS en timetable.json |
| `diagnose` | Mesurer la plausibilité des données |
| `live` | Positions simulées en console |
| `snapshot` | État des LED en JSON |
| `compile-firmware` | Précalculer metro.bin pour l'ESP32 |
| `export-stations` | CSV station → index LED pour le câblage |

## Fichiers

| Fichier | Rôle |
|---|---|
| `stm_phase0.py` | Script principal (build, simulate, compile, export) |
| `make_rem_gtfs.py` | Génère le GTFS du REM depuis rem.info + coordonnées OSM |
| `osm_to_coords.py` | Convertit un export Overpass en coordonnées REM |
| `make_fake_gtfs.py` | GTFS synthétique pour tester sans données réelles |
| `generate_panel_svg.py` | Génère le SVG schématique du panneau |
| `layout.json` | Positions des stations sur la grille schématique |
| `firmware_esp32/main.py` | Firmware MicroPython pour l'ESP32 |
| `firmware_esp32/config.py` | Configuration WiFi, luminosité, GPIO |
| `wokwi/main.py` | Simulation Wokwi (MicroPython, 32 LED de démo) |
| `wokwi/diagram.json` | Diagramme Wokwi (ESP32/Pico W + 2 anneaux NeoPixel) |

## Mise à jour des horaires

Le GTFS de la STM change environ 5 fois par an. Relancer :

```bash
python3 make_rem_gtfs.py --coords rem_coords.csv --derive-run-times --scale 1.33
python3 stm_phase0.py build gtfs_stm.zip gtfs_rem.zip \
        --merge-stations REM --per-direction
python3 stm_phase0.py compile-firmware
mpremote cp metro.bin :metro.bin
```

## Matériel

- ESP32 DevKit (WROOM-32 ou S3) : ~10 $
- 200 pixels WS2812B pré-câblés 12 mm : ~35 $
- Résistance 470 Ω + condensateur 1000 µF : ~2 $
- Panneau contreplaqué 5 mm, ~85 × 60 cm : ~15 $
- Chargeur USB 5V 2A existant
- Budget total : ~65-75 $ CAD

## Licence

Données OSM : © les contributeurs d'OpenStreetMap (ODbL).
Données GTFS STM : conditions du portail développeurs de la STM.
Données REM : horaires et cadences publiés sur rem.info.
