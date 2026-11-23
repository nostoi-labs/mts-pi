#!/usr/bin/env python3
"""A new version of the program installed from a file on the boot partition, without writing the card again
(ready-made card image only).

How it is used: copy mts-pi-update-<version>.tar.gz, as downloaded and without unpacking it, to the boot partition of
the card (the small one every computer shows, where mtspi.txt is) and start the Pi. It installs the update, restarts
once and writes what happened in mts-pi-update-result.txt next to the package. To go back to the version before, put
a file named mts-pi-rollback.txt (empty is fine) in the same place.

A Pi on a bike loses power without warning, so every step can be cut at any moment:
  1. the package is unpacked into a NEW folder next to the program and every file is checked against the list inside
     the package. The program in use is not touched. A cut here: the next start begins again from the package.
  2. the new folder must load (the same check the installer makes). If it does not, nothing changes.
  3. the two folders change places in ONE step of the file system (renameat2 with RENAME_EXCHANGE): after a cut there
     is either the old program or the new one, never half of each. The old one is kept, for the way back.
  4. the files of ours that live outside the folder (service units, small configuration files) are copied by the new
     version's own system/sysfiles.sh. A mark in the folder says this step is still to do: after a cut it is done
     again at the next start (it can be repeated safely).
  5. the result is written, the package is renamed so that it is not installed twice, the Pi restarts.
Only the program is updated, never the operating system: a package that needs a newer card image says so and is
refused. With the card protected (read-only system with a layer in memory on top) the real system partition is under
/media/root-ro: it is made writable only for the time of the update.

    update.py                                            at every start (mtspi-update.service)
    update.py pack TREE VERSION OUT [--needs-image N]    makes a package from a folder holding the repository
"""
import ctypes
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path, PurePosixPath

BOOT = Path(os.environ.get("MTSPI_BOOT", "/boot/firmware"))
LOWER = Path(os.environ.get("MTSPI_LOWER", "/media/root-ro"))          # the real system partition of a protected card
PROC_CMDLINE = Path(os.environ.get("MTSPI_PROC_CMDLINE", "/proc/cmdline"))
PROC_MOUNTS = Path(os.environ.get("MTSPI_PROC_MOUNTS", "/proc/mounts"))
PROGRAM = os.environ.get("MTSPI_PROGRAM", "opt/mts-pi")               # under the system partition
REBOOT = os.environ.get("MTSPI_REBOOT", "systemctl reboot")
REBOOT_WAIT = float(os.environ.get("MTSPI_REBOOT_WAIT", "300"))
# the folder must load before it takes the place of the one in use; the list of modules is the installer's
LOAD_CHECK = os.environ.get("MTSPI_LOAD_CHECK", '.venv/bin/python -c "import mtspi.bridge, mtspi.hfp_ag"')
COMPILE = os.environ.get("MTSPI_COMPILE", ".venv/bin/python -m compileall -q mtspi")
ROOT_OVERRIDE = os.environ.get("MTSPI_ROOT")                          # tests: a folder standing in for the system

PACKAGES = ("mts-pi-update*.tar.gz", "mts-pi-update*.tgz", "mts-pi-update*.tar")
ROLLBACK = "mts-pi-rollback*"
RESULT = "mts-pi-update-result.txt"
MANIFEST = "UPDATE.json"
STAMP = ".update"                 # in the program folder: which package it came from, and a counter that only grows
PENDING = ".apply-pending"        # in the program folder: system/sysfiles.sh still has to run
ROLLED_BACK = ".rolled-back"      # in the program folder: the rollback file that brought it back
KEEP = (".venv",)                 # carried over from the folder in use (the Python environment made by the installer)
IMAGE_LEVEL = "etc/mtspi-image"   # what the card image holds besides the program (a number, written when it is built)
MAX_BYTES = 64 << 20              # of unpacked files: the program is about 2 MB


class Refused(Exception):
    """The package is not installed, and the reason is one the person can read in the result file."""


def say(*a):
    print("mtspi-update:", *a, flush=True)


# ---------------------------------------------------------------- the package

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def safe_name(name):
    """The place of a file inside the folder, or Refused: a package may write only below the folder it is given."""
    p = PurePosixPath(name)
    parts = [x for x in p.parts if x != "."]
    if p.is_absolute() or not parts or ".." in parts or any("\\" in x or "\0" in x for x in parts):
        raise Refused(f"the package holds a file with a name that is not allowed: {name!r}")
    return "/".join(parts)


def pack(tree, version, out, needs_image=1):
    """A package from a folder holding the repository. Left out: version control, Python's caches, the environment,
    and what the Pi never uses when it runs (documentation, tests, what builds the card image and the installer)."""
    tree, out = Path(tree), Path(out)
    skip_dirs = {".git", ".venv", "__pycache__", ".github", ".pytest_cache", "out", "dist", "build"}
    skip_top = {"docs", "tests", "image", "installer"}
    files = {}
    for root, dirs, names in os.walk(tree):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs and not d.startswith(".")
                         and not (Path(root) == tree and d in skip_top))
        for n in sorted(names):
            p = Path(root) / n
            rel = p.relative_to(tree).as_posix()
            if n.endswith(".pyc") or rel in (MANIFEST, "VERSION") or p.is_symlink() or not p.is_file():
                continue
            files[rel] = p
    if "system/update.py" not in files or "system/sysfiles.sh" not in files:
        raise SystemExit(f"{tree} does not look like the repository (no system/update.py or system/sysfiles.sh)")
    # a package is made from the tree that is published, and from nothing else: a working folder holds more
    # (sources that are published only as binaries, tools, keys of test devices), and much of it is not for a card
    sources = sorted(r for r in files if r.startswith("myspin/") and r.endswith(".py") and r != "myspin/__init__.py")
    if sources:
        raise SystemExit(f"{tree} is a working folder, not the published tree ({sources[0]} is there as source)")
    if sum(p.stat().st_size for p in files.values()) > MAX_BYTES:
        raise SystemExit(f"{tree} holds much more than the program: not a clean copy of the published tree")
    version_bytes = (version + "\n").encode()
    sums = {rel: sha256_file(p) for rel, p in files.items()}
    sums["VERSION"] = hashlib.sha256(version_bytes).hexdigest()
    manifest = json.dumps({"name": "mts-pi", "version": version, "needs_image": int(needs_image), "files": sums},
                          indent=1, sort_keys=True).encode()

    def add_bytes(tar, name, data, mode=0o644):
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.mtime = len(data), mode, int(time.time())
        tar.addfile(info, io.BytesIO(data))

    with tarfile.open(out, "w:gz") as tar:
        add_bytes(tar, MANIFEST, manifest)
        add_bytes(tar, "VERSION", version_bytes)
        for rel, p in sorted(files.items()):
            info = tarfile.TarInfo(rel)
            info.size, info.mtime = p.stat().st_size, int(p.stat().st_mtime)
            info.mode = 0o755 if os.access(p, os.X_OK) else 0o644
            with open(p, "rb") as f:
                tar.addfile(info, f)
    return out


def unpack(package, dest):
    """Unpacks into an empty folder and returns the package's own description. Refused unless every file is a plain
    file below the folder, is in the list inside the package with the right content, and the list has no file more."""
    dest = Path(dest)
    seen, total = {}, 0
    try:
        tar = tarfile.open(package, "r:*")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise Refused(f"the file is not a package that can be read (copied only in part?): {e}")
    try:
        with tar:
            for m in tar:
                rel = safe_name(m.name)
                if m.isdir():
                    (dest / rel).mkdir(parents=True, exist_ok=True)
                    continue
                if not m.isreg():
                    raise Refused(f"the package holds something that is not a plain file: {m.name}")
                total += m.size
                if total > MAX_BYTES:
                    raise Refused("the package is much larger than the program can be")
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                src = tar.extractfile(m)
                with open(target, "wb") as f:
                    for block in iter(lambda: src.read(1 << 16), b""):
                        h.update(block)
                        f.write(block)
                os.chmod(target, 0o755 if m.mode & 0o111 else 0o644)
                seen[rel] = h.hexdigest()
    except (tarfile.TarError, EOFError, OSError) as e:
        raise Refused(f"the package cannot be read to the end (copied only in part?): {e}")
    if MANIFEST not in seen:
        raise Refused(f"the package has no {MANIFEST}: it is not an MTS Pi update")
    try:
        manifest = json.loads((dest / MANIFEST).read_text())
        files = dict(manifest["files"])
        if manifest["name"] != "mts-pi" or not isinstance(manifest["version"], str) or not manifest["version"].strip():
            raise ValueError("not an MTS Pi update")
        manifest["needs_image"] = int(manifest.get("needs_image", 1))
    except (ValueError, KeyError, TypeError) as e:
        raise Refused(f"the description inside the package is not valid: {e}")
    del seen[MANIFEST]
    for rel, digest in seen.items():
        if files.get(rel) != digest:
            raise Refused(f"a file in the package is not the one its list describes: {rel}")
    missing = sorted(set(files) - set(seen))
    if missing:
        raise Refused(f"the package lacks {len(missing)} of the files in its list (first: {missing[0]})")
    (dest / MANIFEST).unlink()
    return manifest


# ---------------------------------------------------------------- the system around it

def protected():
    try:
        return "overlayroot=tmpfs" in PROC_CMDLINE.read_text()
    except OSError:
        return False


def real_root():
    """Where the system partition can be written: / on a writable card, the lower layer on a protected one."""
    if ROOT_OVERRIDE:
        return Path(ROOT_OVERRIDE)
    return LOWER if protected() else Path("/")


def mount_options(mountpoint):
    """The options a folder is mounted with, or None when nothing is mounted exactly there."""
    try:
        lines = PROC_MOUNTS.read_text().splitlines()
    except OSError:
        return None
    found = None
    for line in lines:
        f = line.split()
        if len(f) >= 4 and f[1].replace("\\040", " ") == str(mountpoint):
            found = set(f[3].split(","))            # the last line wins: the mount on top
    return found


class Writable:
    """A partition mounted read-only is writable inside the block, and read-only again after it."""

    def __init__(self, mountpoint):
        self.mp, self.was_ro = str(mountpoint), False

    def __enter__(self):
        opts = mount_options(self.mp)
        if opts is not None and "ro" in opts:
            r = subprocess.run(["mount", "-o", "remount,rw", self.mp], capture_output=True, text=True)
            if r.returncode != 0:
                raise OSError(f"{self.mp} cannot be made writable: {r.stderr.strip()}")
            self.was_ro = True
        return self

    def __exit__(self, *exc):
        os.sync()
        if self.was_ro:
            subprocess.run(["mount", "-o", "remount,ro", self.mp], capture_output=True)
        return False


def exchange(a, b):
    """Two folders change places in one step: after a power cut each name holds one whole folder or the other."""
    libc = ctypes.CDLL(None, use_errno=True)
    a, b = os.fsencode(str(a)), os.fsencode(str(b))
    if sys.platform == "darwin":                    # the tests run on a Mac too
        r = libc.renamex_np(a, b, ctypes.c_uint(2))                       # RENAME_SWAP
    else:
        r = libc.renameat2(-100, a, -100, b, ctypes.c_uint(2))            # AT_FDCWD, RENAME_EXCHANGE
    if r != 0:
        e = ctypes.get_errno()
        raise OSError(e, f"the two folders cannot change places in one step: {os.strerror(e)}")


def sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path):
    try:
        v = json.loads(Path(path).read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def version_of(folder):
    try:
        return (Path(folder) / "VERSION").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def serial_of(folder):
    try:
        return int(read_json(Path(folder) / STAMP).get("serial", 0))
    except (TypeError, ValueError):
        return 0


def image_level(root):
    try:
        return int((root / IMAGE_LEVEL).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def user_of(root, folder):
    """The name of the user who owns the program folder, read from the system being updated."""
    uid = os.stat(folder).st_uid
    try:
        for line in (root / "etc/passwd").read_text().splitlines():
            f = line.split(":")
            if len(f) > 2 and f[2] == str(uid):
                return f[0]
    except OSError:
        pass
    return None


def run(cmd, cwd, timeout=300):
    r = subprocess.run(cmd, cwd=str(cwd), shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


# ---------------------------------------------------------------- the steps

class Folders:
    def __init__(self, root):
        self.root = Path(root)
        self.prog = self.root / PROGRAM
        base = str(self.prog)
        self.staging, self.new, self.old = Path(base + ".staging"), Path(base + ".new"), Path(base + ".old")
        self.trash = Path(base + ".trash")           # what is being thrown away: never looked at, only deleted

    def whole(self, folder):
        """A folder that can be the program: its version, its code, its system scripts."""
        f = Path(folder)
        return (f / "VERSION").is_file() and (f / "mtspi").is_dir() and (f / "system").is_dir()


def tidy(d):
    """Puts in order what a power cut left behind. A folder still being filled is thrown away. A complete new folder
    was either not yet in place (thrown away: it is made again from the package) or had already changed places with
    the program, and then it holds the previous version: it becomes the one kept for the way back. The counter in the
    stamp tells the two apart: a new folder always has the counter of the program in use plus one."""
    shutil.rmtree(d.staging, ignore_errors=True)
    empty_trash(d)
    if d.new.exists():
        if serial_of(d.new) > serial_of(d.prog):
            say("a new folder that never took its place is thrown away")
            shutil.rmtree(d.new, ignore_errors=True)
        else:
            say("the previous version is kept for the way back")
            keep_previous(d)


def stage(d, package, digest):
    """Steps 1 and 2: a complete, checked new folder next to the program, under the name that says so."""
    free = shutil.disk_usage(d.prog.parent).free
    if free < 3 * os.path.getsize(package) + (48 << 20):
        raise Refused(f"not enough free space on the card ({free >> 20} MB)")
    shutil.rmtree(d.staging, ignore_errors=True)
    d.staging.mkdir()
    manifest = unpack(package, d.staging)
    level = image_level(d.root)
    if manifest["needs_image"] > level:
        raise Refused(f"version {manifest['version']} needs a newer card image than the one on this card "
                      f"(level {manifest['needs_image']}, this card has {level}): write the card again with the "
                      f"image of that version")
    if not (d.staging / "system/sysfiles.sh").is_file() or not (d.staging / "system/update.py").is_file():
        raise Refused("the package does not hold the whole program")
    for name in KEEP:
        src = d.prog / name
        if src.is_dir():                            # same partition: hard links, no copy and no extra space
            shutil.copytree(src, d.staging / name, symlinks=True, copy_function=os.link)
    for cmd, what in ((COMPILE, "be prepared"), (LOAD_CHECK, "load")):
        if not cmd:
            continue
        try:
            code, out = run(cmd, d.staging)
        except (subprocess.TimeoutExpired, OSError) as e:
            code, out = 1, str(e)
        if code != 0:
            raise Refused(f"the new version does not {what} on this card: {out[-300:] or 'no message'}")
    stamp = {"serial": serial_of(d.prog) + 1, "version": manifest["version"], "package_sha256": digest,
             "from": version_of(d.prog), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    (d.staging / STAMP).write_text(json.dumps(stamp))
    (d.staging / PENDING).write_text(manifest["version"] + "\n")
    owner = os.stat(d.prog)
    for root, dirs, names in os.walk(d.staging):
        for n in dirs + names:
            os.chown(os.path.join(root, n), owner.st_uid, owner.st_gid, follow_symlinks=False)
    os.chown(d.staging, owner.st_uid, owner.st_gid)
    os.chmod(d.staging, owner.st_mode & 0o7777)
    os.sync()
    if d.new.exists():                              # a previous version that could not be put aside, still in the way
        os.rename(d.new, Path(f"{d.trash}-new-{os.getpid()}"))
    os.rename(d.staging, d.new)
    sync_dir(d.new.parent)
    return manifest


def swap(d):
    """Step 3. After it the program is the new one and the previous version is kept for the way back."""
    try:
        exchange(d.prog, d.new)
    except OSError as e:                            # nothing has changed yet
        shutil.rmtree(d.new, ignore_errors=True)
        raise Refused(str(e))
    sync_dir(d.prog.parent)
    keep_previous(d)
    os.sync()


def empty_trash(d):
    for t in d.trash.parent.glob(d.trash.name + "*"):
        shutil.rmtree(t, ignore_errors=True)


def keep_previous(d):
    """The folder that has just left its place becomes the one kept for the way back. The one kept until now is not
    deleted where it is: it is moved aside under another name in one step, and deleted there. So the name of the way
    back always holds a whole folder, the older one or the newer one, never the rest of a deletion that stopped half
    way (a file that refuses to go, an error of the card). If even the moving fails, it is tried again at the next
    start: the update itself is done."""
    try:
        empty_trash(d)
        if d.old.exists():
            aside = Path(f"{d.trash}-{serial_of(d.prog)}-{os.getpid()}")
            os.rename(d.old, aside)
        os.rename(d.new, d.old)
        sync_dir(d.prog.parent)
    except OSError as e:
        say("the previous version could not be put aside yet:", e)
    empty_trash(d)


def apply_sysfiles(d):
    """Step 4, with the scripts of the version now in place. True when done (the mark is removed)."""
    script = d.prog / "system/sysfiles.sh"
    user = user_of(d.root, d.prog)
    if not script.is_file():
        # a version from before this script existed (reached only by going back to it): there is nothing to run, now
        # or at any later start, so the mark goes. The unit files stay those of the newer version.
        say("this version has no system/sysfiles.sh: the system files are left as they are")
        (d.prog / PENDING).unlink(missing_ok=True)
        return True
    if not user:
        say("system files not copied: the owner of the program folder is not a user of this system")
        return False
    root_arg = "" if str(d.root) == "/" else str(d.root)
    if root_arg and not ROOT_OVERRIDE:              # protected card: the settings in use are on the data partition
        os.environ.setdefault("MTSPI_SETTINGS", "/etc/default/mtspi")
    try:
        code, out = run(["bash", str(script), root_arg, "/" + PROGRAM, user], d.prog)
    except (subprocess.TimeoutExpired, OSError) as e:
        code, out = 1, str(e)
    if code != 0:
        say("system files not copied:", out[-400:])
        return False
    os.sync()                       # what the script wrote is on the card before the mark that asks for it goes
    (d.prog / PENDING).unlink(missing_ok=True)
    sync_dir(d.prog)
    os.sync()
    return True


def find(pattern_list):
    found = []
    for pattern in pattern_list:
        found += [p for p in BOOT.glob(pattern) if p.is_file()]
    # beta10 comes after beta9, 1.10 after 1.9: numbers inside the names count as numbers
    return sorted(set(found), key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", p.name)])


def on_boot(action):
    """Something done on the boot partition, which a protected card keeps read-only. False if it could not be done."""
    try:
        with Writable(BOOT):
            action()
        return True
    except OSError as e:
        say("the boot partition could not be written:", e)
        return False


def report(lines):
    text = "MTS Pi update\n" + time.strftime("%Y-%m-%d %H:%M") + " (the Pi's own clock)\n\n" + "\n".join(lines) + "\n"
    say(*lines)
    return on_boot(lambda: (BOOT / RESULT).write_text(text))


def rename_on_boot(path, suffix):
    target = path.with_name(path.name + suffix)
    return on_boot(lambda: os.replace(path, target))


def restart():
    say("restarting the Pi")
    subprocess.call(REBOOT, shell=True)
    time.sleep(REBOOT_WAIT)         # the services ordered after this one wait here instead of starting the old program


def install(d, package, others):
    digest = sha256_file(package)
    notes = [f"Other package left aside (only one is installed at a time): {p.name}" for p in others]
    if read_json(d.prog / STAMP).get("package_sha256") == digest:
        # the same package again: copied twice, or the power was cut after the program had already changed
        pending = (d.prog / PENDING).exists()
        done = apply_sysfiles(d) if pending else True
        report([f"Package: {package.name}", f"Result: ALREADY INSTALLED (version {version_of(d.prog)}).",
                "The package was renamed with .installed at the end: it can be deleted."] + notes +
               ([] if done else ["Some system files could not be copied: it is tried again at every start."]))
        rename_on_boot(package, ".installed")
        return pending and done
    before = version_of(d.prog)
    try:
        try:
            manifest = stage(d, package, digest)
        except OSError as e:                        # card full, or a file that cannot be written: nothing has changed
            raise Refused(f"the card could not be written: {e}")
        swap(d)
    except Refused as e:
        shutil.rmtree(d.staging, ignore_errors=True)
        report([f"Package: {package.name}", f"Result: NOT INSTALLED. {e}",
                f"The version on the card is still {before}. Nothing was changed.",
                "The package was renamed with .failed at the end, so that it is not tried again at every start."]
               + notes)
        rename_on_boot(package, ".failed")
        return False
    done = apply_sysfiles(d)
    report([f"Package: {package.name}", f"Result: INSTALLED. Version {before} -> {manifest['version']}.",
            "The Pi restarted once to use the new version.",
            "The package was renamed with .installed at the end: it can be deleted.",
            "To go back to the version before: put a file named mts-pi-rollback.txt next to this one "
            "and start the Pi."] + notes +
           ([] if done else ["Some system files could not be copied: it is tried again at every start."]))
    rename_on_boot(package, ".installed")
    return True


def rollback(d, request):
    ident = f"{request.name} {request.stat().st_size} {int(request.stat().st_mtime)}"
    try:
        already = (d.prog / ROLLED_BACK).read_text() == ident
    except OSError:
        already = False
    if already:
        # the power was cut after going back, before the request could be removed (or the boot partition could not
        # be written): the going back is not done twice, but what was left to do is done now
        pending = (d.prog / PENDING).exists()
        done = apply_sysfiles(d) if pending else True
        report([f"Request: {request.name}", f"Result: ALREADY DONE. The card went back to version {version_of(d.prog)} "
                "at an earlier start; the request file had stayed on the card."])
        if on_boot(request.unlink):
            (d.prog / ROLLED_BACK).unlink(missing_ok=True)
            sync_dir(d.prog)
        return pending and done
    if not d.whole(d.old):                          # nothing kept, or something that is not a whole program
        report([f"Request: {request.name}", "Result: NOTHING TO GO BACK TO. There is no previous version on the card.",
                f"The version on the card is still {version_of(d.prog)}."])
        on_boot(request.unlink)
        return False
    now, back = version_of(d.prog), version_of(d.old)
    (d.old / ROLLED_BACK).write_text(ident)
    (d.old / PENDING).write_text(back + "\n")
    os.sync()
    exchange(d.prog, d.old)                         # the version left becomes the one kept: asking again undoes this
    sync_dir(d.prog.parent)
    os.sync()
    done = apply_sysfiles(d)
    report([f"Request: {request.name}", f"Result: WENT BACK. Version {now} -> {back}.",
            "The Pi restarted once. The request file was removed."] +
           ([] if done else ["Some system files could not be copied: it is tried again at every start."]))
    if on_boot(request.unlink):
        # the request is gone for good: the note about it goes too, so that the same file, copied again another
        # day, is a new request (an empty file copied twice looks exactly the same)
        (d.prog / ROLLED_BACK).unlink(missing_ok=True)
        sync_dir(d.prog)
    return True


def main():
    packages, requests = find(PACKAGES), find([ROLLBACK])
    root = real_root()
    d = Folders(root)
    if not d.prog.is_dir():
        say(f"no program folder at {d.prog}: nothing to do")
        return 0
    leftovers = d.staging.exists() or d.new.exists() or any(d.trash.parent.glob(d.trash.name + "*"))
    pending = (d.prog / PENDING).exists()
    if not (packages or requests or pending or leftovers):
        return 0
    again = False
    try:
        with Writable(root):
            tidy(d)
            if packages:
                again = install(d, packages[-1], packages[:-1])
                for p in packages[:-1]:
                    rename_on_boot(p, ".skipped")
                for r in requests:
                    on_boot(r.unlink)
            elif requests:
                again = rollback(d, requests[-1])
            elif pending:
                again = apply_sysfiles(d)
    except Exception as e:                          # whatever happens, the other services must start
        say("stopped by an error, the program on the card is left as it is:", repr(e))
        report(["Result: STOPPED BY AN ERROR.", repr(e), f"The version on the card is {version_of(d.prog)}."])
        return 1
    if again:
        restart()
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pack":
        import argparse
        ap = argparse.ArgumentParser(prog="update.py pack")
        ap.add_argument("tree"), ap.add_argument("version"), ap.add_argument("out")
        ap.add_argument("--needs-image", type=int, default=1)
        a = ap.parse_args(sys.argv[2:])
        print(pack(a.tree, a.version, a.out, a.needs_image))
        sys.exit(0)
    sys.exit(main())
