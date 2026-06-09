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
# Notification rendering (verdict headline + plain-English summary)
# ---------------------------------------------------------------------------

class TestNotificationRendering(unittest.TestCase):

    def _audit(self):
        audit = SeaDexAudit.__new__(SeaDexAudit)
        audit.tag_full = "seadex"
        audit.tag_partial = "partial-seadex"
        audit.tag_upgrade = "seadex-upgrade-available"
        audit.tag_too_large = "seadex-too-large"
        audit.tag_when_too_large = True
        audit.al_cache = {}
        return audit

    # -- _verdict: headline answers "do I act?", actionable states win --------

    def test_verdict_full_no_action(self):
        emoji, verdict = SeaDexAudit._verdict(
            _make_result(seadex_status="full", upgrade_available=False)
        )
        self.assertEqual(emoji, "🟢")
        self.assertIn("nothing to do", verdict)

    def test_verdict_upgrade_leads_over_full(self):
        # full coverage but an upgrade exists -> headline must lead with action
        emoji, verdict = SeaDexAudit._verdict(
            _make_result(seadex_status="full", upgrade_available=True)
        )
        self.assertEqual(emoji, "🟠")
        self.assertIn("better release", verdict)

    def test_verdict_too_large_leads(self):
        emoji, verdict = SeaDexAudit._verdict(
            _make_result(seadex_status="full", upgrade_available=True, too_large=True)
        )
        self.assertEqual(emoji, "🔴")
        self.assertIn("too large", verdict)

    def test_verdict_none(self):
        emoji, verdict = SeaDexAudit._verdict(_make_result(seadex_status="none"))
        self.assertEqual(emoji, "⚪")

    # -- _summary_sentence: plain English, group names only with context ------

    def test_sentence_new_full_match(self):
        audit = self._audit()
        r = _make_result(
            seadex_status="full", seadex_rgs=["uba"], library_rgs=["Netaro"]
        )
        sentence = audit._summary_sentence(r, old_state=None)
        self.assertIn("now tracks", sentence)
        self.assertIn("Netaro", sentence)

    def test_sentence_list_changed_still_covered(self):
        # The exact screenshot case: full coverage, SeaDex added a group (+uba),
        # library group (Netaro) differs but still satisfies -> no false alarm.
        audit = self._audit()
        old = _make_state(seadex_status="full", seadex_rgs=[], library_rgs=["Netaro"])
        r = _make_result(
            seadex_status="full", seadex_rgs=["uba"], library_rgs=["Netaro"]
        )
        sentence = audit._summary_sentence(r, old_state=old)
        self.assertIn("added uba", sentence)
        self.assertIn("no action needed", sentence)
        # never echo the misleading bare side-by-side compare
        self.assertNotIn("Coverage:", sentence)

    def test_sentence_upgrade_available(self):
        audit = self._audit()
        r = _make_result(seadex_status="full", upgrade_available=True)
        sentence = audit._summary_sentence(r, old_state=None)
        self.assertIn("recommends a release you don't have", sentence)
        self.assertIn(audit.tag_upgrade, sentence)

    def test_sentence_too_large(self):
        audit = self._audit()
        r = _make_result(
            seadex_status="full",
            upgrade_available=True,
            too_large=True,
            seadex_size_bytes=64 * 1024**3,
            library_size_bytes=24 * 1024**3,
        )
        sentence = audit._summary_sentence(r, old_state=None)
        self.assertIn("64.0 GB", sentence)
        self.assertIn(audit.tag_too_large, sentence)

    def test_sentence_partial(self):
        audit = self._audit()
        r = _make_result(seadex_status="partial")
        sentence = audit._summary_sentence(r, old_state=None)
        self.assertIn("no release passes your filters", sentence)

    # -- _item_label: name the matched part (season / movie / special) --------

    def test_item_label_movie(self):
        audit = self._audit()
        self.assertEqual(audit._item_label({"al_format": "MOVIE"}), "Movie")

    def test_item_label_special_and_ova(self):
        audit = self._audit()
        self.assertEqual(audit._item_label({"al_format": "SPECIAL"}), "Special")
        self.assertEqual(audit._item_label({"al_format": "OVA"}), "OVA")

    def test_item_label_season_from_tvdb(self):
        audit = self._audit()
        self.assertEqual(
            audit._item_label({"al_format": "TV", "tvdb_season": 2}), "Season 2"
        )

    def test_item_label_tvdb_season_zero_is_specials(self):
        audit = self._audit()
        self.assertEqual(
            audit._item_label({"al_format": "TV", "tvdb_season": 0}), "Specials"
        )

    def test_item_label_falls_back_to_title(self):
        audit = self._audit()
        self.assertEqual(
            audit._item_label({"tvdb_season": -1, "anilist_title": "Some OVA"}),
            "Some OVA",
        )

    # -- _build_embed: one field per matched item, covered ones included ------

    def _entry(self, **kw):
        base = dict(
            al_id=1, sd_url="https://seadex.moe/x", anilist_title="Item",
            al_format="TV", tvdb_season=1, seadex_status="full",
            seadex_rgs=["uba"], seadex_size_bytes=0, library_rgs=["Netaro"],
            library_size_bytes=0, upgrade_available=False, too_large=False,
            missing_episodes=[],
        )
        base.update(kw)
        return base

    @patch("seadexarr.modules.audit.get_anilist_thumb", return_value=(None, {}))
    def test_build_embed_lists_each_item_with_type(self, _thumb):
        audit = self._audit()
        r = _make_result(
            anilist_title="Blend S",
            seadex_status="full",
            upgrade_available=True,
            entries=[
                self._entry(al_id=1, al_format="TV", tvdb_season=1, library_rgs=["Netaro"]),
                self._entry(
                    al_id=2, al_format="MOVIE", tvdb_season=1, upgrade_available=True,
                    seadex_size_bytes=12 * 1024**3, library_size_bytes=9 * 1024**3,
                    missing_episodes=[(1, 1)],
                ),
            ],
        )
        embed = audit._build_embed(r, old_state=None)
        # headline counts actionable items across the series
        self.assertIn("1 of 2 items need action", embed["title"])
        names = [f["name"] for f in embed["fields"]]
        self.assertIn("Season 1", names)
        self.assertIn("Movie", names)
        # actionable item sorts first
        self.assertEqual(embed["fields"][0]["name"], "Movie")
        self.assertIn("missing", embed["fields"][0]["value"])
        # covered item is still listed, not collapsed away
        season = next(f for f in embed["fields"] if f["name"] == "Season 1")
        self.assertIn("covered", season["value"])

    @patch("seadexarr.modules.audit.get_anilist_thumb", return_value=(None, {}))
    def test_build_embed_untracked_items_collapse_to_count(self, _thumb):
        audit = self._audit()
        r = _make_result(
            anilist_title="Blend S",
            seadex_status="full",
            entries=[
                self._entry(al_id=1, tvdb_season=1),
                self._entry(al_id=2, seadex_status="none", tvdb_season=0),
                self._entry(al_id=3, seadex_status="none", tvdb_season=2),
            ],
        )
        embed = audit._build_embed(r, old_state=None)
        # only the tracked item gets a field
        self.assertEqual(len(embed["fields"]), 1)
        self.assertIn("2 other item(s) not tracked", embed["description"])

    @patch("seadexarr.modules.audit.get_anilist_thumb", return_value=(None, {}))
    def test_build_embed_overflow_summarised(self, _thumb):
        audit = self._audit()
        entries = [self._entry(al_id=i, tvdb_season=i) for i in range(1, 26)]
        r = _make_result(anilist_title="Long Show", seadex_status="full", entries=entries)
        embed = audit._build_embed(r, old_state=None)
        # capped at MAX_ITEM_FIELDS items + one overflow summary field
        self.assertEqual(len(embed["fields"]), audit.MAX_ITEM_FIELDS + 1)
        self.assertIn("more covered", embed["fields"][-1]["name"])

    @patch("seadexarr.modules.audit.get_anilist_thumb", return_value=(None, {}))
    def test_build_embed_no_legacy_field_dump(self, _thumb):
        audit = self._audit()
        old = _make_state(seadex_status="full", seadex_rgs=[], library_rgs=["Netaro"])
        r = _make_result(
            anilist_title="BLEND-S", seadex_status="full",
            seadex_rgs=["uba"], library_rgs=["Netaro"],
            entries=[self._entry(library_rgs=["Netaro"])],
        )
        embed = audit._build_embed(r, old_state=old)
        for junk in ("Coverage:", "Library:", "SeaDex:", "Changes:"):
            self.assertNotIn(junk, embed["description"])

    # -- _item_lines: free-win marker + alt-tier annotation -------------------

    def test_item_lines_free_win_when_upgrade_is_smaller(self):
        audit = self._audit()
        entry = self._entry(
            upgrade_available=True,
            seadex_size_bytes=6 * 1024**3,
            library_size_bytes=10 * 1024**3,
        )
        head = audit._item_lines(entry)[0]
        self.assertIn("💰 free win", head)
        self.assertIn("better AND smaller", head)
        self.assertIn("-4.0 GB", head)

    def test_item_lines_ordinary_upgrade_when_larger(self):
        audit = self._audit()
        entry = self._entry(
            upgrade_available=True,
            seadex_size_bytes=12 * 1024**3,
            library_size_bytes=9 * 1024**3,
        )
        head = audit._item_lines(entry)[0]
        self.assertIn("🟠 upgrade available", head)
        self.assertNotIn("free win", head)

    def test_item_lines_no_free_win_when_sizes_unknown(self):
        # 0-byte seadex size must not masquerade as a saving vs a known library.
        audit = self._audit()
        entry = self._entry(
            upgrade_available=True,
            seadex_size_bytes=0,
            library_size_bytes=9 * 1024**3,
        )
        head = audit._item_lines(entry)[0]
        self.assertNotIn("free win", head)

    def test_item_lines_alt_tier_named_when_alt_acceptable(self):
        audit = self._audit()
        audit.alt_is_acceptable = True
        entry = self._entry(
            library_rgs=["AltGroup"],
            seadex_best_rgs=["BestGroup"],
            seadex_alt_rgs=["AltGroup"],
        )
        lines = audit._item_lines(entry)
        alt_line = next((l for l in lines if "alt release" in l), None)
        self.assertIsNotNone(alt_line)
        self.assertIn("AltGroup", alt_line)
        self.assertIn("SeaDex best: BestGroup", alt_line)

    def test_item_lines_no_alt_note_when_owning_best(self):
        audit = self._audit()
        audit.alt_is_acceptable = True
        entry = self._entry(
            library_rgs=["BestGroup"],
            seadex_best_rgs=["BestGroup"],
            seadex_alt_rgs=["AltGroup"],
        )
        self.assertFalse(
            any("alt release" in l for l in audit._item_lines(entry))
        )

    def test_item_lines_no_alt_note_when_feature_disabled(self):
        audit = self._audit()  # alt_is_acceptable unset -> getattr False
        entry = self._entry(
            library_rgs=["AltGroup"],
            seadex_best_rgs=["BestGroup"],
            seadex_alt_rgs=["AltGroup"],
        )
        self.assertFalse(
            any("alt release" in l for l in audit._item_lines(entry))
        )

    def test_item_lines_upgrade_offers_smaller_alt(self):
        audit = self._audit()
        audit.alt_is_acceptable = True
        entry = self._entry(
            upgrade_available=True,
            seadex_size_bytes=12 * 1024**3,
            library_size_bytes=9 * 1024**3,
            alt_release_rg="AltGroup",
            alt_release_size_bytes=8 * 1024**3,
        )
        alt = next((l for l in audit._item_lines(entry) if "alt option" in l), None)
        self.assertIsNotNone(alt)
        self.assertIn("8.0 GB", alt)
        self.assertIn("-1.0 GB", alt)
        self.assertIn("AltGroup", alt)

    def test_item_lines_too_large_offers_alt_that_fits(self):
        audit = self._audit()
        audit.alt_is_acceptable = True
        entry = self._entry(
            upgrade_available=True,
            too_large=True,
            seadex_size_bytes=90 * 1024**3,
            library_size_bytes=10 * 1024**3,
            alt_release_rg="AltGroup",
            alt_release_size_bytes=7 * 1024**3,
        )
        lines = audit._item_lines(entry)
        self.assertTrue(any("too large" in l for l in lines))
        alt = next((l for l in lines if "alt option" in l), None)
        self.assertIsNotNone(alt)
        self.assertIn("7.0 GB", alt)

    def test_item_lines_no_alt_option_when_feature_disabled(self):
        audit = self._audit()  # disabled
        entry = self._entry(
            upgrade_available=True,
            alt_release_rg="AltGroup",
            alt_release_size_bytes=8 * 1024**3,
        )
        self.assertFalse(any("alt option" in l for l in audit._item_lines(entry)))

    def test_item_lines_no_alt_option_when_size_unknown(self):
        audit = self._audit()
        audit.alt_is_acceptable = True
        entry = self._entry(
            upgrade_available=True,
            alt_release_rg=None,
            alt_release_size_bytes=0,
        )
        self.assertFalse(any("alt option" in l for l in audit._item_lines(entry)))


# ---------------------------------------------------------------------------
# Incremental notify marking (per-batch, crash-safe)
# ---------------------------------------------------------------------------

class TestIncrementalNotify(unittest.TestCase):

    def _audit(self):
        audit = SeaDexAudit.__new__(SeaDexAudit)
        audit.discord_url = "https://discord.test/webhook"
        audit.tag_full = "seadex"
        audit.tag_upgrade = "seadex-upgrade-available"
        audit.tag_too_large = "seadex-too-large"
        audit.al_cache = {}
        audit.logger = MagicMock()
        return audit

    def _results(self, n):
        return [_make_result(sonarr_id=i, seadex_status="full") for i in range(n)]

    @patch("seadexarr.modules.audit.time.sleep", return_value=None)
    @patch("seadexarr.modules.audit.get_anilist_thumb", return_value=(None, {}))
    @patch("seadexarr.modules.discord.Discord")
    def test_on_sent_fires_once_per_batch(self, _disc, _thumb, _sleep):
        audit = self._audit()
        sent_batches = []
        audit._send_batch_discord(
            self._results(23), old_states={}, on_sent=sent_batches.append
        )
        # 23 results -> batches of 3 (conservative size, see BATCH_SIZE)
        self.assertEqual([len(b) for b in sent_batches], [3, 3, 3, 3, 3, 3, 3, 2])

    @patch("seadexarr.modules.audit.time.sleep", return_value=None)
    @patch("seadexarr.modules.audit.get_anilist_thumb", return_value=(None, {}))
    @patch("seadexarr.modules.discord.Discord")
    def test_crash_mid_notify_leaves_later_batches_unstamped(self, disc, _thumb, _sleep):
        audit = self._audit()
        # First post succeeds, second raises -> only the first batch is stamped.
        disc.return_value.post.side_effect = [None, RuntimeError("429")]
        stamped = []
        with self.assertRaises(RuntimeError):
            audit._send_batch_discord(
                self._results(15), old_states={}, on_sent=stamped.append
            )
        self.assertEqual(len(stamped), 1)
        self.assertEqual(len(stamped[0]), 3)  # only the first batch

    @patch("seadexarr.modules.audit.time.sleep", return_value=None)
    @patch("seadexarr.modules.audit.get_anilist_thumb", return_value=(None, {}))
    @patch("seadexarr.modules.discord.Discord")
    def test_single_send_stamps_after_post(self, _disc, _thumb, _sleep):
        audit = self._audit()
        stamped = []
        audit._send_single_discord(
            _make_result(seadex_status="full"), old_state=None, on_sent=stamped.append
        )
        self.assertEqual(len(stamped), 1)


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

        # Seed the AniList cache so _audit_al_id's format lookup is a cache hit
        # (no live network call) — mirrors get_anilist_title warming the cache.
        audit.al_cache = {12345: {"data": {"Media": {"format": "TV"}}}}

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

    def test_entry_exposes_best_and_alt_tiers(self):
        audit = self._make_audit(alt_is_acceptable=True)
        torrents = [
            self._make_torrent("BestGroup", is_best=True),
            self._make_torrent("AltGroup", is_best=False),
        ]
        result = self._run(audit, library_rgs=["AltGroup"], sd_torrents=torrents)
        self.assertEqual(result["seadex_best_rgs"], ["BestGroup"])
        self.assertEqual(result["seadex_alt_rgs"], ["AltGroup"])

    def test_tiers_group_best_on_any_torrent_excluded_from_alt(self):
        # A group tagged best on one torrent and alt on another counts as best.
        audit = self._make_audit(alt_is_acceptable=True)
        sd_entry = MagicMock()
        sd_entry.torrents = [
            self._make_torrent("Mixed", is_best=True),
            self._make_torrent("Mixed", is_best=False),
            self._make_torrent("PureAlt", is_best=False),
        ]
        best, alt = audit._seadex_rg_tiers(sd_entry)
        self.assertEqual(best, {"Mixed"})
        self.assertEqual(alt, {"PureAlt"})

    def _with_size(self, torrent, gb):
        torrent.files = [MagicMock(size=int(gb * 1024**3))]
        return torrent

    def test_smallest_alt_release_picks_min(self):
        audit = self._make_audit(alt_is_acceptable=True)
        sd = MagicMock()
        sd.torrents = [
            self._make_torrent("BestGroup", is_best=True),
            self._with_size(self._make_torrent("AltBig", is_best=False), 10),
            self._with_size(self._make_torrent("AltSmall", is_best=False), 4),
        ]
        rg, size = audit._smallest_alt_release(sd)
        self.assertEqual(rg, "AltSmall")
        self.assertEqual(size, 4 * 1024**3)

    def test_smallest_alt_release_excludes_best_group(self):
        audit = self._make_audit(alt_is_acceptable=True)
        sd = MagicMock()
        sd.torrents = [
            self._with_size(self._make_torrent("BestGroup", is_best=True), 1),
            self._with_size(self._make_torrent("AltGroup", is_best=False), 5),
        ]
        rg, _ = audit._smallest_alt_release(sd)
        self.assertEqual(rg, "AltGroup")

    def test_smallest_alt_release_none_when_sizes_unknown(self):
        audit = self._make_audit(alt_is_acceptable=True)
        sd = MagicMock()
        sd.torrents = [self._make_torrent("Alt", is_best=False)]  # files=[]
        self.assertEqual(audit._smallest_alt_release(sd), (None, 0))

    def test_entry_exposes_smallest_alt_size(self):
        audit = self._make_audit(alt_is_acceptable=True)
        torrents = [
            self._make_torrent("BestGroup", is_best=True),
            self._with_size(self._make_torrent("AltGroup", is_best=False), 3),
        ]
        # Library owns neither -> upgrade stays, alt size surfaced for the embed.
        result = self._run(audit, library_rgs=["SomethingElse"], sd_torrents=torrents)
        self.assertEqual(result["alt_release_rg"], "AltGroup")
        self.assertEqual(result["alt_release_size_bytes"], 3 * 1024**3)

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

        # Seed the AniList cache so the format lookup is a cache hit (no network).
        audit.al_cache = {12345: {"data": {"Media": {"format": "TV"}}}}

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


# ---------------------------------------------------------------------------
# Notification rules: library-only changes and first-run seeding
# ---------------------------------------------------------------------------

class TestLibraryOnlyChange(unittest.TestCase):

    def _with_state(self, fn):
        path = _tmp_db()
        state = AuditState(path)
        try:
            fn(state)
        finally:
            state.close()
            if os.path.exists(path):
                os.unlink(path)

    def test_library_rg_change_quiet_by_default(self):
        def check(state):
            old = _make_state(seadex_status="full", seadex_rgs=["uba"],
                              library_rgs=["GroupA"])
            state.update_series(old)
            new = _make_state(seadex_status="full", seadex_rgs=["uba"],
                              library_rgs=["GroupA", "GroupB"])
            self.assertFalse(state.should_notify(new, {}))
        self._with_state(check)

    def test_library_rg_change_notifies_when_opted_in(self):
        def check(state):
            old = _make_state(seadex_status="full", seadex_rgs=["uba"],
                              library_rgs=["GroupA"])
            state.update_series(old)
            new = _make_state(seadex_status="full", seadex_rgs=["uba"],
                              library_rgs=["GroupA", "GroupB"])
            cfg = {"notify_on_library_change": True}
            self.assertTrue(state.should_notify(new, cfg))
        self._with_state(check)

    def test_seadex_rg_change_still_notifies(self):
        def check(state):
            old = _make_state(seadex_status="full", seadex_rgs=["uba"],
                              library_rgs=["GroupA"])
            state.update_series(old)
            new = _make_state(seadex_status="full", seadex_rgs=["uba", "Netaro"],
                              library_rgs=["GroupA", "GroupB"])
            # Not a library-only change — falls to notify_on_state_change.
            self.assertTrue(state.should_notify(new, {}))
        self._with_state(check)


class TestFirstRunActionable(unittest.TestCase):

    def test_series_actionable(self):
        self.assertFalse(
            SeaDexAudit._series_actionable(_make_result(seadex_status="full"))
        )
        self.assertTrue(
            SeaDexAudit._series_actionable(
                _make_result(seadex_status="full", upgrade_available=True)
            )
        )
        self.assertTrue(
            SeaDexAudit._series_actionable(
                _make_result(seadex_status="full", missing_season=True)
            )
        )

    def test_movie_actionable(self):
        from seadexarr.modules.audit import MovieAuditResult
        covered = MovieAuditResult(
            radarr_id=1, tmdb_id=1, radarr_title="M", anilist_title="M",
            al_id=1, sd_url=None, seadex_status="full",
        )
        self.assertFalse(SeaDexAudit._movie_actionable(covered))
        covered.hardlink_mismatch = True
        self.assertTrue(SeaDexAudit._movie_actionable(covered))


# ---------------------------------------------------------------------------
# notify_pending: failed posts retried on the next run
# ---------------------------------------------------------------------------

class TestNotifyPending(unittest.TestCase):

    def _with_state(self, fn):
        path = _tmp_db()
        state = AuditState(path)
        try:
            fn(state)
        finally:
            state.close()
            if os.path.exists(path):
                os.unlink(path)

    def test_pending_retries_when_nothing_changed(self):
        def check(state):
            s = _make_state(seadex_status="full")
            # Run decided to notify but the post never succeeded.
            state.update_series(s, notified=False, pending=True)
            same = _make_state(seadex_status="full")
            cfg = {"notify_on_no_change": False}
            self.assertTrue(state.should_notify(same, cfg))
        self._with_state(check)

    def test_successful_notify_clears_pending(self):
        def check(state):
            s = _make_state(seadex_status="full")
            state.update_series(s, notified=False, pending=True)
            state.update_series(_make_state(seadex_status="full"), notified=True)
            same = _make_state(seadex_status="full")
            cfg = {"notify_on_no_change": False}
            self.assertFalse(state.should_notify(same, cfg))
        self._with_state(check)

    def test_no_pending_no_change_stays_quiet(self):
        def check(state):
            state.update_series(_make_state(seadex_status="full"), notified=False)
            same = _make_state(seadex_status="full")
            cfg = {"notify_on_no_change": False}
            self.assertFalse(state.should_notify(same, cfg))
        self._with_state(check)

    def test_old_schema_db_gains_pending_column(self):
        # A pre-notify_pending database opens cleanly and reads pending=False.
        import sqlite3
        path = _tmp_db()
        try:
            conn = sqlite3.connect(path)
            conn.execute("""
                CREATE TABLE series (
                    sonarr_id INTEGER PRIMARY KEY, tvdb_id INTEGER,
                    title TEXT NOT NULL, seadex_status TEXT NOT NULL,
                    seadex_rgs TEXT NOT NULL DEFAULT '[]',
                    seadex_size_bytes INTEGER NOT NULL DEFAULT 0,
                    library_rgs TEXT NOT NULL DEFAULT '[]',
                    upgrade_available INTEGER NOT NULL DEFAULT 0,
                    too_large INTEGER NOT NULL DEFAULT 0,
                    missing_specials INTEGER NOT NULL DEFAULT 0,
                    missing_season INTEGER NOT NULL DEFAULT 0,
                    last_notified TEXT,
                    last_audited TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute(
                "INSERT INTO series (sonarr_id, title, seadex_status) VALUES (7, 'Old Show', 'full')"
            )
            conn.commit()
            conn.close()

            state = AuditState(path)
            loaded = state.get_series(7)
            self.assertIsNotNone(loaded)
            self.assertFalse(loaded.notify_pending)
            state.close()
        finally:
            if os.path.exists(path):
                os.unlink(path)


# ---------------------------------------------------------------------------
# Partial status: entry exists but every release filtered out
# ---------------------------------------------------------------------------

class TestPartialStatus(unittest.TestCase):

    def _make_audit(self):
        audit = SeaDexAudit.__new__(SeaDexAudit)
        audit.alt_is_acceptable = False
        audit.size_filter_enabled = False
        audit.ignore_tags = []
        audit.trackers = ["nyaa"]
        audit.public_only = True
        audit.log_line_length = 80
        audit.logger = MagicMock()
        # Seed the AniList cache so format lookups are cache hits (no network).
        audit.al_cache = {12345: {"data": {"Media": {"format": "TV"}}}}
        return audit

    def test_series_entry_with_all_releases_filtered_is_partial(self):
        audit = self._make_audit()
        mock_series = MagicMock()
        mock_series.id = 1
        mock_series.title = "Test Show"
        mock_sd_entry = MagicMock()
        mock_sd_entry.url = "https://seadex.moe/test"

        with patch.object(audit, "get_seadex_entry", return_value=mock_sd_entry), \
             patch.object(audit, "get_anilist_title", return_value="Test Show"), \
             patch.object(audit, "get_ep_list", return_value=[]), \
             patch.object(audit, "get_sonarr_release_dict", return_value={}), \
             patch.object(audit, "get_seadex_dict", return_value={}):
            out = audit._audit_al_id(mock_series, 12345, {})

        self.assertEqual(out["seadex_status"], "partial")
        self.assertFalse(out["upgrade_available"])

    def test_series_no_entry_stays_none(self):
        audit = self._make_audit()
        mock_series = MagicMock()
        mock_series.id = 1
        mock_series.title = "Test Show"

        with patch.object(audit, "get_seadex_entry", return_value=None):
            out = audit._audit_al_id(mock_series, 12345, {})

        self.assertEqual(out["seadex_status"], "none")

    def test_movie_entry_with_all_releases_filtered_is_partial(self):
        audit = self._make_audit()
        audit.al_cache = {12345: {"data": {"Media": {"format": "MOVIE"}}}}
        mock_movie = MagicMock()
        mock_movie.id = 1
        mock_movie.title = "Test Movie"
        mock_sd_entry = MagicMock()
        mock_sd_entry.url = "https://seadex.moe/test"

        with patch.object(audit, "get_seadex_entry", return_value=mock_sd_entry), \
             patch.object(audit, "get_anilist_title", return_value="Test Movie"), \
             patch.object(audit, "get_seadex_dict", return_value={}):
            out = audit._audit_radarr_al_id(mock_movie, 12345, {None: {"size": None}})

        self.assertEqual(out["seadex_status"], "partial")

    def test_item_lines_partial_head(self):
        audit = self._make_audit()
        entry = {
            "seadex_status": "partial",
            "library_rgs": [],
            "seadex_size_bytes": 0,
            "library_size_bytes": 0,
            "upgrade_available": False,
            "too_large": False,
        }
        head = audit._item_lines(entry)[0]
        self.assertIn("🟡", head)
        self.assertIn("no release passes your filters", head)


if __name__ == "__main__":
    unittest.main()
