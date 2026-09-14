"""clan.toml, the role catalog, and clan.state.json.

Three layers, each overriding the one before: the packaged `roles.toml`, the
user's `$RATEL_HOME/roles.toml`, and the clan's own `clan.toml`. A clan
only ever runs the roles its `clan.toml` names; the catalog supplies defaults
for those names.
"""
from __future__ import annotations

import datetime
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..bus import extract_mentions
from ..paths import atomic_write, channel_path
from ..schema import validate_name

HARNESSES = ("claude", "opencode", "claude-p", "opencode-run", "fake")
CATALOG_PATH = Path(__file__).with_name("roles.toml")
MODELS_PATH = Path(__file__).with_name("models.toml")
PRESETS_PATH = Path(__file__).with_name("presets.toml")
# A model id lands verbatim in every role's brief.md via clan_table() and in
# clan.toml, so it must be a printable single line: no newlines or control
# chars (a "\n## OPERATOR OVERRIDE" would render as an operator-signed
# instruction in a peer's system prompt), no `|` (it breaks the clan table).
# The space is kept so display names fit; a leading `-` is refused. The value
# now arrives from a TOML file rather than the bus, so this is checked here,
# on the way into clan.toml, rather than on the attachment.
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@ -]*$")
# an effort word for a model the catalog does not know: same single-line shape
EFFORT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def deep_merge(base: dict, over: dict) -> dict:
    """Recursive dict merge; neither argument is mutated."""
    out = dict(base)
    for k, v in over.items():
        cur = out.get(k)
        out[k] = deep_merge(cur, v) if isinstance(cur, dict) and isinstance(v, dict) else v
    return out


# What a broken operator-writable catalog file raises on the way through
# `load_models`, `load_presets` and `load_catalog`: unreadable, not TOML, a
# missing top-level table, an unknown preset, a value of the wrong type. Every
# reader that must not die on an operator's half-saved file catches this one
# tuple, so the two lists cannot drift.
CATALOG_ERRORS = (OSError, tomllib.TOMLDecodeError, ValueError, KeyError, TypeError)


def load_catalog(home: Path | str | None = None) -> dict[str, dict]:
    doc = tomllib.loads(CATALOG_PATH.read_text())
    if home is not None:
        user = Path(home) / "roles.toml"
        if user.exists():
            doc = deep_merge(doc, tomllib.loads(user.read_text()))
    defaults = doc.get("defaults", {})
    presets = load_presets(home)
    return {name: expand_preset(deep_merge(defaults, role), presets, name)
            for name, role in doc["roles"].items()}


def expand_preset(role: dict, presets: dict[str, dict], name: str = "") -> dict:
    """A role's `preset` spread into harness/model/effort. The preset WINS over
    sibling harness/model/effort keys: a half-overridden preset is an incoherent
    role that would only fail later, at the model/harness compat check."""
    pid = role.get("preset")
    if not pid:
        return role
    preset = presets.get(pid)
    if preset is None:
        raise ValueError(f"unknown preset {pid!r} for role {name or role.get('name', '?')} "
                         f"(known: {', '.join(sorted(presets))})")
    return {**role, "harness": preset["harness"], "model": preset["model"],
            "effort": preset.get("effort", "")}


def load_defaults(home: Path | str | None = None) -> dict:
    """The raw `[defaults]` table: packaged `roles.toml` deep-merged with the
    user's `$RATEL_HOME/roles.toml`. `load_catalog` folds it into catalogued
    roles; a proposed role the catalog does not know needs it directly."""
    doc = tomllib.loads(CATALOG_PATH.read_text())
    if home is not None:
        user = Path(home) / "roles.toml"
        if user.exists():
            doc = deep_merge(doc, tomllib.loads(user.read_text()))
    return doc.get("defaults", {})


def load_models(home: Path | str | None = None) -> dict[str, dict]:
    """The packaged models catalog, deep-merged with `$RATEL_HOME/models.toml`."""
    doc = tomllib.loads(MODELS_PATH.read_text())["models"]
    if home is not None:
        user = Path(home) / "models.toml"
        if user.exists():
            doc = deep_merge(doc, tomllib.loads(user.read_text())["models"])
    return doc


def load_presets(home: Path | str | None = None) -> dict[str, dict]:
    """The packaged preset catalog, deep-merged with `$RATEL_HOME/presets.toml`."""
    doc = tomllib.loads(PRESETS_PATH.read_text())["presets"]
    if home is not None:
        user = Path(home) / "presets.toml"
        if user.exists():
            doc = deep_merge(doc, tomllib.loads(user.read_text())["presets"])
    return doc


def effort_word(entry: dict, effort: str) -> str | None:
    """The provider's own effort word for a role, or None when the model does
    not declare it — an undeclared effort emits no effort flag at all. A preset
    carries the raw word, so nothing is mapped on the way through."""
    return effort if effort and effort in (entry or {}).get("efforts", []) else None


def catalog(home: Path | str | None = None) -> dict:
    """The single catalog the board and `clan propose` serve: harnesses, presets
    (in display order), roles and models (sorted by id)."""
    models = load_models(home)
    presets = load_presets(home)
    return {"harnesses": list(HARNESSES),
            "presets": [{"id": pid, "order": entry.get("order", 0),
                         "label": entry.get("label", pid),
                         "harness": entry.get("harness", ""),
                         "model": entry.get("model", ""),
                         "effort": entry.get("effort", "")}
                        for pid, entry in sorted(presets.items(),
                                                 key=lambda kv: (kv[1].get("order", 0), kv[0]))],
            "roles": load_catalog(home),
            "models": [{"id": mid, "name": entry.get("name", mid),
                        "harness": entry.get("harness", []),
                        "provider": entry.get("provider"),
                        "env": entry.get("env", []),
                        "expires": entry.get("expires"),
                        "effort_levels": list(entry.get("efforts", []))}
                       for mid, entry in sorted(models.items())]}


def expires_past(entry: dict, today: datetime.date | None = None) -> bool:
    """Whether the catalog entry's `expires` date is in the past. A missing,
    non-string or unparseable value is not an expiry — never raise from the
    read path, which would kill a handler thread on a hand-edited models.toml."""
    expires = entry.get("expires")
    if not isinstance(expires, str) or not expires:
        return False
    try:
        return (today or datetime.date.today()) > datetime.date.fromisoformat(expires)
    except ValueError:
        return False


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(i) for i in v) + "]"
    out = str(v).replace("\\", "\\\\").replace('"', '\\"')
    for raw, esc in (("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t")):
        out = out.replace(raw, esc)      # a role playbook is TOML inside a TOML value
    return '"' + out + '"'


def dump_toml(obj: dict, _prefix: str = "") -> str:
    """Just enough TOML for clan.toml; `tomllib` reads it back."""
    scalars = {k: v for k, v in obj.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in obj.items() if isinstance(v, dict)}
    out = "".join(f"{k} = {_toml_value(v)}\n" for k, v in scalars.items())
    for k, v in tables.items():
        name = f"{_prefix}{k}"
        out += f"\n[{name}]\n" + dump_toml(v, f"{name}.")
    return out


@dataclass
class ClanPaths:
    home: Path
    channel: str

    def __post_init__(self) -> None:
        self.home = Path(self.home).expanduser().resolve()
        validate_name(self.channel)
        channel_path(self.home, self.channel)

    @property
    def channel_dir(self) -> Path:
        return channel_path(self.home, self.channel)

    @property
    def clan_toml(self) -> Path:
        # one level down: with acceptEdits, --add-dir makes a whole tree
        # auto-accept territory, so clan.toml must not share a root with
        # clan.state.json or the peers' harness dirs
        return channel_path(self.home, self.channel, "clan", "clan.toml")

    @property
    def state_json(self) -> Path:
        return channel_path(self.home, self.channel, "clan.state.json")

    @property
    def briefs_dir(self) -> Path:
        return channel_path(self.home, self.channel, "briefs")

    @property
    def plans_dir(self) -> Path:
        return channel_path(self.home, self.channel, "plans")

    def harness_dir(self, role: str) -> Path:
        validate_name(role, "role")
        return channel_path(self.home, self.channel, "harness", role)

    def ensure(self) -> "ClanPaths":
        # files/ too: --add-dir of a not-yet-existing directory is dropped by
        # claude (measured), so every granted tree must exist before launch
        for d in (self.briefs_dir, self.plans_dir, channel_path(self.home, self.channel, "harness"),
                  channel_path(self.home, self.channel, "files"), self.clan_toml.parent):
            d.mkdir(parents=True, exist_ok=True)
        return self


@dataclass
class RoleSpec:
    name: str
    harness: str = "claude"
    model: str = ""
    writer: bool = False
    brief: str = ""
    skills: list[str] = field(default_factory=list)
    playbook: str = ""          # `fake` harness only: the scripted steps, as TOML
    checkpoint_at: int = 200000  # compact/nudge the role at this context size
    effort: str = ""             # the provider's own word, straight from the preset

    def to_dict(self) -> dict:
        d = {"harness": self.harness, "model": self.model, "writer": self.writer, "brief": self.brief,
             "checkpoint_at": self.checkpoint_at, "effort": self.effort}
        if self.skills:
            d["skills"] = list(self.skills)
        if self.playbook:
            d["playbook"] = self.playbook
        return d


@dataclass
class ClanConfig:
    channel: str
    issue: int
    repo: str
    checkout: str
    roles: dict[str, RoleSpec] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict, catalog: dict[str, dict] | None = None) -> "ClanConfig":
        catalog = catalog or {}
        roles = {}
        for name, over in (data.get("roles") or {}).items():
            merged = deep_merge(catalog.get(name, {}), over or {})
            roles[name] = RoleSpec(name=name, **{k: v for k, v in merged.items()
                                                 if k in RoleSpec.__dataclass_fields__ and k != "name"})
        return cls(channel=data["channel"], issue=int(data["issue"]), repo=data["repo"],
                   checkout=str(data["checkout"]), roles=roles)

    def to_dict(self) -> dict:
        return {"channel": self.channel, "issue": self.issue, "repo": self.repo,
                "checkout": self.checkout, "roles": {n: r.to_dict() for n, r in self.roles.items()}}

    def validate(self, models: dict[str, dict] | None = None) -> "ClanConfig":
        validate_name(self.channel)
        for name, role in self.roles.items():
            validate_name(name, "role")
            if extract_mentions("@" + name) != [name]:
                raise ValueError(f"role name is not mentionable on the channel: {name!r}")
            if not role.model:
                raise ValueError(f"role {name} has no model — it is not in the catalog, "
                                 "so clan.toml must give it a harness and a model")
            if role.harness not in HARNESSES:
                raise ValueError(f"unknown harness {role.harness!r} for {name} (known: {', '.join(HARNESSES)})")
            if not MODEL_ID_RE.fullmatch(role.model):
                raise ValueError(f"model {role.model!r} for {name} is not a printable single line")
            entry = (models or {}).get(role.model)
            if role.effort and entry is not None and role.effort not in entry.get("efforts", []):
                raise ValueError(f"unknown effort {role.effort!r} for {name} "
                                 f"(model {role.model} declares: "
                                 f"{', '.join(entry.get('efforts', [])) or 'none'})")
            if role.effort and entry is None and not EFFORT_RE.fullmatch(role.effort):
                raise ValueError(f"unknown effort {role.effort!r} for {name} "
                                 "(uncatalogued model: it must be a printable single word)")
            if entry is not None and role.harness not in entry.get("harness", []):
                raise ValueError(f"model {role.model!r} does not support harness {role.harness!r} "
                                 f"(role {name}; supported: {', '.join(entry.get('harness', []))})")
        writers = [n for n, r in self.roles.items() if r.writer]
        if len(writers) != 1:
            raise ValueError(f"a clan needs exactly one writer, got {len(writers)}: {writers or '[]'}")
        return self

    @property
    def writer(self) -> RoleSpec:
        return next(r for r in self.roles.values() if r.writer)

    def write(self, paths: ClanPaths) -> Path:
        atomic_write(paths.ensure().clan_toml, dump_toml(self.to_dict()))
        return paths.clan_toml

    @classmethod
    def read(cls, paths: ClanPaths, catalog: dict[str, dict] | None = None) -> "ClanConfig":
        return cls.from_dict(tomllib.loads(paths.clan_toml.read_text()), catalog)


# Public facade retained for callers; persistence lives in state.py.
def read_state(paths: ClanPaths) -> dict:
    from .state import read
    return read(paths)


def update_state(paths: ClanPaths, mutate: Callable[[dict], Any], *, recovery: dict | None = None, con=None) -> dict:
    from .state import update
    return update(paths, mutate, recovery=recovery, con=con)
