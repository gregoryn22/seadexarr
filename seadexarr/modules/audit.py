"""
Audit-only mode for SeaDexArr.

Scans Sonarr, classifies SeaDex coverage, applies tags, sends Discord
notifications. Never calls add_torrent() or any torrent-client method.
"""

import signal
import threading
import time
from dataclasses import dataclass, field
from itertools import groupby
from typing import Optional

from discordwebhook import Discord

from .anilist import get_anilist_format, get_anilist_thumb
from .audit_state import (
    AuditState, SeriesAuditState, MovieAuditState,
    rg_diff, state_changed, movie_state_changed,
)
from .log import centred_string, left_aligned_string
from .seadex_sonarr import SeaDexSonarr, get_tvdb_season
from .sonarr_tags import SonarrTagManager, RadarrTagManager

BYTES_PER_GB = 1_073_741_824

# Discord embed colours
_COLOUR_FULL = 0x2ECC71           # green
_COLOUR_PARTIAL = 0xF1C40F        # yellow
_COLOUR_UPGRADE = 0xE67E22        # orange
_COLOUR_TOO_LARGE = 0xE74C3C      # red
_COLOUR_MISSING_SEASON = 0x9B59B6    # purple
_COLOUR_MISSING_SPECIALS = 0x3498DB  # blue
_COLOUR_NONE = 0x95A5A6           # grey


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
    missing_specials: bool = False
    missing_season: bool = False
    desired_tags: list[str] = field(default_factory=list)
    notify: bool = False
    error: Optional[str] = None
    # Per-AniList-entry breakdown (one per season / cour / part / movie) so
    # notifications can name exactly what is actionable and link its SeaDex page.
    entries: list[dict] = field(default_factory=list)


@dataclass
class MovieAuditResult:
    radarr_id: int
    tmdb_id: Optional[int]
    radarr_title: str
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
    entries: list[dict] = field(default_factory=list)
    # Cross-reference: populated when this movie's al_id also maps to a Sonarr special
    sonarr_specials_title: Optional[str] = None
    sonarr_specials_rgs: list[str] = field(default_factory=list)
    hardlink_candidate: bool = False   # same RG in both — verify hard-link
    hardlink_mismatch: bool = False    # different RGs — hard-link not possible as-is


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
        self.tag_missing_specials: str = tags_cfg.get("missing_specials", "seadex-missing-specials")
        self.tag_missing_season: str = tags_cfg.get("missing_season", "seadex-missing-season")
        self.tag_ignored: str = tags_cfg.get("ignored", "seadex-ignored")
        self.managed_tag_labels: list[str] = [
            self.tag_full,
            self.tag_partial,
            self.tag_upgrade,
            self.tag_too_large,
            self.tag_missing_specials,
            self.tag_missing_season,
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

        self.audit_include_radarr: bool = audit_cfg.get("include_radarr", True)
        self.radarr_tag_manager: Optional[RadarrTagManager] = None
        if self.radarr is not None and self.audit_include_radarr:
            self.radarr_tag_manager = RadarrTagManager(
                radarr_url=self.radarr.radarr_url,
                radarr_api_key=self.radarr.radarr_api_key,
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

        # Wall-clock start so the summary can report how long the run took.
        run_start = time.monotonic()

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
            "missing_specials": 0,
            "missing_season": 0,
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
            if result.missing_specials:
                stats["missing_specials"] += 1
            if result.missing_season:
                stats["missing_season"] += 1

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

            # No fixed per-series sleep: AniList — the only external API the
            # audit hits — is throttled at the request layer based on its own
            # rate-limit headers (see anilist.get_query). Series that hit the
            # cache or have no SeaDex entry never call out, so they never wait.

        # Persist state before notifications so a crash during Discord doesn't
        # leave all audited series looking new on the next run.
        if self.audit_state is not None and not dry_run:
            for r in results:
                if not r.error:
                    self.audit_state.update_series(self._to_state(r), notified=False)

        # Notifications. Mark last_notified as each batch actually posts (not all
        # at the end), so a crash mid-notify can't replay already-sent series on
        # the next run.
        def _mark_notified(sent: list[AuditResult]):
            if self.audit_state is None or dry_run:
                return
            for r in sent:
                if not r.error:
                    self.audit_state.update_series(self._to_state(r), notified=True)
            self.audit_state.save()

        to_notify = [r for r in results if r.notify and r.seadex_status != "none"]
        if to_notify and (self.audit_notify_discord or notify_only) and self.discord_url:
            if self.discord_cfg.get("batch_notifications", True):
                self._send_batch_discord(to_notify, old_states, on_sent=_mark_notified)
            else:
                for r in to_notify:
                    self._send_single_discord(
                        r, old_states.get(r.sonarr_id), on_sent=_mark_notified
                    )
            stats["notified"] = len(to_notify)

        # ------------------------------------------------------------------
        # Radarr audit
        # ------------------------------------------------------------------

        movie_results: list[MovieAuditResult] = []
        movie_old_states: dict[int, Optional[MovieAuditState]] = {}
        radarr_stats: dict[str, int] = {
            "total": 0, "matched": 0, "full": 0, "partial": 0,
            "upgrade": 0, "too_large": 0, "tagged": 0, "notified": 0,
            "errors": 0, "hardlink_candidates": 0, "hardlink_mismatches": 0,
        }

        if (
            self.radarr is not None
            and self.audit_include_radarr
            and self.all_radarr_movies
            and not self._shutdown.is_set()
        ):
            all_movies = self.all_radarr_movies
            n_movies = len(all_movies)
            radarr_stats["total"] = n_movies

            self.log_arr_start(arr="radarr", n_items=n_movies)

            # Build a lookup of al_ids already seen in Sonarr specials (season 0
            # or MOVIE-format entries). Dict lookup during Radarr pass is O(1).
            sonarr_specials_by_al_id: dict[int, tuple[str, list[str]]] = {}
            for r in results:
                for entry in r.entries:
                    al_id_e = entry.get("al_id")
                    al_format_e = (entry.get("al_format") or "").upper()
                    tvdb_season_e = entry.get("tvdb_season")
                    if al_id_e and (al_format_e == "MOVIE" or tvdb_season_e == 0):
                        sonarr_specials_by_al_id[al_id_e] = (
                            r.sonarr_title,
                            entry.get("library_rgs", []),
                        )

            for idx, movie in enumerate(all_movies):
                if self._shutdown.is_set():
                    self.logger.info(
                        left_aligned_string(
                            f"Audit interrupted after {idx}/{n_movies} Radarr movies.",
                            total_length=self.log_line_length,
                        )
                    )
                    break

                self.log_arr_item_start(
                    arr="radarr",
                    item_title=movie.title,
                    n_item=idx + 1,
                    n_items=n_movies,
                )

                if self.audit_state is not None:
                    movie_old_states[movie.id] = self.audit_state.get_movie(movie.id)

                movie_result = self._audit_radarr_movie(movie, sonarr_specials_by_al_id)
                movie_results.append(movie_result)

                if movie_result.error:
                    radarr_stats["errors"] += 1
                    continue

                if movie_result.seadex_status != "none":
                    radarr_stats["matched"] += 1
                if movie_result.seadex_status == "full":
                    radarr_stats["full"] += 1
                elif movie_result.seadex_status == "partial":
                    radarr_stats["partial"] += 1
                if movie_result.upgrade_available:
                    radarr_stats["upgrade"] += 1
                if movie_result.too_large:
                    radarr_stats["too_large"] += 1
                if movie_result.hardlink_candidate:
                    radarr_stats["hardlink_candidates"] += 1
                if movie_result.hardlink_mismatch:
                    radarr_stats["hardlink_mismatches"] += 1

                self._log_movie_result(movie_result)

                if self.audit_state is not None:
                    movie_result.notify = self.audit_state.should_notify_movie(
                        new_state=self._to_movie_state(movie_result),
                        discord_cfg=self.discord_cfg,
                    )
                else:
                    movie_result.notify = (
                        movie_result.seadex_status != "none"
                        or movie_result.hardlink_mismatch
                    )

                do_tags = (apply_tags or self.audit_update_tags) and not notify_only
                if do_tags and not dry_run and self.radarr_tag_manager is not None:
                    changed = self._apply_movie_tags(movie, movie_result, dry_run=False)
                    if changed:
                        radarr_stats["tagged"] += 1
                elif dry_run and do_tags:
                    self._log_dry_run_movie_tags(movie_result)

            # Persist movie state before notifications
            if self.audit_state is not None and not dry_run:
                for mr in movie_results:
                    if not mr.error:
                        self.audit_state.update_movie(self._to_movie_state(mr), notified=False)

            def _mark_movies_notified(sent: list[MovieAuditResult]):
                if self.audit_state is None or dry_run:
                    return
                for mr in sent:
                    if not mr.error:
                        self.audit_state.update_movie(self._to_movie_state(mr), notified=True)
                self.audit_state.save()

            movies_to_notify = [
                mr for mr in movie_results
                if mr.notify and (mr.seadex_status != "none" or mr.hardlink_mismatch)
            ]
            if movies_to_notify and (self.audit_notify_discord or notify_only) and self.discord_url:
                if self.discord_cfg.get("batch_notifications", True):
                    self._send_batch_movie_discord(
                        movies_to_notify, movie_old_states, on_sent=_mark_movies_notified
                    )
                else:
                    for mr in movies_to_notify:
                        self._send_single_movie_discord(
                            mr, movie_old_states.get(mr.radarr_id), on_sent=_mark_movies_notified
                        )
                radarr_stats["notified"] = len(movies_to_notify)

        # Persist the warm AniList cache so the next run skips those calls (and
        # their rate-limit sleeps). Notifications above may have added thumbnail
        # lookups, so do this after them.
        self.persist_al_cache()

        self._log_summary(stats, radarr_stats, elapsed_s=time.monotonic() - run_start)
        return True

    # ------------------------------------------------------------------
    # Per-series audit
    # ------------------------------------------------------------------

    def _series_is_ignored(self, sonarr_series) -> bool:
        """True if the series has the seadex-ignored tag in Sonarr."""
        try:
            all_tags = self.tag_manager.get_all_tags()  # cached
            ignored_id = all_tags.get(self.tag_ignored)
            if ignored_id is None:
                return False
            series_tags = getattr(sonarr_series, "tags", None) or []
            for t in series_tags:
                tag_id = t if isinstance(t, int) else getattr(t, "id", None)
                if tag_id == ignored_id:
                    return True
        except Exception:
            pass
        return False

    def _movie_is_ignored(self, radarr_movie) -> bool:
        """True if the movie has the seadex-ignored tag in Radarr."""
        if self.radarr_tag_manager is None:
            return False
        try:
            all_tags = self.radarr_tag_manager.get_all_tags()  # cached
            ignored_id = all_tags.get(self.tag_ignored)
            if ignored_id is None:
                return False
            movie_tags = getattr(radarr_movie, "tags", None) or []
            for t in movie_tags:
                tag_id = t if isinstance(t, int) else getattr(t, "id", None)
                if tag_id == ignored_id:
                    return True
        except Exception:
            pass
        return False

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

        # Skip series manually tagged seadex-ignored in Sonarr
        if self._series_is_ignored(sonarr_series):
            self.logger.info(
                centred_string(
                    f"Skipping — tagged {self.tag_ignored}",
                    total_length=self.log_line_length,
                )
            )
            self.logger.info(
                centred_string(
                    "-" * self.log_line_length,
                    total_length=self.log_line_length,
                )
            )
            return result

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
            result.missing_specials = any(
                d.get("missing_specials") or d.get("missing_specials_unknown")
                for d in per_al
            )
            result.missing_season = any(d.get("missing_season") for d in per_al)
            result.desired_tags = self._compute_desired_tags(result)

            # Keep every sub-entry so notifications can break down which
            # season / cour / part / movie is actionable, with its own link.
            result.entries = per_al

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
            # Item identity for notifications: AniList format (TV / MOVIE /
            # SPECIAL / OVA / ONA) plus the TVDB season number, so each match can
            # be labelled "Season 2" / "Movie" / "Specials" instead of collapsing
            # to the series name. tvdb_season is free (already in the mapping);
            # al_format is filled below only when SeaDex actually tracks the item.
            "al_format": None,
            "tvdb_season": get_tvdb_season(mapping),
            "seadex_status": "none",
            "seadex_rgs": [],
            "seadex_size_bytes": 0,
            # SeaDex release groups split by tier (after tracker/public/ignore
            # filters) so notifications can flag when an owned release is an alt
            # and name the best one the user could move to.
            "seadex_best_rgs": [],
            "seadex_alt_rgs": [],
            # Smallest alt release (name + total bytes) so an upgrade notification
            # can offer a smaller alt to grab instead of the best recommendation
            # — relevant when alt_is_acceptable and you own neither tier.
            "alt_release_rg": None,
            "alt_release_size_bytes": 0,
            "library_rgs": [],
            "library_size_bytes": 0,
            "upgrade_available": False,
            "too_large": False,
            "missing_episodes": [],
            # S00 episodes SeaDex tracks that are absent from library entirely.
            # List of (season, episode) pairs; empty = none missing.
            "missing_specials": [],
            # True when SeaDex covers this as specials but filenames couldn't be
            # parsed so specific episode numbers are unknown.
            "missing_specials_unknown": False,
            # True when SeaDex tracks this non-specials entry (season/OVA/etc.)
            # but library has zero files for it.
            "missing_season": False,
        }

        sd_entry = self.get_seadex_entry(al_id=al_id)
        if sd_entry is None:
            return out

        out["sd_url"] = sd_entry.url
        out["anilist_title"] = self.get_anilist_title(al_id=al_id, sd_entry=sd_entry)
        # get_anilist_title warmed al_cache with the full Media query, so this is
        # a cache hit — no extra network round-trip.
        out["al_format"], self.al_cache = get_anilist_format(
            al_id=al_id, al_cache=self.al_cache
        )

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

        # Does the library already hold a SeaDex-listed release (best OR alt)?
        # When alt_is_acceptable, this both rescues the upgrade flag below and
        # stops filter_seadex_downloads from logging a misleading "tagging" line
        # for a series we won't actually flag.
        best_rgs, alt_rgs = self._seadex_rg_tiers(sd_entry)
        out["seadex_best_rgs"] = sorted(best_rgs)
        out["seadex_alt_rgs"] = sorted(alt_rgs)
        alt_rg, alt_size = self._smallest_alt_release(sd_entry, alt_rgs)
        out["alt_release_rg"] = alt_rg
        out["alt_release_size_bytes"] = alt_size
        owned_sd_rgs = set(out["library_rgs"]) & (best_rgs | alt_rgs)
        acceptable_alt_owned = self.alt_is_acceptable and bool(owned_sd_rgs)

        _, seadex_dict = self.filter_seadex_downloads(
            al_id=al_id,
            seadex_dict=seadex_dict,
            arr="sonarr",
            arr_release_dict=sonarr_release_dict,
            ep_list=ep_list,
            acceptable_alt_owned=acceptable_alt_owned,
        )
        out["upgrade_available"] = self.get_any_to_download(seadex_dict=seadex_dict)

        # Which episodes drove the upgrade flag — union of episodes covered by
        # every torrent flagged for download. Empty for movies / packs that
        # Sonarr couldn't map to episode numbers.
        out["missing_episodes"] = self._collect_download_episodes(seadex_dict)

        # Narrow seadex_size_bytes to only the releases flagged for download so
        # the too_large check and Discord display reflect the actual upgrade size,
        # not the sum of every release SeaDex lists.
        if out["upgrade_available"]:
            dl_size = self._sum_download_size(seadex_dict)
            if dl_size > 0:
                out["seadex_size_bytes"] = dl_size

        if out["upgrade_available"] and self.size_filter_enabled:
            sd_gb = out["seadex_size_bytes"] / BYTES_PER_GB
            lib_bytes = out["library_size_bytes"]
            ratio = (out["seadex_size_bytes"] / lib_bytes) if lib_bytes > 0 else 0
            if sd_gb > self.max_absolute_gb or ratio > self.max_size_multiplier:
                out["too_large"] = True

        # If alt releases are acceptable and the library already has any
        # SeaDex-listed release (best OR alt), don't flag for upgrade.
        if (out["upgrade_available"] or out["too_large"]) and acceptable_alt_owned:
            out["upgrade_available"] = False
            out["too_large"] = False
            out["missing_episodes"] = []

        # Detect S00 episodes SeaDex tracks that are completely absent from
        # library (no file on disk), distinct from having the wrong release.
        # Only applies when this entry's mapping explicitly targets season 0,
        # OR when the resolved ep_list only contains S00 episodes (some mappings
        # omit tvdb_season but Sonarr still places the episodes in S00).
        # S00 episodes appearing in a main-series torrent (e.g. a bonus episode
        # bundled with S01) are incidental and must not trigger missing_specials,
        # since specials are audited via their own AniList entry.
        ep_list_seasons = {ep.get("seasonNumber") for ep in ep_list if ep.get("seasonNumber") is not None}
        is_specials_entry = out.get("tvdb_season") == 0 or (ep_list_seasons == {0})
        if is_specials_entry:
            # Restrict to S00 episodes this mapping is responsible for.  When a
            # shared torrent covers multiple specials entries (e.g. one pack for
            # both Kokoro-chan S00E03 and Valentine Days S00E02), each AniList
            # entry's ep_list only contains the episode(s) it maps to.  Without
            # this filter every entry would see the other's episode as "missing".
            ep_list_s00_numbers = {
                ep["episodeNumber"]
                for ep in ep_list
                if ep.get("seasonNumber") == 0
            }
            seadex_s00_eps = {
                ep["episode"]
                for rg in seadex_dict.values()
                for url in (rg.get("urls") or {}).values()
                for ep in url.get("episodes", [])
                if ep.get("season") == 0 and ep.get("episode") in ep_list_s00_numbers
            }
            if seadex_s00_eps:
                library_s00_eps = {
                    ep["episodeNumber"]
                    for ep in ep_list
                    if ep.get("seasonNumber") == 0 and ep.get("episodeFileId", 0) != 0
                }
                missing = sorted(seadex_s00_eps - library_s00_eps)
                out["missing_specials"] = [(0, e) for e in missing]
                # All tracked specials absent → upgrade_available is a false positive
                if missing and not library_s00_eps:
                    out["upgrade_available"] = False
                    out["too_large"] = False
                    out["missing_episodes"] = []
            elif not out["library_rgs"] and out["seadex_status"] == "full":
                # SeaDex covers this as specials but filenames couldn't be parsed,
                # so we can't enumerate which episodes are missing.
                out["missing_specials_unknown"] = True
                out["upgrade_available"] = False
                out["too_large"] = False
                out["missing_episodes"] = []

        # Detect non-specials entries SeaDex tracks that are entirely absent.
        # library_rgs == [] means zero files on disk for this entry.
        if (
            out["seadex_status"] == "full"
            and not out["library_rgs"]
            and not is_specials_entry
        ):
            out["missing_season"] = True
            # upgrade_available is a false positive when nothing is in library
            out["upgrade_available"] = False
            out["too_large"] = False
            out["missing_episodes"] = []

        return out

    @staticmethod
    def _collect_download_episodes(seadex_dict) -> list[tuple[int, int]]:
        """(season, episode) pairs covered by any torrent flagged for download."""
        eps: set[tuple[int, int]] = set()
        for rg_item in seadex_dict.values():
            for url_item in (rg_item.get("urls") or {}).values():
                if not url_item.get("download"):
                    continue
                for ep in url_item.get("episodes", []):
                    season = ep.get("season")
                    episode = ep.get("episode")
                    if season is not None and episode is not None:
                        eps.add((season, episode))
        return sorted(eps)

    @staticmethod
    def _format_episode_ranges(episodes: list[tuple[int, int]]) -> str:
        """Compress (season, episode) pairs to e.g. 'S01E01-13, S02E03'."""
        if not episodes:
            return ""
        parts: list[str] = []
        for season, group in groupby(sorted(episodes), key=lambda x: x[0]):
            ep_nums = sorted(e for _, e in group)
            runs: list[tuple[int, int]] = []
            start = prev = ep_nums[0]
            for n in ep_nums[1:]:
                if n == prev + 1:
                    prev = n
                else:
                    runs.append((start, prev))
                    start = prev = n
            runs.append((start, prev))
            for a, b in runs:
                parts.append(f"S{season:02d}E{a:02d}" + (f"-{b:02d}" if b != a else ""))
        return ", ".join(parts)

    def _seadex_rg_tiers(self, sd_entry) -> tuple[set, set]:
        """SeaDex release groups split into (best, alt) sets, honoring the same
        tracker/public/ignore-tag filters as get_seadex_dict so the tiers match
        what we'd actually grab. A group tagged best on any of its torrents
        counts as best and is excluded from the alt set.

        IMPORTANT: release groups on trackers not in self.trackers (or on private
        trackers when public_only=True) are excluded from both sets. If you own a
        SeaDex-listed release that lives on a tracker outside your allowed list,
        it will NOT appear in alt_rgs, owned_sd_rgs will be empty, and
        alt_is_acceptable will have no effect — the entry will still be flagged
        for upgrade. Fix: add the tracker to your trackers list in config, or set
        public_only: false if it is a private tracker.
        """
        candidates = [
            t for t in sd_entry.torrents
            if not set(self.ignore_tags) & set(t.tags)
            and t.tracker.lower() in self.trackers
        ]
        if self.public_only:
            candidates = [t for t in candidates if t.tracker.is_public()]
        best_rgs = {t.release_group for t in candidates if t.is_best}
        alt_rgs = {t.release_group for t in candidates if not t.is_best} - best_rgs

        self.logger.debug(
            left_aligned_string(
                f"SeaDex tiers — best: {sorted(best_rgs) or 'none'} | alt: {sorted(alt_rgs) or 'none'}",
                total_length=self.log_line_length,
            )
        )

        return best_rgs, alt_rgs

    def _smallest_alt_release(self, sd_entry, alt_rgs=None) -> tuple[Optional[str], int]:
        """Smallest alt (non-best) release as ``(release_group, total_bytes)``,
        honoring the same tracker/public/ignore filters as the tiers. Each
        torrent's total is the sum of its file sizes; the smallest single torrent
        among the alt groups wins. ``(None, 0)`` if no alt has a known size.

        Pass ``alt_rgs`` to reuse an already-computed alt set, else it's derived.
        """
        if alt_rgs is None:
            _, alt_rgs = self._seadex_rg_tiers(sd_entry)
        candidates = [
            t for t in sd_entry.torrents
            if t.release_group in alt_rgs
            and not set(self.ignore_tags) & set(t.tags)
            and t.tracker.lower() in self.trackers
        ]
        if self.public_only:
            candidates = [t for t in candidates if t.tracker.is_public()]

        best: Optional[tuple[int, str]] = None
        for t in candidates:
            total = sum(f.size for f in t.files if getattr(f, "size", 0))
            if total <= 0:
                continue
            if best is None or total < best[0]:
                best = (total, t.release_group)
        return (best[1], best[0]) if best else (None, 0)

    def _library_has_seadex_rg(self, sd_entry, library_rgs) -> bool:
        """True if the library holds any SeaDex-listed release group (best or
        alt), honoring the same tracker/public/ignore-tag filters as
        get_seadex_dict so acceptability matches what we'd actually grab."""
        best_rgs, alt_rgs = self._seadex_rg_tiers(sd_entry)
        return bool(set(library_rgs) & (best_rgs | alt_rgs))

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

        if result.missing_specials:
            tags.append(self.tag_missing_specials)

        if result.missing_season:
            tags.append(self.tag_missing_season)

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
        if result.missing_season:
            return _COLOUR_MISSING_SEASON
        if result.missing_specials:
            return _COLOUR_MISSING_SPECIALS
        if result.upgrade_available:
            return _COLOUR_UPGRADE
        if result.seadex_status == "full":
            return _COLOUR_FULL
        if result.seadex_status == "partial":
            return _COLOUR_PARTIAL
        return _COLOUR_NONE

    @staticmethod
    def _record_links(
        al_id: Optional[int] = None,
        tvdb_id: Optional[int] = None,
        sd_url: Optional[str] = None,
    ) -> str:
        """Markdown link list for the AniList / TVDB / SeaDex records.

        Embed field values and descriptions render markdown, so these turn the
        bare IDs already on the result into clickable records — mirroring the
        log line's ``AniList: <title> (<releases.moe url>)``.
        """
        links = []
        if al_id:
            links.append(f"[AniList](https://anilist.co/anime/{al_id})")
        if tvdb_id:
            links.append(f"[TVDB](https://thetvdb.com/dereferrer/series/{tvdb_id})")
        if sd_url:
            links.append(f"[SeaDex]({sd_url})")
        return " • ".join(links)

    # AniList format -> label for non-seasonal items. TV / TV_SHORT fall through
    # to a "Season N" label derived from the TVDB season instead.
    _FORMAT_LABELS = {
        "MOVIE": "Movie",
        "SPECIAL": "Special",
        "OVA": "OVA",
        "ONA": "ONA",
        "MUSIC": "Music video",
    }
    MAX_ITEM_FIELDS = 20  # leave headroom under Discord's 25-field embed cap

    def _item_label(self, entry: dict) -> str:
        """Name which part of the series this match is: "Movie", "Special",
        "OVA", or "Season N" (TVDB season 0 -> "Specials"). Falls back to the
        AniList title when neither format nor season pins it down."""
        fmt = (entry.get("al_format") or "").upper()
        if fmt in self._FORMAT_LABELS:
            return self._FORMAT_LABELS[fmt]
        season = entry.get("tvdb_season")
        if season == 0:
            return "Specials"
        if isinstance(season, int) and season >= 1:
            return f"Season {season}"
        return entry.get("anilist_title") or "Entry"

    @staticmethod
    def _item_rank(entry: dict) -> int:
        """Sort priority: too-large (0), missing (1), upgrade (2), covered (3)."""
        if entry.get("too_large"):
            return 0
        if (
            entry.get("missing_season")
            or entry.get("missing_specials")
            or entry.get("missing_specials_unknown")
        ):
            return 1
        if entry.get("upgrade_available"):
            return 2
        return 3

    def _item_lines(self, entry: dict) -> list[str]:
        """Status + detail lines for one matched item (no field name)."""
        lib = ", ".join(entry.get("library_rgs") or [])
        sd_bytes = entry.get("seadex_size_bytes", 0)
        lib_bytes = entry.get("library_size_bytes", 0)
        sd_gb = sd_bytes / BYTES_PER_GB
        delta_gb = sd_gb - lib_bytes / BYTES_PER_GB
        # "Free win": SeaDex's recommended release is both better AND smaller, so
        # the upgrade costs no extra disk. Only when both sizes are known, else a
        # 0-byte unknown would masquerade as a saving.
        free_win = (
            entry.get("upgrade_available")
            and sd_bytes > 0
            and lib_bytes > 0
            and delta_gb < 0
        )

        missing_specials_eps = entry.get("missing_specials", [])
        missing_specials_unknown = entry.get("missing_specials_unknown", False)

        if entry.get("too_large"):
            head = f"🔴 upgrade too large — {sd_gb:.1f} GB ({delta_gb:+.1f} GB vs yours)"
        elif entry.get("missing_season"):
            head = "🟣 missing — in SeaDex but not in library"
        elif missing_specials_eps or missing_specials_unknown:
            if missing_specials_unknown:
                head = "🔵 missing specials — in SeaDex but not in library"
            else:
                eps = self._format_episode_ranges(missing_specials_eps)
                head = f"🔵 missing specials — {eps}"
        elif entry.get("upgrade_available"):
            detail = f"{sd_gb:.1f} GB ({delta_gb:+.1f} GB vs yours)"
            eps = self._format_episode_ranges(entry.get("missing_episodes", []))
            if eps:
                detail += f", missing {eps}"
            if free_win:
                head = f"💰 free win — better AND smaller, {detail}"
            else:
                head = f"🟠 upgrade available — {detail}"
        else:
            head = f"🟢 covered — you have {lib}" if lib else "🟢 covered"

        lines = [head]

        alt_acceptable = getattr(self, "alt_is_acceptable", False)
        actionable = entry.get("upgrade_available") or entry.get("too_large")

        # Upgrade/too-large case: you own neither best nor alt (else the upgrade
        # would have been suppressed). When alts are acceptable, offer the
        # smallest alt as a lighter alternative to the best recommendation —
        # especially valuable when the best is flagged too large but an alt fits.
        if alt_acceptable and actionable:
            alt_bytes = entry.get("alt_release_size_bytes", 0)
            alt_rg = entry.get("alt_release_rg")
            if alt_bytes > 0 and alt_rg:
                alt_gb = alt_bytes / BYTES_PER_GB
                alt_delta = alt_gb - lib_bytes / BYTES_PER_GB
                lines.append(
                    f"↳ alt option: {alt_gb:.1f} GB ({alt_delta:+.1f} GB vs yours) "
                    f"via {alt_rg}"
                )

        # Covered-by-alt case: name the owned alt and the best release the user
        # could move to.
        if alt_acceptable and not actionable:
            best_rgs = entry.get("seadex_best_rgs") or []
            alt_rgs = entry.get("seadex_alt_rgs") or []
            owned = set(entry.get("library_rgs") or [])
            owned_alt = owned & set(alt_rgs)
            owned_best = owned & set(best_rgs)
            if owned_alt and not owned_best and best_rgs:
                lines.append(
                    f"↳ {', '.join(sorted(owned_alt))} is an alt release • "
                    f"SeaDex best: {', '.join(best_rgs)}"
                )

        # Each item maps to its own AniList entry and SeaDex page; the TVDB
        # record is series-level and shown once in the embed description.
        links = self._record_links(al_id=entry.get("al_id"), sd_url=entry.get("sd_url"))
        if links:
            lines.append(links)
        return lines

    def _item_field(self, entry: dict, name: str) -> dict:
        return {
            "name": name[:256],
            "value": "\n".join(self._item_lines(entry))[:1024],
            "inline": False,
        }

    def _tracked_items(self, result: AuditResult) -> list[dict]:
        """Items SeaDex actually tracks (covered or actionable), worst first.
        Items SeaDex doesn't list at all are excluded — they're only counted."""
        tracked = [
            e for e in result.entries
            if e.get("seadex_status") in ("full", "partial")
        ]
        tracked.sort(key=lambda e: (self._item_rank(e), self._item_label(e)))
        return tracked

    def _labelled_items(self, items: list[dict]) -> list[tuple[dict, str]]:
        """Pair each item with its display label, disambiguating split cours
        that share a TVDB season by appending the AniList title."""
        labels = [self._item_label(e) for e in items]
        dupes = {label for label in labels if labels.count(label) > 1}
        out = []
        for entry, label in zip(items, labels):
            if label in dupes and entry.get("anilist_title"):
                label = f"{label} · {entry['anilist_title']}"
            out.append((entry, label))
        return out

    @staticmethod
    def _verdict(result: AuditResult) -> tuple[str, str]:
        """Headline ``(emoji, verdict)`` — answers "do I need to act?", not the
        internal status. Actionable states win so the headline leads with them
        even when SeaDex coverage is otherwise "full"."""
        if result.too_large:
            return "🔴", "upgrade skipped (too large)"
        if result.upgrade_available:
            return "🟠", "better release available"
        if result.seadex_status == "partial":
            return "🟡", "partially tracked"
        if result.seadex_status == "full":
            return "🟢", "covered, nothing to do"
        return "⚪", "no SeaDex match"

    def _summary_sentence(
        self, result: AuditResult, old_state: Optional[SeriesAuditState]
    ) -> str:
        """One plain-English sentence describing the event that fired this
        notification — what SeaDex did and whether action is needed. Release
        groups appear only with context, never as a bare side-by-side compare."""
        was_new = old_state is None or old_state.seadex_status == "none"
        have = (
            f"your release ({', '.join(result.library_rgs)})"
            if result.library_rgs
            else "your library"
        )

        if result.too_large:
            sd_gb = result.seadex_size_bytes / BYTES_PER_GB
            delta_gb = sd_gb - result.library_size_bytes / BYTES_PER_GB
            return (
                f"SeaDex's recommended release is {sd_gb:.1f} GB "
                f"({delta_gb:+.1f} GB vs yours) — over your size limit. "
                f"Tagged `{self.tag_too_large}` — not downloaded."
            )

        if result.upgrade_available:
            lead = "SeaDex now tracks this and recommends" if was_new else "SeaDex recommends"
            return (
                f"{lead} a release you don't have yet. "
                f"Tagged `{self.tag_upgrade}` — not downloaded (audit mode)."
            )

        if result.seadex_status == "partial":
            return "SeaDex covers some entries for this title but not all."

        if result.seadex_status != "full":
            return "SeaDex has no tracked release for this title."

        # full coverage, no action needed — distinguish a brand-new match from a
        # later change to SeaDex's release list.
        if was_new:
            return f"SeaDex now tracks this title and {have} is on its list."

        added, removed = rg_diff(old_state, self._to_state(result))
        if added or removed:
            changes = []
            if added:
                changes.append(f"added {', '.join(added)}")
            if removed:
                changes.append(f"removed {', '.join(removed)}")
            return (
                f"SeaDex updated its release list ({'; '.join(changes)}), but {have} "
                f"still satisfies it — no action needed."
            )
        return f"SeaDex still tracks this title and {have} is on its list."

    def _build_embed(
        self, result: AuditResult, old_state: Optional[SeriesAuditState]
    ) -> dict:
        """Build one Discord embed shared by the single- and batch-send paths so
        both render identically: a verdict headline, one explanatory sentence,
        and one field per SeaDex-tracked item (season / cour / movie / special)
        — covered ones included — so the reader sees exactly what matched."""
        tracked = self._tracked_items(result)
        n_action = sum(1 for e in tracked if self._item_rank(e) < 3)

        emoji, verdict = self._verdict(result)
        title = result.anilist_title or result.sonarr_title
        # With several matched items, the headline counts how many need action;
        # a single item keeps the plain verdict.
        if len(tracked) > 1 and n_action:
            verdict = f"{n_action} of {len(tracked)} items need action"
        headline = f"{emoji} {title}"
        if verdict:
            headline = f"{headline} — {verdict}"

        desc_parts = [self._summary_sentence(result, old_state)]

        # One field per tracked item, worst first, capped under Discord's field
        # limit. Overflow is lowest-priority (already covered) so it collapses
        # to a count rather than being dropped silently.
        labelled = self._labelled_items(tracked)
        shown = labelled[: self.MAX_ITEM_FIELDS]
        fields = [self._item_field(entry, name) for entry, name in shown]
        overflow = len(labelled) - len(shown)
        if overflow:
            fields.append({
                "name": f"+{overflow} more covered",
                "value": "Already hold a SeaDex-listed release.",
                "inline": False,
            })

        # Items SeaDex doesn't track at all are noted, not listed individually.
        untracked = sum(
            1 for e in result.entries if e.get("seadex_status") == "none"
        )
        if untracked:
            desc_parts.append(f"_{untracked} other item(s) not tracked by SeaDex._")

        # Series-level TVDB record — per-item fields carry their own AniList and
        # SeaDex links, so only the series-wide record belongs here.
        tvdb_record = self._record_links(tvdb_id=result.tvdb_id)
        if tvdb_record:
            desc_parts.append(tvdb_record)

        embed: dict = {
            "author": {
                "name": "SeaDexArr Audit",
                "url": "https://github.com/bbtufty/seadexarr",
            },
            "title": headline[:256],
            "description": "\n\n".join(desc_parts),
            "color": self._embed_colour(result),
        }
        if result.sd_url:
            embed["url"] = result.sd_url
        if fields:
            embed["fields"] = fields

        anilist_thumb, self.al_cache = get_anilist_thumb(
            al_id=result.al_id,
            al_cache=self.al_cache,
        )
        if anilist_thumb:
            embed["thumbnail"] = {"url": anilist_thumb}

        return embed

    def _discord_post_with_retry(self, embeds: list, label: str = ""):
        """Post embeds to Discord, retrying up to 3 times on 429 rate-limit responses."""
        discord = Discord(url=self.discord_url)
        for attempt in range(3):
            response = discord.post(embeds=embeds)
            if response is None or response.ok:
                return response
            if response.status_code == 429:
                try:
                    retry_after = float(response.json().get("retry_after", 1.0))
                except Exception:
                    retry_after = 1.0
                sleep_for = retry_after + 0.2
                self.logger.warning(
                    "Discord rate limited%s — sleeping %.1fs (attempt %d/3)",
                    f" for {label}" if label else "", sleep_for, attempt + 1,
                )
                time.sleep(sleep_for)
                discord = Discord(url=self.discord_url)
                continue
            return response
        return response

    def _send_single_discord(
        self,
        result: AuditResult,
        old_state: Optional[SeriesAuditState],
        on_sent=None,
    ):
        if not self.discord_url:
            return
        response = self._discord_post_with_retry(
            [self._build_embed(result, old_state)], label=result.sonarr_title
        )
        if response is not None and not response.ok:
            self.logger.warning(
                "Discord post failed for %s (%s): %s",
                result.sonarr_title, response.status_code, response.text[:200],
            )
            time.sleep(1)
            return
        if on_sent is not None:
            on_sent([result])
        time.sleep(1)

    def _send_batch_discord(
        self,
        results: list[AuditResult],
        old_states: dict[int, Optional[SeriesAuditState]],
        on_sent=None,
    ):
        if not self.discord_url or not results:
            return

        BATCH_SIZE = 3  # conservative default; falls back to 1 on 400
        for i in range(0, len(results), BATCH_SIZE):
            batch = results[i : i + BATCH_SIZE]
            embeds = [
                self._build_embed(r, old_states.get(r.sonarr_id)) for r in batch
            ]
            response = self._discord_post_with_retry(embeds)
            if response is not None and not response.ok:
                if len(batch) > 1:
                    # Batch exceeded Discord's 6000-char limit — retry one at a time.
                    self.logger.warning(
                        "Discord batch post failed (%s) — retrying %d embeds individually",
                        response.status_code, len(batch),
                    )
                    time.sleep(1)
                    for r, embed in zip(batch, embeds):
                        r_resp = self._discord_post_with_retry([embed], label=r.sonarr_title)
                        if r_resp is not None and not r_resp.ok:
                            self.logger.warning(
                                "Discord post failed for %s (%s): %s",
                                r.sonarr_title, r_resp.status_code, r_resp.text[:200],
                            )
                            time.sleep(1)
                            continue
                        if on_sent is not None:
                            on_sent([r])
                        time.sleep(1)
                else:
                    self.logger.warning(
                        "Discord post failed for %s (%s): %s",
                        batch[0].sonarr_title, response.status_code, response.text[:200],
                    )
                    time.sleep(1)
                continue
            # Stamp this batch as notified only after its post succeeds, so a
            # crash on a later batch never replays the batches already sent.
            if on_sent is not None:
                on_sent(batch)
            time.sleep(1)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _iter_unique_urls(seadex_dict: dict):
        """Yield (url_data,) for each URL, skipping duplicate infohashes.

        The same torrent hosted on multiple trackers shares an infohash.
        Deduplicating here prevents double-counting sizes.
        """
        seen_hashes: set = set()
        for rg_data in seadex_dict.values():
            for url_data in (rg_data.get("urls") or {}).values():
                h = (url_data or {}).get("hash")
                if h is not None:
                    if h in seen_hashes:
                        continue
                    seen_hashes.add(h)
                yield url_data

    def _sum_seadex_size(self, seadex_dict: dict) -> int:
        """Sum all SeaDex file sizes (all releases), deduplicating by infohash."""
        total = 0
        for url_data in self._iter_unique_urls(seadex_dict):
            sizes = (url_data or {}).get("size", []) or []
            total += sum(s for s in sizes if s)
        return total

    def _sum_download_size(self, seadex_dict: dict) -> int:
        """Sum sizes of URLs flagged for download, deduplicating by infohash."""
        total = 0
        for url_data in self._iter_unique_urls(seadex_dict):
            if not (url_data or {}).get("download"):
                continue
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
            missing_specials=result.missing_specials,
            missing_season=result.missing_season,
        )

    # ------------------------------------------------------------------
    # Radarr audit
    # ------------------------------------------------------------------

    def _audit_radarr_movie(
        self,
        radarr_movie,
        sonarr_specials_by_al_id: dict,
    ) -> MovieAuditResult:
        result = MovieAuditResult(
            radarr_id=radarr_movie.id,
            tmdb_id=radarr_movie.tmdbId,
            radarr_title=radarr_movie.title,
            anilist_title=radarr_movie.title,
            al_id=None,
            sd_url=None,
            seadex_status="none",
        )

        # Skip movies manually tagged seadex-ignored in Radarr
        if self._movie_is_ignored(radarr_movie):
            self.logger.info(
                centred_string(
                    f"Skipping — tagged {self.tag_ignored}",
                    total_length=self.log_line_length,
                )
            )
            self.logger.info(
                centred_string(
                    "-" * self.log_line_length,
                    total_length=self.log_line_length,
                )
            )
            return result

        try:
            al_mappings = self.get_anilist_ids(
                tmdb_id=radarr_movie.tmdbId,
                imdb_id=radarr_movie.imdbId,
                tmdb_type="movie",
            )
            if not al_mappings:
                return result

            radarr_release_dict = self.radarr.get_radarr_release_dict(
                radarr_movie_id=radarr_movie.id
            )

            per_al: list[dict] = []
            for al_id, mapping in al_mappings.items():
                if al_id is None:
                    continue
                per_al.append(
                    self._audit_radarr_al_id(radarr_movie, al_id, radarr_release_dict)
                )

            if not per_al:
                return result

            STATUS_ORDER = ["none", "partial", "full"]
            best_status = max(
                per_al, key=lambda d: STATUS_ORDER.index(d["seadex_status"])
            )["seadex_status"]
            top_entries = [d for d in per_al if d["seadex_status"] == best_status]
            representative = top_entries[0]

            result.al_id = representative["al_id"]
            result.sd_url = representative["sd_url"]
            result.anilist_title = representative["anilist_title"]
            result.seadex_status = best_status
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
            result.desired_tags = self._compute_desired_tags_movie(result)
            result.entries = per_al

            # Cross-reference: check if any of these al_ids also appeared as a
            # Sonarr special / season-0 entry during the earlier Sonarr pass.
            for entry in per_al:
                al_id_e = entry.get("al_id")
                if al_id_e is None:
                    continue
                hit = sonarr_specials_by_al_id.get(al_id_e)
                if hit is None:
                    continue
                sonarr_title, sonarr_rgs = hit
                result.sonarr_specials_title = sonarr_title
                result.sonarr_specials_rgs = sonarr_rgs
                radarr_rg_set = set(result.library_rgs)
                sonarr_rg_set = set(sonarr_rgs)
                if radarr_rg_set and sonarr_rg_set:
                    if radarr_rg_set & sonarr_rg_set:
                        result.hardlink_candidate = True
                    else:
                        result.hardlink_mismatch = True
                break

        except Exception as e:
            import traceback as _tb
            result.error = str(e)
            self.logger.error(
                left_aligned_string(
                    f"[Radarr] Error auditing {radarr_movie.title}: {e}",
                    total_length=self.log_line_length,
                )
            )
            for tb_line in _tb.format_exc().splitlines():
                if tb_line.strip():
                    self.logger.error(f"  {tb_line}")

        return result

    def _audit_radarr_al_id(self, radarr_movie, al_id: int, radarr_release_dict: dict) -> dict:
        out: dict = {
            "al_id": al_id,
            "sd_url": None,
            "anilist_title": radarr_movie.title,
            "al_format": None,
            "seadex_status": "none",
            "seadex_rgs": [],
            "seadex_size_bytes": 0,
            "seadex_best_rgs": [],
            "seadex_alt_rgs": [],
            "alt_release_rg": None,
            "alt_release_size_bytes": 0,
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
        out["al_format"], self.al_cache = get_anilist_format(
            al_id=al_id, al_cache=self.al_cache
        )

        self.logger.debug(
            left_aligned_string(
                f"[Radarr] AL:{al_id} format={out['al_format']}",
                total_length=self.log_line_length,
            )
        )

        # Radarr only handles movies. If the AniList entry is explicitly a
        # non-movie format (SPECIAL, TV, OVA, etc.), this is a bad TMDB/IMDb
        # mapping collision — skip it. None means AniList couldn't determine
        # the format; let those through rather than silently dropping valid entries.
        al_fmt = (out["al_format"] or "").upper()
        if al_fmt and al_fmt != "MOVIE":
            self.logger.info(
                centred_string(
                    f"[Radarr] Skipping AL:{al_id} — format {out['al_format']} ≠ MOVIE (bad mapping?)",
                    total_length=self.log_line_length,
                )
            )
            return out

        out["library_rgs"] = [k for k in radarr_release_dict if k is not None]
        out["library_size_bytes"] = sum(
            (v.get("size") or 0)
            for k, v in radarr_release_dict.items()
            if k is not None and isinstance(v.get("size"), (int, float))
        )

        seadex_dict = self.get_seadex_dict(sd_entry=sd_entry)
        if not seadex_dict:
            return out

        out["seadex_status"] = "full"
        out["seadex_rgs"] = list(seadex_dict.keys())
        out["seadex_size_bytes"] = self._sum_seadex_size(seadex_dict)

        best_rgs, alt_rgs = self._seadex_rg_tiers(sd_entry)
        out["seadex_best_rgs"] = sorted(best_rgs)
        out["seadex_alt_rgs"] = sorted(alt_rgs)
        alt_rg, alt_size = self._smallest_alt_release(sd_entry, alt_rgs)
        out["alt_release_rg"] = alt_rg
        out["alt_release_size_bytes"] = alt_size

        owned_sd_rgs = set(out["library_rgs"]) & (best_rgs | alt_rgs)
        acceptable_alt_owned = self.alt_is_acceptable and bool(owned_sd_rgs)

        # filter_by_release_group expects string keys and list-valued sizes.
        # Radarr's get_radarr_release_dict uses None key when no release group
        # exists and stores size as a scalar int — strip None keys, wrap sizes.
        radarr_release_dict_norm = {
            rg: {
                **data,
                "size": (
                    [data["size"]]
                    if isinstance(data.get("size"), (int, float))
                    else (data.get("size") or [])
                ),
            }
            for rg, data in radarr_release_dict.items()
            if rg is not None
        }

        _, seadex_dict = self.filter_seadex_downloads(
            al_id=al_id,
            seadex_dict=seadex_dict,
            arr="radarr",
            arr_release_dict=radarr_release_dict_norm,
            acceptable_alt_owned=acceptable_alt_owned,
        )
        out["upgrade_available"] = self.get_any_to_download(seadex_dict=seadex_dict)

        if out["upgrade_available"]:
            dl_size = self._sum_download_size(seadex_dict)
            if dl_size > 0:
                out["seadex_size_bytes"] = dl_size

        if out["upgrade_available"] and self.size_filter_enabled:
            sd_gb = out["seadex_size_bytes"] / BYTES_PER_GB
            lib_bytes = out["library_size_bytes"]
            ratio = (out["seadex_size_bytes"] / lib_bytes) if lib_bytes > 0 else 0
            if sd_gb > self.max_absolute_gb or ratio > self.max_size_multiplier:
                out["too_large"] = True

        if (out["upgrade_available"] or out["too_large"]) and acceptable_alt_owned:
            out["upgrade_available"] = False
            out["too_large"] = False

        return out

    def _compute_desired_tags_movie(self, result: MovieAuditResult) -> list[str]:
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

    def _apply_movie_tags(
        self, radarr_movie, result: MovieAuditResult, dry_run: bool
    ) -> bool:
        if self.radarr_tag_manager is None:
            return False
        try:
            movie_json = self.radarr_tag_manager.get_movie_json(radarr_movie.id)
            current_tags = movie_json.get("tags", [])
            new_tags, changed = self.radarr_tag_manager.compute_tag_changes(
                current_tag_ids=current_tags,
                desired_labels=result.desired_tags,
                managed_labels=self.managed_tag_labels,
                remove_stale=self.audit_remove_stale,
            )
            if changed:
                self.radarr_tag_manager.set_movie_tags(
                    radarr_movie.id, new_tags, dry_run=dry_run
                )
                self.logger.info(
                    centred_string(
                        f"[Radarr] Tags updated for {result.radarr_title}: {result.desired_tags}",
                        total_length=self.log_line_length,
                    )
                )
            return changed
        except Exception as e:
            self.logger.error(
                left_aligned_string(
                    f"[Radarr] Tag update failed for {result.radarr_title}: {e}",
                    total_length=self.log_line_length,
                )
            )
            return False

    def _log_dry_run_movie_tags(self, result: MovieAuditResult):
        self.logger.info(
            centred_string(
                f"[DRY RUN] [Radarr] {result.radarr_title} → tags: {result.desired_tags}",
                total_length=self.log_line_length,
            )
        )

    def _to_movie_state(self, result: MovieAuditResult) -> MovieAuditState:
        return MovieAuditState(
            radarr_id=result.radarr_id,
            tmdb_id=result.tmdb_id,
            title=result.radarr_title,
            seadex_status=result.seadex_status,
            seadex_rgs=result.seadex_rgs,
            seadex_size_bytes=result.seadex_size_bytes,
            library_rgs=result.library_rgs,
            upgrade_available=result.upgrade_available,
            too_large=result.too_large,
            hardlink_mismatch=result.hardlink_mismatch,
        )

    # ------------------------------------------------------------------
    # Radarr Discord
    # ------------------------------------------------------------------

    @staticmethod
    def _embed_colour_movie(result: MovieAuditResult) -> int:
        if result.hardlink_mismatch:
            return _COLOUR_TOO_LARGE
        if result.too_large:
            return _COLOUR_TOO_LARGE
        if result.upgrade_available:
            return _COLOUR_UPGRADE
        if result.seadex_status == "full":
            return _COLOUR_FULL
        if result.seadex_status == "partial":
            return _COLOUR_PARTIAL
        return _COLOUR_NONE

    def _verdict_movie(self, result: MovieAuditResult) -> tuple[str, str]:
        if result.hardlink_mismatch:
            return "⚠️", "hard-link mismatch"
        if result.too_large:
            return "🔴", "upgrade skipped (too large)"
        if result.upgrade_available:
            return "🟠", "better release available"
        if result.seadex_status == "partial":
            return "🟡", "partially tracked"
        if result.seadex_status == "full":
            return "🟢", "covered, nothing to do"
        return "⚪", "no SeaDex match"

    def _summary_sentence_movie(
        self,
        result: MovieAuditResult,
        old_state: Optional[MovieAuditState],
    ) -> str:
        was_new = old_state is None or old_state.seadex_status == "none"
        have = (
            f"your release ({', '.join(result.library_rgs)})"
            if result.library_rgs
            else "your library"
        )

        if result.hardlink_mismatch:
            radarr_rg = ", ".join(result.library_rgs) or "no file"
            sonarr_rg = ", ".join(result.sonarr_specials_rgs) or "no file"
            return (
                f"This movie is also in Sonarr specials ({result.sonarr_specials_title}) "
                f"but with a different release group — "
                f"Radarr: {radarr_rg}, Sonarr: {sonarr_rg}. "
                f"Hard-linking is not possible as-is."
            )

        if result.too_large:
            sd_gb = result.seadex_size_bytes / BYTES_PER_GB
            delta_gb = sd_gb - result.library_size_bytes / BYTES_PER_GB
            return (
                f"SeaDex's recommended release is {sd_gb:.1f} GB "
                f"({delta_gb:+.1f} GB vs yours) — over your size limit. "
                f"Tagged `{self.tag_too_large}` — not downloaded."
            )

        if result.upgrade_available:
            lead = "SeaDex now tracks this and recommends" if was_new else "SeaDex recommends"
            return (
                f"{lead} a release you don't have yet. "
                f"Tagged `{self.tag_upgrade}` — not downloaded (audit mode)."
            )

        if result.seadex_status == "partial":
            return "SeaDex covers some entries for this title but not all."

        if result.seadex_status != "full":
            return "SeaDex has no tracked release for this title."

        if was_new:
            return f"SeaDex now tracks this title and {have} is on its list."

        return f"SeaDex still tracks this title and {have} is on its list."

    def _build_movie_embed(
        self,
        result: MovieAuditResult,
        old_state: Optional[MovieAuditState],
    ) -> dict:
        emoji, verdict = self._verdict_movie(result)
        title = result.anilist_title or result.radarr_title
        headline = f"{emoji} {title} — {verdict}"

        desc_parts = [self._summary_sentence_movie(result, old_state)]

        fields: list[dict] = []

        # Status field for the movie itself
        lib = ", ".join(result.library_rgs) if result.library_rgs else None
        sd_gb = result.seadex_size_bytes / BYTES_PER_GB
        lib_bytes = result.library_size_bytes
        delta_gb = sd_gb - lib_bytes / BYTES_PER_GB
        free_win = (
            result.upgrade_available
            and result.seadex_size_bytes > 0
            and lib_bytes > 0
            and delta_gb < 0
        )
        if result.too_large:
            status_val = f"🔴 upgrade too large — {sd_gb:.1f} GB ({delta_gb:+.1f} GB vs yours)"
        elif result.upgrade_available:
            if free_win:
                status_val = f"💰 free win — better AND smaller, {sd_gb:.1f} GB ({delta_gb:+.1f} GB vs yours)"
            else:
                status_val = f"🟠 upgrade available — {sd_gb:.1f} GB ({delta_gb:+.1f} GB vs yours)"
        else:
            status_val = f"🟢 covered — you have {lib}" if lib else "🟢 covered"

        # Alt-release lines — mirrors _item_lines logic, pulling per-al-id
        # detail from the representative entry.
        alt_lines: list[str] = []
        rep = result.entries[0] if result.entries else {}
        actionable = result.upgrade_available or result.too_large
        if self.alt_is_acceptable and actionable:
            alt_bytes = rep.get("alt_release_size_bytes", 0)
            alt_rg = rep.get("alt_release_rg")
            if alt_bytes > 0 and alt_rg:
                alt_gb = alt_bytes / BYTES_PER_GB
                alt_delta = alt_gb - lib_bytes / BYTES_PER_GB
                alt_lines.append(
                    f"↳ alt option: {alt_gb:.1f} GB ({alt_delta:+.1f} GB vs yours) "
                    f"via {alt_rg}"
                )
        if self.alt_is_acceptable and not actionable:
            best_rgs = rep.get("seadex_best_rgs") or []
            alt_rgs = rep.get("seadex_alt_rgs") or []
            owned = set(result.library_rgs)
            owned_alt = owned & set(alt_rgs)
            owned_best = owned & set(best_rgs)
            if owned_alt and not owned_best and best_rgs:
                alt_lines.append(
                    f"↳ {', '.join(sorted(owned_alt))} is an alt release • "
                    f"SeaDex best: {', '.join(best_rgs)}"
                )

        if alt_lines:
            status_val += "\n" + "\n".join(alt_lines)

        links = self._record_links(al_id=result.al_id, sd_url=result.sd_url)
        if links:
            status_val += f"\n{links}"
        fields.append({"name": "Movie", "value": status_val[:1024], "inline": False})

        # Cross-reference field
        if result.sonarr_specials_title:
            if result.hardlink_mismatch:
                xref_name = "⚠️ Hard-link mismatch"
                radarr_rg_str = ", ".join(result.library_rgs) or "no file"
                sonarr_rg_str = ", ".join(result.sonarr_specials_rgs) or "no file"
                xref_val = (
                    f"Also in Sonarr specials: **{result.sonarr_specials_title}**\n"
                    f"Radarr: {radarr_rg_str} · Sonarr: {sonarr_rg_str}\n"
                    f"Different release groups — hard-link not possible as-is."
                )
            elif result.hardlink_candidate:
                xref_name = "✅ Hard-link candidate"
                xref_val = (
                    f"Also in Sonarr specials: **{result.sonarr_specials_title}**\n"
                    f"Same release group — verify hard-link is configured."
                )
            else:
                xref_name = "ℹ️ Also in Sonarr specials"
                xref_val = result.sonarr_specials_title or ""
            fields.append({"name": xref_name, "value": xref_val[:1024], "inline": False})

        # TMDB record link in description
        if result.tmdb_id:
            desc_parts.append(
                f"[TMDB](https://www.themoviedb.org/movie/{result.tmdb_id})"
            )

        embed: dict = {
            "author": {
                "name": "SeaDexArr Audit",
                "url": "https://github.com/bbtufty/seadexarr",
            },
            "title": headline[:256],
            "description": "\n\n".join(desc_parts),
            "color": self._embed_colour_movie(result),
        }
        if result.sd_url:
            embed["url"] = result.sd_url
        if fields:
            embed["fields"] = fields

        anilist_thumb, self.al_cache = get_anilist_thumb(
            al_id=result.al_id, al_cache=self.al_cache
        )
        if anilist_thumb:
            embed["thumbnail"] = {"url": anilist_thumb}

        return embed

    def _send_single_movie_discord(
        self,
        result: MovieAuditResult,
        old_state: Optional[MovieAuditState],
        on_sent=None,
    ):
        if not self.discord_url:
            return
        response = self._discord_post_with_retry(
            [self._build_movie_embed(result, old_state)], label=result.radarr_title
        )
        if response is not None and not response.ok:
            self.logger.warning(
                "Discord post failed for %s (%s): %s",
                result.radarr_title, response.status_code, response.text[:200],
            )
            time.sleep(1)
            return
        if on_sent is not None:
            on_sent([result])
        time.sleep(1)

    def _send_batch_movie_discord(
        self,
        results: list[MovieAuditResult],
        old_states: dict[int, Optional[MovieAuditState]],
        on_sent=None,
    ):
        if not self.discord_url or not results:
            return
        BATCH_SIZE = 3
        for i in range(0, len(results), BATCH_SIZE):
            batch = results[i : i + BATCH_SIZE]
            embeds = [
                self._build_movie_embed(mr, old_states.get(mr.radarr_id)) for mr in batch
            ]
            response = self._discord_post_with_retry(embeds)
            if response is not None and not response.ok:
                if len(batch) > 1:
                    self.logger.warning(
                        "Discord batch post failed (%s) — retrying %d embeds individually",
                        response.status_code, len(batch),
                    )
                    time.sleep(1)
                    for mr, embed in zip(batch, embeds):
                        r_resp = self._discord_post_with_retry([embed], label=mr.radarr_title)
                        if r_resp is not None and not r_resp.ok:
                            self.logger.warning(
                                "Discord post failed for %s (%s): %s",
                                mr.radarr_title, r_resp.status_code, r_resp.text[:200],
                            )
                            time.sleep(1)
                            continue
                        if on_sent is not None:
                            on_sent([mr])
                        time.sleep(1)
                else:
                    self.logger.warning(
                        "Discord post failed for %s (%s): %s",
                        batch[0].radarr_title, response.status_code, response.text[:200],
                    )
                    time.sleep(1)
                continue
            if on_sent is not None:
                on_sent(batch)
            time.sleep(1)

    def _log_movie_result(self, result: MovieAuditResult):
        status_str = result.seadex_status
        if result.upgrade_available:
            status_str += " | upgrade-available"
        if result.too_large:
            status_str += " | too-large"
        if result.hardlink_mismatch:
            status_str += " | hardlink-mismatch"
        elif result.hardlink_candidate:
            status_str += " | hardlink-candidate"
        self.logger.info(
            centred_string(
                f"[Radarr] {result.radarr_title}: {status_str}",
                total_length=self.log_line_length,
            )
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

    def _log_summary(
        self,
        stats: dict[str, int],
        radarr_stats: Optional[dict[str, int]] = None,
        elapsed_s: float = 0.0,
    ):
        sep = "=" * self.log_line_length
        self.logger.info(centred_string(sep, total_length=self.log_line_length))
        self.logger.info(centred_string("Audit Summary", total_length=self.log_line_length))
        self.logger.info(centred_string(sep, total_length=self.log_line_length))
        self.logger.info(centred_string("Sonarr", total_length=self.log_line_length))
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
        if radarr_stats and radarr_stats.get("total", 0) > 0:
            self.logger.info(centred_string("-" * self.log_line_length, total_length=self.log_line_length))
            self.logger.info(centred_string("Radarr", total_length=self.log_line_length))
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
                ("Hard-link candidates", "hardlink_candidates"),
                ("Hard-link mismatches", "hardlink_mismatches"),
            ]:
                self.logger.info(
                    centred_string(
                        f"{label}: {radarr_stats[key]}",
                        total_length=self.log_line_length,
                    )
                )
        mins, secs = divmod(int(elapsed_s), 60)
        elapsed_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        self.logger.info(centred_string(sep, total_length=self.log_line_length))
        self.logger.info(
            centred_string(
                f"Duration: {elapsed_str}",
                total_length=self.log_line_length,
            )
        )
        self.logger.info(centred_string(sep, total_length=self.log_line_length))
