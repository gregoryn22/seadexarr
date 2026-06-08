import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

SCHEMA_VERSION = 2


@dataclass
class MovieAuditState:
    radarr_id: int
    tmdb_id: Optional[int]
    title: str
    seadex_status: str          # none / partial / full
    seadex_rgs: list[str] = field(default_factory=list)
    seadex_size_bytes: int = 0
    library_rgs: list[str] = field(default_factory=list)
    upgrade_available: bool = False
    too_large: bool = False
    hardlink_mismatch: bool = False
    last_notified: Optional[str] = None
    last_audited: str = ""


def movie_state_changed(old: Optional[MovieAuditState], new: MovieAuditState) -> bool:
    if old is None:
        return True
    return (
        old.seadex_status != new.seadex_status
        or set(old.seadex_rgs) != set(new.seadex_rgs)
        or set(old.library_rgs) != set(new.library_rgs)
        or old.upgrade_available != new.upgrade_available
        or old.too_large != new.too_large
        or old.hardlink_mismatch != new.hardlink_mismatch
    )


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
    missing_specials: bool = False
    missing_season: bool = False
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
        or old.missing_specials != new.missing_specials
        or old.missing_season != new.missing_season
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
        self._conn.row_factory = sqlite3.Row
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
                missing_specials  INTEGER NOT NULL DEFAULT 0,
                missing_season    INTEGER NOT NULL DEFAULT 0,
                last_notified    TEXT,
                last_audited     TEXT    NOT NULL DEFAULT ''
            )
        """)
        # Migration: add new columns to existing databases
        for col_def in [
            "missing_specials INTEGER NOT NULL DEFAULT 0",
            "missing_season INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                self._conn.execute(f"ALTER TABLE series ADD COLUMN {col_def}")
                self._conn.commit()
            except Exception:
                pass  # Column already exists
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                radarr_id         INTEGER PRIMARY KEY,
                tmdb_id           INTEGER,
                title             TEXT    NOT NULL,
                seadex_status     TEXT    NOT NULL,
                seadex_rgs        TEXT    NOT NULL DEFAULT '[]',
                seadex_size_bytes  INTEGER NOT NULL DEFAULT 0,
                library_rgs       TEXT    NOT NULL DEFAULT '[]',
                upgrade_available INTEGER NOT NULL DEFAULT 0,
                too_large         INTEGER NOT NULL DEFAULT 0,
                hardlink_mismatch INTEGER NOT NULL DEFAULT 0,
                last_notified     TEXT,
                last_audited      TEXT    NOT NULL DEFAULT ''
            )
        """)
        self._conn.commit()

    def _migrate_from_json(self, json_path: str):
        import logging as _logging
        _log = _logging.getLogger(__name__)
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                os.rename(json_path, json_path + ".migrated")
                return
            data = json.loads(content)
        except Exception as exc:
            _log.warning("audit_state: could not read legacy JSON %s: %s — starting fresh", json_path, exc)
            return

        migrated = failed = 0
        for key, entry in data.get("series", {}).items():
            try:
                self._upsert_row(SeriesAuditState(**entry))
                migrated += 1
            except Exception as exc:
                failed += 1
                _log.warning("audit_state: skipping entry %s during migration: %s", key, exc)

        self._conn.commit()
        if failed == 0:
            os.rename(json_path, json_path + ".migrated")
            _log.info("audit_state: migrated %d entries from JSON (renamed to .migrated)", migrated)
        else:
            _log.warning(
                "audit_state: migrated %d entries; %d failed — JSON preserved at %s",
                migrated, failed, json_path,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _upsert_row(self, state: SeriesAuditState):
        self._conn.execute(
            """
            INSERT OR REPLACE INTO series
                (sonarr_id, tvdb_id, title, seadex_status, seadex_rgs,
                 seadex_size_bytes, library_rgs, upgrade_available, too_large,
                 missing_specials, missing_season, last_notified, last_audited)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                int(state.missing_specials),
                int(state.missing_season),
                state.last_notified,
                state.last_audited,
            ),
        )

    def _row_to_state(self, row) -> SeriesAuditState:
        return SeriesAuditState(
            sonarr_id=row["sonarr_id"],
            tvdb_id=row["tvdb_id"],
            title=row["title"],
            seadex_status=row["seadex_status"],
            seadex_rgs=json.loads(row["seadex_rgs"]),
            seadex_size_bytes=row["seadex_size_bytes"],
            library_rgs=json.loads(row["library_rgs"]),
            upgrade_available=bool(row["upgrade_available"]),
            too_large=bool(row["too_large"]),
            missing_specials=bool(row["missing_specials"]),
            missing_season=bool(row["missing_season"]),
            last_notified=row["last_notified"],
            last_audited=row["last_audited"],
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

        newly_missing_specials = new_state.missing_specials and (
            old is None or not old.missing_specials
        )
        if newly_missing_specials:
            return discord_cfg.get("notify_on_missing_specials", True)

        newly_missing_season = new_state.missing_season and (
            old is None or not old.missing_season
        )
        if newly_missing_season:
            return discord_cfg.get("notify_on_missing_season", True)

        # State changed but no specific rule matched (e.g. partial→full when already seen)
        return discord_cfg.get("notify_on_state_change", True)

    def update_series(self, state: SeriesAuditState, notified: bool = False):
        now = datetime.now(timezone.utc).isoformat()
        state.last_audited = now
        if notified:
            state.last_notified = now
        self._upsert_row(state)
        self._conn.commit()

    def save(self):
        """No-op: SQLite writes are committed immediately in update_series."""

    # ------------------------------------------------------------------
    # Movies (Radarr)
    # ------------------------------------------------------------------

    def _upsert_movie_row(self, state: MovieAuditState):
        self._conn.execute(
            """
            INSERT OR REPLACE INTO movies
                (radarr_id, tmdb_id, title, seadex_status, seadex_rgs,
                 seadex_size_bytes, library_rgs, upgrade_available, too_large,
                 hardlink_mismatch, last_notified, last_audited)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.radarr_id,
                state.tmdb_id,
                state.title,
                state.seadex_status,
                json.dumps(state.seadex_rgs),
                state.seadex_size_bytes,
                json.dumps(state.library_rgs),
                int(state.upgrade_available),
                int(state.too_large),
                int(state.hardlink_mismatch),
                state.last_notified,
                state.last_audited,
            ),
        )

    def _movie_row_to_state(self, row) -> MovieAuditState:
        return MovieAuditState(
            radarr_id=row["radarr_id"],
            tmdb_id=row["tmdb_id"],
            title=row["title"],
            seadex_status=row["seadex_status"],
            seadex_rgs=json.loads(row["seadex_rgs"]),
            seadex_size_bytes=row["seadex_size_bytes"],
            library_rgs=json.loads(row["library_rgs"]),
            upgrade_available=bool(row["upgrade_available"]),
            too_large=bool(row["too_large"]),
            hardlink_mismatch=bool(row["hardlink_mismatch"]),
            last_notified=row["last_notified"],
            last_audited=row["last_audited"],
        )

    def get_movie(self, radarr_id: int) -> Optional[MovieAuditState]:
        row = self._conn.execute(
            "SELECT * FROM movies WHERE radarr_id = ?", (radarr_id,)
        ).fetchone()
        return self._movie_row_to_state(row) if row is not None else None

    def update_movie(self, state: MovieAuditState, notified: bool = False):
        now = datetime.now(timezone.utc).isoformat()
        state.last_audited = now
        if notified:
            state.last_notified = now
        self._upsert_movie_row(state)
        self._conn.commit()

    def should_notify_movie(self, new_state: MovieAuditState, discord_cfg: dict) -> bool:
        old = self.get_movie(new_state.radarr_id)

        # Hardlink mismatch always fires when first detected or when it reappears.
        if new_state.hardlink_mismatch and (old is None or not old.hardlink_mismatch):
            return True

        if not movie_state_changed(old, new_state):
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

        return discord_cfg.get("notify_on_state_change", True)

    def close(self):
        self._conn.close()

    def __del__(self):
        try:
            self._conn.close()
        except Exception:
            pass
