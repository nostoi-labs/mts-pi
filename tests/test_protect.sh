#!/bin/bash
# Test of system/protect.sh (the switch of the protected card) with stand-in files: what "apply" does at start-up
# for each combination of what mtspi.txt asks and how the card started. Needs GNU sed (run it on Linux; the image
# build runs it).
set -u
cd "$(dirname "$0")/.."
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
BASE='console=serial0,115200 console=tty1 root=PARTUUID=27b94958-02 rootfstype=ext4 fsck.repair=yes rootwait'
ON="overlayroot=tmpfs:recurse=0 $BASE fsck.mode=skip panic=10"
fail=0
check() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }
run() { MTSPI_CMDLINE="$T/cmdline.txt" MTSPI_PROC_CMDLINE="$T/proc" MTSPI_SETUP_FILE="$T/mtspi.txt" MTSPI_BOOT="$T/noboot" \
        MTSPI_FSTAB="$T/fstab" MTSPI_REBOOT="touch $T/rebooted" bash system/protect.sh "$@" > "$T/out" 2>&1; }
fresh() { echo "$1" > "$T/cmdline.txt"; echo "$1" > "$T/proc"; rm -f "$T/rebooted"
          printf 'PARTUUID=27b94958-01  /boot/firmware  vfat  defaults,nofail,x-systemd.device-timeout=10s  0  0\n' > "$T/fstab"; }

# 1. first start of a fresh card: writable, the template asks for nothing -> switched on, one restart
fresh "$BASE"; cp system/mtspi.txt "$T/mtspi.txt"
run apply
check "fresh card: read-only from the next start" '[ "$(cat "$T/cmdline.txt")" = "$ON" ]'
check "fresh card: restarts once" '[ -e "$T/rebooted" ]'
check "fresh card: boot partition read-only from the next start" 'grep -q "defaults,ro,nofail" "$T/fstab"'
# 2. the start after that: already protected -> nothing happens
fresh "$ON"
run apply
check "protected card: left alone, no restart" '[ "$(cat "$T/cmdline.txt")" = "$ON" ] && [ ! -e "$T/rebooted" ]'
# 3. PROTECT=off on a protected card (file saved on Windows, upper case) -> writable, one restart; then nothing
printf 'WIFI_COUNTRY=IT\r\nPROTECT = OFF\r\n' > "$T/mtspi.txt"
run apply
check "PROTECT=off: boot file back to what it was" '[ "$(cat "$T/cmdline.txt")" = "$BASE" ]'
check "PROTECT=off: restarts once" '[ -e "$T/rebooted" ]'
fresh "$BASE"
run apply
check "PROTECT=off, next start: left alone, no restart" '[ "$(cat "$T/cmdline.txt")" = "$BASE" ] && [ ! -e "$T/rebooted" ]'
# 4. the word deleted, or nonsense, or no file at all -> protected again
for v in 'PROTECT=' 'PROTECT=on' 'PROTECT=banana' ''; do
    fresh "$BASE"; printf '%s\n' "$v" > "$T/mtspi.txt"
    run apply
    check "'$v': protected again, one restart" '[ "$(cat "$T/cmdline.txt")" = "$ON" ] && [ -e "$T/rebooted" ]'
done
fresh "$BASE"; rm -f "$T/mtspi.txt"
run apply
check "no settings file: protected, one restart" '[ "$(cat "$T/cmdline.txt")" = "$ON" ] && [ -e "$T/rebooted" ]'
# 5. on twice does not pile up the words; off after on gives the original line
fresh "$BASE"; run on; run on
check "on twice: one copy of each word" '[ "$(cat "$T/cmdline.txt")" = "$ON" ]'
run off
check "off: the original line, nothing left behind" '[ "$(cat "$T/cmdline.txt")" = "$BASE" ]'
# 5b. the command and the file agree: "off" by hand on a protected card is not undone at the next start, "on" neither
fresh "$ON"; cp system/mtspi.txt "$T/mtspi.txt"
run off
check "off by hand: written in mtspi.txt, once" '[ "$(grep -c "^PROTECT=off$" "$T/mtspi.txt")" = 1 ] && [ "$(grep -c "^PROTECT=" "$T/mtspi.txt")" = 1 ] && grep -q "^LOGS=" "$T/mtspi.txt"'
echo "$BASE" > "$T/proc"; rm -f "$T/rebooted"
run apply
check "off by hand, next start: stays writable, no restart" '[ "$(cat "$T/cmdline.txt")" = "$BASE" ] && [ ! -e "$T/rebooted" ]'
run on
check "on by hand: written in mtspi.txt" '[ "$(grep -c "^PROTECT=on$" "$T/mtspi.txt")" = 1 ] && [ "$(cat "$T/cmdline.txt")" = "$ON" ]'
echo "$ON" > "$T/proc"; rm -f "$T/rebooted"
run apply
check "on by hand, next start: stays protected, no restart" '[ "$(cat "$T/cmdline.txt")" = "$ON" ] && [ ! -e "$T/rebooted" ]'
printf 'WIFI_COUNTRY=IT\n' > "$T/mtspi.txt"; run off
check "a file without the line gets it" 'grep -qx "PROTECT=off" "$T/mtspi.txt" && grep -qx "WIFI_COUNTRY=IT" "$T/mtspi.txt"'
# 6. a boot file that cannot be changed: no restart (it would restart for ever)
fresh "$BASE"; printf 'PROTECT=on\n' > "$T/mtspi.txt"; chmod 555 "$T"; chmod 444 "$T/cmdline.txt"
run apply
chmod 755 "$T"
check "boot file not writable: no restart" '[ ! -e "$T/rebooted" ] || [ "$(id -u)" = 0 ]'
[ "$fail" = 0 ] && echo "ALL OK" || { echo "SOME FAILED"; cat "$T/out"; exit 1; }
