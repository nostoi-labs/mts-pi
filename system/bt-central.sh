#!/bin/bash
# Keeps the Pi "central" on every classic Bluetooth link. With a single radio, an iPhone link where the Pi is
# central plus a helmet or bike link where the other side took the central role (Pi peripheral) make two
# piconets, and the radio has to alternate between them. Measured on the bench with a helmet intercom: music
# from 0-7 gaps per 40 s down to 0, latency p95 from 130-180 ms down to 60 ms, moving map at 13 fps.
# Every 10 s it asks for a role switch on the links where the Pi is peripheral; if the other side refuses it
# retries 3 times 10 s apart, then once a minute (some devices refuse right after connecting and accept
# later; an iPhone that connects to the Pi by itself always refuses). BT_CENTRAL=0 in /etc/default/mtspi turns it off.
set -u
[ -f /etc/default/mtspi ] && . /etc/default/mtspi
[ "${BT_CENTRAL:-1}" = "0" ] && exec sleep infinity
declare -A tries
while true; do
    while read -r addr handle; do
        key="$addr-$handle"
        n=${tries[$key]:-0}
        tries[$key]=$((n + 1))
        [ "$n" -ge 3 ] && [ $(( (n - 3) % 6 )) -ne 5 ] && continue     # after 3 attempts: one every 6 rounds (60 s)
        if hcitool sr "$addr" master >/dev/null 2>&1; then echo "central role requested (handle $handle, attempt $((n + 1)))"
        else echo "role switch refused (handle $handle, attempt $((n + 1)))"; fi
    done < <(hcitool con 2>/dev/null | awk '/ACL/ && /PERIPHERAL/ {print $3, $5}')
    sleep 10
done
