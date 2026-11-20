#!/bin/sh
# Protected card: the Pi has no clock of its own, and with a read-only system the last known time is not saved any
# more, so every start began from the day the card was made. The last real time is kept on the data partition:
#   save   once per power-on, when the network has given the real time
#   load   at start-up: the clock restarts from there (never backwards, never more than ten years ahead)
F=/data/clock
case "$1" in
load)
    [ -f $F ] || exit 0
    t=$(stat -c %Y $F); now=$(date +%s)
    if [ "$t" -gt "$now" ] && [ "$t" -lt $((now + 315360000)) ]; then date -s "@$t" >/dev/null; fi
    exit 0 ;;
save)
    mountpoint -q /data || exit 0
    touch $F && sync $F ;;
esac
