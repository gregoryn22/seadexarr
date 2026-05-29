"""
Audit-only mode for SeaDexArr.

Scans Sonarr, classifies SeaDex coverage, applies tags, sends Discord
notifications. Never calls add_torrent() or any torrent-client method.
"""

import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from discordwebhook import Discord

from .anilist import get_anilist_thumb
from .audit_state import AuditState, SeriesAuditState, rg_diff, state_changed
from .log import centred_string, left_aligned_string
from .seadex_sonarr import SeaDexSonarr
from .sonarr_tags import SonarrTagManager

BYTES_PER_GB = 1_073_741_824

# Discord embed colours
_COLOUR_FULL = 0x2ECC71      # green
_COLOUR_PARTIAL = 0xF1C40F   # yellow
_COLOUR_UPGRADE = 0xE67E22   # orange
_COLOUR_TOO_LARGE = 0xE74C3C # red
_COLOUR_NONE = 0x95A5A6      # grey


@dataclass
class AuditResult:
    sonarr_id: int
    tvdb_id: Optional[int]
    sonarr_title: str
    anilist_title: str
    al_id: Optional[int]
    sd_url: Optional[str]
    seadex_status: str          # none / partial / full
    seadex_rgs: list[str] = field(default_factory=list)
    seadex_size_bytes: int = 0
    library_rgs: list[str] = field(default_factory=list)
    library_size_bytes: int = 0
    upgrade_available: bool = False
    too_large: bool = False
    desired_tags: list[str] = field(default_factory=list)
    notify: bool = False
    error: Optional[str] = None


class SeaDexAudit(SeaDexSonarr):
    """Audit-only SeaDex scanner. Extends SeaDexSonarr but never grabs torrents."""

    def __init__(self, config: str, cache: str, logger=None):
        super().__init__(config=config, cache=cache, logger=logger)

        self.audit_mode = True

        audit_cfg = self.config.get("audit", {}) or {}
        self.audit_dry_run: bool = audit_cfg.get("dry_run", True)
        self.audit_notify_discord: bool = audit_cfg.get("notify_discord", True)
        self.audit_update_tags: bool = audit_cfg.get("update_sonarr_tags", True)
        self.audit_remove_stale: bool = audit_cfg.get("remove_stale_tags", False)
        if self.audit_remove_stale:
            self.logger.warning(
                "Config: remove_stale_tags has no effect — managed tags are always "
                "synced and user-added non-managed tags are always preserved"
            )

        tags_cfg = audit_cfg.get("tags", {}) or {}
        self.tag_full: str = tags_cfg.get("full_seadex", "seadex")
        self.tag_partial: str = tags_cfg.get("partial_seadex", "partial-seadex")
        self.tag_upgrade: str = tags_cfg.get("upgrade_available", "seadex-upgrade-available")
        self.tag_too_large: str = tags_cfg.get("too_large", "seadex-too-large")
        self.tag_ignored: str = tags_cfg.get("ignored", "seadex-ignored")
        self.managed_tag_labels: list[str] = [
            self.tag_full,
            self.tag_partial,
            self.tag_upgrade,
            self.tag_too_large,
            self.tag_ignored,
        ]

        size_cfg = audit_cfg.get("size_filters", {}) or {}
        self.size_filter_enabled: bool = size_cfg.get("enabled", True)
        self.max_absolute_gb: float = size_cfg.get("max_absolute_gb", 80)
        self.max_size_multiplier: float = size_cfg.get("max_size_multiplier", 2.0)
        self.tag_when_too_large: bool = size_cfg.get("tag_when_too_large", True)
        self.alt_is_acceptable: bool = audit_cfg.get("alt_is_acceptable", False)

        self.discord_cfg: dict = audit_cfg.get("discord", {}) or {}

        state_cfg = audit_cfg.get("state", {}) or {}
        self.state_enabled: bool = state_cfg.get("enabled", True)
        state_path: str = state_cfg.get("path") or "/config/audit_state.db"
        self.audit_state: Optional[AuditState] = (
            AuditState(state_path) if self.state_enabled else None
        )

        self.tag_manager = SonarrTagManager(
            sonarr_url=self.sonarr_url,
            sonarr_api_key=self.sonarr_api_key,
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        dry_run: bool = False,
        apply_tags: bool = False,
        notify_only: bool = False,
    ) -> bool:
        """Audit Sonarr library against SeaDex. Never adds torrents.

        Args:
            dry_run: Log intended changes only; no Sonarr or state mutations.
            apply_tags: Explicitly override config dry_run and apply tags.
            notify_only: Send Discord notifications without changing tags.
        """
        # Config dry_run is the safety default. --apply-tags overrides it;
        # --dry-run always wins regardless of config.
        if dry_run:
            effective_dry_run = True
        elif apply_tags:
            effective_dry_run = False
        else:
            effective_dry_run = self.audit_dry_run
        dry_run = effective_dry_run

        # Graceful shutdown: SIGTERM/SIGINT finishes the current al_id then exits.
        # Stored on self so _audit_series can check between al_id iterations.
        self._shutdown = threading.Event()

        def _handle_signal(sig, frame):
            self.logger.warning(
                left_aligned_string(
                    "Shutdown signal received — finishing current series then saving state.",
                    total_length=self.log_line_length,
                )
            )
            self._shutdown.set()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        all_series = self.get_all_sonarr_series()
        n_total = len(all_series)

        self.log_arr_start(arr="sonarr", n_items=n_total)

        stats: dict[str, int] = {
            "total": n_total,
            "matched": 0,
            "full": 0,
            "partial": 0,
            "upgrade": 0,
            "too_large": 0,
            "tagged": 0,
            "notified": 0,
            "errors": 0,
        }

        results: list[AuditResult] = []
        # old_states keyed by sonarr_id — captured before any update_series call
        old_states: dict[int, Optional[SeriesAuditState]] = {}

        for idx, series in enumerate(all_series):
            if self._shutdown.is_set():
                self.logger.info(
                    left_aligned_string(
                        f"Audit interrupted after {idx}/{n_total} series.",
                        total_length=self.log_line_length,
                    )
                )
                break

            self.log_arr_item_start(
                arr="sonarr",
                item_title=series.title,
                n_item=idx + 1,
                n_items=n_total,
            )

            # Snapshot old state before this audit run
            if self.audit_state is not None:
                old_states[series.id] = self.audit_state.get_series(series.id)

            result = self._audit_series(series)
            results.append(result)

            if result.error:
                stats["errors"] += 1
                time.sleep(self.sleep_time)
                continue

            if result.seadex_status != "none":
                stats["matched"] += 1
            if result.seadex_status == "full":
                stats["full"] += 1
            elif result.seadex_status == "partial":
                stats["partial"] += 1
            if result.upgrade_available:
                stats["upgrade"] += 1
            if result.too_large:
                stats["too_large"] += 1

            self._log_result(result)

            if self.audit_state is not None:
                result.notify = self.audit_state.should_notify(
                    new_state=self._to_state(result),
                    discord_cfg=self.discord_cfg,
                )
            else:
                result.notify = result.seadex_status != "none"

            # Tag management
            do_tags = (apply_tags or self.audit_update_tags) and not notify_only
            if do_tags and not dry_run:
                changed = self._apply_series_tags(series, result, dry_run=False)
                if changed:
                    stats["tagged"] += 1
            elif dry_run and do_tags:
                self._log_dry_run_tags(result)

            time.sleep(self.sleep_time)

        # Persist state before notifications so a crash during Discord doesn't
        # leave all audited series looking new on the next run.
        if self.audit_state is not None and not dry_run:
            for r in results:
                if not r.error:
                    self.audit_state.update_series(self._to_state(r), notified=False)

        # Notifications
        to_notify = [r for r in results if r.notify and r.seadex_status != "none"]
        if to_notify and (self.audit_notify_discord or notify_only) and self.discord_url:
            if self.discord_cfg.get("batch_notifications", True):
                self._send_batch_discord(to_notify, old_states)
            else:
                for r in to_notify:
                    self._send_single_discord(r, old_states.get(r.sonarr_id))
            stats["notified"] = len(to_notify)

        # Update last_notified for series that Discord was actually sent for
        if self.audit_state is not None and not dry_run:
            for r in to_notify:
                if not r.error:
                    self.audit_state.update_series(self._to_state(r), notified=True)
            self.audit_state.save()

        self._log_summary(stats)
        return True

    # ------------------------------------------------------------------
    # Per-series audit
    # ------------------------------------------------------------------

    def _audit_series(self, sonarr_series) -> AuditResult:
        result = AuditResult(
            sonarr_id=sonarr_series.id,
            tvdb_id=sonarr_series.tvdbId,
            sonarr_title=sonarr_series.title,
            anilist_title=sonarr_series.title,
            al_id=None,
            sd_url=None,
            seadex_status="none",
        )

        try:
            al_mappings = self.get_anilist_ids(
                tvdb_id=sonarr_series.tvdbId,
                imdb_id=sonarr_series.imdbId,
            )
            if not al_mappings:
                return result

            per_al: list[dict] = []
            for al_id, mapping in al_mappings.items():
                if getattr(self, "_shutdown", None) and self._shutdown.is_set():
                    break
                if al_id is None:
                    continue
                per_al.append(self._audit_al_id(sonarr_series, al_id, mapping))

            if not per_al:
                return result

            STATUS_ORDER = ["none", "partial", "full"]

            best_status = max(
                per_al, key=lambda d: STATUS_ORDER.index(d["seadex_status"])
            )["seadex_status"]

            # Pick representative entry for URL/title from the highest-status group
            top_entries = [d for d in per_al if d["seadex_status"] == best_status]
            representative = top_entries[0]

            result.al_id = representative["al_id"]
            result.sd_url = representative["sd_url"]
            result.anilist_title = representative["anilist_title"]
            result.seadex_status = best_status

            # Union RGs across ALL top-status entries so per-entry changes are
            # detected even when the aggregate status doesn't change.
            result.seadex_rgs = sorted(
                {rg for d in top_entries for rg in d["seadex_rgs"]}
            )
            result.seadex_size_bytes = max(
                (d["seadex_size_bytes"] for d in top_entries), default=0
            )
            result.library_rgs = representative["library_rgs"]
            result.library_size_bytes = representative["library_size_bytes"]

            result.upgrade_available = any(d["upgrade_available"] for d in per_al)
            result.too_large = any(d["too_large"] for d in per_al)
            result.desired_tags = self._compute_desired_tags(result)

        except Exception as e:
            import traceback as _tb
            result.error = str(e)
            self.logger.error(
                left_aligned_string(
                    f"Error auditing {sonarr_series.title}: {e}",
                    total_length=self.log_line_length,
                )
            )
            for tb_line in _tb.format_exc().splitlines():
                if tb_line.strip():
                    self.logger.error(f"  {tb_line}")

        return result

    def _audit_al_id(self, sonarr_series, al_id: int, mapping: dict) -> dict:
        out: dict = {
            "al_id": al_id,
            "sd_url": None,
            "anilist_title": sonarr_series.title,
            "seadex_status": "none",
            "seadex_rgs": [],
            "seadex_size_bytes": 0,
            "library_rgs": [],
            "library_size_bytes": 0,
            "upgrade_available": False,
            "too_large": False,
        }

        sd_entry = self.get_seadex_entry(al_id=al_id)
        if sd_entry is None:
            return out

        out["sd_url"] = sd_entry.url
        out["anilist_title"] = self.get_anilist_title(al_id=al_id, sd_entry=sd_entry)

        ep_list = self.get_ep_list(
            sonarr_series_id=sonarr_series.id,
            al_id=al_id,
            mapping=mapping,
        )
        if ep_list is None:
            ep_list = []

        sonarr_release_dict = self.get_sonarr_release_dict(ep_list=ep_list)
        out["library_rgs"] = list(sonarr_release_dict.keys())
        out["library_size_bytes"] = sum(
            sum(s for s in rg_data.get("size", []) if s)
            for rg_data in sonarr_release_dict.values()
        )

        seadex_dict = self.get_seadex_dict(sd_entry=sd_entry)

        if len(seadex_dict) == 0:
            # SeaDex has an entry but all torrents were filtered by the user's
            # tracker/public_only/want_best config — no actionable release exists.
            return out  # seadex_status stays "none"

        out["seadex_status"] = "full"
        out["seadex_rgs"] = list(seadex_dict.keys())
        out["seadex_size_bytes"] = self._sum_seadex_size(seadex_dict)

        seadex_dict = self.parse_episodes_from_seadex(seadex_dict=seadex_dict)
        _, seadex_dict = self.filter_seadex_downloads(
            al_id=al_id,
            seadex_dict=seadex_dict,
            arr="sonarr",
            arr_release_dict=sonarr_release_dict,
            ep_list=ep_list,
        )
        out["upgrade_available"] = self.get_any_to_download(seadex_dict=seadex_dict)

        if out["upgrade_available"] and self.size_filter_enabled:
            sd_gb = out["seadex_size_bytes"] / BYTES_PER_GB
            lib_bytes = out["library_size_bytes"]
            ratio = (out["seadex_size_bytes"] / lib_bytes) if lib_bytes > 0 else 0
            if sd_gb > self.max_absolute_gb or ratio > self.max_size_multiplier:
                out["too_large"] = True

        # If alt releases are acceptable and the library already has any
        # SeaDex-listed release (best OR alt), don't flag for upgrade.
        if (out["upgrade_available"] or out["too_large"]) and self.alt_is_acceptable:
            all_candidates = [
                t for t in sd_entry.torrents
                if not set(self.ignore_tags) & set(t.tags)
                and t.tracker.lower() in self.trackers
            ]
            if self.public_only:
                all_candidates = [t for t in all_candidates if t.tracker.is_public()]
            all_sd_rgs = {t.release_group for t in all_candidates}
            if set(out["library_rgs"]) & all_sd_rgs:
                out["upgrade_available"] = False
                out["too_large"] = False

        return out

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    def _compute_desired_tags(self, result: AuditResult) -> list[str]:
        tags: list[str] = []
        if result.seadex_status == "full":
            tags.append(self.tag_full)
        elif result.seadex_status == "partial":
            tags.append(self.tag_partial)

        if result.upgrade_available:
            if result.too_large and self.tag_when_too_large:
                tags.append(self.tag_too_large)
            else:
                tags.append(self.tag_upgrade)

        return tags

    def _apply_series_tags(self, sonarr_series, result: AuditResult, dry_run: bool) -> bool:
        try:
            series_json = self.tag_manager.get_series_json(sonarr_series.id)
            current_tags = series_json.get("tags", [])

            new_tags, changed = self.tag_manager.compute_tag_changes(
                current_tag_ids=current_tags,
                desired_labels=result.desired_tags,
                managed_labels=self.managed_tag_labels,
                remove_stale=self.audit_remove_stale,
            )

            if changed:
                self.tag_manager.set_series_tags(sonarr_series.id, new_tags, dry_run=dry_run)
                self.logger.info(
                    centred_string(
                        f"Tags updated for {result.sonarr_title}: {result.desired_tags}",
                        total_length=self.log_line_length,
                    )
                )
            return changed
        except Exception as e:
            self.logger.error(
                left_aligned_string(
                    f"Tag update failed for {result.sonarr_title}: {e}",
                    total_length=self.log_line_length,
                )
            )
            return False

    def _log_dry_run_tags(self, result: AuditResult):
        self.logger.info(
            centred_string(
                f"[DRY RUN] {result.sonarr_title} → tags: {result.desired_tags}",
                total_length=self.log_line_length,
            )
        )

    # ------------------------------------------------------------------
    # Discord
    # ------------------------------------------------------------------

    @staticmethod
    def _embed_colour(result: AuditResult) -> int:
        if result.too_large:
            return _COLOUR_TOO_LARGE
        if result.upgrade_available:
            return _COLOUR_UPGRADE
        if result.seadex_status == "full":
            return _COLOUR_FULL
        if result.seadex_status == "partial":
            return _COLOUR_PARTIAL
        return _COLOUR_NONE

    def _send_single_discord(self, result: AuditResult, old_state: Optional[SeriesAuditState]):
        if not self.discord_url:
            return

        sd_gb = result.seadex_size_bytes / BYTES_PER_GB
        lib_gb = result.library_size_bytes / BYTES_PER_GB
        delta_gb = sd_gb - lib_gb

        old_s = old_state  # may be None on first ever audit
        added_rgs, removed_rgs = rg_diff(old_s, self._to_state(result))

        # Status transition label
        old_status = old_s.seadex_status if old_s else "new"
        new_status = result.seadex_status.capitalize()
        if old_s and old_s.seadex_status != result.seadex_status:
            status_label = f"{old_status.capitalize()} → {new_status}"
        else:
            status_label = new_status

        fields = [
            {"name": "Coverage", "value": status_label, "inline": True},
            {
                "name": "Library",
                "value": ", ".join(result.library_rgs) or "None",
                "inline": True,
            },
            {
                "name": "SeaDex",
                "value": ", ".join(result.seadex_rgs) or "None",
                "inline": True,
            },
        ]

        # Release-group diff field — only when something actually changed
        if added_rgs or removed_rgs:
            diff_parts = [f"+{rg}" for rg in added_rgs] + [f"-{rg}" for rg in removed_rgs]
            fields.append({
                "name": "Changes",
                "value": "  ".join(diff_parts),
                "inline": False,
            })

        if result.upgrade_available:
            size_str = f"{sd_gb:.1f} GB ({delta_gb:+.1f} GB vs current)"
            if result.library_size_bytes > 0:
                size_str += f" / {result.seadex_size_bytes / result.library_size_bytes:.1f}× current"
            fields.append({"name": "Size", "value": size_str, "inline": False})

        if result.too_large:
            action = f"Flagged too large — tagged `{self.tag_too_large}`; no download performed"
        elif result.upgrade_available:
            action = f"Tagged `{self.tag_upgrade}`; no download performed"
        else:
            action = "Already have recommended release"
        fields.append({"name": "Action", "value": action, "inline": False})

        if result.sd_url:
            fields.append({"name": "SeaDex entry", "value": result.sd_url, "inline": False})

        anilist_thumb, self.al_cache = get_anilist_thumb(
            al_id=result.al_id,
            al_cache=self.al_cache,
        )

        embed: dict = {
            "author": {
                "name": "SeaDexArr Audit",
                "url": "https://github.com/bbtufty/seadexarr",
            },
            "title": result.anilist_title or result.sonarr_title,
            "description": result.sd_url or "",
            "color": self._embed_colour(result),
            "fields": fields,
        }
        if anilist_thumb:
            embed["thumbnail"] = {"url": anilist_thumb}

        discord = Discord(url=self.discord_url)
        discord.post(embeds=[embed])
        time.sleep(1)

    def _send_batch_discord(
        self,
        results: list[AuditResult],
        old_states: dict[int, Optional[SeriesAuditState]],
    ):
        if not self.discord_url or not results:
            return

        BATCH_SIZE = 10  # Discord API maximum embeds per message
        for i in range(0, len(results), BATCH_SIZE):
            batch = results[i : i + BATCH_SIZE]
            embeds = []
            for r in batch:
                sd_gb = r.seadex_size_bytes / BYTES_PER_GB
                lib_gb = r.library_size_bytes / BYTES_PER_GB
                delta_gb = sd_gb - lib_gb

                old_s = old_states.get(r.sonarr_id)
                added_rgs, removed_rgs = rg_diff(old_s, self._to_state(r))

                parts = [f"Coverage: {r.seadex_status.capitalize()}"]
                if r.library_rgs:
                    parts.append(f"Library: {', '.join(r.library_rgs)}")
                if r.seadex_rgs:
                    parts.append(f"SeaDex: {', '.join(r.seadex_rgs)}")
                if added_rgs or removed_rgs:
                    diff_parts = [f"+{rg}" for rg in added_rgs] + [f"-{rg}" for rg in removed_rgs]
                    parts.append(f"Changes: {' '.join(diff_parts)}")
                if r.upgrade_available:
                    flag = " ⚠ Too Large" if r.too_large else " ↑ Upgrade"
                    parts.append(f"Size: {sd_gb:.1f} GB ({delta_gb:+.1f} GB){flag}")

                embeds.append({
                    "author": {"name": "SeaDexArr Audit"},
                    "title": r.anilist_title or r.sonarr_title,
                    "description": "\n".join(parts),
                    "color": self._embed_colour(r),
                    "url": r.sd_url or "",
                })

            discord = Discord(url=self.discord_url)
            discord.post(embeds=embeds)
            time.sleep(1)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sum_seadex_size(self, seadex_dict: dict) -> int:
        total = 0
        for rg_data in seadex_dict.values():
            for url_data in (rg_data.get("urls") or {}).values():
                sizes = (url_data or {}).get("size", []) or []
                total += sum(s for s in sizes if s)
        return total

    def _to_state(self, result: AuditResult) -> SeriesAuditState:
        return SeriesAuditState(
            sonarr_id=result.sonarr_id,
            tvdb_id=result.tvdb_id,
            title=result.sonarr_title,
            seadex_status=result.seadex_status,
            seadex_rgs=result.seadex_rgs,
            seadex_size_bytes=result.seadex_size_bytes,
            library_rgs=result.library_rgs,
            upgrade_available=result.upgrade_available,
            too_large=result.too_large,
        )

    def _log_result(self, result: AuditResult):
        status_str = result.seadex_status
        if result.upgrade_available:
            status_str += " | upgrade-available"
        if result.too_large:
            status_str += " | too-large"
        self.logger.info(
            centred_string(
                f"{result.sonarr_title}: {status_str}",
                total_length=self.log_line_length,
            )
        )

    def _log_summary(self, stats: dict[str, int]):
        sep = "=" * self.log_line_length
        self.logger.info(centred_string(sep, total_length=self.log_line_length))
        self.logger.info(centred_string("Audit Summary", total_length=self.log_line_length))
        self.logger.info(centred_string(sep, total_length=self.log_line_length))
        for label, key in [
            ("Scanned", "total"),
            ("Matched to SeaDex", "matched"),
            ("Full coverage", "full"),
            ("Partial coverage", "partial"),
            ("Upgrade available", "upgrade"),
            ("Too large", "too_large"),
            ("Tags updated", "tagged"),
            ("Notifications sent", "notified"),
            ("Errors", "errors"),
        ]:
            self.logger.info(
                centred_string(
                    f"{label}: {stats[key]}",
                    total_length=self.log_line_length,
                )
            )
        self.logger.info(centred_string(sep, total_length=self.log_line_length))
