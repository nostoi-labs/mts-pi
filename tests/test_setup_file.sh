#!/bin/bash
# Test of system/setup-file.sh with stand-in system commands. Needs GNU sed (run it on Linux; the image build runs it).
set -u
cd "$(dirname "$0")/.."
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/home"
for c in nmcli iw rfkill systemctl chown; do printf '#!/bin/sh\necho "%s $*" >> "%s/calls"\nexit 0\n' "$c" "$T" > "$T/bin/$c"; chmod +x "$T/bin/$c"; done
printf '#!/bin/sh\necho "tester:x:1000:1000::%s/home:/bin/sh"\n' "$T" > "$T/bin/getent"; chmod +x "$T/bin/getent"
printf '#!/bin/sh\nfor a in "$@"; do last=$a; done\nmkdir -p "$last"\n' > "$T/bin/install"; chmod +x "$T/bin/install"
fail=0
check() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }
run() { PATH="$T/bin:$PATH" MTSPI_SETUP_FILE="$T/mtspi.txt" MTSPI_DEFAULTS="$T/defaults" MTSPI_USER=tester MTSPI_P2P_CONF="$T/p2p.conf" MTSPI_SUDOERS="$T/sudoers" bash system/setup-file.sh > "$T/out" 2>&1; }

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
check "SSH turned on" 'grep -q "^systemctl enable --now ssh" "$T/calls"'

# 3. second start with the same file: Wi-Fi is not added again, the key is not duplicated, country still applied
: > "$T/calls"
run
check "second start: Wi-Fi not added again" '! grep -q "connection add" "$T/calls"'
check "second start: key not duplicated" '[ "$(grep -c AAAATESTKEY "$T/home/.ssh/authorized_keys")" = 1 ]'
check "second start: country applied again" 'grep -q "^iw reg set IT" "$T/calls"'

# 4. no file at all: the remembered country is still applied
rm "$T/mtspi.txt"; : > "$T/calls"
run
check "no file: remembered country applied" 'grep -q "^iw reg set IT" "$T/calls"'

# 5. nonsense values are ignored
printf 'WIFI_COUNTRY=Italy\nSSH_KEY=hello\n' > "$T/mtspi.txt"; : > "$T/calls"; : > "$T/defaults"
run
check "bad country ignored" '! grep -q WIFI_COUNTRY "$T/defaults"'
check "bad key ignored" '! grep -q "enable --now ssh" "$T/calls"'
[ "$fail" = 0 ] && echo "ALL OK" || { echo "SOME FAILED"; cat "$T/out"; exit 1; }
