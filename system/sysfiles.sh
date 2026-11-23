#!/bin/bash
# The files of ours that live outside the program folder: the service units and a few small configuration files.
# One place for them, used twice: by install.sh during an installation, and by system/update.py after an update
# (then the system being written may not be the one that is running: a protected card is updated from the outside).
#
#     sysfiles.sh ROOT REPO USER
#
#   ROOT   "" for the running system, or the folder where the system to write is mounted
#   REPO   where the program is when that system runs (/opt/mts-pi on the card image)
#   USER   the user the services run as
#
# It copies from the folder this script is in. It can be repeated, needs no network, starts and stops nothing:
# whoever calls it reloads or restarts what is needed. Run it as root.
set -euo pipefail                # pipefail: a file of ours that is missing stops the script, it does not install an empty one
ROOT=${1%/}; REPO=$2; U=$3
SRC=$(cd "$(dirname "$0")" && pwd)
ENTRY=$(awk -F: -v u="$U" '$1 == u {print $3 ":" $4 ":" $6}' "$ROOT/etc/passwd")
[ -n "$ENTRY" ] || { echo "sysfiles: no user $U in $ROOT/etc/passwd"; exit 1; }
UID_N=${ENTRY%%:*}; REST=${ENTRY#*:}; GID_N=${REST%%:*}; UHOME=${REST#*:}
sc() { if [ -n "$ROOT" ]; then systemctl --root="$ROOT" "$@"; else systemctl "$@"; fi; }
# a file appears whole or not at all, also when the power goes while it is written
put() { cat > "$1.new"; chmod "${2:-644}" "$1.new"; mv "$1.new" "$1"; }

# service units
mkdir -p "$ROOT/etc/systemd/system"
for u in "$SRC"/units/*.service "$SRC"/units/*.path; do
    sed -e "s#@REPO@#$REPO#g" -e "s#@USER@#$U#g" -e "s#@UID@#$UID_N#g" "$u" | put "$ROOT/etc/systemd/system/$(basename "$u")"
done

# permissions: NetworkManager, oFono, the Carlinkit on USB; NetworkManager keeps away from the Wi-Fi Direct interfaces
mkdir -p "$ROOT/etc/polkit-1/rules.d" "$ROOT/etc/dbus-1/system.d" "$ROOT/etc/udev/rules.d" "$ROOT/etc/NetworkManager/conf.d"
sed "s/@USER@/$U/g" "$SRC/conf/50-mtspi-nm.rules" | put "$ROOT/etc/polkit-1/rules.d/50-mtspi-nm.rules"
sed "s/@USER@/$U/g" "$SRC/conf/mtspi-ofono.conf" | put "$ROOT/etc/dbus-1/system.d/mtspi-ofono.conf"
for f in 90-mtspi-bt.rules 60-mtspi-carlinkit.rules; do put "$ROOT/etc/udev/rules.d/$f" < "$SRC/conf/$f"; done
put "$ROOT/etc/NetworkManager/conf.d/99-mtspi-p2p.conf" < "$SRC/conf/99-mtspi-p2p.conf"

# PipeWire: Bluetooth roles, calls through oFono. With the Carlinkit, music and voice go through CarPlay: the Pi must
# not be an audio output for the iPhone.
WP="$ROOT$UHOME/.config/wireplumber/wireplumber.conf.d"
mkdir -p "$WP"
put "$WP/51-mtspi-bluez.conf" < "$SRC/conf/51-mtspi-bluez.conf"
# (the settings of a protected card are not in the system being written: update.py says where they are)
if grep -q '^CARLINKIT=usb' "${MTSPI_SETTINGS:-$ROOT/etc/default/mtspi}" 2>/dev/null; then
    put "$WP/52-mtspi-carlinkit.conf" < "$SRC/conf/52-mtspi-carlinkit.conf"
else
    rm -f "$WP/52-mtspi-carlinkit.conf"
fi
chown -R "$UID_N:$GID_N" "$ROOT$UHOME/.config" 2>/dev/null || true

# which services start with the system
sc disable mtspi-p2p 2>/dev/null || true     # started and stopped by mtspi-wifi-mode
sc enable ofono mtspi-setup mtspi-firstboot mtspi-adapters mtspi-hfp mtspi-wifi-mode mtspi-btcentral mtspi-bridge
sc enable mtspi-tpms 2>/dev/null || true         # experimental, runs only with TPMS=1 in the settings
sc enable mtspi-uart-flow 2>/dev/null || true    # does nothing unless Bluetooth is on the mini UART
# the card image only (install.sh --image leaves this mark): the protection against power cuts, and the updates
if [ -e "$ROOT/etc/mtspi-image" ]; then
    sc enable mtspi-data-check mtspi-logs mtspi-btstore-load mtspi-btstore mtspi-clock-load mtspi-clock-save.path \
        mtspi-protect mtspi-update
fi
