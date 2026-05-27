"""
Unit tests for audit mode.

Tests cover status classification, state deduplication, tag diff logic,
dry-run guards, and stale tag removal scoping.

Run with: python -m pytest tests/test_audit.py -v
"""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

from seadexarr.modules.audit import AuditResult, SeaDexAudit
from seadexarr.modules.audit_state import AuditState, SeriesAuditState, state_changed
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
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            state = AuditState(path)
            new = _make_state(seadex_status="full")
            cfg = {"notify_on_new_seadex_match": True}
            self.assertTrue(state.should_notify(new, cfg))
        finally:
            os.unlink(path)

    def test_should_not_notify_no_change(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            state = AuditState(path)
            existing = _make_state(seadex_status="full", upgrade_available=False)
            state.update_series(existing)
            new = _make_state(seadex_status="full", upgrade_available=False)
            cfg = {"notify_on_no_change": False}
            self.assertFalse(state.should_notify(new, cfg))
        finally:
            os.unlink(path)

    def test_should_notify_new_upgrade(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            state = AuditState(path)
            old = _make_state(seadex_status="full", upgrade_available=False)
            state.update_series(old)
            new = _make_state(seadex_status="full", upgrade_available=True)
            cfg = {"notify_on_new_upgrade_available": True}
            self.assertTrue(state.should_notify(new, cfg))
        finally:
            os.unlink(path)

    def test_state_persists_across_load(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            state = AuditState(path)
            s = _make_state(sonarr_id=42, seadex_status="full")
            state.update_series(s)
            state.save()

            state2 = AuditState(path)
            loaded = state2.get_series(42)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.seadex_status, "full")
        finally:
            os.unlink(path)


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
            # currently has both tags; desired is only seadex; stale removal ON
            new_ids, changed = mgr.compute_tag_changes(
                current_tag_ids=[1, 2],
                desired_labels=["seadex"],
                managed_labels=["seadex", "seadex-upgrade-available"],
                remove_stale=True,
            )
        self.assertIn(1, new_ids)
        self.assertNotIn(2, new_ids)
        self.assertTrue(changed)

    def test_stale_managed_tag_not_removed_when_disabled(self):
        mgr = self._manager({"seadex": 1, "seadex-upgrade-available": 2})
        with patch.object(mgr, "get_or_create_tag", side_effect=lambda l: mgr._tag_cache.get(l, 99)):
            new_ids, changed = mgr.compute_tag_changes(
                current_tag_ids=[1, 2],
                desired_labels=["seadex"],
                managed_labels=["seadex", "seadex-upgrade-available"],
                remove_stale=False,
            )
        self.assertIn(1, new_ids)
        self.assertIn(2, new_ids)  # kept because remove_stale=False
        self.assertFalse(changed)

    def test_user_tags_never_removed(self):
        mgr = self._manager({"seadex": 1})
        user_tag_id = 99  # not in managed_labels
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
        """In dry_run mode, _apply_series_tags must not call set_series_tags."""
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

        self.assertIn(10, new_ids)     # desired
        self.assertNotIn(20, new_ids)  # stale managed, removed
        self.assertIn(arbitrary_user_tag, new_ids)  # user tag, kept


if __name__ == "__main__":
    unittest.main()
