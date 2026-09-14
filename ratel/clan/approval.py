"""Durable approval intent and repeatable application to configuration and SQLite state.

An intent is committed before configuration or state changes. Lifecycle commands refuse
pending applications; `clan approve` replays the saved snapshot after a crash.
No shell commands run while the database transaction is held.
"""
import json

from .config import ClanConfig, ClanPaths, update_state


def recover(paths: ClanPaths, store):
    with store.connection(write=True) as con:
        pending = store.pending(con)
        if pending is None:
            return None
        approval_id, raw = pending
        snapshot = json.loads(raw)
        cfg = ClanConfig.from_dict(snapshot["config"]).validate()
        if cfg.channel != paths.channel or not isinstance(snapshot["state"], dict):
            raise ValueError("pending approval has an invalid snapshot")
        cfg.write(paths)

        def apply(state):
            state.update(writers={n: r.writer for n, r in cfg.roles.items()},
                         briefs={n: r.brief for n, r in cfg.roles.items()})

        update_state(paths, apply, recovery=snapshot["state"], con=con)
        con.execute("UPDATE approval_applications SET pending=0 WHERE approval_id=?", (approval_id,))
        return approval_id, cfg.to_dict()


def prepare(con, cfg: ClanConfig, state: dict, approval_id: str):
    snapshot = json.dumps({"config": cfg.to_dict(), "state": state})
    con.execute("INSERT INTO approval_applications(approval_id,config,pending) VALUES(?,?,1)",
                (approval_id, snapshot))


def completed(con, approval_id: str):
    row = con.execute("SELECT config FROM approval_applications WHERE approval_id=? AND pending=0",
                      (approval_id,)).fetchone()
    return json.loads(row[0])["config"] if row else None
