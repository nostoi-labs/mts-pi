#!/bin/bash
# Starts the bridge (mtspi-bridge.service): the Pi's home screen on the dashboard, CarPlay through the Carlinkit
# and the Bluetooth bridge iPhone -> Pi -> bike (music, calls, phonebook).
# Settings come from /etc/default/mtspi. Each run writes its log (JSON lines) to a new folder under $DATA.
# For a bench without the bike: BT=emulated, CONTROL=tcp:9150, WIFI=none.
set -u
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
DATA=${DATA:-/var/lib/mtspi}
RUN=$DATA/run-$(date +%Y%m%d-%H%M%S)
mkdir -p "$RUN"
# keep the logs of the last LOG_KEEP runs (default 20)
ls -1d "$DATA"/run-* 2>/dev/null | head -n -"${LOG_KEEP:-20}" | xargs -r rm -rf
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
    --log "$RUN/bridge.jsonl" --ap-credentials-file "$DATA/ap.jsonl" "$@"
