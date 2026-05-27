import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

SCHEMA_VERSION = 2


@dataclass
class SeriesAuditState:
    sonarr_id: int
    tvdb_id: Optional[int]
    title: str
    seadex_status: str          # none / partial / full
    seadex_rgs: list[str] = field(default_factory=list)
    seadex_size_bytes: int = 0
    library_rgs: list[str] = field(default_factory=list)
    upgrade_available: bool = False
    too_large: bool = False
    last_notified: Optional[str] = None
    last_audited: str = ""

    def state_key(self) -> str:
        return str(self.sonarr_id)


def state_changed(old: Optional[SeriesAuditState], new: SeriesAuditState) -> bool:
    if old is None:
        return True
    return (
        old.seadex_status != new.seadex_status
        or set(old.seadex_rgs) != set(new.seadex_rgs)
        or set(old.library_rgs) != set(new.library_rgs)
        or old.upgrade_available != new.upgrade_available
        or old.too_large != new.too_large
    )


def rg_diff(
    old: Optional[SeriesAuditState], new: SeriesAuditState
) -> tuple[list[str], list[str]]:
    """Return (added_rgs, removed_rgs) for seadex_rgs between old and new state."""
    old_rgs = set(old.seadex_rgs) if old else set()
    new_rgs = set(new.seadex_rgs)
    return sorted(new_rgs - old_rgs), sorted(old_rgs - new_rgs)


class AuditState:
    """SQLite-backed audit state. Accepts .json path for migration from the old backend."""

    def __init__(self, path: str):
        # Normalise to .db; keep old .json path for one-time migration
        if path.endswith(".json"):
            self._legacy_json: Optional[str] = path
            db_path = path[:-5] + ".db"
        else:
            self._legacy_json = None
            db_path = path

        self.path = db_path

        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)

        self._conn = sqlite3.connect(db_path)
        self._init_schema()

        if self._legacy_json and os.path.exists(self._legacy_json):
            self._migrate_from_json(self._legacy_json)

    # ------------------------------------------------------------------
    # Schema / migration
    # ------------------------------------------------------------------

    def _init_schema(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS series (
                sonarr_id        INTEGER PRIMARY KEY,
                tvdb_id          INTEGER,
                title            TEXT    NOT NULL,
                seadex_status    TEXT    NOT NULL,
                seadex_rgs       TEXT    NOT NULL DEFAULT '[]',
                seadex_size_bytes INTEGER NOT NULL DEFAULT 0,
                library_rgs      TEXT    NOT NULL DEFAULT '[]',
                upgrade_available INTEGER NOT NULL DEFAULT 0,
                too_large        INTEGER NOT NULL DEFAULT 0,
                last_notified    TEXT,
                last_audited     TEXT    NOT NULL DEFAULT ''
            )
        """)
        self._conn.commit()

    def _migrate_from_json(self, json_path: str):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                return
            data = json.loads(content)
            for entry in data.get("series", {}).values():
                self._upsert_row(SeriesAuditState(**entry))
            self._conn.commit()
            os.rename(json_path, json_path + ".migrated")
        except Exception:
            pass  # migration failure must not block startup

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _upsert_row(self, state: SeriesAuditState):
        self._conn.execute(
            """
            INSERT OR REPLACE INTO series
                (sonarr_id, tvdb_id, title, seadex_status, seadex_rgs,
                 seadex_size_bytes, library_rgs, upgrade_available, too_large,
                 last_notified, last_audited)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.sonarr_id,
                state.tvdb_id,
                state.title,
                state.seadex_status,
                json.dumps(state.seadex_rgs),
                state.seadex_size_bytes,
                json.dumps(state.library_rgs),
                int(state.upgrade_available),
                int(state.too_large),
                state.last_notified,
                state.last_audited,
            ),
        )

    def _row_to_state(self, row: tuple) -> SeriesAuditState:
        return SeriesAuditState(
            sonarr_id=row[0],
            tvdb_id=row[1],
            title=row[2],
            seadex_status=row[3],
            seadex_rgs=json.loads(row[4]),
            seadex_size_bytes=row[5],
            library_rgs=json.loads(row[6]),
            upgrade_available=bool(row[7]),
            too_large=bool(row[8]),
            last_notified=row[9],
            last_audited=row[10],
        )

    # ------------------------------------------------------------------
    # Public API (same surface as the old JSON-backed class)
    # ------------------------------------------------------------------

    def get_series(self, sonarr_id: int) -> Optional[SeriesAuditState]:
        row = self._conn.execute(
            "SELECT * FROM series WHERE sonarr_id = ?", (sonarr_id,)
        ).fetchone()
        return self._row_to_state(row) if row is not None else None

    def should_notify(self, new_state: SeriesAuditState, discord_cfg: dict) -> bool:
        old = self.get_series(new_state.sonarr_id)

        if not state_changed(old, new_state):
            return discord_cfg.get("notify_on_no_change", False)

        was_none = old is None or old.seadex_status == "none"

        if new_state.seadex_status == "partial" and was_none:
            return discord_cfg.get("notify_on_partial_match", True)

        if new_state.seadex_status == "full" and was_none:
            return discord_cfg.get("notify_on_new_seadex_match", True)

        newly_upgrade = new_state.upgrade_available and (old is None or not old.upgrade_available)
        if newly_upgrade:
            return discord_cfg.get("notify_on_new_upgrade_available", True)

        newly_large = new_state.too_large and (old is None or not old.too_large)
        if newly_large:
            return discord_cfg.get("notify_on_too_large", True)

        return True

    def update_series(self, state: SeriesAuditState, notified: bool = False):
        now = datetime.now(timezone.utc).isoformat()
        state.last_audited = now
        if notified:
            state.last_notified = now
        self._upsert_row(state)
        self._conn.commit()

    def save(self):
        """No-op: SQLite writes are committed immediately in update_series."""

    def close(self):
        self._conn.close()

    def __del__(self):
        try:
            self._conn.close()
        except Exception:
            pass
