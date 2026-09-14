"""The `clan` attachment: the schema a clan proposal/approval travels in.

Built by `clan propose` (status "proposed"), confirmed on the board or in the
orchestrator tab (status "approved"), consumed by `clan approve`. The board
serves and accepts this shape; validation is shared by bus, board and CLI.
"""
from __future__ import annotations

import json
import re

from ..bus import extract_mentions
from ..ulid import is_ulid
from .config import ClanConfig, RoleSpec, deep_merge, load_catalog, load_defaults, load_models, load_presets

CLAN_LIMITS = dict(roles=8, why=160, skills=8, skill=40, model=80, preset=40, bytes=2600)
# a skill spec reaches `npx skills add <spec>`: it must never start with `-`
# (argv injection into npx) and may carry no whitespace or shell metachars.
# Documented shapes: `tdd`, `superpowers:tdd`, `owner/repo@name`,
# `@anthropic/skill`, `https://github.com/owner/repo` — this regex is the ONLY
# argv guard for install_skills (the skills CLI ignores `--`).
SKILL_SPEC_RE = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9._/@:-]*$")
# A preset id is a slug the operator picks from the catalog, never free text:
# harness, model and effort all come from the preset's own row in presets.toml,
# so this is the only role field the bus can steer.
PRESET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _bad(field: str, detail: str) -> ValueError:
    return ValueError(f"attachment {field}: {detail}")


def validate_clan_attachment(att: dict, catalog: dict) -> dict:
    """Raise ValueError naming the field on the first failure; return the
    attachment unchanged on success. Order below is the contract."""
    if not isinstance(att, dict) or att.get("type") != "clan":
        raise _bad("type", "must be \"clan\"")
    if att.get("status") not in ("proposed", "approved"):
        raise _bad("status", f"must be proposed or approved, got {att.get('status')!r}")
    issue = att.get("issue")
    if not isinstance(issue, int) or isinstance(issue, bool):
        raise _bad("issue", f"must be an int, got {issue!r}")
    roles = att.get("roles")
    if not isinstance(roles, list) or not roles or len(roles) > CLAN_LIMITS["roles"]:
        raise _bad("roles", f"must be a non-empty list of at most "
                            f"{CLAN_LIMITS['roles']} roles, got {type(roles).__name__}")
    seen: set[str] = set()
    writers: list[str] = []
    presets = {p["id"] for p in catalog.get("presets", [])}
    for role in roles:
        if not isinstance(role, dict):
            raise _bad("roles", f"each role must be an object, got {type(role).__name__}")
        name = role.get("name")
        if not isinstance(name, str) or not name:
            raise _bad("name", f"every role needs a name, got {name!r}")
        if name in seen:
            raise _bad("name", f"duplicate role {name!r}")
        seen.add(name)
        if extract_mentions("@" + name) != [name]:
            raise _bad("name", f"{name!r} is not mentionable on the channel")
        if name == "orchestrator":
            raise _bad("name", "orchestrator is fixed at kickoff and never proposed")
        preset = role.get("preset")
        if (not isinstance(preset, str) or not preset or len(preset) > CLAN_LIMITS["preset"]
                or not PRESET_ID_RE.fullmatch(preset)):
            raise _bad("preset", f"{preset!r} for {name}")
        if preset not in presets:
            raise _bad("preset", f"unknown preset {preset!r} for {name} (known: "
                                 f"{', '.join(sorted(presets))})")
        writer = role.get("writer")
        if not isinstance(writer, bool):
            raise _bad("writer", f"{writer!r} for {name}")
        if writer:
            writers.append(name)
        skills = role.get("skills")
        if (not isinstance(skills, list) or len(skills) > CLAN_LIMITS["skills"]
                or any(not isinstance(s, str) or not s or len(s) > CLAN_LIMITS["skill"]
                       or not SKILL_SPEC_RE.match(s) or ".." in s.split("/")
                       for s in skills)):
            raise _bad("skills", f"{skills!r} for {name}")
        why = role.get("why")
        if not isinstance(why, str) or len(why) > CLAN_LIMITS["why"]:
            raise _bad("why", f"{why!r} for {name}")
    if len(writers) != 1:
        raise _bad("writer", f"a clan needs exactly one writer, got {len(writers)}: {writers}")
    supersedes = att.get("supersedes")
    if supersedes is not None and not is_ulid(supersedes):
        raise _bad("supersedes", f"{supersedes!r} is not a ULID")
    # compact separators — the page's JSON.stringify mirror counts the same form
    if len(json.dumps(att, separators=(",", ":")).encode()) > CLAN_LIMITS["bytes"]:
        raise _bad("bytes", f"attachment exceeds {CLAN_LIMITS['bytes']} bytes")
    return att


def clan_config_from(att: dict, base: ClanConfig, home) -> ClanConfig:
    """The ClanConfig an approved attachment describes: the base's channel,
    issue, repo, checkout and orchestrator, plus one role per attachment role.
    Only the orchestrator survives from base — an amend that drops a role must
    drop it from clan.toml too, or the next `clan up` starts it."""
    catalog_roles = load_catalog(home)
    defaults = load_defaults(home)
    # re-read at approve time on purpose: the catalog may have changed between
    # propose and approve, and clan.toml must carry what the preset says now
    presets = load_presets(home)
    orchestrator = base.roles.get("orchestrator")
    roles = {"orchestrator": orchestrator} if orchestrator else {}
    for role in att["roles"]:
        preset = presets.get(role["preset"])
        if preset is None:
            raise ValueError(f"unknown preset {role['preset']!r} for role {role['name']} "
                             f"(known: {', '.join(sorted(presets))})")
        merged = deep_merge(defaults, catalog_roles.get(role["name"], {}))
        roles[role["name"]] = RoleSpec(
            name=role["name"], harness=preset["harness"], model=preset["model"],
            effort=preset.get("effort", ""), writer=role["writer"],
            skills=list(role["skills"]),
            brief=merged.get("brief", role["name"]),
            checkpoint_at=merged.get("checkpoint_at", 200000))
    return ClanConfig(channel=base.channel, issue=base.issue, repo=base.repo,
                      checkout=base.checkout, roles=roles).validate(load_models(home))
