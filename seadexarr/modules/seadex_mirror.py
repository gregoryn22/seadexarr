"""Local SQLite mirror of the SeaDex catalogue.

SeaDex is small (a few thousand entries) and its PocketBase API exposes the
whole catalogue with torrents expanded. Querying it once per title every run
(and again for every cache-freshness check) means N API calls for a library of
N titles, every single run. Instead we keep a local copy and refresh it
*incrementally*: each sync pulls only entries updated since the last watermark,
so steady-state runs cost roughly one request. Library lookups then become
local reads with no network at all.

The mirror stores each entry as ``EntryRecord.to_json()``, so a read rehydrates
the exact same object ``get_seadex_entry()`` used to return from the API — no
adapter needed, the rest of the pipeline is unchanged.

Because the mirror is a *complete* copy, "not in the mirror" means "not in
SeaDex": there is no per-entry live fallback to gain anything. The only useful
fallback is the last good mirror itself — if a sync fails (SeaDex down, network
blip), we keep serving yesterday's copy rather than crashing the run.
"""

import os
import sqlite3
from datetime import timedelta

from seadex import EntryRecord

from .log import centred_string


# PocketBase accepts space-separated datetimes without a trailing 'Z' in filter
# expressions (verified against releases.moe). Seconds precision is plenty here.
_PB_DATETIME_FMT = "%Y-%m-%d %H:%M:%S"

# Re-pull entries from one second before the newest one we've seen. Combined
# with an upsert this guarantees we never miss an update sharing the boundary
# timestamp, at the cost of re-fetching a handful of entries — cheap and safe.
_WATERMARK_SAFETY = timedelta(seconds=1)


class SeaDexMirror:
    """A local, incrementally-synced copy of the SeaDex entries collection."""

    def __init__(
        self,
        db_path,
        seadex,
        logger,
        log_line_length=80,
    ):
        """
        Args:
            db_path (str): Path to the SQLite mirror file.
            seadex: A ``seadex.SeaDexEntry`` client (reused from the caller so
                we share its HTTP client and base URL).
            logger: Logging instance.
            log_line_length (int, optional): Width for centred log lines.
                Defaults to 80.
        """

        self.db_path = db_path
        self.seadex = seadex
        self.logger = logger
        self.log_line_length = log_line_length

        db_dir = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(db_dir, exist_ok=True)

        self.conn = sqlite3.connect(db_path)
        self._init_db()

    def _init_db(self):
        """Create the entries/meta tables if they don't already exist."""

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                anilist_id INTEGER PRIMARY KEY,
                updated    TEXT,
                data       TEXT
            )
            """
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        self.conn.commit()

    def _get_meta(self, key):
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def _set_meta(self, key, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )

    def is_empty(self):
        """True if no entries are stored yet (first run / cleared mirror)."""

        return (
            self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        )

    def count(self):
        """Number of entries currently in the mirror."""

        return self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def _store(self, record):
        """Upsert a single EntryRecord, returning its updated_at datetime."""

        self.conn.execute(
            "INSERT OR REPLACE INTO entries (anilist_id, updated, data) "
            "VALUES (?, ?, ?)",
            (
                record.anilist_id,
                record.updated_at.strftime(_PB_DATETIME_FMT),
                record.to_json(indent=-1),
            ),
        )
        return record.updated_at

    def sync(self):
        """Refresh the mirror from SeaDex.

        Empty mirror → full bootstrap over every entry. Otherwise → incremental
        pull of everything updated at/after the stored watermark. On any network
        error we log and keep the existing copy (the run continues against the
        last good mirror).

        Returns:
            bool: True if the mirror is usable afterwards (synced, or a
                non-empty copy survived a failed sync); False only when there is
                no usable data at all (cold start during an outage).
        """

        watermark = self._get_meta("watermark")
        bootstrapping = self.is_empty() or watermark is None

        try:
            if bootstrapping:
                self.logger.info(
                    centred_string(
                        "Bootstrapping local SeaDex mirror (first run)",
                        total_length=self.log_line_length,
                    )
                )
                records = self.seadex.iterator()
            else:
                records = self.seadex.from_filter(f'updated>="{watermark}"')

            newest = None
            n = 0
            for record in records:
                updated_at = self._store(record)
                if newest is None or updated_at > newest:
                    newest = updated_at
                n += 1

            # Advance the watermark to just before the newest entry seen so the
            # next incremental pull re-checks the boundary. If nothing changed,
            # leave the existing watermark untouched.
            if newest is not None:
                new_watermark = (newest - _WATERMARK_SAFETY).strftime(
                    _PB_DATETIME_FMT
                )
                self._set_meta("watermark", new_watermark)

            self.conn.commit()

            self.logger.info(
                centred_string(
                    f"SeaDex mirror synced: {n} entr"
                    f"{'y' if n == 1 else 'ies'} updated, "
                    f"{self.count()} total",
                    total_length=self.log_line_length,
                )
            )
            return True

        except Exception as e:
            # Roll back any partial write so a half-finished page can't corrupt
            # the watermark/contents, then fall back to the existing copy.
            self.conn.rollback()

            if self.is_empty():
                self.logger.error(
                    centred_string(
                        f"SeaDex mirror sync failed and no local copy exists: {e}",
                        total_length=self.log_line_length,
                    )
                )
                return False

            self.logger.warning(
                centred_string(
                    f"SeaDex mirror sync failed ({e}); "
                    f"using last good copy ({self.count()} entries)",
                    total_length=self.log_line_length,
                )
            )
            return True

    def get(self, al_id):
        """Return the mirrored EntryRecord for an AniList ID, or None.

        Args:
            al_id (int): AniList ID.
        """

        row = self.conn.execute(
            "SELECT data FROM entries WHERE anilist_id = ?", (int(al_id),)
        ).fetchone()
        if row is None:
            return None
        return EntryRecord.from_json(row[0])

    def close(self):
        """Close the underlying SQLite connection."""

        self.conn.close()

    def __del__(self):
        # In scheduled mode a fresh arr (and mirror) is built every cycle; close
        # the connection when this one is collected so handles don't pile up over
        # a long-running process. getattr guards a connect() that never happened.
        conn = getattr(self, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
