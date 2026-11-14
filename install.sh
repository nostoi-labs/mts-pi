#!/bin/bash
# MTS Pi installer. Run it on the Raspberry Pi, as the normal user, from the folder of the repository:
#
#     ./install.sh                 # standard setup with the Carlinkit dongle
#     ./install.sh --name "My Pi"  # another Bluetooth name (default "MTS Pi")
#     ./install.sh --no-carlinkit  # Bluetooth bridge only (music, calls, phonebook), no CarPlay
#
# Tested on: Raspberry Pi 3 Model A+, Raspberry Pi OS Lite 64-bit (Debian 13 "trixie").
# It can be run again at any time (after an update of the repository, or to change the name).
# It does not touch the boot files (config.txt, cmdline.txt) and does not remove Bluetooth pairings.
#
# --image is used only by image/build.sh: it installs into a system image that is not running (as root, in a
# chroot), creates the service user "mtspi" and skips everything that needs a live system.
set -eu
cd "$(dirname "$0")"
REPO=$PWD
NAME="MTS Pi"
CARLINKIT=usb
IMAGE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --name) NAME=$2; shift 2 ;;
        --no-carlinkit) CARLINKIT=none; shift ;;
        --image) IMAGE=1; shift ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1 (see ./install.sh --help)"; exit 1 ;;
    esac
done

say() { printf '\n== %s\n' "$*"; }
warn() { printf '   WARNING: %s\n' "$*"; }
live() { [ "$IMAGE" = 0 ]; }

say "checks"
if live; then
    [ "$(id -u)" != 0 ] || { echo "run it as the normal user, not as root (it calls sudo when needed)"; exit 1; }
    sudo -n true 2>/dev/null || { echo "this user needs sudo without a password (the default on Raspberry Pi OS)"; exit 1; }
    SUDO=sudo
    U=$(id -un)
else
    [ "$(id -u)" = 0 ] || { echo "--image runs as root inside the image"; exit 1; }
    SUDO=
    U=mtspi
    id "$U" >/dev/null 2>&1 || useradd -m -s /bin/bash "$U"
    passwd -l "$U" >/dev/null          # service account: no password, no login
fi
UID_N=$(id -u "$U")
UHOME=$(getent passwd "$U" | cut -d: -f6)
OS_CODENAME=$(. /etc/os-release; echo "${VERSION_CODENAME:-unknown}")   # in a subshell: os-release defines NAME too
[ "$OS_CODENAME" = trixie ] || warn "tested only on Raspberry Pi OS based on Debian 13 (trixie); this is $OS_CODENAME"
[ "$(uname -m)" = aarch64 ] || warn "tested only on the 64-bit system (aarch64); this is $(uname -m)"
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
[ "$PYV" = 3.13 ] || { echo "the binary files in myspin/ need Python 3.13 (Raspberry Pi OS based on Debian 13); this system has Python $PYV"; exit 1; }
HOME_SSID=""
COUNTRY=""
if live; then
    MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)
    case "$MODEL" in *"Pi 3 Model A Plus"*) ;; *) warn "tested only on the Raspberry Pi 3 Model A+; this is: $MODEL" ;; esac
    [ -e /sys/class/net/wlan0 ] || { echo "no wlan0: the Pi needs its Wi-Fi to reach the bike"; exit 1; }
    HOME_SSID=$(nmcli -t -f ACTIVE,SSID device wifi 2>/dev/null | sed -n 's/^yes://p' | head -1 || true)
    OLD_HOME=$(sed -n 's/^HOME_SSID="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' /etc/default/mtspi 2>/dev/null || true)
    HOME_SSID=${HOME_SSID:-$OLD_HOME}
    [ -n "$HOME_SSID" ] || warn "the Pi is not on a Wi-Fi network now: it will go back to any Wi-Fi network saved on it"
    COUNTRY=$(raspi-config nonint get_wifi_country 2>/dev/null || true)
    [ -n "$COUNTRY" ] || COUNTRY=$(iw reg get 2>/dev/null | awk '/^country/ { sub(":", "", $2); print $2; exit }')
    case "$COUNTRY" in ""|00) echo "Wi-Fi country not set: run  sudo raspi-config  (Localisation > WLAN Country) and start again"; exit 1 ;; esac
fi
echo "   user $U, home Wi-Fi \"${HOME_SSID:-none}\", Wi-Fi country ${COUNTRY:-from the system}, Bluetooth name \"$NAME\", Carlinkit: $CARLINKIT"

say "packages"
$SUDO apt-get update -q
$SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    bluez bluez-obexd ofono pipewire pipewire-pulse pipewire-audio wireplumber libspa-0.2-bluetooth \
    python3-venv python3-pil python3-numpy python3-av \
    fonts-inter iw rfkill wpasupplicant dhcpcd-base sudo

say "Python environment"
[ -x .venv/bin/python ] || python3 -m venv --system-site-packages .venv
.venv/bin/pip install -q dbus-fast pyusb
.venv/bin/python -c "import mtspi.bridge, mtspi.hfp_ag" || { echo "the program does not load: see the error above"; exit 1; }
live || chown -R "$U:$U" "$REPO"

say "data folder, groups, the two commands the bridge runs as root"
$SUDO mkdir -p /var/lib/mtspi
$SUDO chown "$U:$U" /var/lib/mtspi
$SUDO usermod -aG plugdev,netdev,bluetooth,audio "$U"
# the bridge removes a stale Wi-Fi Direct interface (iw) and asks the bike for an address (dhcpcd)
echo "$U ALL=(root) NOPASSWD: /usr/sbin/iw, /usr/sbin/dhcpcd" | $SUDO tee /etc/sudoers.d/mtspi >/dev/null
$SUDO chmod 440 /etc/sudoers.d/mtspi
$SUDO visudo -cf /etc/sudoers.d/mtspi >/dev/null || { $SUDO rm -f /etc/sudoers.d/mtspi; echo "sudoers rule not valid, removed"; exit 1; }

say "Bluetooth: the Pi presents itself to the bike as a phone"
$SUDO python3 - <<'PY'
import configparser
p = "/etc/bluetooth/main.conf"
c = configparser.ConfigParser(strict=False, interpolation=None)
c.optionxform = str
c.read(p)
# Class: phone/smartphone. DeviceID: the vendor/product record the bike reads at every connection.
for sec, kv in {"General": {"Class": "0x5A020C", "DiscoverableTimeout": "0",
                            "DeviceID": "bluetooth:0075:0100:0200",
                            "PairableTimeout": "0", "FastConnectable": "true", "JustWorksRepairing": "always"},
                "Policy": {"AutoEnable": "true", "ReconnectAttempts": "30", "ReconnectIntervals": "1,2,4,8,16"}}.items():
    if not c.has_section(sec):
        c.add_section(sec)
    for k, v in kv.items():
        c.set(sec, k, v)
with open(p, "w") as f:
    c.write(f)
PY

say "settings in /etc/default/mtspi"
# HFP_AG=1: the Pi is the bike's phone - HFP_SCO=1: with call audio - HFP_SOURCE=bridge: call audio comes from CarPlay
# WPA_*: the Wi-Fi Direct wpa_supplicant - HOME_SSID: your home network (mtspi-wifi-mode goes back to it without the bike)
D=/etc/default/mtspi
$SUDO touch $D
setv() { $SUDO sed -i "/^$1=/d" $D; echo "$1=$2" | $SUDO tee -a $D >/dev/null; }
for kv in HFP_AG=1 HFP_SCO=1 WPA_CTRL=/run/wpa_p2p WPA_P2P_DEV=p2p-dev-wlan0; do
    grep -q "^${kv%%=*}=" $D || echo "$kv" | $SUDO tee -a $D >/dev/null
done
setv CARLINKIT "$CARLINKIT"
if [ "$CARLINKIT" = usb ]; then setv HFP_SOURCE bridge; else $SUDO sed -i '/^HFP_SOURCE=/d' $D; fi
[ -z "$HOME_SSID" ] || setv HOME_SSID "\"$HOME_SSID\""

say "PipeWire: Bluetooth roles, calls through oFono"
WP="$UHOME/.config/wireplumber/wireplumber.conf.d"
mkdir -p "$WP"
cp system/conf/51-mtspi-bluez.conf "$WP/"
# with the Carlinkit, music and voice go through CarPlay: the Pi must not be an audio output for the iPhone
if [ "$CARLINKIT" = usb ]; then
    cp system/conf/52-mtspi-carlinkit.conf "$WP/"
else
    rm -f "$WP/52-mtspi-carlinkit.conf"
fi
live || chown -R "$U:$U" "$UHOME/.config"

say "permissions: NetworkManager, oFono, Carlinkit on USB"
sed "s/@USER@/$U/g" system/conf/50-mtspi-nm.rules | $SUDO tee /etc/polkit-1/rules.d/50-mtspi-nm.rules >/dev/null
sed "s/@USER@/$U/g" system/conf/mtspi-ofono.conf | $SUDO tee /etc/dbus-1/system.d/mtspi-ofono.conf >/dev/null
$SUDO cp system/conf/90-mtspi-bt.rules system/conf/60-mtspi-carlinkit.rules /etc/udev/rules.d/
live && $SUDO udevadm control --reload

say "Wi-Fi Direct to the bike: dedicated wpa_supplicant, NetworkManager keeps away from the P2P interfaces"
# in an image the country is not known yet: the line is left out and the system's own setting applies
if [ -n "$COUNTRY" ]; then
    sed "s/@COUNTRY@/$COUNTRY/" system/conf/p2p.conf | $SUDO tee /etc/wpa_supplicant/mtspi-p2p.conf >/dev/null
else
    sed '/@COUNTRY@/d' system/conf/p2p.conf | $SUDO tee /etc/wpa_supplicant/mtspi-p2p.conf >/dev/null
fi
$SUDO cp system/conf/99-mtspi-p2p.conf /etc/NetworkManager/conf.d/
live && { $SUDO systemctl reload NetworkManager || true; }

say "names shown to the bike and to the iPhone"
$SUDO system/names.sh "$NAME"

say "system log kept on the SD card (it survives the bike cutting the power)"
$SUDO mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSyncIntervalSec=10s\nSystemMaxUse=200M\n' | $SUDO tee /etc/systemd/journald.conf.d/50-mtspi.conf >/dev/null
live && $SUDO systemctl restart systemd-journald

say "faster boot: no wait for the network, no automatic updates"
# cloud-init is turned off only on a system that is already set up: in an image it still has to apply the
# settings chosen in Raspberry Pi Imager at the first boot
live && { $SUDO touch /etc/cloud/cloud-init.disabled 2>/dev/null || true; }
for u in NetworkManager-wait-online.service apt-daily.timer apt-daily-upgrade.timer man-db.timer e2scrub_reap.service; do
    if live; then $SUDO systemctl disable --now "$u" 2>/dev/null || true; else systemctl disable "$u" 2>/dev/null || true; fi
done

say "services"
# the audio services run in the user's session, which must exist without anybody logged in
if live; then
    $SUDO loginctl enable-linger "$U"
    # no mpris-proxy: the bridge registers the music player for the bike itself, only on the bike's radio
    systemctl --user disable --now mpris-proxy.service 2>/dev/null || true
else
    mkdir -p /var/lib/systemd/linger && touch "/var/lib/systemd/linger/$U"
    systemctl --global disable mpris-proxy.service 2>/dev/null || true
fi
for u in system/units/*.service; do
    sed -e "s#@REPO@#$REPO#g" -e "s#@USER@#$U#g" -e "s#@UID@#$UID_N#g" "$u" | $SUDO tee "/etc/systemd/system/$(basename "$u")" >/dev/null
done
live && $SUDO systemctl daemon-reload
$SUDO systemctl disable mtspi-p2p 2>/dev/null || true    # started and stopped by mtspi-wifi-mode
$SUDO systemctl enable ofono mtspi-adapters mtspi-hfp mtspi-wifi-mode mtspi-btcentral mtspi-bridge
if live; then
    $SUDO rfkill unblock bluetooth   # Raspberry Pi OS leaves it blocked at first boot
    $SUDO systemctl restart bluetooth ofono
    systemctl --user restart wireplumber pipewire pipewire-pulse || true
    $SUDO systemctl restart mtspi-adapters

    say "checks after the installation"
    bluetoothctl list || true
    systemctl is-enabled ofono mtspi-adapters mtspi-hfp mtspi-wifi-mode mtspi-btcentral mtspi-bridge | tr '\n' ' '; echo

    cat <<MSG

== done. Now:  sudo reboot

At every power-on the Pi first looks for the bike: without the bike it comes back to your home Wi-Fi
("${HOME_SSID:-any network saved on the Pi}") after about one minute. So after the reboot wait a minute
before connecting again.
Next steps (pairing with the bike and with the iPhone): see README.md, section "Pairing".
MSG
else
    say "done (image)"
fi
