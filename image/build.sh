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
EXTRA_MB=1200                      # room for the packages; the system grows to the whole card at first boot
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
truncate -s +${EXTRA_MB}M "$IMG"

LOOP=$(losetup -fP --show "$IMG")
cleanup() {
    set +e
    for m in dev/pts dev proc sys boot/firmware ""; do mountpoint -q "$ROOT/$m" && umount "$ROOT/$m"; done
    losetup -d "$LOOP" 2>/dev/null
}
trap cleanup EXIT

echo "== grow the system partition"
parted -s "$LOOP" resizepart 2 100%
partprobe "$LOOP"; sleep 1
e2fsck -fy "${LOOP}p2" || [ $? -le 1 ]
resize2fs "${LOOP}p2"

echo "== mount"
mount "${LOOP}p2" "$ROOT"
mount "${LOOP}p1" "$ROOT/boot/firmware"
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

echo "== self-test of the settings-file script inside the image"
chroot "$ROOT" /bin/bash -c "cd /opt/mts-pi && bash tests/test_setup_file.sh"

echo "== what the image contains"
chroot "$ROOT" /bin/bash -c "id mtspi; systemctl is-enabled ofono mtspi-adapters mtspi-hfp mtspi-wifi-mode mtspi-btcentral mtspi-bridge | tr '\n' ' '; echo; ls /var/lib/systemd/linger; cat /etc/default/mtspi; cd /opt/mts-pi && .venv/bin/python -c 'import mtspi.bridge, mtspi.hfp_ag, myspin.phone; print(\"program loads:\", myspin.phone.__file__)'; ls -l /boot/firmware/mtspi.txt; cat /etc/hostname; df -h / | tail -1"

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
