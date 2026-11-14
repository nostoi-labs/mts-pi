#!/bin/bash
# Who uses the Pi's Wi-Fi: the bike or the home network. Runs as root (mtspi-wifi-mode.service).
# - bike: dedicated wpa_supplicant (mtspi-p2p.service) for Wi-Fi Direct, NetworkManager leaves wlan0 alone;
# - home: NetworkManager on the home Wi-Fi (SSH, updates).
# At boot it starts in bike mode (the case that has to be quick). It moves to home only if the bike does not
# connect over Bluetooth for BIKE_GRACE seconds and a known Wi-Fi network is in sight (HOME_SSID, or any network
# saved on the Pi); without one it stays in bike mode. It goes back to bike as
# soon as the bike connects. It also keeps the Bluetooth link to the bike up (the bike does not open it by
# itself) and the iPhone's oFono modem powered (off = the dashboard shows "no service").
set -u
[ -r /etc/default/mtspi ] && . /etc/default/mtspi
HOME_SSID=${HOME_SSID:-}
BIKE_GRACE=${BIKE_GRACE:-45}
WPA="/usr/sbin/wpa_cli -p ${WPA_CTRL:-/run/wpa_p2p} -i wlan0"
STATE=/run/mtspi-wifi-mode

log() { echo "$*"; }

bike_path() {   # the paired device whose name starts with DUCATI, on any radio (or BIKE_ADDR if set)
    local p
    for p in $(busctl tree --list org.bluez 2>/dev/null | grep -E '^/org/bluez/hci[0-9]+/dev_[0-9A-F_]+$'); do
        if [ -n "${BIKE_ADDR:-}" ]; then
            [ "${p##*/dev_}" = "${BIKE_ADDR//:/_}" ] && { echo "$p"; return; }
        elif busctl get-property org.bluez "$p" org.bluez.Device1 Name 2>/dev/null | grep -q '"DUCATI'; then
            echo "$p"; return
        fi
    done
}

PRO=0; case "${RADIO:-internal}" in esp32phone|pro) PRO=1 ;; esac   # pro variant: the bike is on the external radio, not on BlueZ

bike_connected() {
    if [ "$PRO" = 1 ]; then grep -q 1 /tmp/mtspi-esp32-slc 2>/dev/null; return; fi
    [ -n "$BIKE" ] && [ "$(busctl get-property org.bluez "$BIKE" org.bluez.Device1 Connected 2>/dev/null)" = "b true" ]
}

profiles_ready() {   # phonebook (PBAP) and mySPIN registered on the bike's radio: the bike looks for them only when it connects
    local u
    u=$(busctl get-property org.bluez "${BIKE%/dev_*}" org.bluez.Adapter1 UUIDs 2>/dev/null)
    echo "$u" | grep -qi 0000112f && echo "$u" | grep -qi 31fd954e
}

radio_busy() {   # another device connected on the bike's radio (a headset, a bench device)
    local p
    for p in $(busctl tree --list org.bluez 2>/dev/null | grep -E "^${BIKE%/dev_*}/dev_[0-9A-F_]+$"); do
        [ "$p" = "$BIKE" ] && continue
        [ "$(busctl get-property org.bluez "$p" org.bluez.Device1 Connected 2>/dev/null)" = "b true" ] && return 0
    done
    return 1
}

modem_on() {
    local m
    m=$(busctl call org.ofono / org.ofono.Manager GetModems 2>/dev/null | grep -o '/hfp/[^"]*' | head -1)
    [ -n "$m" ] && busctl call org.ofono "$m" org.ofono.Modem GetProperties 2>/dev/null | grep -q '"Powered" b false' \
        && busctl call org.ofono "$m" org.ofono.Modem SetProperty sv Powered b true && log "oFono modem powered on"
}

home_ssids() {   # HOME_SSID if set; otherwise every Wi-Fi network saved on the Pi, except the bike's own
    if [ -n "$HOME_SSID" ]; then echo "$HOME_SSID"; return; fi
    local u
    for u in $(nmcli -t -f UUID,TYPE connection show 2>/dev/null | awk -F: '$2 == "802-11-wireless" { print $1 }'); do
        [ "$(nmcli -g ipv4.never-default connection show "$u" 2>/dev/null)" = yes ] && continue
        nmcli --escape no -g 802-11-wireless.ssid connection show "$u" 2>/dev/null
    done
}

home_visible() {   # in bike mode the scan is done by our own wpa_supplicant
    local list
    list=$(home_ssids)
    [ -n "$list" ] || return 1
    $WPA scan >/dev/null 2>&1 || return 1
    sleep 5
    $WPA scan_results 2>/dev/null | awk -F'\t' 'NR == FNR { want[$0] = 1; next } ($5 in want) { f = 1 } END { exit !f }' <(echo "$list") -
}

to_bike() {
    log "bike mode"
    echo bike >$STATE
    rfkill unblock wlan 2>/dev/null || true   # a fresh system keeps Wi-Fi blocked until a country is set
    systemctl start mtspi-p2p     # its ExecStartPre takes wlan0 away from NetworkManager
}

to_home() {
    log "home mode: no bike for ${BIKE_GRACE} s and a known Wi-Fi network in sight"
    echo home >$STATE
    systemctl stop mtspi-p2p
    local i
    for i in $(ls /sys/class/net | grep '^p2p-wlan0-'); do iw dev "$i" del 2>/dev/null; done
    nmcli device set wlan0 managed yes
}

if [ "${MODE_LOCK:-}" = home ]; then    # bench: stay on the home Wi-Fi even if the bench target connects
    log "MODE_LOCK=home: staying in home mode"
    to_home
    # standard variant on the bench: the Pi keeps calling the bench target (BENCH_TARGET, a Bluetooth address)
    # until it is connected, even if it was switched on before the Pi. In the pro variant the bridge does it.
    case "${RADIO:-internal}" in esp32phone|pro) exec sleep infinity ;; esac
    TARGET=${BENCH_TARGET:-${ESP32_TARGET:-}}
    T=""; [ -n "$TARGET" ] && T=$(busctl tree --list org.bluez 2>/dev/null | grep -E "^/org/bluez/hci[0-9]+/dev_${TARGET//:/_}$" | head -1)
    [ -n "$T" ] || exec sleep infinity
    while true; do
        if [ "$(busctl get-property org.bluez "$T" org.bluez.Device1 Connected 2>/dev/null)" != "b true" ] \
            && [ "$(busctl get-property org.bluez "$T" org.bluez.Device1 Blocked 2>/dev/null)" != "b true" ]; then
            busctl --timeout=8 call org.bluez "$T" org.bluez.Device1 Connect >/dev/null 2>&1 && log "bench: ${TARGET} connected"
        fi
        sleep 15
    done
fi
[ -n "$(home_ssids)" ] || log "no Wi-Fi network saved on the Pi: it stays in bike mode"
BIKE=""; [ "$PRO" = 1 ] || BIKE=$(bike_path)
log "bike: ${BIKE:-not paired}$([ "$PRO" = 1 ] && echo " (pro variant: link through the external radio)")"
to_bike
last_bike=$(date +%s)
started=$last_bike
tries=0
early=0
while true; do
    if [ "$PRO" = 1 ]; then
        if bike_connected; then
            last_bike=$(date +%s)
            [ "$(cat $STATE)" = bike ] || to_bike
        fi
        if [ "$(cat $STATE)" = bike ] && [ $(( $(date +%s) - last_bike )) -ge "$BIKE_GRACE" ] && home_visible; then
            to_home
        fi
        sleep 4
        continue
    fi
    [ -z "$BIKE" ] && BIKE=$(bike_path)
    if bike_connected; then
        last_bike=$(date +%s)
        [ "$(cat $STATE)" = bike ] || to_bike
        modem_on
        # connected before phonebook and mySPIN were registered (the bike opened the link): one reconnection
        # round, otherwise the Phone menu stays grey until the next connection
        if ! profiles_ready; then
            early=1
        elif [ "$early" = 1 ]; then
            early=0
            log "phonebook ready after the connection: reconnecting the bike"
            busctl --timeout=8 call org.bluez "$BIKE" org.bluez.Device1 Disconnect >/dev/null 2>&1
            sleep 2
            continue
        fi
    elif [ -n "$BIKE" ]; then
        # at home it tries rarely and only when the radio is free: every attempt keeps it busy for ~5 s and
        # music to another device on the same radio stuttered
        tries=$((tries + 1))
        # waits at most 60 s for the bridge service, then connects anyway (music and calls)
        if ! profiles_ready && [ $(( $(date +%s) - started )) -lt 60 ]; then
            :
        elif [ "$(cat $STATE)" = bike ] || { [ $((tries % 15)) -eq 0 ] && ! radio_busy; }; then
            busctl --timeout=8 call org.bluez "$BIKE" org.bluez.Device1 Connect >/dev/null 2>&1 \
                && { log "bike connected"; continue; }
        fi
    fi
    if [ "$(cat $STATE)" = bike ] && [ $(( $(date +%s) - last_bike )) -ge "$BIKE_GRACE" ] && home_visible; then
        to_home
    fi
    sleep 4
done
