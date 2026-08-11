# main.py — Simulation Wokwi du panneau LED metro de Montreal
# Template : Micropython Pico W
#
# 1. Ouvrir le template "Micropython Pico W" sur wokwi.com
# 2. Cliquer l'onglet diagram.json, remplacer par le contenu ci-dessous
# 3. Coller ce fichier dans main.py
# 4. Lancer la simulation

from machine import Pin
from neopixel import NeoPixel
import time
import math

# --- Configuration ---
N_LEDS = 32
LED_PIN = 0
BRIGHTNESS = 0.4
SPEED = 60  # 1 seconde reelle = 60 minutes simulees

# --- Palette : couleurs des lignes, reparties sur 32 LED ---
LINES = [
    ((0, 200, 190), 8),    # turquoise (REM)
    ((0, 142, 79), 8),     # vert (ligne 1)
    ((239, 129, 34), 8),   # orange (ligne 2)
    ((255, 210, 0), 2),    # jaune (ligne 4)
    ((0, 131, 202), 4),    # bleu (ligne 5)
    ((50, 50, 50), 2),     # gris (remplissage)
]

palette = []
for color, count in LINES:
    for _ in range(count):
        palette.append(color)
palette = palette[:N_LEDS]

# --- NeoPixel ---
strip = NeoPixel(Pin(LED_PIN), N_LEDS)


def set_pixel(i, r, g, b):
    strip[i] = (r, g, b)


def show():
    strip.write()


def fill(r, g, b):
    for i in range(N_LEDS):
        strip[i] = (r, g, b)
    strip.write()


# --- Simulation de trains ---
def train_intensity(led_index, minute, n_in_segment, segment_start, speed=2.5):
    cycle = n_in_segment * speed
    if cycle == 0:
        return 0.0
    pos = (minute % cycle) / speed
    if pos > n_in_segment:
        pos = 2 * n_in_segment - pos
    local = led_index - segment_start
    dist = abs(pos - local)
    if dist < 1.0:
        return 1.0 - dist
    return 0.0


def compute_frame(minute):
    intensities = [0.0] * N_LEDS
    offset = 0
    for color, count in LINES:
        if count < 2:
            offset += count
            continue
        n_trains = 2 if count >= 6 else 1
        for t in range(n_trains):
            phase = minute + t * count * 1.5
            for i in range(count):
                led = offset + i
                if led < N_LEDS:
                    v = train_intensity(led, phase, count, offset)
                    intensities[led] = min(1.0, intensities[led] + v)
        offset += count
    return intensities


# --- Animation de demarrage ---
print("Metro Montreal — simulation Wokwi")
for i in range(N_LEDS):
    r, g, b = palette[i]
    set_pixel(i, int(r * BRIGHTNESS), int(g * BRIGHTNESS), int(b * BRIGHTNESS))
    show()
    time.sleep_ms(30)
time.sleep_ms(500)
fill(0, 0, 0)

# --- Boucle principale ---
t0 = time.ticks_ms()
tick = 0
while True:
    elapsed = time.ticks_diff(time.ticks_ms(), t0) / 1000.0
    minute = (elapsed * SPEED) % 1440

    intensities = compute_frame(minute)

    for i in range(N_LEDS):
        r, g, b = palette[i]
        v = intensities[i] * BRIGHTNESS
        set_pixel(i, int(r * v), int(g * v), int(b * v))
    show()

    tick += 1
    if tick % 100 == 0:
        h = int(minute) // 60
        m = int(minute) % 60
        print("  {:02d}:{:02d}".format(h, m))

    time.sleep_ms(50)
