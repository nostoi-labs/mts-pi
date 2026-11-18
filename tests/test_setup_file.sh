#!/bin/bash
# Test of system/setup-file.sh with stand-in system commands. Needs GNU sed (run it on Linux; the image build runs it).
set -u
cd "$(dirname "$0")/.."
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/home"
for c in nmcli iw rfkill chown; do printf '#!/bin/sh\necho "%s $*" >> "%s/calls"\nexit 0\n' "$c" "$T" > "$T/bin/$c"; chmod +x "$T/bin/$c"; done
# systemctl: "is-enabled" says no until "enable" has been called, as on a card that has never had SSH switched on
printf '#!/bin/sh\necho "systemctl $*" >> "%s/calls"\ncase "$1" in is-enabled) [ -e "%s/ssh_enabled" ]; exit $? ;; enable) : > "%s/ssh_enabled" ;; esac\nexit 0\n' "$T" "$T" "$T" > "$T/bin/systemctl"; chmod +x "$T/bin/systemctl"
printf '#!/bin/sh\necho "tester:x:1000:1000::%s/home:/bin/sh"\n' "$T" > "$T/bin/getent"; chmod +x "$T/bin/getent"
printf '#!/bin/sh\nfor a in "$@"; do last=$a; done\nmkdir -p "$last"\n' > "$T/bin/install"; chmod +x "$T/bin/install"
fail=0
check() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }
run() { PATH="$T/bin:$PATH" MTSPI_SETUP_FILE="$T/mtspi.txt" MTSPI_DEFAULTS="$T/defaults" MTSPI_USER=tester MTSPI_P2P_CONF="$T/p2p.conf" MTSPI_SUDOERS="$T/sudoers" MTSPI_NM_STATE="$T/nm.state" bash system/setup-file.sh > "$T/out" 2>&1; }

# 1. the template as shipped: nothing happens
cp system/mtspi.txt "$T/mtspi.txt"; : > "$T/calls"; : > "$T/defaults"
run
check "empty template changes nothing" '[ ! -s "$T/calls" ] && [ ! -s "$T/defaults" ]'

# 2. a filled file, saved on Windows (CR LF), password with spaces and symbols
printf 'device_name=MTS Pi\ncountry=XX\n' > "$T/p2p.conf"
printf 'WIFI_COUNTRY=it\r\nWIFI_NAME=My Home Net\r\nWIFI_PASSWORD=pa ss=w$rd"x\r\nSSH_KEY=ssh-ed25519 AAAATESTKEY someone\r\n' > "$T/mtspi.txt"
: > "$T/calls"
run
check "country stored upper-case" 'grep -qx "WIFI_COUNTRY=IT" "$T/defaults"'
check "country written to the Wi-Fi Direct settings" 'grep -qx "country=IT" "$T/p2p.conf" && grep -q "^device_name=" "$T/p2p.conf"'
check "administrator rights granted with the key" 'grep -q "^tester ALL=(ALL) NOPASSWD: ALL" "$T/sudoers"'
check "country applied" 'grep -q "^iw reg set IT" "$T/calls"'
check "home Wi-Fi saved with the exact name and password" 'grep -q "ssid My Home Net wifi-sec.key-mgmt wpa-psk wifi-sec.psk pa ss=w\$rd\"x connection.autoconnect yes" "$T/calls"'
check "password removed from the file" '! grep -q "pa ss" "$T/mtspi.txt" && grep -q "^WIFI_PASSWORD=" "$T/mtspi.txt"'
check "name left in the file" 'grep -q "My Home Net" "$T/mtspi.txt"'
check "SSH key installed once" '[ "$(grep -c AAAATESTKEY "$T/home/.ssh/authorized_keys")" = 1 ]'
check "SSH turned on, without waiting for it to start" 'grep -q "^systemctl enable ssh" "$T/calls" && grep -q "^systemctl --no-block start ssh" "$T/calls" && ! grep -q "enable --now" "$T/calls"'

# 3. second start with the same file: Wi-Fi is not added again, the key is not duplicated, country still applied
: > "$T/calls"; before=$(ls -i "$T/defaults" "$T/p2p.conf")
run
check "second start: settings files not written again" '[ "$(ls -i "$T/defaults" "$T/p2p.conf")" = "$before" ]'
check "second start: Wi-Fi not added again" '! grep -q "connection add" "$T/calls"'
check "second start: key not duplicated" '[ "$(grep -c AAAATESTKEY "$T/home/.ssh/authorized_keys")" = 1 ]'
check "second start: country applied again" 'grep -q "^iw reg set IT" "$T/calls"'
check "second start: SSH not enabled again, started without waiting" '! grep -q "^systemctl enable ssh" "$T/calls" && grep -q "^systemctl --no-block start ssh" "$T/calls"'

# 4. no file at all: the remembered country is still applied
rm "$T/mtspi.txt"; : > "$T/calls"
run
check "no file: remembered country applied" 'grep -q "^iw reg set IT" "$T/calls"'

# 5. nonsense values are ignored
printf 'WIFI_COUNTRY=Italy\nSSH_KEY=hello\n' > "$T/mtspi.txt"; : > "$T/calls"; : > "$T/defaults"
run
check "bad country ignored" '! grep -q WIFI_COUNTRY "$T/defaults"'
check "bad key ignored" '! grep -q "start ssh" "$T/calls" && ! grep -q "enable ssh" "$T/calls"'

# 6. settings damaged by an abrupt restart (zero bytes before the line, as seen on the first real start of a card)
#    and Wi-Fi switched off inside NetworkManager
printf 'CARLINKIT=usb\n\000\000\000\000WIFI_COUNTRY=IT\nWIFI_COUNTRY=IT\n' > "$T/defaults"
printf 'WIFI_COUNTRY=IT\n' > "$T/mtspi.txt"; : > "$T/calls"
printf '[main]\nNetworkingEnabled=true\nWirelessEnabled=false\n' > "$T/nm.state"
run
check "damaged settings: zero bytes removed" '[ "$(tr -d "\000" < "$T/defaults" | wc -c)" = "$(wc -c < "$T/defaults")" ]'
check "damaged settings: one country line, other settings kept" '[ "$(grep -c WIFI_COUNTRY "$T/defaults")" = 1 ] && grep -qx "CARLINKIT=usb" "$T/defaults"'
check "Wi-Fi switched on in NetworkManager" 'grep -q "^nmcli radio wifi on" "$T/calls"'

# 7. Wi-Fi already on inside NetworkManager: it is not asked again (early in the boot the question took 12 s)
printf '[main]\nNetworkingEnabled=true\nWirelessEnabled=true\n' > "$T/nm.state"; : > "$T/calls"
run
check "NetworkManager not asked when Wi-Fi is already on" '! grep -q "^nmcli radio" "$T/calls" && grep -q "^iw reg set IT" "$T/calls"'
# 8. Siri: switched on once (the two services are restarted without waiting), nothing done when already set,
#    switched off again, nonsense ignored
printf 'CARLINKIT=usb\n' > "$T/defaults"; printf 'SIRI=On\r\n' > "$T/mtspi.txt"; : > "$T/calls"
run
check "Siri on: stored, services restarted without waiting" 'grep -qx "HFP_VR=1" "$T/defaults" && grep -qx "CARLINKIT=usb" "$T/defaults" && grep -q "^systemctl --no-block try-restart mtspi-bridge.service mtspi-hfp.service" "$T/calls"'
: > "$T/calls"
run
check "Siri on, second start: nothing restarted" '! grep -q "try-restart" "$T/calls" && [ "$(grep -c HFP_VR "$T/defaults")" = 1 ]'
printf 'SIRI=off\n' > "$T/mtspi.txt"; : > "$T/calls"
run
check "Siri off: stored, services restarted" 'grep -qx "HFP_VR=0" "$T/defaults" && [ "$(grep -c HFP_VR "$T/defaults")" = 1 ] && grep -q "try-restart" "$T/calls"'
printf 'SIRI=maybe\n' > "$T/mtspi.txt"; : > "$T/calls"
run
check "Siri: nonsense ignored" 'grep -qx "HFP_VR=0" "$T/defaults" && ! grep -q "try-restart" "$T/calls"'
printf 'SIRI=\n' > "$T/mtspi.txt"; : > "$T/calls"
run
check "Siri: empty line leaves the setting alone" 'grep -qx "HFP_VR=0" "$T/defaults" && ! grep -q "try-restart" "$T/calls"'
[ "$fail" = 0 ] && echo "ALL OK" || { echo "SOME FAILED"; cat "$T/out"; exit 1; }
