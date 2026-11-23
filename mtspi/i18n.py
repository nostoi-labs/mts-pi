"""Pi UI texts in several languages (English by default)."""
from __future__ import annotations

LANGS = ["en", "it"]
LANG_NAMES = {"en": "English", "it": "Italiano"}
_lang = "en"

S = {
    # home: CarPlay
    "carplay": ("CarPlay", "CarPlay"),
    "cp_off": ("Off", "Spento"),
    "cp_off_sub": ("ready when you are", "pronto quando vuoi"),
    "cp_connecting": ("Connecting", "Collegamento"),
    "cp_on": ("Active", "Attivo"),
    "cp_on_sub": ("iPhone connected", "iPhone collegato"),
    "cp_nodongle": ("Not connected", "Non collegato"),
    "cp_nodongle_sub": ("Carlinkit not found", "Carlinkit non trovato"),
    "btn_start": ("Start CarPlay", "Avvia CarPlay"),
    "btn_wait": ("Please wait…", "Attendi…"),
    "btn_back_cp": ("Back to CarPlay", "Torna a CarPlay"),
    "btn_off": ("Turn off", "Spegni"),
    # home: connections
    "links": ("Connections", "Connessioni"),
    "btn_diag": ("Diagnostics", "Diagnostica"),
    "btn_settings": ("Settings", "Impostazioni"),
    "btn_tyres": ("Tyres", "Gomme"),
    "bt": ("Bluetooth", "Bluetooth"),
    "bt_wifi": ("BT + Wi-Fi", "BT + Wi-Fi"),
    "node_bike": ("Bike", "Moto"),
    "audio": ("Audio", "Audio"),
    # diagnostics
    "diag": ("Diagnostics", "Diagnostica"),
    "uptime_back": ("Pi up {up}  ·  OK or hold ↑ = back", "Pi acceso da {up}  ·  OK o ↑ tenuto = indietro"),
    "col_screen": ("Screen · mySPIN", "Schermo · mySPIN"),
    "col_links": ("Connections", "Collegamenti"),
    "col_system": ("System", "Sistema"),
    "d_session": ("session", "sessione"), "active": ("active", "attiva"), "absent": ("none", "assente"),
    "d_version": ("mySPIN version", "versione mySPIN"), "d_screen": ("screen", "schermo"),
    "d_pixfmt": ("pixel format", "formato pixel"), "d_fps": ("fps sent", "fps inviati"),
    "d_jpeg": ("avg JPEG", "JPEG medio"), "d_frames": ("total frames", "fotogrammi totali"),
    "d_keys": ("keys received", "tasti ricevuti"), "d_view": ("view", "vista"), "home": ("home", "home"),
    "d_pings": ("pings sent", "ping inviati"),
    "d_iphone": ("iPhone Bluetooth", "iPhone Bluetooth"), "not_connected": ("not connected", "non collegato"),
    "d_profiles": ("  profiles", "  profili"), "d_bike_bt": ("Bike Bluetooth", "Moto Bluetooth"),
    "connected": ("connected", "collegata"), "no": ("no", "no"),
    "d_bike_wifi": ("Bike Wi-Fi", "Wi-Fi moto"), "d_band": ("  band · signal", "  banda · segnale"),
    "d_ck_usb": ("Carlinkit USB", "Carlinkit USB"), "present": ("present", "presente"), "missing": ("missing", "assente"),
    "d_cp": ("  CarPlay", "  CarPlay"), "st_off": ("off", "spento"), "st_connecting": ("connecting", "collegamento"),
    "st_on": ("active", "attivo"), "d_software": ("  software", "  software"),
    "d_ck_video": ("  video from dongle", "  video dal dongle"), "d_audio_src": ("audio to bike", "audio verso moto"),
    "d_music": ("music BT · CarPlay", "musica BT · CarPlay"),
    "d_temp": ("CPU temperature", "temperatura CPU"), "d_load": ("load 1·5·15 min", "carico 1·5·15 min"),
    "d_mem": ("memory used", "memoria usata"), "d_volt": ("under-voltage", "sottotensione"), "none": ("none", "nessuna"),
    "d_uptime": ("uptime", "acceso da"), "d_ibat": ("iPhone battery", "iPhone batteria"),
    "d_isig": ("iPhone signal", "iPhone segnale"), "d_call": ("call", "chiamata"),
    "d_contacts": ("contacts · SMS", "contatti · SMS"),
    "d_vehicle": ("vehicle data", "dati veicolo"), "v_none": ("none yet", "ancora nessuno"),
    "v_unsupported": ("not in mySPIN 1.1", "assenti in mySPIN 1.1"),
    "na_bench": ("n/a (bench)", "n/d (banco)"),
    "emulated": ("emulated (bench)", "emulato (banco)"), "src_none": ("none", "nessuna"),
    # tyres
    "tyres": ("Tyre pressure", "Pressione gomme"),
    "tyres_back": ("OK or hold ↑ = back", "OK o ↑ tenuto = indietro"),
    "ty_front": ("Front", "Anteriore"), "ty_rear": ("Rear", "Posteriore"), "ty_sensor": ("Sensor", "Sensore"),
    "ty_wait": ("listening for the sensors…", "in ascolto dei sensori…"),
    "ty_wait_sub": ("they speak about every minute and a half", "parlano circa ogni minuto e mezzo"),
    "ty_off": ("The listener is not running", "L'ascolto non è attivo"),
    "ty_ago_s": ("{v} s ago", "{v} s fa"), "ty_ago_min": ("{v} min ago", "{v} min fa"),
    "ty_recommended": ("recommended {a} · loaded {b}", "consigliata {a} · a pieno carico {b}"),
    "ty_recommended_1": ("recommended {a}", "consigliata {a}"),
    "ty_battery": ("battery {v} V", "pila {v} V"),
    "ty_low": ("low", "bassa"), "ty_high": ("high", "alta"), "ty_ok": ("ok", "ok"),
    # settings
    "settings": ("Settings", "Impostazioni"),
    "settings_help": ("↑ ↓ choose · ← → OK change · hold ↑ = save and back", "↑ ↓ scegli · ← → OK cambia · ↑ tenuto = salva ed esci"),
    "s_back": ("Save and go back", "Salva e torna indietro"),
    "s_lang": ("Language", "Lingua"),
    "s_autostart": ("CarPlay at start-up", "CarPlay all'accensione"),
    "s_on_demand": ("on demand", "a richiesta"), "s_auto": ("automatic", "automatico"),
    "s_idle": ("Turn CarPlay off outside Ducati Connect", "Spegni CarPlay fuori da Ducati Connect"),
    "s_never": ("never", "mai"), "s_after": ("after {v}", "dopo {v}"),
    "s_quality": ("Image quality", "Qualità immagine"),
    "s_fps": ("CarPlay smoothness", "Fluidità di CarPlay"),
    "s_fps_n": ("{v} fps (cooler)", "{v} fps (scalda meno)"), "s_fps_max": ("maximum ({v} fps)", "massima ({v} fps)"),
    "s_q_high": ("high", "alta"), "s_q_med": ("medium", "media"), "s_q_low": ("low (more fps)", "bassa (più fps)"),
}


def set_lang(lang: str) -> None:
    global _lang
    _lang = lang if lang in LANGS else "en"


def lang() -> str:
    return _lang


def t(key: str, **fmt) -> str:
    pair = S.get(key)
    if pair is None:
        return key
    text = pair[0] if _lang == "en" else pair[1]
    return text.format(**fmt) if fmt else text
