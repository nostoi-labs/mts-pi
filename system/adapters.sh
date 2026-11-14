#!/bin/bash
# Sets up the Bluetooth radios by ROLE (not by name):
#   bike   = internal radio in the standard variant: the Pi is a phone called BT_NAME ("MTS Pi");
#   iphone = internal radio in the pro variant: the Pi is a car stereo called BT_NAME_PRO;
#   usb    = a USB dongle, if present.
# Names live in /etc/default/mtspi (system/names.sh).
set -u
[ -f /etc/default/mtspi ] && . /etc/default/mtspi
RECONNECT=1; [ "${1:-}" = "--no-reconnect" ] && RECONNECT=0
# Raspberry Pi OS blocks the radios with rfkill at first boot (Wi-Fi is unblocked with the country, Bluetooth is not)
rfkill unblock bluetooth || true
sleep 2
# pro variant: the internal radio is started by mtspi-btattach (firmware load ~10 s): wait until BlueZ sees it
if [ "${RADIO:-internal}" = pro ]; then
    for i in $(seq 1 30); do busctl --system get-property org.bluez /org/bluez/hci0 org.bluez.Adapter1 Address >/dev/null 2>&1 && break; sleep 1; done
fi
for h in /sys/class/bluetooth/hci*; do
    [ -e "$h" ] || continue
    hci=$(basename "$h")
    case "$hci" in *:*) continue ;; esac              # hci0:12 = a connection, not a radio
    if readlink -f "$h/device" | grep -q usb; then
        role=usb; alias="${BT_NAME:-MTS Pi} USB"; cls=0x240420   # Audio/Video, car audio; services Rendering+Audio
    elif [ "${RADIO:-internal}" = pro ]; then
        # pro variant: the bike is served by the external radio; the internal one keeps its own address
        # (mtspi-btattach) and is used only for the iPhone
        role=iphone; alias="${BT_NAME_PRO:-MTS Pi Pro}"; cls=0x240420
    else
        role=bike; alias="${BT_NAME:-MTS Pi}"; cls=0x5A020C                   # Phone, smartphone; services Telephony+Audio+...
    fi
    busctl set-property org.bluez /org/bluez/$hci org.bluez.Adapter1 Alias s "$alias" || true
    busctl set-property org.bluez /org/bluez/$hci org.bluez.Adapter1 Powered b true || true
    busctl set-property org.bluez /org/bluez/$hci org.bluez.Adapter1 DiscoverableTimeout u 0 || true
    if [ "$role" = iphone ]; then
        # pro variant: internal radio NOT discoverable. When discoverable it scans every ~1.3 s a few cm away from
        # the external radio's antenna, and music to the helmet had small glitches. The Pi reconnects the paired iPhone.
        busctl set-property org.bluez /org/bluez/$hci org.bluez.Adapter1 Discoverable b false || true
    else
        busctl set-property org.bluez /org/bluez/$hci org.bluez.Adapter1 Discoverable b true || true
    fi
    busctl set-property org.bluez /org/bluez/$hci org.bluez.Adapter1 Pairable b true || true
    if [ "$role" = bike ] && [ "${HFP_AG:-1}" = "1" ] \
        && hciconfig "$hci" version 2>/dev/null | grep -qE 'Manufacturer: (Broadcom|Cypress)'; then   # the Pi's internal BCM43455
        # BCM43455: SCO voice goes to the PCM pins by default; move it to HCI (Write_SCO_PCM_Int_Param:
        # HCI routing, 512 kHz, short frame, sync, clock out). Needed for call audio to the bike.
        hcitool -i "$hci" cmd 0x3f 0x1c 0x01 0x02 0x00 0x01 0x01 >/dev/null 2>&1 || true
    fi
    # class through HCI directly: btmgmt hangs without a terminal (BlueZ 5.82), and main.conf would give
    # the same class to every radio
    hciconfig "$hci" class $cls || true
    # idle links go to sniff mode after 2 s: an iPhone that is only waiting (phonebook, messages, hands-free)
    # gets there by itself after ~45 s and meanwhile the single radio reserves air time for it, so music to the
    # helmet stutters (from ~75 s down to ~17 s after the connection)
    echo 2000 > /sys/kernel/debug/bluetooth/$hci/idle_timeout 2>/dev/null || true
    echo "$hci: $role \"$alias\" (class $cls)"
done
# Reconnects the devices that are already paired (iPhone and bike) as soon as the Pi powers on, each on ITS radio
# (bluetoothctl connect only looks at the default radio). It runs in a separate unit so it does not delay boot.
# A single round, and often too early (PipeWire's A2DP arrives ~7 s later: "Protocol not available"):
# in the standard variant the bridge retries the iPhone (bluez.keep_connected) and mtspi-wifi-mode retries the bike.
[ "$RECONNECT" = 1 ] && systemd-run --no-block --unit "mtspi-reconnect-$$" /bin/sh -c '
    sleep 5                                        # PipeWire must have registered A2DP/HFP
    for dev in $(busctl --system tree --list org.bluez | grep -E "^/org/bluez/hci[0-9]+/dev_[0-9A-F_]+$"); do
        t=$(busctl --system get-property org.bluez "$dev" org.bluez.Device1 Paired 2>/dev/null)   # really paired, not just "trusted"
        [ "$t" = "b true" ] && timeout 20 busctl --system call org.bluez "$dev" org.bluez.Device1 Connect >/dev/null 2>&1 &
    done
    wait' >/dev/null 2>&1 || true
exit 0
