#!/bin/bash
# Applies the optional settings file on the boot partition (mtspi.txt): Wi-Fi country, home Wi-Fi, SSH key.
# Runs as root at every start (mtspi-setup.service), before the Wi-Fi mode service. Does nothing if the file
# is missing or empty. The boot partition is the only part of the card every computer can read and write,
# which is why the file lives there.
set -u
FILE=${MTSPI_SETUP_FILE:-/boot/firmware/mtspi.txt}
DEF=${MTSPI_DEFAULTS:-/etc/default/mtspi}
U=${MTSPI_USER:-mtspi}
P2P=${MTSPI_P2P_CONF:-/etc/wpa_supplicant/mtspi-p2p.conf}
SUDOERS=${MTSPI_SUDOERS:-/etc/sudoers.d/mtspi-remote}
log() { echo "setup file: $*"; }

# put FILE KEY LINE: FILE keeps a single line for KEY (the new LINE). The file is rebuilt aside and swapped in,
# then flushed to the card: the first start of a fresh card restarts the Pi abruptly, and a plain append left
# zero bytes in the settings (which made systemd ignore the whole file, so the bridge ran without the Carlinkit).
put() {
    local f=$1 tmp
    tmp=$(mktemp "$f.XXXXXX") || return 1
    { [ -f "$f" ] && tr -d '\000' < "$f" | grep -v "^[[:space:]]*$2"; echo "$3"; } > "$tmp"
    if [ -f "$f" ]; then chmod --reference="$f" "$tmp" 2>/dev/null; chown --reference="$f" "$tmp" 2>/dev/null; else chmod 644 "$tmp"; fi
    mv "$tmp" "$f"
    sync
}

COUNTRY=""; SSID=""; PSK=""; KEY=""
if [ -f "$FILE" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%$'\r'}                              # files saved on Windows
        case "$line" in ''|'#'*) continue ;; esac
        case "$line" in *=*) ;; *) continue ;; esac
        k=${line%%=*}; v=${line#*=}
        k=$(printf '%s' "$k" | tr -d '[:space:]')
        v=$(printf '%s' "$v" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/')
        case "$k" in
            WIFI_COUNTRY) COUNTRY=$(printf '%s' "$v" | tr '[:lower:]' '[:upper:]') ;;
            WIFI_NAME) SSID=$v ;;
            WIFI_PASSWORD) PSK=$v ;;
            SSH_KEY) KEY=$v ;;
        esac
    done < "$FILE"
fi

# country: from the file the first time, then remembered in the settings
if [ -n "$COUNTRY" ]; then
    case "$COUNTRY" in
        [A-Z][A-Z])
            put "$DEF" 'WIFI_COUNTRY=' "WIFI_COUNTRY=$COUNTRY"
            [ -f "$P2P" ] && put "$P2P" 'country=' "country=$COUNTRY"
            log "Wi-Fi country $COUNTRY" ;;
        *) log "WIFI_COUNTRY must be two letters, ignored: $COUNTRY"; COUNTRY="" ;;
    esac
fi
[ -n "$COUNTRY" ] || COUNTRY=$(sed -n 's/^WIFI_COUNTRY=//p' "$DEF" 2>/dev/null | tail -1)
if [ -n "$COUNTRY" ]; then
    iw reg set "$COUNTRY" 2>/dev/null || true
    rfkill unblock wlan 2>/dev/null || true
    # a fresh Raspberry Pi OS also keeps Wi-Fi switched off inside NetworkManager until a country is chosen:
    # without this the home network is saved but never joined. Asked only when it is off: early in the boot
    # NetworkManager answers when it has finished starting, and this script stood still for 12 s (17 Nov 2026)
    grep -qs '^WirelessEnabled=true' /var/lib/NetworkManager/NetworkManager.state || nmcli radio wifi on >/dev/null 2>&1 || true
fi

# home Wi-Fi: saved as a NetworkManager network; the Wi-Fi mode service goes back to it when the bike is away
if [ -n "$SSID" ] && [ -n "$PSK" ]; then
    nmcli connection delete mtspi-home >/dev/null 2>&1 || true
    if nmcli connection add type wifi con-name mtspi-home ifname wlan0 ssid "$SSID" \
            wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK" connection.autoconnect yes >/dev/null 2>&1; then
        sed -i 's/^\([[:space:]]*WIFI_PASSWORD[[:space:]]*=\).*/\1/' "$FILE"      # the password does not stay on the card in clear
        sync
        log "home Wi-Fi \"$SSID\" saved, password removed from the file"
    else
        log "could not save the home Wi-Fi \"$SSID\""
    fi
fi

# SSH key: remote login for the service user, with administrator rights
if [ -n "$KEY" ]; then
    case "$KEY" in
        ssh-*|ecdsa-*|sk-*)
            H=$(getent passwd "$U" | cut -d: -f6)
            if [ -n "$H" ]; then
                install -d -m 700 -o "$U" -g "$U" "$H/.ssh"
                touch "$H/.ssh/authorized_keys"
                NEW=0
                grep -qxF "$KEY" "$H/.ssh/authorized_keys" || { echo "$KEY" >> "$H/.ssh/authorized_keys"; NEW=1; }
                chown "$U:$U" "$H/.ssh/authorized_keys"; chmod 600 "$H/.ssh/authorized_keys"
                [ -s "$SUDOERS" ] || { echo "$U ALL=(ALL) NOPASSWD: ALL" > "$SUDOERS"; chmod 440 "$SUDOERS"; NEW=1; }
                # never wait for the start here: early in the boot ssh is queued behind other units, and this
                # script, which the Wi-Fi mode waits for, stood still for 17 s (measured on 17 Nov 2026)
                systemctl is-enabled --quiet ssh 2>/dev/null || systemctl is-enabled --quiet sshd 2>/dev/null \
                    || systemctl enable ssh >/dev/null 2>&1 || systemctl enable sshd >/dev/null 2>&1 || true
                systemctl --no-block start ssh >/dev/null 2>&1 || systemctl --no-block start sshd >/dev/null 2>&1 || true
                [ "$NEW" = 0 ] || sync
                log "remote login enabled for $U"
            fi ;;
        *) log "SSH_KEY does not look like a public key, ignored" ;;
    esac
fi
exit 0
