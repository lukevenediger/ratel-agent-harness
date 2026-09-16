"""BoardReader: read-only reads over Bus, and the list_channels move to ratel.paths."""
from importlib.metadata import entry_points

from ratel import board, paths
from ratel.bus import Bus


def test_list_channels_lives_in_paths_and_board_reuses_it(tmp_path):
    Bus(tmp_path, "alpha").post("a", "hi")
    (tmp_path / "channels" / "junk").mkdir()
    assert paths.list_channels(tmp_path) == ["alpha"]
    assert board.list_channels is paths.list_channels


def test_ratel_tui_console_script_is_declared():
    scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
    assert scripts.get("ratel-tui") == "ratel.tui.main:main"
