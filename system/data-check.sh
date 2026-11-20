#!/bin/sh
# Protected card: the data partition is checked before it is mounted, never for longer than a minute and a half.
# Clean and with nothing left in its journal: not touched at all (even a check that finds nothing rewrites the
# copies of the superblock: 16 sectors at every start, measured on 20 Nov 2026; reading the header writes nothing).
# After a power cut the journal is replayed. Only if the partition cannot be repaired at all it is made again from
# the copies inside the system: pairings are lost, the Pi still starts and can be paired again.
D=/dev/disk/by-label/mtspi-data; O=/run/mtspi-data-check.txt
[ -e "$D" ] || { echo "no data partition" > $O; exit 0; }
H=$(timeout 20 dumpe2fs -h "$D" 2>/dev/null)
if echo "$H" | grep -q '^Filesystem state: *clean$' && ! echo "$H" | grep -q 'needs_recovery'; then
    echo "clean, not touched" > $O; exit 0
fi
timeout 40 e2fsck -p "$D" >/dev/null 2>&1; r=$?
echo "preen $r" > $O
if [ "$r" -ge 4 ] && [ "$r" -ne 124 ]; then
    timeout 90 e2fsck -y "$D" >/dev/null 2>&1; r=$?
    echo "repair $r" >> $O
    if [ "$r" -ge 4 ] && [ "$r" -ne 124 ]; then
        mkfs.ext4 -q -F -L mtspi-data -m 0 "$D" && echo "MADE AGAIN" >> $O
        mkdir -p /run/mtspi-data-new && mount -o noatime "$D" /run/mtspi-data-new && {
            for b in bluetooth:/var/lib/bluetooth mtspi:/var/lib/mtspi nm:/etc/NetworkManager/system-connections default:/etc/default; do
                cp -a "${b#*:}" "/run/mtspi-data-new/${b%%:*}"
            done
            sync; umount /run/mtspi-data-new
        }
    fi
fi
exit 0
