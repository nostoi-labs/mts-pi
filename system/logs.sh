#!/bin/sh
# Protected card: decides where the logs of this run go. Default: memory (nothing is written to the card while
# riding, and the logs are gone at key-off). LOGS=card in mtspi.txt on the boot partition: the log partition is
# checked, mounted and used; if it is damaged beyond repair it is simply made again (it holds nothing else).
MODE=memory
grep -qi '^[[:space:]]*LOGS[[:space:]]*=[[:space:]]*card' /boot/firmware/mtspi.txt 2>/dev/null && MODE=card
ENV=/run/mtspi-logs.env
if [ "$MODE" = card ]; then
    D=/dev/disk/by-label/mtspi-log
    timeout 40 e2fsck -p "$D" >/dev/null 2>&1; r=$?
    if [ "$r" -ge 4 ] && [ "$r" -ne 124 ]; then mkfs.ext4 -q -F -L mtspi-log -m 0 "$D"; fi
    if mount /log 2>/dev/null; then
        mkdir -p /log/journal /log/mtspi /var/log/journal
        chown mtspi:mtspi /log/mtspi
        mount --bind /log/journal /var/log/journal
        printf 'MTSPI_LOG_DIR=/log/mtspi\nMTSPI_LOG_SYNC=1\nMTSPI_LOG_MAX_MB=0\nLOG_KEEP=20\n' > $ENV
    else
        MODE=memory
    fi
fi
if [ "$MODE" = memory ]; then
    printf 'MTSPI_LOG_DIR=/var/lib/mtspi-logs\nMTSPI_LOG_SYNC=0\nMTSPI_LOG_MAX_MB=6\nLOG_KEEP=2\n' > $ENV
fi
echo "$MODE" > /run/mtspi-logs.mode
exit 0
