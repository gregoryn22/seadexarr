import requests


class RadarrTagManager:
    """CRUD for Radarr tags via direct HTTP — no torrent operations."""

    def __init__(self, radarr_url: str, radarr_api_key: str):
        self._base = radarr_url.rstrip("/")
        self._key = radarr_api_key
        self._tag_cache: dict[str, int] | None = None

    def _headers(self) -> dict:
        return {"X-Api-Key": self._key, "Content-Type": "application/json"}

    def get_all_tags(self) -> dict[str, int]:
        """Return mapping of label → tag id, cached per instance."""
        if self._tag_cache is not None:
            return self._tag_cache
        resp = requests.get(f"{self._base}/api/v3/tag", headers=self._headers())
        resp.raise_for_status()
        self._tag_cache = {t["label"]: t["id"] for t in resp.json()}
        return self._tag_cache

    def get_or_create_tag(self, label: str) -> int:
        """Return existing tag id or create and return new one."""
        tags = self.get_all_tags()
        if label in tags:
            return tags[label]
        resp = requests.post(
            f"{self._base}/api/v3/tag",
            headers=self._headers(),
            json={"label": label},
        )
        resp.raise_for_status()
        tag_id = resp.json()["id"]
        self._tag_cache[label] = tag_id  # type: ignore[index]
        return tag_id

    def get_movie_json(self, movie_id: int) -> dict:
        resp = requests.get(
            f"{self._base}/api/v3/movie/{movie_id}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def set_movie_tags(self, movie_id: int, tag_ids: list[int], dry_run: bool = False) -> bool:
        """Replace the tag list on a movie. No-ops when dry_run=True."""
        if dry_run:
            return True
        movie_json = self.get_movie_json(movie_id)
        movie_json["tags"] = tag_ids
        resp = requests.put(
            f"{self._base}/api/v3/movie/{movie_id}",
            headers=self._headers(),
            json=movie_json,
        )
        resp.raise_for_status()
        return True

    def compute_tag_changes(
        self,
        current_tag_ids: list[int],
        desired_labels: list[str],
        managed_labels: list[str],
        remove_stale: bool,
    ) -> tuple[list[int], bool]:
        """Compute final tag id list for a movie. Mirrors SonarrTagManager.compute_tag_changes."""
        all_tags = self.get_all_tags()
        managed_ids = {all_tags[l] for l in managed_labels if l in all_tags}
        desired_ids = {self.get_or_create_tag(l) for l in desired_labels}

        current_set = set(current_tag_ids)
        non_managed = current_set - managed_ids
        final = non_managed | desired_ids

        changed = final != current_set
        return sorted(final), changed


class SonarrTagManager:
    """CRUD for Sonarr tags via direct HTTP — no torrent operations."""

    def __init__(self, sonarr_url: str, sonarr_api_key: str):
        self._base = sonarr_url.rstrip("/")
        self._key = sonarr_api_key
        self._tag_cache: dict[str, int] | None = None

    def _headers(self) -> dict:
        return {"X-Api-Key": self._key, "Content-Type": "application/json"}

    def get_all_tags(self) -> dict[str, int]:
        """Return mapping of label → tag id, cached per instance."""
        if self._tag_cache is not None:
            return self._tag_cache
        resp = requests.get(f"{self._base}/api/v3/tag", headers=self._headers())
        resp.raise_for_status()
        self._tag_cache = {t["label"]: t["id"] for t in resp.json()}
        return self._tag_cache

    def get_or_create_tag(self, label: str) -> int:
        """Return existing tag id or create and return new one."""
        tags = self.get_all_tags()
        if label in tags:
            return tags[label]
        resp = requests.post(
            f"{self._base}/api/v3/tag",
            headers=self._headers(),
            json={"label": label},
        )
        resp.raise_for_status()
        tag_id = resp.json()["id"]
        self._tag_cache[label] = tag_id  # type: ignore[index]
        return tag_id

    def get_series_json(self, series_id: int) -> dict:
        resp = requests.get(
            f"{self._base}/api/v3/series/{series_id}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def set_series_tags(self, series_id: int, tag_ids: list[int], dry_run: bool = False) -> bool:
        """Replace the tag list on a series. No-ops when dry_run=True."""
        if dry_run:
            return True
        series_json = self.get_series_json(series_id)
        series_json["tags"] = tag_ids
        resp = requests.put(
            f"{self._base}/api/v3/series/{series_id}",
            headers=self._headers(),
            json=series_json,
        )
        resp.raise_for_status()
        return True

    def compute_tag_changes(
        self,
        current_tag_ids: list[int],
        desired_labels: list[str],
        managed_labels: list[str],
        remove_stale: bool,
    ) -> tuple[list[int], bool]:
        """Compute final tag id list for a series.

        Returns (new_tag_ids, changed). Managed tags are always kept in sync
        (added when desired, removed when not). remove_stale controls whether
        unrecognised non-managed tags the user added manually are also swept.
        """
        all_tags = self.get_all_tags()
        managed_ids = {all_tags[l] for l in managed_labels if l in all_tags}
        desired_ids = {self.get_or_create_tag(l) for l in desired_labels}

        current_set = set(current_tag_ids)

        # Managed tags are always kept in sync: old ones not in desired_ids are
        # dropped, new desired ones are added. User-added non-managed tags are
        # always preserved. remove_stale is kept for API compatibility but has
        # no effect — managed tags are always synced.
        non_managed = current_set - managed_ids
        final = non_managed | desired_ids

        changed = final != current_set
        return sorted(final), changed
