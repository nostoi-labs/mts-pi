#!/bin/bash
# Names the Pi shows to the bike and to the iPhone. Writes BT_NAME / BT_NAME_PRO / BT_INFO to /etc/default/mtspi,
# the BlueZ Bluetooth name (main.conf) and the Wi-Fi Direct name (the bike-side wpa_supplicant).
# Pairings are tied to the address, not to the name: this script does not touch them.
# Usage: sudo system/names.sh ["MTS Pi"]   then reboot (or restart mtspi-adapters, mtspi-bridge and mtspi-hfp)
set -eu
[ "$(id -u)" = 0 ] || { echo "run it with sudo"; exit 1; }
NAME=${1:-MTS Pi}
PRO=${2:-$NAME Pro}
DEF=/etc/default/mtspi
touch "$DEF"
setq() { sed -i "/^$1=/d" "$DEF"; echo "$1=\"$2\"" >> "$DEF"; }   # quoted: names contain spaces
setq BT_NAME "$NAME"
setq BT_NAME_PRO "$PRO"
setq BT_INFO "[HFP,A2DP]$NAME"
python3 - "$NAME" <<'PY'
import configparser, sys
p = "/etc/bluetooth/main.conf"
c = configparser.ConfigParser(strict=False, interpolation=None)
c.optionxform = str
c.read(p)
if not c.has_section("General"):
    c.add_section("General")
c.set("General", "Name", sys.argv[1])
with open(p, "w") as f:
    c.write(f)
PY
P2P=/etc/wpa_supplicant/mtspi-p2p.conf
[ -f "$P2P" ] && sed -i "s/^device_name=.*/device_name=$NAME/" "$P2P"
echo "name shown to the bike and to the iPhone: \"$NAME\""
