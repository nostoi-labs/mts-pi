#!/bin/bash
# The card protected against power cuts (ready-made card image only).
#
# A Pi on a bike loses power without warning at every key-off. On this card nothing is written while the Pi works:
#   - the system partition is mounted read-only, with a layer in memory on top (whatever the system writes is gone
#     at the next start); the boot partition is read-only too
#   - what must be kept (pairings, phonebook, settings, home Wi-Fi) is on a small data partition (/data), written
#     only when one of those things changes, journal included
#   - logs stay in memory; LOGS=card in mtspi.txt puts them on a third partition (/log), away from everything else
#
#   mtspi-protect status   what is mounted how, and how much was written to each partition since power-on
#   mtspi-protect off      the system is writable from the next start (to update or change it):  sudo reboot
#   mtspi-protect on       read-only again from the next start:  sudo reboot  (do not cut the power before that)
#                          (both also write PROTECT=off|on in mtspi.txt, the same as editing the file by hand)
#   mtspi-protect apply    at every start: follows PROTECT=on|off in mtspi.txt (on when the line is missing or empty),
#                          restarting the Pi once when the card is in the other state. The first start of a fresh
#                          card is writable (the system finishes setting itself up), then this switches it over.
set -eu
CMD=${MTSPI_CMDLINE:-/boot/firmware/cmdline.txt}
FILE=${MTSPI_SETUP_FILE:-/boot/firmware/mtspi.txt}
PROC=${MTSPI_PROC_CMDLINE:-/proc/cmdline}
BOOT=${MTSPI_BOOT:-/boot/firmware}
FSTAB=${MTSPI_FSTAB:-/etc/fstab}
REBOOT=${MTSPI_REBOOT:-systemctl --no-block reboot}
need_root() { [ "$(id -u)" = 0 ] || [ -n "${MTSPI_CMDLINE:-}" ] || { echo "run it with sudo"; exit 1; }; }
overlay_now() { grep -q 'overlayroot=tmpfs' "$PROC"; }
overlay_next() { grep -q 'overlayroot=tmpfs' "$CMD"; }
boot_rw() { mountpoint -q "$BOOT" 2>/dev/null && mount -o remount,rw "$BOOT" 2>/dev/null || true; }
boot_ro() { sync; mountpoint -q "$BOOT" 2>/dev/null && mount -o remount,ro "$BOOT" 2>/dev/null || true; }
strip() { sed -i -e 's/overlayroot=[^ ]* //' -e 's/ fsck\.mode=[^ ]*//' -e 's/ panic=[^ ]*//' "$CMD"; }
wanted() {
    local v
    v=$(tr -d '\r' < "$FILE" 2>/dev/null | sed -n 's/^[[:space:]]*PROTECT[[:space:]]*=[[:space:]]*//p' | tail -1 | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]"')
    case "$v" in off|no|0|false) echo off ;; *) echo on ;; esac
}
# on and off also write the choice in mtspi.txt: that file is what "apply" follows at every start, so a choice made
# with the command would otherwise be undone at the next start
remember() {
    [ -f "$FILE" ] || return 0
    if grep -q '^[[:space:]]*PROTECT[[:space:]]*=' "$FILE"; then
        sed -i "s/^[[:space:]]*PROTECT[[:space:]]*=.*/PROTECT=$1/" "$FILE"
    else
        printf '\nPROTECT=%s\n' "$1" >> "$FILE"
    fi
}
case "${1:-status}" in
status)
    overlay_now && echo "system: READ-ONLY (changes live in memory until the next start)" || echo "system: writable"
    overlay_next && echo "next start: read-only" || echo "next start: writable"
    echo "mtspi.txt asks for: $(wanted)"
    [ -f /data/clock ] && echo "last real time kept: $(date -r /data/clock '+%F %T')" || echo "last real time kept: never"
    echo "logs: $(cat /run/mtspi-logs.mode 2>/dev/null || echo '?')"
    for m in / /data /log "$BOOT" /var/lib/bluetooth /var/lib/mtspi /etc/NetworkManager/system-connections /etc/default /var/lib/mtspi-logs; do
        printf '  %-40s %s\n' "$m" "$(findmnt -no SOURCE,OPTIONS "$m" 2>/dev/null | cut -c1-80 || true)"
    done
    echo "512-byte sectors written since power-on ($(cut -d. -f1 /proc/uptime) s ago):"
    for p in 1 2 3 4; do printf '  p%s %s\n' $p "$(awk '{print $7}' /sys/block/mmcblk0/mmcblk0p$p/stat 2>/dev/null)"; done
    [ -d /media/root-rw ] && df -h /media/root-rw 2>/dev/null | tail -1 | awk '{print "  layer in memory: " $3 " used of " $2}'
    sed 's/^/  partition check at start: /' /run/mtspi-data-check.txt 2>/dev/null || true
    exit 0 ;;
on)
    need_root
    boot_rw
    remember on
    strip
    # panic=10: if the start-up cannot mount the system it does not sit at a console nobody sees, it restarts after 10 s
    # fsck.mode=skip: no check at start-up that could stand waiting; the data partition has its own, with a time limit
    sed -i -e 's/^/overlayroot=tmpfs:recurse=0 /' -e 's/$/ fsck.mode=skip panic=10/' "$CMD"
    # the boot partition is mounted read-only from the next start (only effective while the system is still writable)
    sed -i -e 's#^\([^ ]*[[:space:]]*/boot/firmware[[:space:]]*vfat[[:space:]]*defaults\),nofail#\1,ro,nofail#' "$FSTAB" 2>/dev/null || true
    boot_ro
    echo "read-only from the next start:  sudo reboot  (do not cut the power before it has restarted)" ;;
off)
    need_root
    boot_rw
    remember off
    strip
    boot_ro
    echo "writable from the next start:  sudo reboot" ;;
apply)
    need_root
    want=$(wanted); now=off; overlay_now && now=on
    if [ "$want" = "$now" ]; then
        # a writable system also gets a writable boot partition (updates put the kernel there)
        [ "$now" = on ] || boot_rw
        exit 0
    fi
    "$0" "$want" >/dev/null
    next=off; overlay_next && next=on
    if [ "$next" = "$want" ]; then
        echo "protection goes $want: restarting once"
        sync
        $REBOOT
    else
        echo "could not change the boot file: the card stays as it is"
    fi ;;
*) echo "usage: $0 status|on|off|apply"; exit 1 ;;
esac
