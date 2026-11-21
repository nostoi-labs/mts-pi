"""The Pi's own pages on the dashboard: getting back from Settings with a joystick that has no Back key (found on the
bike on 21 Nov 2026), and the tyre pressure page.
python -m pytest tests, or python tests/test_pages.py"""
import json
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mtspi import bridge as br  # noqa: E402
from mtspi import dashboard, i18n, settings  # noqa: E402
from mtspi import tpms  # noqa: E402


def pages(tyres=False, state=None):
    saved = []
    b = types.SimpleNamespace(
        settings=settings.Settings(), cind={"battchg": 4, "signal": 3}, iphone=object(), dongle=object(), bike=object(),
        session=None, carplay_since=None, carplay_state=lambda: "on", audio_source=lambda: "CarPlay",
        save_settings=lambda: saved.append(1), apply_settings=lambda: None, carplay_key=lambda k: None,
        request_carplay=lambda: None, request_carplay_off=lambda: None, spawn=lambda c: None,
        tyres_enabled=lambda: tyres, tyres=lambda: state or {"listening": True, "wheels": []})
    b.launcher = dashboard.Dashboard(b)
    d = br.Display(b)
    d.mode = "launcher"
    return b, d, saved


def hold_up(d):
    for action in ("PRESS", "LONG_PRESS", "RELEASE"):       # what the bike sends for the joystick held up
        d.on_key("UP", action)


def test_settings_can_be_left_with_the_joystick_held_up():
    b, d, saved = pages()
    b.launcher.focus = "settings"
    d.on_key("OK", "CLICK")
    assert b.launcher.page == "settings"
    hold_up(d)
    assert b.launcher.page == "home" and saved == [1]       # the settings are saved on the way out


def test_settings_has_a_row_that_goes_back():
    b, d, saved = pages()
    b.launcher.page, b.launcher.set_row = "settings", 0
    rows = b.launcher.settings_rows()
    assert rows[-1][0] == "back"
    for _ in rows:
        d.on_key("DOWN", "PRESS")
        d.on_key("DOWN", "RELEASE")
    assert b.launcher.set_row == len(rows) - 1
    d.on_key("LEFT", "CLICK")                               # left and right do nothing on that row
    assert b.launcher.page == "settings"
    d.on_key("OK", "CLICK")
    assert b.launcher.page == "home" and saved == [1]


def test_short_up_and_down_still_move_one_row_and_a_hold_does_not_move_first():
    b, d, _ = pages()
    b.launcher.page, b.launcher.set_row = "settings", 2
    d.on_key("UP", "PRESS")
    assert b.launcher.set_row == 2                          # nothing until the release: it may become a hold
    d.on_key("UP", "RELEASE")
    assert b.launcher.set_row == 1
    d.on_key("DOWN", "CLICK")                               # a bench dashboard sends plain clicks
    assert b.launcher.set_row == 2


def test_diagnostics_and_tyres_go_back_with_ok_or_the_hold():
    for page in ("diag", "tyres"):
        b, d, _ = pages(tyres=True)
        b.launcher.page = page
        hold_up(d)
        assert b.launcher.page == "home"
        b.launcher.page = page
        d.on_key("OK", "CLICK")
        assert b.launcher.page == "home"


def test_tyres_button_only_when_the_listener_is_switched_on():
    b, d, _ = pages(tyres=False)
    assert "tyres" not in b.launcher.focusables()
    b, d, _ = pages(tyres=True)
    assert b.launcher.focusables() == ["carplay_main", "carplay_off", "tyres", "diag", "settings"]
    b.launcher.focus = "tyres"
    d.on_key("OK", "CLICK")
    assert b.launcher.page == "tyres"


def test_every_page_draws_in_both_languages():
    wheels = [{"wheel": "front", "id": "00:11:22", "bar": 2.49, "temp_c": 17, "volt": 2.91, "age_s": 32.0, "recommended": [2.4, 2.4]},
              {"wheel": "rear", "id": "00:33:44", "bar": 2.05, "temp_c": 24, "volt": 2.98, "age_s": 700.0, "recommended": [2.5, 2.9]},
              {"wheel": None, "id": "AA:BB:CC", "bar": None, "temp_c": None, "volt": None, "age_s": 5.0, "recommended": None}]
    try:
        for lang in i18n.LANGS:
            i18n.set_lang(lang)
            for state in ({"listening": True, "wheels": wheels}, {"listening": True, "wheels": []}, {"listening": False, "wheels": []}):
                b, d, _ = pages(tyres=True, state=state)
                for page in ("home", "tyres", "settings", "diag"):
                    if page == "diag":
                        b.diag_info = lambda: {"uptime": "1 min", "myspin": [], "links": [], "system": []}
                    b.launcher.page = page
                    img = b.launcher.render(848, 304)
                    assert img.size == (848, 304)
    finally:
        i18n.set_lang("en")


def test_bridge_reads_what_the_listener_heard():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        now = time.monotonic()
        json.dump({"alive_up": now, "sensors": {
            "AA:BB:CC:00:33:44": {"wheel": "rear", "up": now - 75, "bar": 2.62, "temp_c": 24, "volt": 2.98},
            "AA:BB:CC:00:11:22": {"wheel": "front", "up": now - 30, "bar": 2.49, "temp_c": 17, "volt": 2.91}}}, open(path, "w"))
        b = br.Bridge.__new__(br.Bridge)
        b.TYRES_STATE = path
        old = dict(os.environ)
        os.environ.update(TPMS="1", TPMS_FRONT_BAR="2.4/2.4", TPMS_REAR_BAR="2,5/2,9")
        try:
            assert b.tyres_enabled()
            info = b.tyres()
        finally:
            os.environ.clear()
            os.environ.update(old)
        assert info["listening"] and [w["wheel"] for w in info["wheels"]] == ["front", "rear"]
        front, rear = info["wheels"]
        assert front["id"] == "00:11:22" and front["bar"] == 2.49 and 29 <= front["age_s"] <= 32
        assert front["recommended"] == [2.4, 2.4] and rear["recommended"] == [2.5, 2.9]
        info = b.tyres()                                    # no setting: the Multistrada's own recommended pressures
        assert [w["recommended"] for w in info["wheels"]] == [[2.4, 2.4], [2.5, 2.9]]
        b.TYRES_STATE = os.path.join(tmp, "missing.json")
        assert b.tyres() == {"listening": False, "wheels": []}
        json.dump({"alive_up": now - 400, "sensors": {}}, open(path, "w"))      # the listener stopped long ago
        b.TYRES_STATE = path
        assert b.tyres()["listening"] is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
