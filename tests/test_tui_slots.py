"""slots: eight identity colours in fixed order, first-seen per channel, persisted."""
from ratel.tui import slots
from ratel.tui.slots import PALETTE, SlotMap


def test_palette_matches_design_order():
    assert PALETTE == ("#82A7FF", "#FF7A76", "#3FD9C4", "#FFD43B", "#C0A6FF", "#6FDD8B", "#FFA94D", "#FF7ABF")


def test_first_seen_order_per_channel_and_wraparound(tmp_path):
    m = SlotMap(tmp_path, persist=False)
    assert m.colour("ch", "orch") == PALETTE[0]
    assert m.colour("ch", "dev") == PALETTE[1]
    assert m.colour("ch", "orch") == PALETTE[0]
    assert m.colour("other", "dev") == PALETTE[0]
    for i in range(2, 9):
        m.colour("ch", f"agent{i}")
    assert m.slot("ch", "agent8") == 9 and m.colour("ch", "agent8") == PALETTE[0]
    assert m.wrapped("ch", "agent8") and not m.wrapped("ch", "orch")


def test_persists_to_tui_toml_atomically_and_reloads(tmp_path):
    m = SlotMap(tmp_path, persist=True)
    m.colour("harbor-demo", "orch")
    m.colour("harbor-demo", "dev-1")
    path = tmp_path / "tui.toml"
    assert path.is_file()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".ratel-write-")]
    again = SlotMap(tmp_path, persist=True)
    assert again.colour("harbor-demo", "dev-1") == PALETTE[1]
    assert again.colour("harbor-demo", "new") == PALETTE[2]


def test_no_persist_writes_nothing(tmp_path):
    SlotMap(tmp_path, persist=False).colour("ch", "a")
    assert not (tmp_path / "tui.toml").exists()


def test_malformed_file_starts_a_fresh_map(tmp_path):
    (tmp_path / "tui.toml").write_text("not = [toml")
    assert SlotMap(tmp_path, persist=True).colour("ch", "a") == PALETTE[0]
    (tmp_path / "tui.toml").write_text('[slots.ch]\na = "one"\n')
    m = SlotMap(tmp_path, persist=True)
    assert m.colour("ch", "b") == PALETTE[0]
    (tmp_path / "tui.toml").write_text('[slots."../x"]\na = 1\n[slots.ch]\n"bad name!" = 1\nok = 3\n')
    m = SlotMap(tmp_path, persist=True)
    assert m.slot("ch", "ok") == 3 and m.colour("ch", "fresh") == PALETTE[3]
    assert "../x" not in m.table


def test_dump_and_load_round_trip_quotes_keys():
    text = slots.dump({"harbor-demo": {"dev-1": 1, "orch": 2}})
    assert slots.load(text) == {"harbor-demo": {"dev-1": 1, "orch": 2}}
