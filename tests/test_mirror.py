"""
Unit tests for the local SeaDex mirror.

Covers the sync state machine (bootstrap vs incremental), the watermark
advance, round-trip fidelity of stored EntryRecords, and the
fall-back-to-last-good-copy behaviour when a sync fails.

Run with: python -m pytest tests/test_mirror.py -v
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from seadex import EntryRecord
from seadex._enums import Tracker
from seadex._types import File, TorrentRecord

from seadexarr.modules.seadex_mirror import SeaDexMirror


def _make_torrent(group="Moxie", tracker=Tracker.NYAA, is_best=True):
    return TorrentRecord(
        collection_id="c",
        collection_name="torrents",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        is_dual_audio=True,
        files=(File(name="ep01.mkv", size=100), File(name="ep02.mkv", size=200)),
        id="t1",
        infohash="9dee656eb031c0ef34ada84095b0aad3748d69d7",
        is_best=is_best,
        release_group=group,
        tags=frozenset(),
        tracker=tracker,
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        url="https://nyaa.si/view/1",
        grouped_url=None,
        size=300,
    )


def _make_entry(al_id, updated_at, group="Moxie"):
    return EntryRecord(
        anilist_id=al_id,
        collection_id="c",
        collection_name="entries",
        comparisons=(),
        created_at=datetime(2023, 1, 1, tzinfo=timezone.utc),
        id=f"e{al_id}",
        is_incomplete=False,
        notes="some notes",
        theoretical_best=None,
        torrents=(_make_torrent(group=group),),
        updated_at=updated_at,
        url=f"https://releases.moe/{al_id}/",
        size=300,
    )


class _MirrorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "mirror.db")
        self.seadex = MagicMock()
        self.logger = MagicMock()
        self._mirrors = []

    def tearDown(self):
        # Close every connection before cleanup or Windows can't unlink the DB.
        for m in self._mirrors:
            m.close()
        self.tmp.cleanup()

    def _mirror(self):
        m = SeaDexMirror(
            db_path=self.db_path,
            seadex=self.seadex,
            logger=self.logger,
        )
        self._mirrors.append(m)
        return m


class TestBootstrap(_MirrorTestCase):
    def test_empty_mirror_bootstraps_via_iterator(self):
        entries = [
            _make_entry(110130, datetime(2026, 5, 31, 22, 10, 18, tzinfo=timezone.utc)),
            _make_entry(164, datetime(2026, 5, 31, 14, 6, 53, tzinfo=timezone.utc)),
        ]
        self.seadex.iterator.return_value = iter(entries)

        m = self._mirror()
        ok = m.sync()

        self.assertTrue(ok)
        self.seadex.iterator.assert_called_once()
        self.seadex.from_filter.assert_not_called()
        self.assertEqual(m.count(), 2)
        # Watermark recorded so the next sync goes incremental.
        self.assertIsNotNone(m._get_meta("watermark"))

    def test_get_returns_roundtripped_entry(self):
        entry = _make_entry(
            110130, datetime(2026, 5, 31, 22, 10, 18, tzinfo=timezone.utc)
        )
        self.seadex.iterator.return_value = iter([entry])

        m = self._mirror()
        m.sync()
        got = m.get(110130)

        self.assertIsInstance(got, EntryRecord)
        self.assertEqual(got.anilist_id, 110130)
        self.assertEqual(got.notes, "some notes")
        # Torrent / file / enum fidelity survives the JSON round-trip.
        self.assertEqual(len(got.torrents), 1)
        t = got.torrents[0]
        self.assertEqual(t.release_group, "Moxie")
        self.assertIs(t.tracker, Tracker.NYAA)
        self.assertTrue(t.tracker.is_public())
        self.assertEqual([f.name for f in t.files], ["ep01.mkv", "ep02.mkv"])
        self.assertEqual(t.files[1].size, 200)

    def test_get_miss_returns_none(self):
        self.seadex.iterator.return_value = iter([_make_entry(1, datetime.now(timezone.utc))])
        m = self._mirror()
        m.sync()
        self.assertIsNone(m.get(999999))


class TestIncremental(_MirrorTestCase):
    def test_second_sync_uses_filter_and_upserts(self):
        self.seadex.iterator.return_value = iter(
            [_make_entry(110130, datetime(2026, 5, 30, tzinfo=timezone.utc), group="Old")]
        )
        m = self._mirror()
        m.sync()
        self.assertEqual(m.get(110130).torrents[0].release_group, "Old")

        # A fresh mirror object over the same DB should now go incremental.
        m2 = self._mirror()
        self.seadex.from_filter.return_value = iter(
            [_make_entry(110130, datetime(2026, 6, 1, tzinfo=timezone.utc), group="New")]
        )
        ok = m2.sync()

        self.assertTrue(ok)
        self.seadex.from_filter.assert_called_once()
        # filter string is the watermark query, not None / iterator()
        (called_filter,), _ = self.seadex.from_filter.call_args
        self.assertIn('updated>=', called_filter.replace(" ", ""))
        # Upserted, not duplicated.
        self.assertEqual(m2.count(), 1)
        self.assertEqual(m2.get(110130).torrents[0].release_group, "New")

    def test_no_changes_leaves_watermark(self):
        self.seadex.iterator.return_value = iter(
            [_make_entry(1, datetime(2026, 6, 1, tzinfo=timezone.utc))]
        )
        m = self._mirror()
        m.sync()
        wm = m._get_meta("watermark")

        m2 = self._mirror()
        self.seadex.from_filter.return_value = iter([])  # nothing changed
        m2.sync()

        self.assertEqual(m2._get_meta("watermark"), wm)


class TestFailureFallback(_MirrorTestCase):
    def test_failed_sync_keeps_last_good_copy(self):
        self.seadex.iterator.return_value = iter(
            [_make_entry(1, datetime(2026, 6, 1, tzinfo=timezone.utc))]
        )
        m = self._mirror()
        m.sync()

        m2 = self._mirror()
        self.seadex.from_filter.side_effect = RuntimeError("SeaDex down")
        ok = m2.sync()

        # Usable copy survived → True, entry still served.
        self.assertTrue(ok)
        self.assertEqual(m2.count(), 1)
        self.assertIsNotNone(m2.get(1))

    def test_failed_cold_start_returns_false(self):
        self.seadex.iterator.side_effect = RuntimeError("SeaDex down")
        m = self._mirror()
        ok = m.sync()

        self.assertFalse(ok)
        self.assertTrue(m.is_empty())


if __name__ == "__main__":
    unittest.main()
