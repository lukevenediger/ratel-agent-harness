"""ratel-tui entry point: argument and environment resolution, the no-channels exit."""
from pathlib import Path

import pytest

from ratel.bus import Bus
from ratel.tui import main as tui_main


def test_flags_win_over_environment(tmp_path):
    env = {"RATEL_HOME": "/env/home", "CHANNEL": "envch"}
    opts = tui_main.resolve(["--home", str(tmp_path), "--channel", "flagch", "--no-persist-colours"], env)
    assert opts.home == tmp_path.resolve()
    assert opts.channel == "flagch"
    assert opts.persist_colours is False


def test_environment_fills_missing_flags(tmp_path):
    opts = tui_main.resolve([], {"RATEL_HOME": str(tmp_path), "CHANNEL": "envch"})
    assert opts.home == tmp_path.resolve()
    assert opts.channel == "envch"
    assert opts.persist_colours is True


def test_channel_defaults_to_none_so_the_app_picks_the_newest(tmp_path):
    opts = tui_main.resolve(["--home", str(tmp_path)], {})
    assert opts.channel is None


def test_no_channels_exits_with_a_demo_hint(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        tui_main.main(["--home", str(tmp_path)])
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "no channels" in err and "ratel demo --home" in err


def test_unknown_channel_exits_naming_the_known_ones(tmp_path, capsys):
    Bus(tmp_path, "alpha").post("a", "hi")
    with pytest.raises(SystemExit) as e:
        tui_main.main(["--home", str(tmp_path), "--channel", "nope"])
    assert e.value.code == 1
    assert "alpha" in capsys.readouterr().err


def test_main_builds_the_app_from_the_resolved_options(demo_home, monkeypatch):
    built = {}

    class FakeApp:
        def __init__(self, home, channel=None, persist_colours=True):
            built.update(home=home, channel=channel, persist_colours=persist_colours)

        def run(self):
            built["ran"] = True

    monkeypatch.setattr(tui_main, "RatelTui", FakeApp)
    monkeypatch.delenv("CHANNEL", raising=False)   # a clan role's shell exports its own channel
    assert tui_main.main(["--no-persist-colours"]) == 0
    assert built == {"home": Path(demo_home).resolve(), "channel": None, "persist_colours": False, "ran": True}


def test_module_is_runnable():
    import ratel.tui.__main__  # noqa: F401
