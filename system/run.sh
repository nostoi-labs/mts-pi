#!/bin/bash
# Starts the bridge (mtspi-bridge.service): the Pi's home screen on the dashboard, CarPlay through the Carlinkit
# and the Bluetooth bridge iPhone -> Pi -> bike (music, calls, phonebook).
# Settings come from /etc/default/mtspi. Each run writes its log (JSON lines) to a new folder under $DATA, or under
# MTSPI_LOG_DIR when that is set (a folder in memory: then set MTSPI_LOG_SYNC=0 and MTSPI_LOG_MAX_MB too).
# For a bench without the bike: BT=emulated, CONTROL=tcp:9150, WIFI=none.
set -u
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
DATA=${DATA:-/var/lib/mtspi}
LOGS=${MTSPI_LOG_DIR:-$DATA}
RUN=$LOGS/run-$(date +%Y%m%d-%H%M%S)
# The name comes from the clock, and a Pi that has no internet starts every time from the same moment (the last time
# it knew the real time): a folder is never used twice, the second start in the same second gets "-2" and so on.
BASE=$RUN; n=1; while [ -e "$RUN" ]; do n=$((n + 1)); RUN=$BASE-$n; done
# a log folder that cannot be made (card read-only) must not keep the bridge from starting
mkdir -p "$RUN" 2>/dev/null || { LOGS=/run/user/$(id -u); RUN=$LOGS/run-$(date +%Y%m%d-%H%M%S); mkdir -p "$RUN"; }
# keep the logs of the last LOG_KEEP runs (default 20). Never this run's own folder: the names come from the clock,
# and a Pi whose clock starts behind (no internet) gives the new folder an older name than the ones already there.
ls -1d "$LOGS"/run-* 2>/dev/null | grep -vxF "$RUN" | head -n -"$(( ${LOG_KEEP:-20} - 1 ))" | xargs -r rm -rf
# RADIO=esp32phone|pro: pro variant (external Bluetooth radio on the serial port), not part of this release.
# CARLINKIT=usb with the real dongle, none without it.
if [ "${RADIO:-internal}" = esp32phone ]; then
    CONTROL=esp32 BT=emulated PBAP=esp32 BIKE_AUDIO=esp32
elif [ "${RADIO:-internal}" = pro ]; then
    CONTROL=${CONTROL_PRO:-esp32} BT=real PBAP=esp32 BIKE_AUDIO=esp32
elif [ "${CARLINKIT:-none}" != none ]; then
    # standard variant with the Carlinkit: CarPlay audio and calls go to the mtspi-hfp service (HFP_SOURCE=bridge, port 9160)
    BIKE_AUDIO=${BIKE_AUDIO:-127.0.0.1:9160}
fi
exec "$PY" -m mtspi.bridge \
    --control "${CONTROL:-rfcomm}" --wifi "${WIFI:-nmcli}" \
    --bt "${BT:-real}" --iphone none --carlinkit "${CARLINKIT:-none}" --bike-audio "${BIKE_AUDIO:-none}" \
    --vehicle-keys 128 --bt-name "${BT_NAME:-MTS Pi}" --bt-info "${BT_INFO:-[HFP,A2DP]${BT_NAME:-MTS Pi}}" \
    --settings "$DATA/settings.json" --pbap "${PBAP:-bluez}" --phonebook-cache "$DATA/phonebook.json" \
    ${CALL_FPS:+--call-fps "$CALL_FPS"} \
    ${MUSIC_FPS:+--music-fps "$MUSIC_FPS"} \
    --log "$RUN/bridge.jsonl" --ap-credentials-file "$RUN/ap.jsonl" "$@"
