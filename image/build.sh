#!/bin/bash
# Builds the ready-to-flash SD card image: the official Raspberry Pi OS Lite (64-bit) with MTS Pi installed.
# It runs the same install.sh a person would run on the Pi, inside the image, so the two ways of installing
# cannot drift apart. Needs a Linux machine with an arm64 CPU (the GitHub workflow uses an arm64 runner) and root.
#
#     sudo image/build.sh [version]        ->  out/mts-pi-<version>.img.xz  and its .sha256
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=${1:-dev}
BASE=2026-10-06-raspios-trixie-arm64-lite.img.xz
BASE_URL=https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2026-10-06/$BASE
BASE_SHA256=483db18a48da399b5b7022ffae9a07bc6d99daab30e593aedac09b69ff422843
EXTRA_MB=1200                      # room for the packages. The system partition keeps this size on every card:
                                   # it is read-only while the Pi works, so it has no use for the rest of the card
DATA_MB=256                        # /data: pairings, phonebook, settings (see system/protect.sh)
LOG_MB=1024                        # /log: logs, only when asked for (LOGS=card in mtspi.txt)
WORK=${WORK:-/tmp/mts-pi-image}
ROOT=$WORK/root
OUT=$PWD/out
IMG=$WORK/mts-pi-$VERSION.img

[ "$(id -u)" = 0 ] || { echo "run it as root"; exit 1; }
[ "$(uname -m)" = aarch64 ] || { echo "needs an arm64 machine (this is $(uname -m))"; exit 1; }
mkdir -p "$WORK" "$OUT" "$ROOT"

echo "== base image"
[ -f "$WORK/$BASE" ] || curl -fL --retry 3 -o "$WORK/$BASE" "$BASE_URL"
echo "$BASE_SHA256  $WORK/$BASE" | sha256sum -c -
xz -dc "$WORK/$BASE" > "$IMG"
truncate -s +$((EXTRA_MB + DATA_MB + LOG_MB + 4))M "$IMG"

LOOP=$(losetup -fP --show "$IMG")
cleanup() {
    set +e
    for m in dev/pts dev proc sys boot/firmware data ""; do mountpoint -q "$ROOT/$m" && umount "$ROOT/$m"; done
    losetup -d "$LOOP" 2>/dev/null
}
trap cleanup EXIT

echo "== grow the system partition, add the data and log partitions after it"
P2_END=$(parted -sm "$LOOP" unit s print | awk -F: '$1 == 2 { sub("s", "", $3); print $3 }')
P2_NEW=$((P2_END + EXTRA_MB * 2048))
P3_START=$((P2_NEW + 1)); P3_END=$((P3_START + DATA_MB * 2048 - 1))
P4_START=$((P3_END + 1)); P4_END=$((P4_START + LOG_MB * 2048 - 1))
[ $((P3_START % 2048)) = 0 ] || { echo "the partitions would not start on a megabyte boundary"; exit 1; }
parted -s "$LOOP" unit s resizepart 2 ${P2_NEW}s
parted -s "$LOOP" unit s mkpart primary ext4 ${P3_START}s ${P3_END}s
parted -s "$LOOP" unit s mkpart primary ext4 ${P4_START}s ${P4_END}s
partprobe "$LOOP"; sleep 1
mkfs.ext4 -q -F -L mtspi-data -m 0 "${LOOP}p3"
mkfs.ext4 -q -F -L mtspi-log -m 0 "${LOOP}p4"
e2fsck -fy "${LOOP}p2" || [ $? -le 1 ]
resize2fs "${LOOP}p2"

echo "== mount"
mount "${LOOP}p2" "$ROOT"
mount "${LOOP}p1" "$ROOT/boot/firmware"
mkdir -p "$ROOT/data"
mount "${LOOP}p3" "$ROOT/data"                 # install.sh fills it with a copy of the settings it writes
mount --bind /dev "$ROOT/dev"; mount --bind /dev/pts "$ROOT/dev/pts"
mount -t proc proc "$ROOT/proc"; mount -t sysfs sysfs "$ROOT/sys"
mv "$ROOT/etc/resolv.conf" "$ROOT/etc/resolv.conf.image" 2>/dev/null || true
cp /etc/resolv.conf "$ROOT/etc/resolv.conf"
printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"      # no service starts while packages install
chmod +x "$ROOT/usr/sbin/policy-rc.d"

echo "== copy MTS Pi and install it inside the image"
mkdir -p "$ROOT/opt/mts-pi"
tar -c --exclude=.git --exclude=out --exclude=.venv --exclude=__pycache__ . | tar -x -C "$ROOT/opt/mts-pi"
echo "$VERSION" > "$ROOT/opt/mts-pi/VERSION"
chroot "$ROOT" /bin/bash -c "cd /opt/mts-pi && ./install.sh --image"

echo "== the card does not grow the system partition at first boot; the start-up image knows the read-only layer"
sed -i 's/ resize\( \|$\)/\1/' "$ROOT/boot/firmware/cmdline.txt"
cat "$ROOT/boot/firmware/cmdline.txt"
! grep -qw resize "$ROOT/boot/firmware/cmdline.txt" || { echo "cmdline.txt still asks for a resize"; exit 1; }
has_layer() { chroot "$ROOT" /bin/bash -c 'for f in /boot/firmware/initramfs8 /boot/firmware/initramfs_2712; do [ ! -f $f ] || lsinitramfs $f | grep -q scripts/init-bottom/overlayroot || exit 1; done; [ -f /boot/firmware/initramfs8 ]'; }
has_layer || chroot "$ROOT" /bin/bash -c "update-initramfs -u -k all"
has_layer || { echo "the start-up image (initramfs) does not contain overlayroot"; exit 1; }
chroot "$ROOT" /bin/bash -c "lsinitramfs /boot/firmware/initramfs8 | grep -c -E 'overlay\.ko|init-bottom/overlayroot'; ls -la /boot/firmware/initramfs*"

echo "== self-tests of the settings-file and protection scripts inside the image"
chroot "$ROOT" /bin/bash -c "cd /opt/mts-pi && bash tests/test_setup_file.sh && bash tests/test_protect.sh"
# the update from a file, on the image's own file system (the one the folders will change places on)
chroot "$ROOT" /bin/bash -c "cd /opt/mts-pi && python3 tests/test_update.py > /tmp/test_update.log 2>&1 || { tail -40 /tmp/test_update.log; exit 1; }; grep -c '^ok ' /tmp/test_update.log"

echo "== what the image contains"
chroot "$ROOT" /bin/bash -c "id mtspi; systemctl is-enabled ofono mtspi-adapters mtspi-hfp mtspi-wifi-mode mtspi-btcentral mtspi-bridge | tr '\n' ' '; echo; ls /var/lib/systemd/linger; cat /etc/default/mtspi; cd /opt/mts-pi && .venv/bin/python -c 'import mtspi.bridge, mtspi.hfp_ag, myspin.phone; print(\"program loads:\", myspin.phone.__file__)'; ls -l /boot/firmware/mtspi.txt; cat /etc/hostname; df -h / | tail -1; systemctl is-enabled mtspi-firstboot userconfig cron 2>&1 | tr '\n' ' '; echo; grep -E '^(gpu_mem|boot_delay|dtparam=audio|#dtoverlay=vc4)' /boot/firmware/config.txt | tr '\n' ' '; echo; cat /etc/modprobe.d/mtspi-lean.conf | tr '\n' ' '; echo"

chroot "$ROOT" /bin/bash -c "systemctl is-enabled mtspi-data-check mtspi-logs mtspi-btstore-load mtspi-btstore mtspi-clock-load mtspi-clock-save.path mtspi-protect mtspi-update mtspi-tpms | tr '\n' ' '; echo; cat /etc/fstab; ls -la /data"
parted -s "$LOOP" unit MiB print

echo "== clean up"
chroot "$ROOT" apt-get clean
rm -f "$ROOT/usr/sbin/policy-rc.d" "$ROOT/etc/resolv.conf"
mv "$ROOT/etc/resolv.conf.image" "$ROOT/etc/resolv.conf" 2>/dev/null || true
rm -rf "$ROOT"/var/lib/apt/lists/* "$ROOT"/root/.cache "$ROOT"/tmp/* "$ROOT"/var/log/*.log
# zero the free space so the image compresses well
dd if=/dev/zero of="$ROOT/zero.fill" bs=1M 2>/dev/null || true
rm -f "$ROOT/zero.fill"
cleanup; trap - EXIT

echo "== compress"
xz -T0 -6 -c "$IMG" > "$OUT/mts-pi-$VERSION.img.xz"
(cd "$OUT" && sha256sum "mts-pi-$VERSION.img.xz" > "mts-pi-$VERSION.img.xz.sha256" && ls -lh "mts-pi-$VERSION.img.xz" && cat "mts-pi-$VERSION.img.xz.sha256")

echo "== the same version as an update, for the cards that already run MTS Pi (system/update.py)"
python3 system/update.py pack . "$VERSION" "$OUT/mts-pi-update-$VERSION.tar.gz"
(cd "$OUT" && sha256sum "mts-pi-update-$VERSION.tar.gz" > "mts-pi-update-$VERSION.tar.gz.sha256" && ls -lh "mts-pi-update-$VERSION.tar.gz")
