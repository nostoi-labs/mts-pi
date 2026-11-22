"""The Pi gets back on its feet by itself. Three things seen on the bike on 21 Nov 2026, each of which needed the
ignition (or the Pi's plug) to be cycled by hand:

- the bike's Wi-Fi gave no address: the join failed once and nothing tried again;
- the radio chip restarted itself ("hci0: hardware error"): Bluetooth came back in 5 s, Wi-Fi Direct never did;
- the Bluetooth radio stopped answering ("hci0: command tx timeout" every 2 s): everything on Bluetooth was gone.
On the bench the same night, with Bluetooth tethering traffic on top of music and Wi-Fi: "hardware error", then the
reset itself unanswered ("Opcode 0x0c03 failed: -110") and the radio left switched off. Switching it on again and
reloading its driver did not bring it back; an orderly restart of the Pi did.

On the bench on 22 Nov 2026, after three hours of Siri opened and closed every 20 s, the last hour and a half at
74-76 °C: the voice channel to the helmet stopped opening, with the radio up and no error from it. The kernel filled
with "SCO packet for unknown connection handle" (about 265 a second): the chip carried a voice link the system no
longer knew about. Closing the helmet's link stopped those lines, but new voice links still failed; switching
Bluetooth off and on gave three good openings in eight; an orderly restart of the Pi gave nine in nine.

What happens now, from the mildest step to the last one:
  1. a failed join is tried again as soon as the bike offers its Wi-Fi again (it usually does within a second);
  2. if the bike stays silent after a failure, its Bluetooth link is closed and reopened (what unplugging the Pi did);
  3. after two failures in a row the Wi-Fi Direct program is restarted;
  4. if nothing helps, the Bluetooth radio no longer answers, or it carries a voice link nobody opened, the Pi
     restarts: once, then not again for 15 minutes.
Nothing here acts unless a join has failed or the kernel reports the radio: a bike that simply does not offer its
Wi-Fi (Ducati Connect switched off) is left alone. No step is taken during a phone call, except for a dead radio."""
import json
import os
import subprocess
import time

BOUNCE_AFTER_S = 20          # bike silent this long after a failed join: first Bluetooth reconnection
BOUNCES = 2                  # the second one comes after 60 s more
RESTART_P2P_AT = 2           # failed joins in a row before the Wi-Fi Direct program is restarted
REBOOT_AT = 4                # failed joins in a row before the Pi restarts (about 4 minutes on the bike)
REBOOT_IDLE_S = 90           # bike still silent this long after the last Bluetooth reconnection: the Pi restarts
REBOOT_MIN_UPTIME_S = 120
REBOOT_EVERY_S = 900
RADIO_RESET_RECENT_S = 180
RADIO_BACK_S = 20            # after the chip restarted itself: Bluetooth must be switched on again within this time
STUCK_COUNT, STUCK_WINDOW_S = 4, 30
# Voice packets for a link the system does not know. One to four of these lines can follow an ordinary closing; the
# fault writes ten every five seconds (the kernel holds back the rest).
GHOST_COUNT, GHOST_WINDOW_S = 20, 60
GHOST_BOUNCE_EVERY_S = 60    # when the restart is not allowed: the bike's link is closed and reopened, at most this often


class RadioWatch:
    """Reads the kernel's lines about the Bluetooth radio: "stuck" when it stopped answering, "reset" when the chip
    restarted, "dead" when it did not come back, "ghost" when it carries a voice link the system does not know."""

    def __init__(self, now=time.monotonic):
        self.now = now
        self.timeouts = []
        self.ghosts = []

    def feed(self, line):
        if "Bluetooth: hci" not in line:
            return None
        if "Opcode 0x0c03 failed" in line:          # the reset after a hardware error got no answer: the radio is gone
            return "dead"
        if "hardware error" in line:
            return "reset"
        if "SCO packet for unknown connection handle" in line:
            t = self.now()
            self.ghosts = [x for x in self.ghosts if t - x <= GHOST_WINDOW_S] + [t]
            if len(self.ghosts) >= GHOST_COUNT:
                self.ghosts = []
                return "ghost"
            return None
        if "tx timeout" in line:                    # "command tx timeout", "command 0x041f tx timeout", "link tx timeout"
            t = self.now()
            self.timeouts = [x for x in self.timeouts if t - x <= STUCK_WINDOW_S] + [t]
            if len(self.timeouts) >= STUCK_COUNT:
                self.timeouts = []
                return "stuck"
        return None


def uptime_s():
    try:
        return float(open("/proc/uptime").read().split()[0])
    except (OSError, ValueError):
        return 0.0


class Recovery:
    def __init__(self, log, state_dir="/var/lib/mtspi", run=None, now=time.monotonic, wall=time.time, uptime=uptime_s):
        self.log, self.now, self.wall, self.uptime = log, now, wall, uptime
        self.record = os.path.join(state_dir, "recovery.log")
        self.run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=30))
        self.streak = 0              # failed joins in a row
        self.bounces = 0             # Bluetooth reconnections of the bike in this streak
        self.radio_reset_t = None
        self.skipped = None          # the reason a restart was not allowed, logged once

    # --- what to do next -------------------------------------------------------------------------------------------
    def join_ok(self):
        if self.streak:
            self.log("mirror_recovered", after_failures=self.streak, bluetooth_reconnections=self.bounces)
        self.streak = self.bounces = 0
        self.skipped = None

    def join_failed(self):
        """A join to the bike's Wi-Fi failed. Returns "restart_p2p", "reboot" or None (just wait for the next offer)."""
        self.streak += 1
        recent_reset = self.radio_reset_t is not None and self.now() - self.radio_reset_t <= RADIO_RESET_RECENT_S
        if self.streak >= REBOOT_AT:
            return "reboot"
        if self.streak == RESTART_P2P_AT or (self.streak == 1 and recent_reset):
            return "restart_p2p"
        return None

    def idle_action(self, idle_s, in_call=False):
        """The bike has not reopened its control channel for idle_s seconds after a failed join."""
        if self.streak == 0 or in_call:
            return None
        if self.bounces < BOUNCES:
            if idle_s >= BOUNCE_AFTER_S * (1 + 2 * self.bounces):
                self.bounces += 1
                return "bounce"
            return None
        return "reboot" if idle_s >= REBOOT_IDLE_S else None

    def radio_reset(self):
        self.radio_reset_t = self.now()

    # --- the steps themselves -----------------------------------------------------------------------------------------
    def restart_p2p(self):
        r = self.run(["sudo", "-n", "/usr/bin/systemctl", "restart", "mtspi-p2p.service"])
        self.log("mirror_p2p_restarted", failures=self.streak, code=r.returncode, error=(r.stderr or "").strip()[-120:] or None)
        return r.returncode == 0

    def last_reboot(self):
        try:
            for line in reversed(open(self.record).read().splitlines()):
                rec = json.loads(line)
                if rec.get("what") == "reboot":
                    return float(rec["t"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def reboot_allowed(self):
        up = self.uptime()
        if up < REBOOT_MIN_UPTIME_S:
            return False, f"the Pi started {up:.0f} s ago"
        last = self.last_reboot()
        if last is not None and 0 <= self.wall() - last < REBOOT_EVERY_S:
            return False, f"already restarted {self.wall() - last:.0f} s ago"
        return True, None

    def reboot(self, why):
        ok, reason = self.reboot_allowed()
        if not ok:
            if self.skipped != (why, reason[:20]):
                self.skipped = (why, reason[:20])
                self.log("recovery_reboot_skipped", why=why, because=reason)
            return False
        # kept on the data partition: with the logs in memory nothing else would say why the Pi restarted
        try:
            lines = []
            if os.path.exists(self.record):
                lines = open(self.record).read().splitlines()[-49:]
            lines.append(json.dumps({"t": round(self.wall()), "what": "reboot", "why": why, "failures": self.streak,
                                     "uptime_s": round(self.uptime())}))
            tmp = self.record + ".new"
            with open(tmp, "w") as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.record)
        except OSError as e:
            self.log("recovery_record_failed", error=str(e))
        self.log("recovery_reboot", why=why, failures=self.streak)
        r = self.run(["sudo", "-n", "/usr/bin/systemctl", "reboot"])
        if r.returncode != 0:
            self.log("recovery_reboot_failed", code=r.returncode, error=(r.stderr or "").strip()[-120:])
        return r.returncode == 0
