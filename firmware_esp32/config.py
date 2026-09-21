# config.py — Configuration du panneau LED
# Copiez sur l'ESP32 : mpremote cp config.py :config.py

WIFI_SSID = "Penny get your own wifi"
WIFI_PASS = "2068Bonsai!"

TZ_OFFSET = -4          # -4 = Montreal en ete (EDT), -5 en hiver (EST)
BRIGHTNESS = 0.3        # 0.0 a 1.0 (0.3 est deja lumineux dans le noir)

LED_PIN = 13            # GPIO connecte au fil de donnees (DIN du premier pixel)
N_LEDS = 192            # nombre total de LED dans la chaine
