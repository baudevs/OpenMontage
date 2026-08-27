"""Local registry of people whose likeness we may use as a video reference.

Face references are not interchangeable with ordinary reference images. A face
has to reach the provider as a registered asset, it belongs to a specific asset
group, and which group it lives in decides which models can read it. Losing
those identifiers means re-uploading (and, for a verified group, dragging the
person back to their phone), so they are kept here.

Storage is **per project and outside the repository**, so nothing mixes between
clients and nothing can be committed by accident:

    ~/.openmontage/projects/<project-slug>/people.db

Override with `OPENMONTAGE_PEOPLE_DB` (exact file) or `OPENMONTAGE_STATE_DIR`
(root directory). The file holds names and provider identifiers — no images.

Two kinds of group, and the difference matters:

    AIGC          an ordinary asset group. Readable by every Seedance model.
    LivenessFace  created by real-person verification. Readable by Seedance 2.0
                  only — 2.5 reports its assets as not found.
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

GROUP_TYPES = ("AIGC", "LivenessFace")

# Which models can resolve an asset from each group type.
MODELS_BY_GROUP_TYPE = {
    "AIGC": ("seedance-2.5", "seedance-2.0", "seedance-fast", "seedance-2.0-mini", "seedance-2.0-ultra"),
    "LivenessFace": ("seedance-2.0", "seedance-fast", "seedance-2.0-mini", "seedance-2.0-ultra"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    slug                 TEXT PRIMARY KEY,
    display_name         TEXT NOT NULL,
    consent_confirmed_at TEXT,
    consent_note         TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS asset_groups (
    group_id     TEXT PRIMARY KEY,
    person_slug  TEXT NOT NULL REFERENCES people(slug) ON DELETE CASCADE,
    provider     TEXT NOT NULL DEFAULT 'anyfast',
    group_type   TEXT NOT NULL CHECK (group_type IN ('AIGC', 'LivenessFace')),
    name         TEXT,
    byted_token  TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id     TEXT PRIMARY KEY,
    person_slug  TEXT NOT NULL REFERENCES people(slug) ON DELETE CASCADE,
    group_id     TEXT NOT NULL REFERENCES asset_groups(group_id) ON DELETE CASCADE,
    provider     TEXT NOT NULL DEFAULT 'anyfast',
    asset_type   TEXT NOT NULL,
    name         TEXT,
    source       TEXT,
    status       TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_groups_person ON asset_groups(person_slug);
CREATE INDEX IF NOT EXISTS idx_assets_person ON assets(person_slug);
"""


class RegistryError(RuntimeError):
    """Invalid registry input."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
    if not slug:
        raise RegistryError("a person needs a non-empty name")
    return slug


def project_slug(project_dir: str | Path | None) -> str:
    """Per-project scoping key. Falls back to a shared 'default' store."""
    if not project_dir:
        return "default"
    return slugify(Path(str(project_dir)).expanduser().resolve().name)


def db_path(project_dir: str | Path | None = None) -> Path:
    explicit = os.environ.get("OPENMONTAGE_PEOPLE_DB")
    if explicit:
        return Path(explicit).expanduser()
    root = Path(os.environ.get("OPENMONTAGE_STATE_DIR", "~/.openmontage")).expanduser()
    return root / "projects" / project_slug(project_dir) / "people.db"


@contextmanager
def connect(project_dir: str | Path | None = None, path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    target = Path(path).expanduser() if path else db_path(project_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA)
        yield connection
        connection.commit()
    finally:
        connection.close()


# ---- writes ----


def upsert_person(
    connection: sqlite3.Connection,
    *,
    slug: str,
    display_name: str | None = None,
    consent_confirmed: bool = False,
    consent_note: str | None = None,
) -> dict[str, Any]:
    """Create or update a person. Consent is recorded once and never cleared."""
    slug = slugify(slug)
    now = _now()
    row = connection.execute("SELECT * FROM people WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO people (slug, display_name, consent_confirmed_at, consent_note,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                slug,
                display_name or slug,
                now if consent_confirmed else None,
                consent_note,
                now,
                now,
            ),
        )
    else:
        connection.execute(
            "UPDATE people SET display_name = ?, consent_confirmed_at = ?, consent_note = ?,"
            " updated_at = ? WHERE slug = ?",
            (
                display_name or row["display_name"],
                row["consent_confirmed_at"] or (now if consent_confirmed else None),
                consent_note if consent_note is not None else row["consent_note"],
                now,
                slug,
            ),
        )
    return get_person(connection, slug)


def record_group(
    connection: sqlite3.Connection,
    *,
    person_slug: str,
    group_id: str,
    group_type: str,
    name: str | None = None,
    provider: str = "anyfast",
    byted_token: str | None = None,
) -> dict[str, Any]:
    if group_type not in GROUP_TYPES:
        raise RegistryError("group_type must be AIGC or LivenessFace")
    person_slug = slugify(person_slug)
    if connection.execute("SELECT 1 FROM people WHERE slug = ?", (person_slug,)).fetchone() is None:
        upsert_person(connection, slug=person_slug)
    connection.execute(
        "INSERT INTO asset_groups (group_id, person_slug, provider, group_type, name,"
        " byted_token, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(group_id) DO UPDATE SET person_slug = excluded.person_slug,"
        " group_type = excluded.group_type, name = COALESCE(excluded.name, asset_groups.name),"
        " byted_token = COALESCE(excluded.byted_token, asset_groups.byted_token)",
        (group_id, person_slug, provider, group_type, name, byted_token, _now()),
    )
    return dict(
        connection.execute("SELECT * FROM asset_groups WHERE group_id = ?", (group_id,)).fetchone()
    )


def record_asset(
    connection: sqlite3.Connection,
    *,
    person_slug: str,
    group_id: str,
    asset_id: str,
    asset_type: str = "Image",
    name: str | None = None,
    source: str | None = None,
    status: str | None = None,
    provider: str = "anyfast",
) -> dict[str, Any]:
    person_slug = slugify(person_slug)
    if connection.execute(
        "SELECT 1 FROM asset_groups WHERE group_id = ?", (group_id,)
    ).fetchone() is None:
        raise RegistryError(
            f"group {group_id} is not registered; record_group() it first so the "
            "asset's model compatibility is known"
        )
    connection.execute(
        "INSERT INTO assets (asset_id, person_slug, group_id, provider, asset_type, name,"
        " source, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(asset_id) DO UPDATE SET status = excluded.status,"
        " name = COALESCE(excluded.name, assets.name)",
        (asset_id, person_slug, group_id, provider, asset_type, name, source, status, _now()),
    )
    return dict(connection.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone())


def forget_person(connection: sqlite3.Connection, slug: str) -> dict[str, Any]:
    """Remove a person locally. Remote assets are NOT deleted by this."""
    slug = slugify(slug)
    groups = [dict(r) for r in connection.execute(
        "SELECT * FROM asset_groups WHERE person_slug = ?", (slug,)
    )]
    assets = [dict(r) for r in connection.execute(
        "SELECT * FROM assets WHERE person_slug = ?", (slug,)
    )]
    connection.execute("DELETE FROM people WHERE slug = ?", (slug,))
    return {
        "slug": slug,
        "forgotten_groups": [g["group_id"] for g in groups],
        "forgotten_assets": [a["asset_id"] for a in assets],
        "note": (
            "Local records only. The provider still holds these assets — delete them "
            "with anyfast_assets (delete_asset / delete_group) to remove them there."
        ),
    }


# ---- reads ----


def usable_models(group_type: str) -> list[str]:
    return list(MODELS_BY_GROUP_TYPE.get(group_type, ()))


def get_person(connection: sqlite3.Connection, slug: str) -> dict[str, Any]:
    slug = slugify(slug)
    row = connection.execute("SELECT * FROM people WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise RegistryError(f"no person registered as {slug!r}")
    person = dict(row)
    person["consent_confirmed"] = bool(person.get("consent_confirmed_at"))
    person["groups"] = []
    for group in connection.execute(
        "SELECT * FROM asset_groups WHERE person_slug = ? ORDER BY group_type", (slug,)
    ):
        entry = dict(group)
        entry["usable_models"] = usable_models(entry["group_type"])
        entry["assets"] = [
            dict(a)
            for a in connection.execute(
                "SELECT * FROM assets WHERE group_id = ? ORDER BY created_at", (entry["group_id"],)
            )
        ]
        person["groups"].append(entry)
    person["asset_count"] = sum(len(g["assets"]) for g in person["groups"])
    person["models_available"] = sorted(
        {model for g in person["groups"] for model in g["usable_models"]}
    )
    return person


def list_people(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        get_person(connection, row["slug"])
        for row in connection.execute("SELECT slug FROM people ORDER BY display_name")
    ]


def resolve_upload_group(
    connection: sqlite3.Connection,
    slug: str,
    *,
    prefer: str = "AIGC",
) -> dict[str, Any] | None:
    """Pick the group a new face reference for this person should go into.

    AIGC is preferred because its assets work on every Seedance model; a
    LivenessFace group is only chosen when it is the one that exists.
    """
    person_slug = slugify(slug)
    order = [prefer] + [t for t in GROUP_TYPES if t != prefer]
    for group_type in order:
        row = connection.execute(
            "SELECT * FROM asset_groups WHERE person_slug = ? AND group_type = ?"
            " ORDER BY created_at LIMIT 1",
            (person_slug, group_type),
        ).fetchone()
        if row is not None:
            entry = dict(row)
            entry["usable_models"] = usable_models(entry["group_type"])
            return entry
    return None


def references_for(
    connection: sqlite3.Connection,
    slug: str,
    *,
    model: str | None = None,
    asset_type: str = "Image",
) -> list[dict[str, Any]]:
    """Face references for this person that the given model can actually read."""
    person = get_person(connection, slug)
    references: list[dict[str, Any]] = []
    for group in person["groups"]:
        if model and model not in group["usable_models"]:
            continue
        for asset in group["assets"]:
            if asset_type and asset["asset_type"] != asset_type:
                continue
            if asset.get("status") and asset["status"] != "Active":
                continue
            references.append(
                {
                    "asset_ref": f"asset://{asset['asset_id']}",
                    "asset_id": asset["asset_id"],
                    "name": asset["name"],
                    "group_id": group["group_id"],
                    "group_type": group["group_type"],
                    "usable_models": group["usable_models"],
                }
            )
    return references
