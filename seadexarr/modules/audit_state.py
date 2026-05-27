import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional

SCHEMA_VERSION = 1


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


class AuditState:
    def __init__(self, path: str):
        self.path = path
        self._data: dict = {"schema_version": SCHEMA_VERSION, "series": {}}
        if os.path.exists(path):
            self._load()

    def _load(self):
        with open(self.path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return
        self._data = json.loads(content)

    def save(self):
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    def get_series(self, sonarr_id: int) -> Optional[SeriesAuditState]:
        raw = self._data.get("series", {}).get(str(sonarr_id))
        if raw is None:
            return None
        return SeriesAuditState(**raw)

    def should_notify(self, new_state: SeriesAuditState, discord_cfg: dict) -> bool:
        """True when this state warrants a Discord notification."""
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

        # Generic state change (status flip, rg change, etc.)
        return True

    def update_series(self, state: SeriesAuditState, notified: bool = False):
        now = datetime.now(timezone.utc).isoformat()
        state.last_audited = now
        if notified:
            state.last_notified = now
        self._data.setdefault("series", {})[state.state_key()] = asdict(state)
