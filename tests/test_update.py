"""The update from a file on the boot partition (system/update.py), on a stand-in system in a temporary folder:
what ends up on the card after a good package, a bad one, a power cut at each step, and the way back."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class Patch:
    """Environment variables and attributes changed for one test, put back after it."""

    def __init__(self):
        self.undo = []

    def setenv(self, name, value):
        old = os.environ.get(name)
        self.undo.append(lambda: os.environ.pop(name, None) if old is None else os.environ.__setitem__(name, old))
        os.environ[name] = value

    def setattr(self, obj, name, value):
        old = getattr(obj, name)
        self.undo.append(lambda: setattr(obj, name, old))
        setattr(obj, name, value)

    def close(self):
        for f in reversed(self.undo):
            f()


def card(fn):
    """Each test gets an empty folder of its own (the card) and its own changes to the environment."""
    def test():
        patch = Patch()
        try:
            with tempfile.TemporaryDirectory() as t:
                args = (Path(t).resolve(), patch)
                fn(*args[:fn.__code__.co_argcount])
        finally:
            patch.close()
    test.__name__ = fn.__name__
    return test


@contextlib.contextmanager
def raises(kind):
    try:
        yield
    except kind:
        return
    raise AssertionError(f"{kind.__name__} was expected")


SYSFILES_STUB = '#!/bin/bash\necho "$2 $3" >> "$1/sysfiles-ran"\n[ ! -e "$1/fail-sysfiles" ]\n'


def load(tmp, monkeypatch, **env):
    """The updater as it would start on a card whose system is tmp/root and whose boot partition is tmp/boot."""
    settings = {"MTSPI_ROOT": tmp / "root", "MTSPI_BOOT": tmp / "boot", "MTSPI_PROC_MOUNTS": tmp / "mounts",
                "MTSPI_REBOOT": f"touch '{tmp}/rebooted'", "MTSPI_REBOOT_WAIT": "0",
                "MTSPI_LOAD_CHECK": "true", "MTSPI_COMPILE": ""}
    settings.update(env)
    for k, v in settings.items():
        monkeypatch.setenv(k, str(v))
    spec = importlib.util.spec_from_file_location("mtspi_update", REPO / "system/update.py")
    u = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(u)
    return u


def system(tmp, version="beta8", image=1):
    """A card with the program installed: tmp/root/opt/mts-pi, tmp/boot."""
    prog = tmp / "root/opt/mts-pi"
    for d in (prog / "mtspi", prog / "system", prog / ".venv/bin", tmp / "root/etc", tmp / "boot"):
        d.mkdir(parents=True, exist_ok=True)
    (prog / "mtspi/bridge.py").write_text("WHICH = 'old'\n")
    (prog / "mtspi/gone.py").write_text("# a file the new version no longer has\n")
    (prog / "system/sysfiles.sh").write_text(SYSFILES_STUB)
    (prog / "VERSION").write_text(version + "\n")
    (prog / ".venv/bin/python").write_text("#!/bin/sh\n")
    (tmp / "root/etc/mtspi-image").write_text(f"{image}\n")
    (tmp / "root/etc/passwd").write_text(f"rider:x:{os.getuid()}:{os.getgid()}::/home/rider:/bin/sh\n")
    (tmp / "mounts").write_text("")
    return prog


def package(u, tmp, version="beta9", name=None, needs_image=1, sysfiles=SYSFILES_STUB):
    """A package made the way a release makes it, from a small stand-in repository."""
    tree = tmp / f"tree-{version}"
    (tree / "mtspi").mkdir(parents=True, exist_ok=True)
    (tree / "system").mkdir(exist_ok=True)
    (tree / "mtspi/bridge.py").write_text(f"WHICH = '{version}'\n")
    (tree / "system/update.py").write_text((REPO / "system/update.py").read_text())
    (tree / "system/sysfiles.sh").write_text(sysfiles)
    os.chmod(tree / "system/sysfiles.sh", 0o755)
    out = tmp / "boot" / (name or f"mts-pi-update-{version}.tar.gz")
    u.pack(tree, version, out, needs_image)
    return out


def tar_with(path, members, manifest=None):
    """A hand-made package: members is name -> bytes, or name -> a TarInfo for the odd ones."""
    files = {n: hashlib.sha256(b).hexdigest() for n, b in members.items() if isinstance(b, bytes)}
    if manifest is None:
        manifest = {"name": "mts-pi", "version": "beta9", "needs_image": 1, "files": files}
    with tarfile.open(path, "w:gz") as tar:
        if manifest is not False:
            data = json.dumps(manifest).encode()
            info = tarfile.TarInfo("UPDATE.json"); info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for n, b in members.items():
            if isinstance(b, bytes):
                info = tarfile.TarInfo(n); info.size = len(b)
                tar.addfile(info, io.BytesIO(b))
            else:
                tar.addfile(b)
    return path


def result(tmp):
    return (tmp / "boot/mts-pi-update-result.txt").read_text()


# ------------------------------------------------------------ the package

@card
def test_a_package_unpacks_to_exactly_the_files_it_lists(tmp_path, monkeypatch):
    system(tmp_path)
    u = load(tmp_path, monkeypatch)
    pkg = package(u, tmp_path)
    dest = tmp_path / "out"; dest.mkdir()
    manifest = u.unpack(pkg, dest)
    assert manifest["version"] == "beta9" and manifest["needs_image"] == 1
    assert (dest / "VERSION").read_text() == "beta9\n"
    assert (dest / "mtspi/bridge.py").read_text() == "WHICH = 'beta9'\n"
    assert os.access(dest / "system/sysfiles.sh", os.X_OK)          # scripts stay runnable
    assert not (dest / "UPDATE.json").exists()                      # the list itself is not part of the program


@card
def test_a_package_carries_the_program_and_not_what_only_a_computer_needs(tmp_path, monkeypatch):
    system(tmp_path)
    u = load(tmp_path, monkeypatch)
    tree = tmp_path / "tree-beta9"
    for extra in ("docs/photos/big.jpg", "tests/test_x.py", "image/build.sh", "installer/app.py", ".git/config",
                  "mtspi/__pycache__/bridge.cpython-313.pyc", "installer/dist/app", "mtspi/docs/kept.txt", "README.md"):
        (tree / extra).parent.mkdir(parents=True, exist_ok=True)
        (tree / extra).write_text("x")
    pkg = package(u, tmp_path)
    dest = tmp_path / "out"; dest.mkdir()
    u.unpack(pkg, dest)
    found = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())
    assert found == ["README.md", "VERSION", "mtspi/bridge.py", "mtspi/docs/kept.txt", "system/sysfiles.sh", "system/update.py"]


@card
def test_a_package_that_writes_outside_its_folder_is_refused(tmp_path, monkeypatch):
    system(tmp_path)
    u = load(tmp_path, monkeypatch)
    for bad in ("../outside.py", "/etc/passwd", "mtspi/../../outside.py"):
        dest = tmp_path / "out"; dest.mkdir(exist_ok=True)
        pkg = tar_with(tmp_path / "bad.tar.gz", {bad: b"x"})
        with raises(u.Refused):
            u.unpack(pkg, dest)
        assert not (tmp_path / "outside.py").exists()


@card
def test_a_package_with_a_link_is_refused(tmp_path, monkeypatch):
    system(tmp_path)
    u = load(tmp_path, monkeypatch)
    link = tarfile.TarInfo("mtspi/bridge.py"); link.type = tarfile.SYMTYPE; link.linkname = "/etc/shadow"
    dest = tmp_path / "out"; dest.mkdir()
    with raises(u.Refused):
        u.unpack(tar_with(tmp_path / "bad.tar.gz", {"a.py": b"x", "link": link}), dest)


@card
def test_a_file_changed_added_or_missing_is_refused(tmp_path, monkeypatch):
    system(tmp_path)
    u = load(tmp_path, monkeypatch)
    good = hashlib.sha256(b"good").hexdigest()
    cases = {
        "changed": ({"a.py": b"tampered"}, {"a.py": good}),
        "added": ({"a.py": b"good", "extra.py": b"x"}, {"a.py": good}),
        "missing": ({"a.py": b"good"}, {"a.py": good, "b.py": good}),
    }
    for what, (members, listed) in cases.items():
        dest = tmp_path / f"out-{what}"; dest.mkdir()
        pkg = tar_with(tmp_path / "bad.tar.gz", members,
                       {"name": "mts-pi", "version": "beta9", "needs_image": 1, "files": listed})
        with raises(u.Refused):
            u.unpack(pkg, dest)


@card
def test_a_file_that_is_not_an_update_is_refused(tmp_path, monkeypatch):
    system(tmp_path)
    u = load(tmp_path, monkeypatch)
    cut = tmp_path / "cut.tar.gz"
    whole = package(u, tmp_path).read_bytes()
    cut.write_bytes(whole[: len(whole) // 2])                       # copied only in part
    other = tar_with(tmp_path / "other.tar.gz", {"a.py": b"x"}, manifest=False)
    wrong = tar_with(tmp_path / "wrong.tar.gz", {"a.py": b"x"},
                     {"name": "something-else", "version": "1", "files": {"a.py": hashlib.sha256(b"x").hexdigest()}})
    for i, pkg in enumerate((cut, other, wrong)):
        dest = tmp_path / f"o{i}"; dest.mkdir()
        with raises(u.Refused):
            u.unpack(pkg, dest)


# ------------------------------------------------------------ a good update

@card
def test_a_good_package_becomes_the_program_and_the_old_one_is_kept(tmp_path, monkeypatch):
    prog = system(tmp_path)
    venv_file = (prog / ".venv/bin/python").stat().st_ino
    u = load(tmp_path, monkeypatch)
    pkg = package(u, tmp_path)
    assert u.main() == 0
    assert (prog / "VERSION").read_text() == "beta9\n"
    assert (prog / "mtspi/bridge.py").read_text() == "WHICH = 'beta9'\n"
    assert not (prog / "mtspi/gone.py").exists()                    # nothing of the old version is left mixed in
    assert (prog / ".venv/bin/python").stat().st_ino == venv_file   # the Python environment is carried over
    old = Path(str(prog) + ".old")
    assert (old / "VERSION").read_text() == "beta8\n" and (old / "mtspi/gone.py").exists()
    assert not Path(str(prog) + ".new").exists() and not Path(str(prog) + ".staging").exists()
    stamp = json.loads((prog / ".update").read_text())
    assert stamp["version"] == "beta9" and stamp["from"] == "beta8" and stamp["serial"] == 1
    assert (tmp_path / "root/sysfiles-ran").read_text() == "/opt/mts-pi rider\n"    # the new version's own script
    assert not (prog / ".apply-pending").exists()
    assert "INSTALLED" in result(tmp_path) and "beta8 -> beta9" in result(tmp_path)
    assert not pkg.exists() and (tmp_path / "boot" / (pkg.name + ".installed")).exists()
    assert (tmp_path / "rebooted").exists()


@card
def test_nothing_to_do_means_nothing_happens(tmp_path, monkeypatch):
    system(tmp_path)
    u = load(tmp_path, monkeypatch)
    assert u.main() == 0
    assert not (tmp_path / "rebooted").exists() and not (tmp_path / "boot/mts-pi-update-result.txt").exists()


@card
def test_the_same_package_copied_again_is_not_installed_twice(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    pkg = package(u, tmp_path)
    data = pkg.read_bytes()
    u.main()
    (tmp_path / "rebooted").unlink()
    pkg.write_bytes(data)
    u = load(tmp_path, monkeypatch)
    assert u.main() == 0
    assert "ALREADY INSTALLED" in result(tmp_path)
    assert json.loads((prog / ".update").read_text())["serial"] == 1
    assert (Path(str(prog) + ".old") / "VERSION").read_text() == "beta8\n"       # the way back is still the old one
    assert not pkg.exists() and not (tmp_path / "rebooted").exists()


@card
def test_of_two_packages_one_is_installed_and_the_other_put_aside(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    older = package(u, tmp_path, "beta9")
    package(u, tmp_path, "beta9b")
    u.main()
    assert (prog / "VERSION").read_text() == "beta9b\n"
    assert (tmp_path / "boot" / (older.name + ".skipped")).exists()
    u = load(tmp_path, monkeypatch)
    (tmp_path / "rebooted").unlink()
    u.main()                                                        # the next start does not go back to the other one
    assert (prog / "VERSION").read_text() == "beta9b\n" and not (tmp_path / "rebooted").exists()


# ------------------------------------------------------------ a package that must not go in

@card
def test_a_version_that_does_not_load_leaves_everything_as_it_was(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch, MTSPI_LOAD_CHECK="echo ImportError: no module named x; false")
    pkg = package(u, tmp_path)
    assert u.main() == 0
    assert (prog / "VERSION").read_text() == "beta8\n" and (prog / "mtspi/gone.py").exists()
    assert not Path(str(prog) + ".old").exists() and not Path(str(prog) + ".staging").exists()
    text = result(tmp_path)
    assert "NOT INSTALLED" in text and "does not load" in text and "ImportError" in text and "still beta8" in text
    assert (tmp_path / "boot" / (pkg.name + ".failed")).exists() and not pkg.exists()    # not tried at every start
    assert not (tmp_path / "rebooted").exists()


@card
def test_a_version_that_needs_a_newer_card_image_is_refused(tmp_path, monkeypatch):
    prog = system(tmp_path, image=1)
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path, needs_image=2)
    u.main()
    assert (prog / "VERSION").read_text() == "beta8\n"
    assert "needs a newer card image" in result(tmp_path) and not (tmp_path / "rebooted").exists()


@card
def test_a_damaged_package_is_refused_and_put_aside(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    pkg = package(u, tmp_path)
    pkg.write_bytes(pkg.read_bytes()[:600])
    u.main()
    assert (prog / "VERSION").read_text() == "beta8\n" and "NOT INSTALLED" in result(tmp_path)
    assert (tmp_path / "boot" / (pkg.name + ".failed")).exists()


# ------------------------------------------------------------ the power goes at each step

class PowerCut(BaseException):
    """Not an Exception on purpose: nothing in the updater may catch it and tidy up, as with a real cut."""


def cut_at(u, monkeypatch, name):
    def gone(*a, **k):
        raise PowerCut(name)
    monkeypatch.setattr(u, name, gone)


def start_again(tmp, monkeypatch):
    u = load(tmp, monkeypatch)
    assert u.main() == 0
    return u


def assert_new_version_whole(tmp, prog):
    assert (prog / "VERSION").read_text() == "beta9\n" and not (prog / "mtspi/gone.py").exists()
    assert (Path(str(prog) + ".old") / "VERSION").read_text() == "beta8\n"
    assert not Path(str(prog) + ".new").exists() and not Path(str(prog) + ".staging").exists()
    assert not (prog / ".apply-pending").exists() and (tmp / "root/sysfiles-ran").exists()
    assert not list((tmp / "boot").glob("mts-pi-update*.tar.gz"))


@card
def test_cut_while_the_new_folder_is_being_filled(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path)
    cut_at(u, monkeypatch, "run")                                   # the load check never comes back
    with raises(PowerCut):
        u.main()
    assert (prog / "VERSION").read_text() == "beta8\n" and Path(str(prog) + ".staging").exists()
    start_again(tmp_path, monkeypatch)
    assert_new_version_whole(tmp_path, prog)


@card
def test_cut_when_the_new_folder_is_ready_but_not_yet_in_place(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path)
    cut_at(u, monkeypatch, "exchange")
    with raises(PowerCut):
        u.main()
    assert (prog / "VERSION").read_text() == "beta8\n" and Path(str(prog) + ".new").exists()
    start_again(tmp_path, monkeypatch)
    assert_new_version_whole(tmp_path, prog)
    assert json.loads((prog / ".update").read_text())["serial"] == 1


@card
def test_cut_right_after_the_two_folders_changed_places(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    pkg = package(u, tmp_path)
    d = u.Folders(tmp_path / "root")
    u.stage(d, pkg, u.sha256_file(pkg))
    u.exchange(d.prog, d.new)                                       # and nothing after it
    assert (prog / "VERSION").read_text() == "beta9\n" and (d.new / "VERSION").read_text() == "beta8\n"
    start_again(tmp_path, monkeypatch)
    assert_new_version_whole(tmp_path, prog)
    assert "ALREADY INSTALLED" in result(tmp_path) and (tmp_path / "rebooted").exists()


@card
def test_cut_before_the_system_files_were_copied(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path)
    cut_at(u, monkeypatch, "apply_sysfiles")
    with raises(PowerCut):
        u.main()
    assert (prog / "VERSION").read_text() == "beta9\n" and (prog / ".apply-pending").exists()
    start_again(tmp_path, monkeypatch)
    assert_new_version_whole(tmp_path, prog)
    assert (tmp_path / "rebooted").exists()


@card
def test_cut_after_everything_but_the_renaming_of_the_package(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path)
    cut_at(u, monkeypatch, "report")
    with raises(PowerCut):
        u.main()
    start_again(tmp_path, monkeypatch)
    assert_new_version_whole(tmp_path, prog)
    assert not (tmp_path / "rebooted").exists()                     # already the new version: no second restart


@card
def test_system_files_that_cannot_be_copied_are_tried_again_without_restarting_forever(tmp_path, monkeypatch):
    prog = system(tmp_path)
    (tmp_path / "root/fail-sysfiles").write_text("")
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path)
    u.main()
    assert (prog / "VERSION").read_text() == "beta9\n" and (prog / ".apply-pending").exists()
    assert "tried again at every start" in result(tmp_path) and (tmp_path / "rebooted").exists()
    (tmp_path / "rebooted").unlink()
    start_again(tmp_path, monkeypatch)                              # still failing: no restart, the Pi goes on
    assert (prog / ".apply-pending").exists() and not (tmp_path / "rebooted").exists()
    (tmp_path / "root/fail-sysfiles").unlink()
    start_again(tmp_path, monkeypatch)                              # now it works: done, one restart
    assert not (prog / ".apply-pending").exists() and (tmp_path / "rebooted").exists()


# ------------------------------------------------------------ the way back

@card
def test_the_rollback_file_brings_the_previous_version_back(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path)
    u.main()
    (tmp_path / "rebooted").unlink()
    request = tmp_path / "boot/mts-pi-rollback.txt"
    request.write_text("")
    start_again(tmp_path, monkeypatch)
    assert (prog / "VERSION").read_text() == "beta8\n" and (prog / "mtspi/gone.py").exists()
    assert (Path(str(prog) + ".old") / "VERSION").read_text() == "beta9\n"
    assert "WENT BACK" in result(tmp_path) and "beta9 -> beta8" in result(tmp_path)
    assert not request.exists() and (tmp_path / "rebooted").exists() and not (prog / ".apply-pending").exists()
    assert (tmp_path / "root/sysfiles-ran").read_text().count("\n") == 2          # the old version's files again


@card
def test_a_rollback_whose_file_could_not_be_removed_is_not_done_twice(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path)
    u.main()
    request = tmp_path / "boot/mts-pi-rollback.txt"
    request.write_text("")
    u = load(tmp_path, monkeypatch)
    cut_at(u, monkeypatch, "report")                                # went back, then the power goes
    with raises(PowerCut):
        u.main()
    assert (prog / "VERSION").read_text() == "beta8\n" and request.exists()
    (tmp_path / "rebooted").unlink()
    start_again(tmp_path, monkeypatch)
    assert (prog / "VERSION").read_text() == "beta8\n"              # not swapped forward again
    assert not request.exists() and not (tmp_path / "rebooted").exists()


@card
def test_going_back_to_a_version_without_the_system_files_script_does_not_leave_a_step_pending(tmp_path, monkeypatch):
    prog = system(tmp_path)
    (prog / "system/sysfiles.sh").unlink()                         # the program as it was before the script existed
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path)
    u.main()
    (tmp_path / "rebooted").unlink()
    (tmp_path / "boot/mts-pi-rollback.txt").write_text("")
    start_again(tmp_path, monkeypatch)
    assert (prog / "VERSION").read_text() == "beta8\n" and not (prog / ".apply-pending").exists()
    assert "WENT BACK" in result(tmp_path) and "tried again" not in result(tmp_path) and (tmp_path / "rebooted").exists()
    (tmp_path / "rebooted").unlink()
    start_again(tmp_path, monkeypatch)                              # and the next start has nothing left to do
    assert not (tmp_path / "rebooted").exists()


@card
def test_cut_during_a_rollback_before_the_system_files_were_copied(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path)
    u.main()
    request = tmp_path / "boot/mts-pi-rollback.txt"
    request.write_text("")
    u = load(tmp_path, monkeypatch)
    cut_at(u, monkeypatch, "apply_sysfiles")                       # the folders have changed places, then the power goes
    with raises(PowerCut):
        u.main()
    assert (prog / "VERSION").read_text() == "beta8\n" and (prog / ".apply-pending").exists() and request.exists()
    (tmp_path / "rebooted").unlink()
    (tmp_path / "root/sysfiles-ran").unlink()
    start_again(tmp_path, monkeypatch)
    assert (prog / "VERSION").read_text() == "beta8\n"            # not swapped forward again ...
    assert not (prog / ".apply-pending").exists() and (tmp_path / "root/sysfiles-ran").exists()   # ... and the step is done
    assert not request.exists() and (tmp_path / "rebooted").exists()


@card
def test_the_same_rollback_file_copied_again_another_day_is_a_new_request(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path)
    u.main()
    request = tmp_path / "boot/mts-pi-rollback.txt"
    for expected in ("beta8\n", "beta9\n", "beta8\n"):           # back, forward again, back again: each asked with the same empty file
        request.write_text("")
        os.utime(request, (1_795_000_000, 1_795_000_000))           # the very same file every time, date included
        start_again(tmp_path, monkeypatch)
        assert (prog / "VERSION").read_text() == expected and not request.exists()


@card
def test_of_two_packages_the_higher_number_wins_not_the_alphabet(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path, "beta9")
    package(u, tmp_path, "beta10")
    u.main()
    assert (prog / "VERSION").read_text() == "beta10\n"


@card
def test_a_package_is_not_made_from_a_working_folder(tmp_path, monkeypatch):
    system(tmp_path)
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path)                                            # makes tmp/tree-beta9, a clean tree
    tree = tmp_path / "tree-beta9"
    (tree / ".hidden-tools").mkdir(); (tree / ".hidden-tools/big").write_text("x")
    out = tmp_path / "p.tar.gz"
    u.pack(tree, "beta9", out)
    dest = tmp_path / "o"; dest.mkdir()
    u.unpack(out, dest)
    assert not (dest / ".hidden-tools").exists()                    # folders whose name starts with a dot never go in
    (tree / "myspin").mkdir(); (tree / "myspin/__init__.py").write_text(""); (tree / "myspin/phone.py").write_text("SOURCE = 1\n")
    with raises(SystemExit):
        u.pack(tree, "beta9", out)                                  # sources of the part published as binaries: a working folder


@card
def test_a_previous_version_that_cannot_be_deleted_never_becomes_a_broken_way_back(tmp_path, monkeypatch):
    """Something in the folder kept for the way back refuses to go (a file locked, an error of the card). The folder
    is moved aside whole, so the way back is the right version or nothing, never the rest of a deletion."""
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path)
    u.main()                                                        # beta8 -> beta9: .old is beta8
    old = Path(str(prog) + ".old")
    real = u.shutil.rmtree

    def stubborn(path, ignore_errors=False, **k):                   # deletes everything but one file, and says nothing
        path = Path(path)
        if not path.exists():
            return
        for p in sorted(path.rglob("*"), reverse=True):
            if p.name == "bridge.py" and "trash" in str(path):
                continue
            try:
                p.unlink() if p.is_file() or p.is_symlink() else p.rmdir()
            except OSError:
                pass
    u = load(tmp_path, monkeypatch)
    monkeypatch.setattr(u.shutil, "rmtree", stubborn)
    package(u, tmp_path, "beta10")
    u.main()                                                        # beta9 -> beta10, with the stubborn file
    monkeypatch.setattr(u.shutil, "rmtree", real)
    assert (prog / "VERSION").read_text() == "beta10\n"
    assert (old / "VERSION").read_text() == "beta9\n" and (old / "mtspi/bridge.py").read_text() == "WHICH = 'beta9'\n"
    assert not Path(str(prog) + ".new").exists()
    (tmp_path / "boot/mts-pi-rollback.txt").write_text("")
    start_again(tmp_path, monkeypatch)                              # the way back is the whole beta9
    assert (prog / "VERSION").read_text() == "beta9\n" and (prog / "mtspi/bridge.py").exists()
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path, "beta11")                                  # and updates go on afterwards, leftovers cleared
    u.main()
    assert (prog / "VERSION").read_text() == "beta11\n"
    assert not list(prog.parent.glob("mts-pi.trash*"))


@card
def test_something_that_is_not_a_whole_program_is_never_swapped_in_as_the_previous_version(tmp_path, monkeypatch):
    prog = system(tmp_path)
    old = Path(str(prog) + ".old")
    (old / "system").mkdir(parents=True)
    (old / "system/stuck").write_text("the rest of a deletion")
    request = tmp_path / "boot/mts-pi-rollback.txt"
    request.write_text("")
    u = load(tmp_path, monkeypatch)
    u.main()
    assert (prog / "VERSION").read_text() == "beta8\n" and (prog / "mtspi/bridge.py").exists()
    assert "NOTHING TO GO BACK TO" in result(tmp_path) and not (tmp_path / "rebooted").exists()


@card
def test_a_folder_left_in_the_way_does_not_stop_later_updates(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path)
    u.main()
    stuck = Path(str(prog) + ".new")                                # a previous version that could not be put aside
    (stuck / "mtspi").mkdir(parents=True)
    (stuck / "VERSION").write_text("beta7\n")
    u = load(tmp_path, monkeypatch)
    monkeypatch.setattr(u, "keep_previous", lambda d: None)         # ... and still cannot, at this start
    package(u, tmp_path, "beta10")
    u.main()
    assert (prog / "VERSION").read_text() == "beta10\n" and "INSTALLED" in result(tmp_path)


@card
def test_a_rollback_with_no_previous_version_changes_nothing(tmp_path, monkeypatch):
    prog = system(tmp_path)
    request = tmp_path / "boot/mts-pi-rollback.txt"
    request.write_text("")
    u = load(tmp_path, monkeypatch)
    u.main()
    assert (prog / "VERSION").read_text() == "beta8\n" and "NOTHING TO GO BACK TO" in result(tmp_path)
    assert not request.exists() and not (tmp_path / "rebooted").exists()


@card
def test_an_update_after_a_rollback_installs_normally(tmp_path, monkeypatch):
    prog = system(tmp_path)
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path)
    u.main()
    (tmp_path / "boot/mts-pi-rollback.txt").write_text("")
    start_again(tmp_path, monkeypatch)
    u = load(tmp_path, monkeypatch)
    package(u, tmp_path, "beta10")
    u.main()
    assert (prog / "VERSION").read_text() == "beta10\n"
    assert (Path(str(prog) + ".old") / "VERSION").read_text() == "beta8\n"


# ------------------------------------------------------------ the pieces around it

@card
def test_two_folders_change_places_in_one_step(tmp_path, monkeypatch):
    system(tmp_path)
    u = load(tmp_path, monkeypatch)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir(); (a / "is-a").write_text(""); (b / "is-b").write_text("")
    u.exchange(a, b)
    assert (a / "is-b").exists() and (b / "is-a").exists()
    with raises(OSError):
        u.exchange(a, tmp_path / "not-there")


@card
def test_a_read_only_boot_partition_is_opened_for_the_result_and_closed_again(tmp_path, monkeypatch):
    prog = system(tmp_path)
    calls = tmp_path / "mount-calls"
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/mount").write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n')
    os.chmod(tmp_path / "bin/mount", 0o755)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    u = load(tmp_path, monkeypatch)
    (tmp_path / "mounts").write_text(f"/dev/mmcblk0p1 {tmp_path / 'boot'} vfat ro,relatime 0 0\n")
    package(u, tmp_path)
    u.main()
    lines = calls.read_text().splitlines()
    assert lines and all(str(tmp_path / "boot") in line for line in lines)
    assert lines[0].startswith("-o remount,rw") and lines[-1].startswith("-o remount,ro")
    assert [l.split()[1] for l in lines] == ["remount,rw", "remount,ro"] * (len(lines) // 2)
    assert (prog / "VERSION").read_text() == "beta9\n"


@card
def test_the_real_system_files_script_writes_units_and_switches_services_on(tmp_path):
    root = tmp_path / "root"
    (root / "etc/default").mkdir(parents=True)
    (root / "etc/passwd").write_text(f"rider:x:{os.getuid()}:{os.getgid()}::/home/rider:/bin/sh\n")
    (root / "etc/default/mtspi").write_text("CARLINKIT=usb\n")
    (tmp_path / "bin").mkdir()
    calls = tmp_path / "systemctl-calls"
    (tmp_path / "bin/systemctl").write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n')
    os.chmod(tmp_path / "bin/systemctl", 0o755)
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    run = lambda: subprocess.run(["bash", str(REPO / "system/sysfiles.sh"), str(root), "/opt/mts-pi", "rider"],
                                 env=env, capture_output=True, text=True)
    r = run()
    assert r.returncode == 0, r.stdout + r.stderr
    units = root / "etc/systemd/system"
    for source in (REPO / "system/units").iterdir():
        text = (units / source.name).read_text()
        assert "@REPO@" not in text and "@USER@" not in text and "@UID@" not in text
    assert "ExecStart=/usr/bin/python3 /opt/mts-pi/system/update.py" in (units / "mtspi-update.service").read_text()
    assert f"/run/user/{os.getuid()}" in (units / "mtspi-bridge.service").read_text()
    assert not list(units.glob("*.new"))
    assert (root / "etc/udev/rules.d/60-mtspi-carlinkit.rules").exists()
    assert "rider" in (root / "etc/polkit-1/rules.d/50-mtspi-nm.rules").read_text()
    wp = root / "home/rider/.config/wireplumber/wireplumber.conf.d"
    assert (wp / "51-mtspi-bluez.conf").exists() and (wp / "52-mtspi-carlinkit.conf").exists()
    first = calls.read_text()
    assert all(line.startswith(f"--root={root} ") for line in first.splitlines())
    assert "enable ofono mtspi-setup" in first and "mtspi-bridge" in first
    assert "mtspi-update" not in first and "mtspi-protect" not in first          # not a card image: no mark
    # a card image has the mark: its own services too; and without the Carlinkit the Pi may be an audio output
    (root / "etc/mtspi-image").write_text("1\n")
    (root / "etc/default/mtspi").write_text("CARLINKIT=none\n")
    calls.unlink()
    assert run().returncode == 0
    assert "mtspi-protect mtspi-update" in calls.read_text()
    assert not (wp / "52-mtspi-carlinkit.conf").exists()


@card
def test_the_script_refuses_a_user_the_system_does_not_have(tmp_path):
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/passwd").write_text("someone:x:1000:1000::/home/someone:/bin/sh\n")
    r = subprocess.run(["bash", str(REPO / "system/sysfiles.sh"), str(root), "/opt/mts-pi", "rider"],
                       capture_output=True, text=True)
    assert r.returncode != 0 and not (root / "etc/systemd").exists()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
