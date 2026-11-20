#!/usr/bin/env python3
"""Bluetooth pairings in memory, copied to the card only when a pairing changes. Part of the protected card.

BlueZ keeps its files in /var/lib/bluetooth and rewrites some of them at every power-on (the radio's settings, its
list of services) even when nothing changed. On a card that must not be written while the Pi works, that folder now
lives in memory:
    load    at boot, before BlueZ starts: /data/bluetooth is copied into the memory folder
    watch   while the Pi works: when a pairing appears, changes or is removed, the folder is copied back to
            /data/bluetooth (each file written aside, flushed, then renamed: a power cut leaves the old or the new one)
What counts as "a pairing changed": the files called "info" (one per paired device: keys, name, trust) and the list
of device folders. Everything else is copied along when that happens, and never causes a write by itself.

The same tool can keep other folders too (BTSTORE_LIVE, BTSTORE_KEPT, BTSTORE_MARK, BTSTORE_TAG):
    BTSTORE_WHAT=all    every file counts, not only the pairings
    BTSTORE_OPEN=/data  for a data partition mounted read-only: it is opened for writing only for the copy, then
                        flushed and closed again. A second keeper waits its turn (lock in /run).
Standard library only. Runs as root (BlueZ's folder is root's).
"""
import contextlib
import fcntl
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import time

LIVE = os.environ.get("BTSTORE_LIVE", "/var/lib/bluetooth")
KEPT = os.environ.get("BTSTORE_KEPT", "/data/bluetooth")
MARK = os.environ.get("BTSTORE_MARK", "/run/mtspi-btstore.loaded")
EVERY = float(os.environ.get("BTSTORE_EVERY", "1"))
ALL = os.environ.get("BTSTORE_WHAT", "pairings") == "all"
OPEN = os.environ.get("BTSTORE_OPEN", "")
LOCK = os.environ.get("BTSTORE_LOCK", "/run/mtspi-data.lock")
TAG = os.environ.get("BTSTORE_TAG", "btstore")
ASIDE = (".btstore-new", ".tmp")                # files being written: never copied, never counted


def say(*a):
    print(TAG + ":", *a, flush=True)


def files(root):
    """{relative path: full path} of every regular file under root."""
    out = {}
    for d, _, names in os.walk(root):
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p) and not os.path.islink(p) and not n.endswith(ASIDE):
                out[os.path.relpath(p, root)] = p
    return out


def dirs(root):
    return {os.path.relpath(os.path.join(d, n), root) for d, ns, _ in os.walk(root) for n in ns}


def digest(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def devices(root):
    """The device folders: <radio address>/<device address>, not the cache."""
    return {d for d in dirs(root) if d.count(os.sep) == 1 and os.path.basename(d) != "cache"}


def pairings(root):
    """What must never be lost: the content of every 'info' file, and which device folders exist.
    With BTSTORE_WHAT=all: the content of every file, and every folder."""
    if ALL:
        return {r: digest(p) for r, p in files(root).items()}, dirs(root)
    return {r: digest(p) for r, p in files(root).items() if os.path.basename(r) == "info"}, devices(root)


def glance(root):
    """Cheap fingerprint of the same things, without reading the files: bluetoothd replaces a file by renaming a new
    one over it, so a change always shows as a new inode or a new time."""
    out = []
    for r, p in sorted(files(root).items()):
        if ALL or os.path.basename(r) == "info":
            try:
                st = os.stat(p)
                out.append((r, st.st_ino, st.st_size, st.st_mtime_ns))
            except OSError:
                pass
    return out, sorted(dirs(root) if ALL else devices(root))


def own(src, dst):
    """dst gets the owner of src (the bridge's files are not root's). Not being root is not an error (tests)."""
    try:
        st = os.stat(src)
        os.chown(dst, st.st_uid, st.st_gid)
    except OSError:
        pass


def read_only(mount):
    return bool(os.statvfs(mount).f_flag & os.ST_RDONLY)


@contextlib.contextmanager
def opened():
    """The data partition open for writing, only for as long as the copy takes. Nothing to do without BTSTORE_OPEN
    or when the partition is already writable (system in writable mode, or the experiment switched off)."""
    if not OPEN:
        yield
        return
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        closed = read_only(OPEN)
        if closed:
            subprocess.run(["mount", "-o", "remount,rw", OPEN], check=True)
        try:
            yield
        finally:
            if closed:
                os.sync()
                r = subprocess.run(["mount", "-o", "remount,ro", OPEN])
                if r.returncode:
                    say(f"could not close {OPEN} again (mount said {r.returncode}): it stays writable until restart")


def put(src, dst):
    """Copies one file so that a power cut leaves either the old or the new one."""
    os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
    tmp = dst + ".btstore-new"
    with open(src, "rb") as f:
        data = f.read()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, os.stat(src).st_mode & 0o777)
    own(src, tmp)
    os.replace(tmp, dst)
    fd = os.open(os.path.dirname(dst), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mirror(src, dst, durable):
    """Makes dst equal to src, writing only what differs. Returns (files written, files removed)."""
    wrote = removed = 0
    a, b = files(src), files(dst)
    for d in sorted(dirs(src)):
        if not os.path.isdir(os.path.join(dst, d)):
            os.makedirs(os.path.join(dst, d), mode=0o700, exist_ok=True)
            os.chmod(os.path.join(dst, d), os.stat(os.path.join(src, d)).st_mode & 0o7777)
            own(os.path.join(src, d), os.path.join(dst, d))
    for r, p in a.items():
        if r not in b or digest(p) != digest(b[r]):
            if durable:
                put(p, os.path.join(dst, r))
            else:
                os.makedirs(os.path.dirname(os.path.join(dst, r)), mode=0o700, exist_ok=True)
                shutil.copy2(p, os.path.join(dst, r))
                own(p, os.path.join(dst, r))
            wrote += 1
    touched = set()
    for r in b:
        if r not in a:
            os.remove(b[r])
            removed += 1
            touched.add(os.path.dirname(b[r]))
    for d in sorted(dirs(dst) - dirs(src), key=len, reverse=True):
        try:
            os.rmdir(os.path.join(dst, d))
            touched.discard(os.path.join(dst, d))
            touched.add(os.path.dirname(os.path.join(dst, d)))
        except OSError:
            pass
    if durable:                                  # a removal must reach the card too, not wait in memory
        for d in touched:
            try:
                fd = os.open(d, os.O_RDONLY)
                os.fsync(fd)
                os.close(fd)
            except OSError:
                pass
    return wrote, removed


def load():
    if not os.path.ismount(os.path.dirname(KEPT)) and "BTSTORE_KEPT" not in os.environ:
        say("the data partition is not there: Bluetooth starts without pairings, and nothing will be copied back")
        return 1
    if not os.path.isdir(KEPT):
        with opened():
            os.makedirs(KEPT, mode=0o700, exist_ok=True)
    wrote, _ = mirror(KEPT, LIVE, durable=False)
    if not ALL:
        os.chmod(LIVE, 0o700)
    with open(MARK, "w") as f:
        f.write("ok\n")
    say(f"loaded into memory: {wrote} files" + ("" if ALL else f", {len(devices(LIVE))} devices"))
    return 0


def save(why):
    live, kept = pairings(LIVE), pairings(KEPT)
    if live == kept:
        return False
    if kept[0] and not (files(LIVE) if ALL else dirs(LIVE)):
        say("the memory folder is EMPTY while the card has " + ("files" if ALL else "pairings")
            + ": not copying (the folder in memory was lost?)")
        return False
    t = time.monotonic()
    with opened():
        wrote, removed = mirror(LIVE, KEPT, durable=True)
    say(f"{why}: copied to the card, {wrote} files written, {removed} removed, "
        + ("" if ALL else f"{len(live[1])} devices, ") + f"{(time.monotonic() - t) * 1000:.0f} ms")
    return True


def watch():
    if not os.path.exists(MARK):
        say("pairings were not loaded at boot: not watching (nothing is copied back)")
        return 0
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    try:
        save("at start")
    except (OSError, subprocess.SubprocessError) as e:
        say(f"could not copy to the card: {e}")
    last = glance(LIVE)
    say("watching " + ("the folder" if ALL else "the pairings"))
    while not stop:
        time.sleep(EVERY)
        now = glance(LIVE)
        if now != last:
            if ALL:                              # a program may still be writing: wait until two looks agree
                time.sleep(EVERY)
                again = glance(LIVE)
                if again != now:
                    continue
            last = now
            try:
                save("changed" if ALL else "pairing changed")
            except (OSError, subprocess.SubprocessError) as e:
                say(f"could not copy to the card: {e}")
                last = None                      # try again at the next round
    try:
        save("at stop")
    except (OSError, subprocess.SubprocessError) as e:
        say(f"could not copy to the card: {e}")
    return 0


if __name__ == "__main__":
    sys.exit({"load": load, "watch": watch}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: 2)())
