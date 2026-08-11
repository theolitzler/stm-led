# main.py — Firmware du panneau LED metro de Montreal (ESP32)
#
# Autonome : lit metro.bin depuis la flash, pas de dependance reseau
# une fois l'heure synchronisee.
#
# Installation :
#   1. Flasher MicroPython sur l'ESP32 :
#      esptool.py erase_flash
#      esptool.py write_flash -z 0x1000 ESP32_GENERIC-20241025-v1.24.0.bin
#   2. Copier ce fichier et metro.bin sur l'ESP32 :
#      mpremote cp main.py :main.py
#      mpremote cp metro.bin :metro.bin
#   3. Creer config.py (voir ci-dessous) puis :
#      mpremote cp config.py :config.py
#   4. Redemarrer l'ESP32
#
# config.py (a la racine de l'ESP32) :
#   WIFI_SSID = "ton_reseau"
#   WIFI_PASS = "ton_mot_de_passe"
#   TZ_OFFSET = -4          # UTC-4 = Montreal (EDT), -5 en hiver (EST)
#   BRIGHTNESS = 0.3        # 0.0 a 1.0
#   LED_PIN = 13            # GPIO du fil de donnees
#   N_LEDS = 192            # nombre de LED dans la chaine

import machine
import neopixel
import struct
import time

# ---------------------------------------------------------------------------
# Configuration (valeurs par defaut, surchargees par config.py)
# ---------------------------------------------------------------------------

WIFI_SSID = ""
WIFI_PASS = ""
TZ_OFFSET = -4
BRIGHTNESS = 0.3
LED_PIN = 13
N_LEDS = 192
NTP_INTERVAL = 3600     # resynchroniser toutes les heures
REFRESH_SECS = 2        # secondes entre deux mises a jour des LED
BIN_FILE = "/metro.bin"

try:
    from config import *    # noqa: F401,F403
    print("config.py charge")
except ImportError:
    print("config.py absent — valeurs par defaut")


# ---------------------------------------------------------------------------
# WiFi + NTP
# ---------------------------------------------------------------------------

_ntp_unix = 0
_ntp_mono = 0


def wifi_connect(timeout=15):
    import network
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    if sta.isconnected():
        return True
    if not WIFI_SSID:
        print("WiFi : pas de SSID configure")
        return False
    print("WiFi : connexion a", WIFI_SSID)
    sta.connect(WIFI_SSID, WIFI_PASS)
    t0 = time.ticks_ms()
    while not sta.isconnected():
        if time.ticks_diff(time.ticks_ms(), t0) > timeout * 1000:
            print("WiFi : timeout")
            return False
        time.sleep_ms(200)
    print("WiFi : connecte,", sta.ifconfig()[0])
    return True


def sync_ntp():
    """Synchronise l'heure via NTP. Retourne True en cas de succes."""
    global _ntp_unix, _ntp_mono
    try:
        if not wifi_connect():
            return False
        import ntptime
        ntptime.settime()  # regle l'horloge RTC interne de l'ESP32
        # ntptime.settime() met l'heure en UTC. On applique le decalage.
        now = time.time() + TZ_OFFSET * 3600
        _ntp_unix = now
        _ntp_mono = time.ticks_ms()
        lt = time.localtime(now)
        print("NTP : {:02d}:{:02d}:{:02d}".format(lt[3], lt[4], lt[5]))
        return True
    except Exception as e:
        print("NTP echec :", e)
        return False


def now():
    """Retourne (day_type, minute_du_jour).

    day_type : 0=semaine, 1=samedi, 2=dimanche.
    Utilise l'heure NTP corrigee du fuseau si disponible,
    sinon l'horloge RTC interne (imprecise sans synchro).
    """
    if _ntp_unix > 0:
        elapsed_ms = time.ticks_diff(time.ticks_ms(), _ntp_mono)
        unix = _ntp_unix + elapsed_ms // 1000
        t = time.localtime(unix)
    else:
        # Repli : horloge interne (UTC), avec offset manuel
        t = time.localtime(time.time() + TZ_OFFSET * 3600)

    weekday = t[6]  # 0=lundi en MicroPython
    if weekday < 5:
        day_type = 0
    elif weekday == 5:
        day_type = 1
    else:
        day_type = 2

    minute = t[3] * 60 + t[4]
    return day_type, minute


# ---------------------------------------------------------------------------
# Lecteur du fichier binaire
# ---------------------------------------------------------------------------

class MetroBin:
    """Lit le fichier precalcule par compile-firmware."""

    def __init__(self, path):
        self.f = open(path, "rb")

        # Header : 4B magic + 1B version + 1B n_days + 2B n_minutes + 2B n_leds
        header = self.f.read(10)
        if header[:4] != b"MTLM":
            raise ValueError("magic invalide")
        self.version = header[4]
        self.n_days = header[5]
        self.n_minutes = struct.unpack_from("<H", header, 6)[0]
        self.n_leds = struct.unpack_from("<H", header, 8)[0]

        # Palette : n_leds * 3 octets RGB
        pal_raw = self.f.read(self.n_leds * 3)
        self.palette = []
        for i in range(self.n_leds):
            self.palette.append((pal_raw[i*3], pal_raw[i*3+1], pal_raw[i*3+2]))

        self.data_offset = 10 + self.n_leds * 3
        self.day_size = self.n_minutes * self.n_leds
        self._buf = bytearray(self.n_leds)

    def frame(self, day_type, minute):
        """Lit une trame : n_leds octets d'intensite."""
        day = min(day_type, self.n_days - 1)
        mins = min(minute, self.n_minutes - 1)
        offset = self.data_offset + day * self.day_size + mins * self.n_leds
        self.f.seek(offset)
        self.f.readinto(self._buf)
        return self._buf

    def apply(self, strip, day_type, minute, brightness=1.0):
        """Ecrit les couleurs directement dans le strip NeoPixel."""
        intensities = self.frame(day_type, minute)
        n = min(self.n_leds, len(strip))
        for i in range(n):
            intensity = intensities[i]
            if intensity == 0:
                strip[i] = (0, 0, 0)
            else:
                r, g, b = self.palette[i]
                strip[i] = (
                    int(r * intensity * brightness / 255),
                    int(g * intensity * brightness / 255),
                    int(b * intensity * brightness / 255),
                )


# ---------------------------------------------------------------------------
# Boucle principale
# ---------------------------------------------------------------------------

def startup_animation(strip, metro):
    """Parcourt toute la chaine pour verifier le cablage."""
    n = min(metro.n_leds, len(strip))
    for i in range(n):
        strip[i] = metro.palette[i]
        strip.write()
        time.sleep_ms(8)
    time.sleep_ms(400)
    for i in range(n):
        strip[i] = (0, 0, 0)
    strip.write()


def main():
    print("Metro Montreal — demarrage ESP32")
    print("  firmware v1, binaire :", BIN_FILE)

    # Charger le binaire
    try:
        metro = MetroBin(BIN_FILE)
    except Exception as e:
        print("ERREUR :", e)
        print("Copiez metro.bin sur l'ESP32 avec : mpremote cp metro.bin :metro.bin")
        return
    print("  {} LED, {} types de jour".format(metro.n_leds, metro.n_days))

    # Initialiser le strip NeoPixel
    pin = machine.Pin(LED_PIN)
    n_pixels = min(N_LEDS, metro.n_leds)
    strip = neopixel.NeoPixel(pin, n_pixels)
    print("  NeoPixel sur GPIO{}, {} pixels".format(LED_PIN, n_pixels))

    # Animation de demarrage
    startup_animation(strip, metro)

    # Synchroniser l'heure
    synced = sync_ntp()
    if not synced:
        print("  Horloge non synchronisee — l'heure sera approximative")
        print("  Pour configurer le WiFi, creez config.py")

    last_ntp = time.ticks_ms()
    last_minute = -1

    print("  Luminosite : {}%".format(int(BRIGHTNESS * 100)))
    print("  Pret.\n")

    while True:
        # Resynchronisation periodique
        if time.ticks_diff(time.ticks_ms(), last_ntp) > NTP_INTERVAL * 1000:
            sync_ntp()
            last_ntp = time.ticks_ms()

        day_type, minute = now()

        # Ne mettre a jour que si la minute a change
        if minute != last_minute:
            metro.apply(strip, day_type, minute, BRIGHTNESS)
            strip.write()
            last_minute = minute

            # Log toutes les 15 minutes
            if minute % 15 == 0:
                h, m = minute // 60, minute % 60
                jours = ("sem", "sam", "dim")
                lit = sum(1 for i in range(n_pixels) if strip[i] != (0, 0, 0))
                print("  {:02d}:{:02d} ({}) — {} LED".format(h, m, jours[day_type], lit))

        time.sleep(REFRESH_SECS)


main()
