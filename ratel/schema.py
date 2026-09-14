"""Structural validation shared by writers, SQLite readers and JSONL import.

Catalog membership and clan approval semantics are deliberately validated at
approval time: old messages must remain readable when a preset is removed.
"""
import math
import re
from datetime import datetime

from .ulid import is_ulid


def validate_name(value: str, kind: str = "channel") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in (".", ".."):
        raise ValueError(f"invalid {kind} name")
    return value


def valid_timestamp(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _strings(doc, keys):
    for key in keys:
        if key in doc and doc[key] is not None and not isinstance(doc[key], str):
            raise ValueError(f"attachment {key}: must be a string")


def validate_attachment(att):
    if not isinstance(att, dict):
        raise ValueError("attachment must be an object")
    kind = att.get("type")
    if not kind:
        kind = next((t for k, t in (("ref", "file"), ("url", "link"), ("body", "code"),
                                    ("items", "tasks"), ("roles", "clan")) if k in att), None)
    if kind not in ("code", "file", "link", "tasks", "clan"):
        raise ValueError("attachment type: unknown or missing")
    _strings(att, ("name", "mime", "ref", "url", "href", "file", "lang", "body"))
    if kind == "code" and not isinstance(att.get("body"), str):
        raise ValueError("attachment body: code requires a string")
    if kind == "file" and not isinstance(att.get("ref"), str):
        raise ValueError("attachment ref: file requires a string")
    if kind == "link" and not isinstance(att.get("url", att.get("href")), str):
        raise ValueError("attachment url: link requires a string")
    if "pages" in att and (type(att["pages"]) is not int or att["pages"] < 0):
        raise ValueError("attachment pages: must be a non-negative integer")
    if kind in ("tasks", "clan"):
        key = "items" if kind == "tasks" else "roles"
        items = att.get(key)
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError(f"attachment {key}: must be a list of objects")
        for item in items:
            required = "text" if kind == "tasks" else "name"
            if not isinstance(item.get(required), str):
                raise ValueError(f"attachment {key}.{required}: must be a string")
            if kind == "tasks":
                _strings(item, ("who",))
                if "done" in item and not isinstance(item["done"], bool):
                    raise ValueError("attachment items.done: must be a boolean")
            else:
                validate_name(item["name"], "role")
                _strings(item, ("preset", "why", "harness", "model", "effort"))
                if "writer" in item and not isinstance(item["writer"], bool):
                    raise ValueError("attachment roles.writer: must be a boolean")
                if "skills" in item and (not isinstance(item["skills"], list)
                                         or any(not isinstance(s, str) for s in item["skills"])):
                    raise ValueError("attachment roles.skills: must be a list of strings")
        if kind == "clan":
            if "status" in att and att["status"] not in ("proposed", "approved"):
                raise ValueError("attachment status: must be proposed or approved")
            if "issue" in att and (type(att["issue"]) is not int or att["issue"] <= 0):
                raise ValueError("attachment issue: must be a positive integer")
            if att.get("supersedes") is not None and not is_ulid(att["supersedes"]):
                raise ValueError("attachment supersedes: must be a ULID")
    return att


def validate_message(doc):
    if not isinstance(doc, dict) or not is_ulid(doc.get("id")):
        raise ValueError("message id: must be a ULID")
    validate_name(doc.get("from"), "agent")
    if not isinstance(doc.get("text"), str):
        raise ValueError("message text: must be a string")
    if not valid_timestamp(doc.get("ts")):
        raise ValueError("message ts: must be an ISO timestamp with timezone")
    if doc.get("parent") is not None and not is_ulid(doc["parent"]):
        raise ValueError("message parent: must be a ULID")
    if not isinstance(doc.get("mentions"), list):
        raise ValueError("message mentions: must be a list")
    for name in doc["mentions"]:
        validate_name(name, "mention")
    if not isinstance(doc.get("attachments", []), list):
        raise ValueError("message attachments: must be a list")
    for att in doc.get("attachments", []):
        validate_attachment(att)
    pin = doc.get("pin", False)
    if not isinstance(pin, bool) and not is_ulid(pin):
        raise ValueError("message pin: must be a boolean or ULID")
    if doc.get("unpin") is not None and not is_ulid(doc["unpin"]):
        raise ValueError("message unpin: must be a ULID")
    return doc


def is_message(doc) -> bool:
    try:
        validate_message(doc)
        return True
    except (ValueError, TypeError):
        return False


def validate_limit(limit):
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError("limit must be a positive integer")


def validate_timeout(timeout):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be a finite non-negative number")
