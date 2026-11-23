#!/bin/sh
# Protected card: the data partition, and the boot partition, are checked before they are mounted, never for longer
# than a minute and a half for the first and 40 seconds for the second.
# Clean and with nothing left in its journal: not touched at all (even a check that finds nothing rewrites the
# copies of the superblock: 16 sectors at every start, measured on 20 Nov 2026; reading the header writes nothing).
# After a power cut the journal is replayed. Only if the partition cannot be repaired at all it is made again from
# the copies inside the system: pairings are lost, the Pi still starts and can be paired again.
D=/dev/disk/by-label/mtspi-data; O=/run/mtspi-data-check.txt
: > $O

# The boot partition (FAT) is read-only too, and opened for writing only for a moment: to take the Wi-Fi password out
# of mtspi.txt, to write the result of an update. A power cut in that moment leaves its "not properly unmounted" mark,
# and nothing ever takes it away again (the system does not check this partition at start, and Linux keeps a mark it
# found): seen on 23 Nov 2026 after a cut while the result of an update was being written. A computer that finds
# the mark checks the card before showing it, or shows it read-only. So: one byte is read (it writes nothing), and
# only when the mark is there the partition is checked and the mark removed, before it is mounted.
boot_check() {
    B=$(awk '$2 == "/boot/firmware" {print $1}' /etc/fstab 2>/dev/null | head -1)
    case "$B" in PARTUUID=*) B=/dev/disk/by-partuuid/${B#PARTUUID=} ;; LABEL=*) B=/dev/disk/by-label/${B#LABEL=} ;; esac
    [ -n "$B" ] || return 0
    n=0; while [ ! -e "$B" ] && [ $n -lt 20 ]; do sleep 0.5; n=$((n + 1)); done
    [ -e "$B" ] || { echo "boot partition: not found" >> $O; return 0; }
    grep -q " /boot/firmware " /proc/mounts && return 0            # already mounted: left alone
    # FAT32 keeps the mark in byte 65 of its first sector, FAT16 in byte 37; which one it is, is written at byte 82
    off=37; [ "$(dd if="$B" bs=1 skip=82 count=5 2>/dev/null)" = FAT32 ] && off=65
    v=$(dd if="$B" bs=1 skip=$off count=1 2>/dev/null | od -An -tu1 | tr -d ' ')
    if [ $(( ${v:-0} & 1 )) = 0 ]; then echo "boot partition: clean, not touched" >> $O; return 0; fi
    timeout 40 fsck.vfat -a -w "$B" >/dev/null 2>&1
    echo "boot partition: not properly unmounted, checked ($?)" >> $O
}
boot_check
[ -e "$D" ] || { echo "no data partition" >> $O; exit 0; }
H=$(timeout 20 dumpe2fs -h "$D" 2>/dev/null)
if echo "$H" | grep -q '^Filesystem state: *clean$' && ! echo "$H" | grep -q 'needs_recovery'; then
    echo "clean, not touched" >> $O; exit 0
fi
timeout 40 e2fsck -p "$D" >/dev/null 2>&1; r=$?
echo "preen $r" >> $O
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
