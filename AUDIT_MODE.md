# SeaDexArr — Audit Mode

Audit mode scans your Sonarr library against SeaDex, applies Sonarr tags, and
sends Discord notifications. **It never downloads, grabs, or replaces files.**

---

## Quick start

```bash
# Preview what would change (no Sonarr mutations, no Discord)
seadexarr audit --dry-run

# Apply Sonarr tags (still no downloads)
seadexarr audit --apply-tags

# Send Discord notifications only (no tag changes)
seadexarr audit --notify-only
```

All three flags can be combined; `--dry-run` always wins over `--apply-tags`.

---

## What it does

For each Sonarr series that has an AniList mapping:

1. Looks up the series in SeaDex.
2. Classifies coverage:
   - **`none`** — no SeaDex entry.
   - **`partial`** — SeaDex entry exists but no releases pass your filters
     (`public_only`, `want_best`, `trackers`, `ignore_tags`).
   - **`full`** — at least one release passes filters.
3. Checks whether the SeaDex-recommended release differs from your current
   library files (release-group or torrent-hash comparison — same logic as
   the existing grab mode).
4. Flags `upgrade_available` when there is a mismatch.
5. Flags `too_large` when the recommended release exceeds your size limits.
6. Applies Sonarr tags (when `--apply-tags` or `update_sonarr_tags: true`).
7. Sends Discord notifications for new or changed findings (deduped by
   persistent state so you are not spammed on every run).

---

## Config

Add an `audit:` section to your `config.yml`. Running `seadexarr config init`
copies a template with all defaults already filled in.

```yaml
audit:
  dry_run: true           # safe default: no mutations until you set false
  include_radarr: true    # also audit Radarr movies (needs radarr_url/radarr_api_key)
  notify_discord: true
  update_sonarr_tags: true
  remove_stale_tags: false  # only removes tags listed in audit.tags

  tags:
    full_seadex: seadex
    partial_seadex: partial-seadex
    upgrade_available: seadex-upgrade-available
    too_large: seadex-too-large
    missing_specials: seadex-missing-specials
    missing_season: seadex-missing-season
    ignored: seadex-ignored

  size_filters:
    enabled: true
    max_absolute_gb: 80          # flag if SeaDex release > 80 GB total
    max_size_multiplier: 2.0     # flag if > 2× your current files' size
    tag_when_too_large: true

  discord:
    notify_on_new_seadex_match: true
    notify_on_new_upgrade_available: true
    notify_on_partial_match: true
    notify_on_too_large: true
    notify_on_missing_specials: true
    notify_on_missing_season: true
    notify_on_state_change: true   # any other state change (e.g. SeaDex release list updated)
    notify_on_library_change: false  # your own files changed but nothing actionable did
    notify_on_no_change: false
    first_run_actionable_only: true  # first run seeds state quietly; only actionable items post
    batch_notifications: true    # group a few series per Discord message

  state:
    enabled: true
    path:    # leave blank to use /config/audit_state.db
```

The `discord_url` from the top-level config is reused.

---

## Sonarr tags

Tags are created automatically if they do not exist. Only tags listed in
`audit.tags` are ever removed (only when `remove_stale_tags: true`). Tags
you add manually are never touched.

| Tag | Applied when |
|-----|-------------|
| `seadex` | Full SeaDex coverage |
| `partial-seadex` | Entry exists but nothing passes your filters |
| `seadex-upgrade-available` | Recommended release differs from library |
| `seadex-too-large` | Upgrade available but exceeds size limits |
| `seadex-ignored` | Reserved for manual use; never applied automatically |

---

## Notification detail

Each Discord embed lists one field per SeaDex-tracked item (season / cour /
movie / special), worst-first:

- **🟢 covered** — your release satisfies SeaDex. When `alt_is_acceptable: true`
  and your release is an *alt* (not the best), the field also names the best
  release you could move to, e.g. `↳ AltGroup is an alt release • SeaDex best:
  BestGroup`.
- **🟠 upgrade available** — SeaDex recommends a release you don't have, with the
  size delta vs yours.
- **💰 free win** — same as upgrade, but the recommended release is *smaller*
  than what you hold: better quality at no extra disk cost.
- **🔴 upgrade too large** — recommended (best) release exceeds your size limits.

When `alt_is_acceptable: true` and you own neither the best nor an alt, upgrade
and too-large fields also offer the smallest alt as a lighter alternative:
`↳ alt option: 7.0 GB (-3.0 GB vs yours) via AltGroup`. This is most useful on a
too-large series, where the best is skipped but a smaller alt still fits.

---

## State file

`audit_state.db` (default `/config/audit_state.db`; legacy `.json` state is
migrated automatically) tracks per-series
state between runs so Discord is not spammed. Notifications fire only when:

- A series newly appears on SeaDex.
- Coverage changes (`none → partial`, `partial → full`, etc.).
- The recommended release group changes.
- Your library files change and now differ from the recommendation.
- A release newly crosses the "too large" threshold.

Delete the state file to reset and re-notify everything.

---

## Persistent state fields

```json
{
  "sonarr_id": 12345,
  "tvdb_id": 67890,
  "title": "My Favourite Show",
  "seadex_status": "full",
  "seadex_rgs": ["SubsPlease"],
  "seadex_size_bytes": 5368709120,
  "library_rgs": ["EMBER"],
  "upgrade_available": true,
  "too_large": false,
  "last_notified": "2026-01-01T12:00:00+00:00",
  "last_audited": "2026-01-01T12:00:00+00:00"
}
```

---

## Docker / Unraid

Mount `/config` persistently. The state file, config, and cache all live there.

```yaml
# docker-compose.yml excerpt
volumes:
  - /mnt/user/appdata/seadexarr:/config
environment:
  - CONFIG_DIR=/config
```

Run audit on demand:

```bash
docker exec seadexarr seadexarr audit --dry-run
docker exec seadexarr seadexarr audit --apply-tags
```

Or add a scheduled cron inside the container / via Unraid's scheduler.

---

## Running tests

```bash
pip install pytest
python -m pytest tests/test_audit.py -v
```

---

## Logged summary

After each run, the log shows:

```
================================================================================
Audit Summary
================================================================================
Scanned: 150
Matched to SeaDex: 87
Full coverage: 72
Partial coverage: 15
Upgrade available: 23
Too large: 4
Tags updated: 23
Notifications sent: 6
Errors: 0
================================================================================
```

---

## Guarantee

Audit mode **never calls** `add_torrent()` or `add_torrent_to_qbit()`.
These methods exist on the parent class but are not invoked anywhere in
`SeaDexAudit.run()`. The qBittorrent client may be initialized (if your
existing config has credentials) but is never used in audit mode.
