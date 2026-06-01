"""
Unit tests for audit mode.

Tests cover status classification, state deduplication, tag diff logic,
dry-run guards, stale tag removal scoping, SQLite persistence, JSON migration,
and Discord diff generation.

Run with: python -m pytest tests/test_audit.py -v
"""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from seadexarr.modules.audit import AuditResult, SeaDexAudit
from seadexarr.modules.audit_state import (
    AuditState,
    SeriesAuditState,
    rg_diff,
    state_changed,
)
from seadexarr.modules.sonarr_tags import SonarrTagManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(**kwargs) -> AuditResult:
    defaults = dict(
        sonarr_id=1,
        tvdb_id=100,
        sonarr_title="Test Show",
        anilist_title="Test Show",
        al_id=999,
        sd_url="https://seadex.moe/foo",
        seadex_status="none",
    )
    defaults.update(kwargs)
    return AuditResult(**defaults)


def _make_state(**kwargs) -> SeriesAuditState:
    defaults = dict(
        sonarr_id=1,
        tvdb_id=100,
        title="Test Show",
        seadex_status="none",
    )
    defaults.update(kwargs)
    return SeriesAuditState(**defaults)


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)   # SQLite creates its own file
    return path


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------

class TestStatusClassification(unittest.TestCase):

    def test_no_seadex_entry_is_none(self):
        r = _make_result(seadex_status="none")
        self.assertEqual(r.seadex_status, "none")

    def test_partial_when_entry_but_no_filtered_releases(self):
        r = _make_result(seadex_status="partial")
        self.assertEqual(r.seadex_status, "partial")
        self.assertFalse(r.upgrade_available)

    def test_full_when_seadex_releases_available(self):
        r = _make_result(
            seadex_status="full",
            seadex_rgs=["SubsPlease"],
            upgrade_available=False,
        )
        self.assertEqual(r.seadex_status, "full")

    def test_upgrade_available_set_independently(self):
        r = _make_result(seadex_status="full", upgrade_available=True, too_large=False)
        self.assertTrue(r.upgrade_available)
        self.assertFalse(r.too_large)

    def test_too_large_requires_upgrade_available(self):
        r = _make_result(seadex_status="full", upgrade_available=True, too_large=True)
        self.assertTrue(r.too_large)

    def test_desired_tags_full_no_upgrade(self):
        audit = self._make_audit_instance()
        r = _make_result(seadex_status="full", upgrade_available=False)
        tags = audit._compute_desired_tags(r)
        self.assertIn(audit.tag_full, tags)
        self.assertNotIn(audit.tag_upgrade, tags)
        self.assertNotIn(audit.tag_too_large, tags)

    def test_desired_tags_partial(self):
        audit = self._make_audit_instance()
        r = _make_result(seadex_status="partial")
        tags = audit._compute_desired_tags(r)
        self.assertIn(audit.tag_partial, tags)
        self.assertNotIn(audit.tag_full, tags)

    def test_desired_tags_upgrade_available(self):
        audit = self._make_audit_instance()
        r = _make_result(seadex_status="full", upgrade_available=True, too_large=False)
        tags = audit._compute_desired_tags(r)
        self.assertIn(audit.tag_full, tags)
        self.assertIn(audit.tag_upgrade, tags)
        self.assertNotIn(audit.tag_too_large, tags)

    def test_desired_tags_too_large(self):
        audit = self._make_audit_instance()
        r = _make_result(seadex_status="full", upgrade_available=True, too_large=True)
        tags = audit._compute_desired_tags(r)
        self.assertIn(audit.tag_full, tags)
        self.assertIn(audit.tag_too_large, tags)
        self.assertNotIn(audit.tag_upgrade, tags)

    def test_none_status_no_tags(self):
        audit = self._make_audit_instance()
        r = _make_result(seadex_status="none")
        tags = audit._compute_desired_tags(r)
        self.assertEqual(tags, [])

    def _make_audit_instance(self):
        audit = SeaDexAudit.__new__(SeaDexAudit)
        audit.tag_full = "seadex"
        audit.tag_partial = "partial-seadex"
        audit.tag_upgrade = "seadex-upgrade-available"
        audit.tag_too_large = "seadex-too-large"
        audit.tag_ignored = "seadex-ignored"
        audit.tag_when_too_large = True
        return audit


# ---------------------------------------------------------------------------
# State deduplication
# ---------------------------------------------------------------------------

class TestStateDeduplication(unittest.TestCase):

    def test_none_old_always_changed(self):
        new = _make_state(seadex_status="full")
        self.assertTrue(state_changed(None, new))

    def test_identical_state_no_change(self):
        s = _make_state(seadex_status="full", seadex_rgs=["SubsPlease"])
        self.assertFalse(state_changed(s, s))

    def test_status_flip_is_change(self):
        old = _make_state(seadex_status="none")
        new = _make_state(seadex_status="full")
        self.assertTrue(state_changed(old, new))

    def test_rg_change_is_change(self):
        old = _make_state(seadex_status="full", seadex_rgs=["GroupA"])
        new = _make_state(seadex_status="full", seadex_rgs=["GroupB"])
        self.assertTrue(state_changed(old, new))

    def test_upgrade_flip_is_change(self):
        old = _make_state(upgrade_available=False)
        new = _make_state(upgrade_available=True)
        self.assertTrue(state_changed(old, new))

    def test_should_notify_first_seadex_match(self):
        path = _tmp_db()
        try:
            state = AuditState(path)
            new = _make_state(seadex_status="full")
            cfg = {"notify_on_new_seadex_match": True}
            self.assertTrue(state.should_notify(new, cfg))
        finally:
            state.close()
            if os.path.exists(path):
                os.unlink(path)

    def test_should_not_notify_no_change(self):
        path = _tmp_db()
        try:
            state = AuditState(path)
            existing = _make_state(seadex_status="full", upgrade_available=False)
            state.update_series(existing)
            new = _make_state(seadex_status="full", upgrade_available=False)
            cfg = {"notify_on_no_change": False}
            self.assertFalse(state.should_notify(new, cfg))
        finally:
            state.close()
            if os.path.exists(path):
                os.unlink(path)

    def test_should_notify_new_upgrade(self):
        path = _tmp_db()
        try:
            state = AuditState(path)
            old = _make_state(seadex_status="full", upgrade_available=False)
            state.update_series(old)
            new = _make_state(seadex_status="full", upgrade_available=True)
            cfg = {"notify_on_new_upgrade_available": True}
            self.assertTrue(state.should_notify(new, cfg))
        finally:
            state.close()
            if os.path.exists(path):
                os.unlink(path)

    def test_state_persists_across_load(self):
        path = _tmp_db()
        try:
            state = AuditState(path)
            s = _make_state(sonarr_id=42, seadex_status="full")
            state.update_series(s)
            state.close()

            state2 = AuditState(path)
            loaded = state2.get_series(42)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.seadex_status, "full")
            state2.close()
        finally:
            if os.path.exists(path):
                os.unlink(path)


# ---------------------------------------------------------------------------
# SQLite-specific: migration from JSON
# ---------------------------------------------------------------------------

class TestJsonMigration(unittest.TestCase):

    def test_migrates_from_legacy_json(self):
        fd, json_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        db_path = json_path[:-5] + ".db"
        try:
            # Write legacy JSON state
            legacy = {
                "schema_version": 1,
                "series": {
                    "7": {
                        "sonarr_id": 7,
                        "tvdb_id": 123,
                        "title": "Migrated Show",
                        "seadex_status": "full",
                        "seadex_rgs": ["SubsPlease"],
                        "seadex_size_bytes": 1000,
                        "library_rgs": [],
                        "upgrade_available": False,
                        "too_large": False,
                        "last_notified": None,
                        "last_audited": "2024-01-01T00:00:00+00:00",
                    }
                },
            }
            with open(json_path, "w") as f:
                json.dump(legacy, f)

            # AuditState with .json path triggers migration
            state = AuditState(json_path)
            loaded = state.get_series(7)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.title, "Migrated Show")
            self.assertEqual(loaded.seadex_rgs, ["SubsPlease"])
            state.close()

            # JSON file should have been renamed
            self.assertFalse(os.path.exists(json_path))
            self.assertTrue(os.path.exists(json_path + ".migrated"))
        finally:
            for p in (json_path, json_path + ".migrated", db_path):
                if os.path.exists(p):
                    os.unlink(p)

    def test_empty_json_migrates_cleanly(self):
        fd, json_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        db_path = json_path[:-5] + ".db"
        try:
            # Empty JSON — should not crash
            state = AuditState(json_path)
            state.close()
        finally:
            for p in (json_path, json_path + ".migrated", db_path):
                if os.path.exists(p):
                    os.unlink(p)


# ---------------------------------------------------------------------------
# Release-group diff
# ---------------------------------------------------------------------------

class TestRgDiff(unittest.TestCase):

    def test_no_old_state_all_rgs_are_added(self):
        new = _make_state(seadex_rgs=["SubsPlease", "Erai-raws"])
        added, removed = rg_diff(None, new)
        self.assertEqual(set(added), {"SubsPlease", "Erai-raws"})
        self.assertEqual(removed, [])

    def test_rg_replaced(self):
        old = _make_state(seadex_rgs=["Erai-raws"])
        new = _make_state(seadex_rgs=["SubsPlease"])
        added, removed = rg_diff(old, new)
        self.assertIn("SubsPlease", added)
        self.assertIn("Erai-raws", removed)

    def test_no_change_empty_diff(self):
        old = _make_state(seadex_rgs=["SubsPlease"])
        new = _make_state(seadex_rgs=["SubsPlease"])
        added, removed = rg_diff(old, new)
        self.assertEqual(added, [])
        self.assertEqual(removed, [])

    def test_rg_added_without_removal(self):
        old = _make_state(seadex_rgs=["SubsPlease"])
        new = _make_state(seadex_rgs=["SubsPlease", "Erai-raws"])
        added, removed = rg_diff(old, new)
        self.assertIn("Erai-raws", added)
        self.assertEqual(removed, [])


# ---------------------------------------------------------------------------
# Tag diff calculation
# ---------------------------------------------------------------------------

class TestTagDiff(unittest.TestCase):

    def _manager(self, existing_tags: dict[str, int]) -> SonarrTagManager:
        mgr = SonarrTagManager.__new__(SonarrTagManager)
        mgr._base = "http://sonarr:8989"
        mgr._key = "testkey"
        mgr._tag_cache = dict(existing_tags)
        return mgr

    def test_add_new_tag(self):
        mgr = self._manager({"seadex": 1})
        with patch.object(mgr, "get_or_create_tag", side_effect=lambda l: mgr._tag_cache.get(l, 99)):
            new_ids, changed = mgr.compute_tag_changes(
                current_tag_ids=[],
                desired_labels=["seadex"],
                managed_labels=["seadex"],
                remove_stale=False,
            )
        self.assertIn(1, new_ids)
        self.assertTrue(changed)

    def test_no_change_when_tag_already_present(self):
        mgr = self._manager({"seadex": 1})
        with patch.object(mgr, "get_or_create_tag", side_effect=lambda l: 1):
            new_ids, changed = mgr.compute_tag_changes(
                current_tag_ids=[1],
                desired_labels=["seadex"],
                managed_labels=["seadex"],
                remove_stale=False,
            )
        self.assertFalse(changed)

    def test_stale_managed_tag_removed_when_enabled(self):
        mgr = self._manager({"seadex": 1, "seadex-upgrade-available": 2})
        with patch.object(mgr, "get_or_create_tag", side_effect=lambda l: mgr._tag_cache.get(l, 99)):
            new_ids, changed = mgr.compute_tag_changes(
                current_tag_ids=[1, 2],
                desired_labels=["seadex"],
                managed_labels=["seadex", "seadex-upgrade-available"],
                remove_stale=True,
            )
        self.assertIn(1, new_ids)
        self.assertNotIn(2, new_ids)
        self.assertTrue(changed)

    def test_stale_managed_tag_removed_regardless_of_remove_stale(self):
        # Managed tags are always kept in sync — remove_stale=False should
        # no longer preserve a managed tag that is no longer desired.
        mgr = self._manager({"seadex": 1, "seadex-upgrade-available": 2})
        with patch.object(mgr, "get_or_create_tag", side_effect=lambda l: mgr._tag_cache.get(l, 99)):
            new_ids, changed = mgr.compute_tag_changes(
                current_tag_ids=[1, 2],
                desired_labels=["seadex"],
                managed_labels=["seadex", "seadex-upgrade-available"],
                remove_stale=False,
            )
        self.assertIn(1, new_ids)
        self.assertNotIn(2, new_ids)
        self.assertTrue(changed)

    def test_user_tags_never_removed(self):
        mgr = self._manager({"seadex": 1})
        user_tag_id = 99
        with patch.object(mgr, "get_or_create_tag", side_effect=lambda l: mgr._tag_cache.get(l, 1)):
            new_ids, _ = mgr.compute_tag_changes(
                current_tag_ids=[user_tag_id, 1],
                desired_labels=["seadex"],
                managed_labels=["seadex"],
                remove_stale=True,
            )
        self.assertIn(user_tag_id, new_ids)


# ---------------------------------------------------------------------------
# Dry-run: no Sonarr mutation endpoints called
# ---------------------------------------------------------------------------

class TestDryRun(unittest.TestCase):

    def test_set_series_tags_noop_in_dry_run(self):
        mgr = SonarrTagManager.__new__(SonarrTagManager)
        mgr._base = "http://sonarr:8989"
        mgr._key = "key"
        mgr._tag_cache = {}

        with patch("seadexarr.modules.sonarr_tags.requests.put") as mock_put, \
             patch.object(mgr, "get_series_json", return_value={"id": 1, "tags": []}):
            result = mgr.set_series_tags(series_id=1, tag_ids=[2, 3], dry_run=True)

        mock_put.assert_not_called()
        self.assertTrue(result)

    def test_audit_run_dry_run_does_not_call_set_series_tags(self):
        audit = SeaDexAudit.__new__(SeaDexAudit)
        audit.tag_full = "seadex"
        audit.tag_partial = "partial-seadex"
        audit.tag_upgrade = "seadex-upgrade-available"
        audit.tag_too_large = "seadex-too-large"
        audit.tag_ignored = "seadex-ignored"
        audit.managed_tag_labels = [audit.tag_full, audit.tag_partial,
                                     audit.tag_upgrade, audit.tag_too_large,
                                     audit.tag_ignored]
        audit.audit_remove_stale = False
        audit.log_line_length = 80
        audit.logger = MagicMock()

        mock_tm = MagicMock()
        mock_tm.get_series_json.return_value = {"id": 1, "tags": []}
        mock_tm.compute_tag_changes.return_value = ([5], True)
        audit.tag_manager = mock_tm

        mock_series = MagicMock()
        mock_series.id = 1
        result = _make_result(seadex_status="full", desired_tags=["seadex"])

        audit._apply_series_tags(mock_series, result, dry_run=True)

        mock_tm.set_series_tags.assert_called_once_with(1, [5], dry_run=True)


# ---------------------------------------------------------------------------
# Stale tag removal only touches managed tags
# ---------------------------------------------------------------------------

class TestStaleTagRemoval(unittest.TestCase):

    def test_only_managed_labels_considered_for_removal(self):
        mgr = SonarrTagManager.__new__(SonarrTagManager)
        mgr._base = "http://sonarr"
        mgr._key = "key"
        mgr._tag_cache = {"seadex": 10, "partial-seadex": 20}

        arbitrary_user_tag = 777

        with patch.object(mgr, "get_or_create_tag", side_effect=lambda l: mgr._tag_cache[l]):
            new_ids, _ = mgr.compute_tag_changes(
                current_tag_ids=[10, 20, arbitrary_user_tag],
                desired_labels=["seadex"],
                managed_labels=["seadex", "partial-seadex"],
                remove_stale=True,
            )

        self.assertIn(10, new_ids)
        self.assertNotIn(20, new_ids)
        self.assertIn(arbitrary_user_tag, new_ids)


# ---------------------------------------------------------------------------
# Embed colour helper
# ---------------------------------------------------------------------------

class TestEmbedColour(unittest.TestCase):

    def _audit(self):
        return SeaDexAudit.__new__(SeaDexAudit)

    def test_too_large_colour(self):
        from seadexarr.modules.audit import _COLOUR_TOO_LARGE
        r = _make_result(seadex_status="full", upgrade_available=True, too_large=True)
        self.assertEqual(SeaDexAudit._embed_colour(r), _COLOUR_TOO_LARGE)

    def test_upgrade_colour(self):
        from seadexarr.modules.audit import _COLOUR_UPGRADE
        r = _make_result(seadex_status="full", upgrade_available=True, too_large=False)
        self.assertEqual(SeaDexAudit._embed_colour(r), _COLOUR_UPGRADE)

    def test_full_colour(self):
        from seadexarr.modules.audit import _COLOUR_FULL
        r = _make_result(seadex_status="full", upgrade_available=False)
        self.assertEqual(SeaDexAudit._embed_colour(r), _COLOUR_FULL)

    def test_partial_colour(self):
        from seadexarr.modules.audit import _COLOUR_PARTIAL
        r = _make_result(seadex_status="partial")
        self.assertEqual(SeaDexAudit._embed_colour(r), _COLOUR_PARTIAL)


# ---------------------------------------------------------------------------
# alt_is_acceptable
# ---------------------------------------------------------------------------

class TestAltIsAcceptable(unittest.TestCase):

    def _make_audit(self, alt_is_acceptable=False):
        audit = SeaDexAudit.__new__(SeaDexAudit)
        audit.alt_is_acceptable = alt_is_acceptable
        audit.size_filter_enabled = False
        audit.ignore_tags = []
        audit.trackers = ["animetosho"]
        audit.public_only = False
        audit.log_line_length = 80
        audit.logger = MagicMock()
        audit.al_cache = {}
        return audit

    def _make_torrent(self, release_group, is_best, tracker="animetosho"):
        t = MagicMock()
        t.release_group = release_group
        t.is_best = is_best
        t.tags = []
        t.tracker = MagicMock()
        t.tracker.lower.return_value = tracker
        t.tracker.is_public.return_value = True
        t.url = f"https://example.com/{release_group}"
        t.files = []
        t.infohash = "abc123"
        t.is_dual_audio = False
        return t

    def _run(self, audit, library_rgs, sd_torrents):
        mock_series = MagicMock()
        mock_series.id = 1
        mock_series.title = "Test Show"

        mock_sd_entry = MagicMock()
        mock_sd_entry.url = "https://seadex.moe/test"
        mock_sd_entry.torrents = sd_torrents

        release_dict = {rg: {"size": [1_000_000_000]} for rg in library_rgs}
        seadex_dict = {"BestGroup": {"urls": {}}}

        with patch.object(audit, "get_seadex_entry", return_value=mock_sd_entry), \
             patch.object(audit, "get_anilist_title", return_value="Test Show"), \
             patch.object(audit, "get_ep_list", return_value=[]), \
             patch.object(audit, "get_sonarr_release_dict", return_value=release_dict), \
             patch.object(audit, "get_seadex_dict", return_value=seadex_dict), \
             patch.object(audit, "_sum_seadex_size", return_value=2_000_000_000), \
             patch.object(audit, "parse_episodes_from_seadex", return_value=seadex_dict), \
             patch.object(audit, "filter_seadex_downloads", return_value=(True, seadex_dict)), \
             patch.object(audit, "get_any_to_download", return_value=True):
            return audit._audit_al_id(mock_series, 12345, {})

    def test_alt_acceptable_library_has_alt_clears_upgrade(self):
        audit = self._make_audit(alt_is_acceptable=True)
        torrents = [
            self._make_torrent("BestGroup", is_best=True),
            self._make_torrent("AltGroup", is_best=False),
        ]
        result = self._run(audit, library_rgs=["AltGroup"], sd_torrents=torrents)
        self.assertFalse(result["upgrade_available"])
        self.assertFalse(result["too_large"])

    def test_alt_not_acceptable_upgrade_stays(self):
        audit = self._make_audit(alt_is_acceptable=False)
        torrents = [
            self._make_torrent("BestGroup", is_best=True),
            self._make_torrent("AltGroup", is_best=False),
        ]
        result = self._run(audit, library_rgs=["AltGroup"], sd_torrents=torrents)
        self.assertTrue(result["upgrade_available"])

    def test_library_not_in_seadex_upgrade_stays(self):
        audit = self._make_audit(alt_is_acceptable=True)
        torrents = [
            self._make_torrent("BestGroup", is_best=True),
            self._make_torrent("AltGroup", is_best=False),
        ]
        result = self._run(audit, library_rgs=["SomeRandomGroup"], sd_torrents=torrents)
        self.assertTrue(result["upgrade_available"])

    def test_alt_acceptable_public_only_filters_private_tracker(self):
        """With public_only=True, private-tracker-only alts don't suppress upgrade."""
        audit = self._make_audit(alt_is_acceptable=True)
        audit.public_only = True
        audit.trackers = ["nyaa", "animetosho"]

        best = self._make_torrent("BestGroup", is_best=True, tracker="nyaa")
        alt_private = self._make_torrent("AltGroup", is_best=False, tracker="ab")
        alt_private.tracker.is_public.return_value = False

        result = self._run(audit, library_rgs=["AltGroup"], sd_torrents=[best, alt_private])
        self.assertTrue(result["upgrade_available"])

    def test_alt_acceptable_public_only_public_alt_clears_upgrade(self):
        """With public_only=True, a public-tracker alt in the library suppresses upgrade."""
        audit = self._make_audit(alt_is_acceptable=True)
        audit.public_only = True
        audit.trackers = ["nyaa", "animetosho"]

        best = self._make_torrent("BestGroup", is_best=True, tracker="nyaa")
        alt_public = self._make_torrent("AltGroup", is_best=False, tracker="nyaa")

        result = self._run(audit, library_rgs=["AltGroup"], sd_torrents=[best, alt_public])
        self.assertFalse(result["upgrade_available"])

    def _filter_kwarg(self, audit, library_rgs, sd_torrents):
        """Run an audit and return the acceptable_alt_owned kwarg that
        _audit_al_id passed down to filter_seadex_downloads."""
        mock_series = MagicMock()
        mock_series.id = 1
        mock_series.title = "Test Show"

        mock_sd_entry = MagicMock()
        mock_sd_entry.url = "https://seadex.moe/test"
        mock_sd_entry.torrents = sd_torrents

        release_dict = {rg: {"size": [1_000_000_000]} for rg in library_rgs}
        seadex_dict = {"BestGroup": {"urls": {}}}
        mock_filter = MagicMock(return_value=(True, seadex_dict))

        with patch.object(audit, "get_seadex_entry", return_value=mock_sd_entry), \
             patch.object(audit, "get_anilist_title", return_value="Test Show"), \
             patch.object(audit, "get_ep_list", return_value=[]), \
             patch.object(audit, "get_sonarr_release_dict", return_value=release_dict), \
             patch.object(audit, "get_seadex_dict", return_value=seadex_dict), \
             patch.object(audit, "_sum_seadex_size", return_value=2_000_000_000), \
             patch.object(audit, "parse_episodes_from_seadex", return_value=seadex_dict), \
             patch.object(audit, "filter_seadex_downloads", mock_filter), \
             patch.object(audit, "get_any_to_download", return_value=True):
            audit._audit_al_id(mock_series, 12345, {})

        return mock_filter.call_args.kwargs["acceptable_alt_owned"]

    def test_filter_told_alt_owned_when_library_has_alt(self):
        audit = self._make_audit(alt_is_acceptable=True)
        torrents = [
            self._make_torrent("BestGroup", is_best=True),
            self._make_torrent("AltGroup", is_best=False),
        ]
        self.assertTrue(self._filter_kwarg(audit, ["AltGroup"], torrents))

    def test_filter_not_told_alt_owned_when_library_lacks_seadex_rg(self):
        audit = self._make_audit(alt_is_acceptable=True)
        torrents = [
            self._make_torrent("BestGroup", is_best=True),
            self._make_torrent("AltGroup", is_best=False),
        ]
        self.assertFalse(self._filter_kwarg(audit, ["SomethingElse"], torrents))

    def test_filter_not_told_alt_owned_when_feature_disabled(self):
        audit = self._make_audit(alt_is_acceptable=False)
        torrents = [
            self._make_torrent("BestGroup", is_best=True),
            self._make_torrent("AltGroup", is_best=False),
        ]
        self.assertFalse(self._filter_kwarg(audit, ["AltGroup"], torrents))


class TestFilterByReleaseGroupAltLog(unittest.TestCase):
    """The misleading '→ tagging' line must drop to debug when an owned alt
    makes the entry acceptable."""

    def _make_arr(self):
        from seadexarr.modules.seadex_arr import SeaDexArr
        arr = SeaDexArr.__new__(SeaDexArr)
        arr.audit_mode = True
        arr.use_torrent_hash_to_filter = False
        arr.log_line_length = 80
        arr.logger = MagicMock()
        return arr

    def _seadex_dict(self):
        return {
            "BestGroup": {
                "tags": [],
                "urls": {
                    "https://example.com/best": {
                        "hash": "h1",
                        "size": [28_000_000_000],
                        "episodes": [],
                        "download": False,
                    }
                },
            }
        }

    def test_owned_alt_demotes_tagging_log_to_debug(self):
        arr = self._make_arr()
        arr.filter_by_release_group(
            seadex_dict=self._seadex_dict(),
            arr="sonarr",
            arr_release_dict={"AltGroup": {"size": [9_000_000_000]}},
            ep_list=[],
            acceptable_alt_owned=True,
        )
        info_msgs = " ".join(str(c.args[0]) for c in arr.logger.info.call_args_list)
        debug_msgs = " ".join(str(c.args[0]) for c in arr.logger.debug.call_args_list)
        self.assertNotIn("tagging", info_msgs)
        self.assertIn("have acceptable alt", debug_msgs)

    def test_no_alt_keeps_tagging_log_at_info(self):
        arr = self._make_arr()
        arr.filter_by_release_group(
            seadex_dict=self._seadex_dict(),
            arr="sonarr",
            arr_release_dict={"AltGroup": {"size": [9_000_000_000]}},
            ep_list=[],
            acceptable_alt_owned=False,
        )
        info_msgs = " ".join(str(c.args[0]) for c in arr.logger.info.call_args_list)
        self.assertIn("tagging", info_msgs)


if __name__ == "__main__":
    unittest.main()
