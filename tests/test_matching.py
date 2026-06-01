"""
Unit tests for AniList/AniDB episode matching.

Regression coverage for the special/OVA/movie mis-mapping bug: the Kometa
Anime-IDs file keys each entry by its AniDB ID (the ID lives in the dict key,
not the value), so get_mappings_from_anime_mappings must fold the key into the
mapping as ``anidb_id``. Without it, get_ep_list never consults the AniDB
episode mapping-list and the naive offset slice picks the wrong special episode
(e.g. S00E01 instead of S00E31 for Attack on Titan ~Chronicle~).

Run with: python -m pytest tests/test_matching.py -v
"""

import unittest
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

from seadexarr.modules.seadex_arr import SeaDexArr
from seadexarr.modules.seadex_sonarr import SeaDexSonarr


# AniDB anime-list fragment for Shingeki no Kyojin: Chronicle (anidbid 15582).
# mapping ;1-31; means TVDB S00E31 maps to the single AniList episode.
_ANIDB_XML = """
<anime-list>
  <anime anidbid="15582" tvdbid="267440" defaulttvdbseason="0">
    <name>Shingeki no Kyojin: Chronicle</name>
    <mapping-list>
      <mapping anidbseason="1" tvdbseason="0">;1-31;</mapping>
    </mapping-list>
  </anime>
</anime-list>
"""

# The real Kometa Anime-IDs shape: AniDB ID is the KEY, never inside the value.
_ANIME_IDS = {
    "15582": {
        "tvdb_id": 267440,
        "tvdb_season": 0,
        "tvdb_epoffset": 0,
        "imdb_id": "tt12415546",
        "mal_id": 42091,
        "anilist_id": 119113,
    },
}

# Sonarr episode payload: the recap special sits at S00E31 (has the Meakes
# file); S00E01 is a different, file-less recap; plus a normal S01 episode.
_SONARR_EPISODES = [
    {"seasonNumber": 0, "episodeNumber": 1, "episodeFileId": 0, "monitored": True},
    {
        "seasonNumber": 0,
        "episodeNumber": 31,
        "episodeFileId": 37615,
        "monitored": True,
        "episodeFile": {"releaseGroup": "Meakes", "size": 34_860_000_000},
    },
    {
        "seasonNumber": 1,
        "episodeNumber": 1,
        "episodeFileId": 5,
        "monitored": True,
        "episodeFile": {"releaseGroup": "SomeoneElse", "size": 1_000_000_000},
    },
]


# AniDB fragment where one AniDB episode spans several TVDB episodes via a
# '+'-joined group ("1-1+2+3"): the Burn the Witch case that crashed int().
_ANIDB_XML_PLUS = """
<anime-list>
  <anime anidbid="16321" tvdbid="389481" defaulttvdbseason="0">
    <name>Burn the Witch</name>
    <mapping-list>
      <mapping anidbseason="1" tvdbseason="0">;1-1+2+3;</mapping>
    </mapping-list>
  </anime>
</anime-list>
"""

_SONARR_EPISODES_PLUS = [
    {
        "seasonNumber": 0,
        "episodeNumber": 1,
        "episodeFileId": 1,
        "monitored": True,
        "episodeFile": {"releaseGroup": "GroupA", "size": 1_000_000_000},
    },
    {
        "seasonNumber": 0,
        "episodeNumber": 2,
        "episodeFileId": 2,
        "monitored": True,
        "episodeFile": {"releaseGroup": "GroupA", "size": 1_000_000_000},
    },
    {
        "seasonNumber": 0,
        "episodeNumber": 3,
        "episodeFileId": 3,
        "monitored": True,
        "episodeFile": {"releaseGroup": "GroupA", "size": 1_000_000_000},
    },
]


def _make_arr() -> SeaDexArr:
    arr = SeaDexArr.__new__(SeaDexArr)
    arr.anime_mappings = _ANIME_IDS
    arr.anibridge_mappings = {}
    arr.logger = MagicMock()
    return arr


def _make_sonarr(anidb_mappings) -> SeaDexSonarr:
    s = SeaDexSonarr.__new__(SeaDexSonarr)
    s.sonarr_url = "http://sonarr.test"
    s.sonarr_api_key = "key"
    s.anidb_mappings = anidb_mappings
    s.al_cache = {}
    s.logger = MagicMock()
    s.log_line_length = 80
    return s


# ---------------------------------------------------------------------------
# anidb_id wiring
# ---------------------------------------------------------------------------

class TestAnimeMappingAnidbId(unittest.TestCase):

    def test_anidb_id_folded_from_key(self):
        arr = _make_arr()
        mappings = arr.get_mappings_from_anime_mappings(tvdb_id=267440)

        self.assertIn(119113, mappings)
        self.assertEqual(mappings[119113]["anidb_id"], "15582")
        # Original value fields preserved
        self.assertEqual(mappings[119113]["tvdb_season"], 0)
        self.assertEqual(mappings[119113]["anilist_id"], 119113)

    def test_existing_anidb_id_in_value_not_clobbered(self):
        arr = _make_arr()
        arr.anime_mappings = {
            "15582": {**_ANIME_IDS["15582"], "anidb_id": "explicit"},
        }
        mappings = arr.get_mappings_from_anime_mappings(tvdb_id=267440)
        self.assertEqual(mappings[119113]["anidb_id"], "explicit")

    def test_anidb_id_present_via_imdb_lookup(self):
        arr = _make_arr()
        mappings = arr.get_mappings_from_anime_mappings(imdb_id="tt12415546")
        self.assertEqual(mappings[119113]["anidb_id"], "15582")


# ---------------------------------------------------------------------------
# get_ep_list special-episode selection
# ---------------------------------------------------------------------------

class TestGetEpListSpecial(unittest.TestCase):

    def _run_get_ep_list(self, mapping, anidb_mappings, episodes=None, al_id=119113):
        s = _make_sonarr(anidb_mappings)

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = list(episodes or _SONARR_EPISODES)

        with patch(
            "seadexarr.modules.seadex_sonarr.requests.get", return_value=resp
        ), patch(
            "seadexarr.modules.seadex_sonarr.get_anilist_format",
            return_value=("MOVIE", {}),
        ), patch(
            "seadexarr.modules.seadex_sonarr.get_anilist_n_eps",
            return_value=(1, {}),
        ):
            return s.get_ep_list(
                sonarr_series_id=63,
                al_id=al_id,
                mapping=mapping,
            )

    def test_anidb_mapping_selects_correct_special(self):
        """With anidb_id wired, the AniDB mapping-list picks S00E31 (Meakes)."""
        root = ElementTree.fromstring(_ANIDB_XML)
        mapping = {
            "tvdb_id": 267440,
            "tvdb_season": 0,
            "tvdb_epoffset": 0,
            "anilist_id": 119113,
            "anidb_id": "15582",
        }

        ep_list = self._run_get_ep_list(mapping, root)

        self.assertEqual(len(ep_list), 1)
        self.assertEqual(ep_list[0]["seasonNumber"], 0)
        self.assertEqual(ep_list[0]["episodeNumber"], 31)
        self.assertEqual(ep_list[0]["episodeFile"]["releaseGroup"], "Meakes")

    def test_missing_anidb_id_regresses_to_wrong_episode(self):
        """Demonstrates the original bug: no anidb_id => offset slice picks E01."""
        mapping = {
            "tvdb_id": 267440,
            "tvdb_season": 0,
            "tvdb_epoffset": 0,
            "anilist_id": 119113,
            # anidb_id intentionally absent
        }

        ep_list = self._run_get_ep_list(mapping, None)

        self.assertEqual(len(ep_list), 1)
        self.assertEqual(ep_list[0]["episodeNumber"], 1)
        self.assertEqual(ep_list[0]["episodeFileId"], 0)


    def test_offset_wins_when_anidb_slot_unowned(self):
        """Carnival Phantasm EX Season: AniDB's stale ;1-5; maps to a file-less
        S00E05, but the Kometa offset (tvdb_epoffset=1) correctly lands on the
        owned S00E02. When AniDB resolves to an unowned slot and the offset slot
        holds a file, prefer the offset slice."""
        anidb_xml = """
        <anime-list>
          <anime anidbid="8824" tvdbid="251047" defaulttvdbseason="0" episodeoffset="1">
            <mapping-list>
              <mapping anidbseason="1" tvdbseason="0">;1-5;</mapping>
            </mapping-list>
          </anime>
        </anime-list>
        """
        root = ElementTree.fromstring(anidb_xml)
        mapping = {
            "tvdb_id": 251047,
            "tvdb_season": 0,
            "tvdb_epoffset": 1,
            "anilist_id": 12187,
            "anidb_id": "8824",
        }

        # S00E02 holds the EX Season file (Komorebi); S00E05 is empty — exactly
        # where the stale AniDB mapping points.
        episodes = [
            {"seasonNumber": 0, "episodeNumber": 1, "episodeFileId": 0, "monitored": True},
            {
                "seasonNumber": 0,
                "episodeNumber": 2,
                "episodeFileId": 49090,
                "monitored": True,
                "episodeFile": {"releaseGroup": "Komorebi", "size": 987_000_000},
            },
            {"seasonNumber": 0, "episodeNumber": 5, "episodeFileId": 0, "monitored": True},
        ]

        ep_list = self._run_get_ep_list(mapping, root, episodes=episodes, al_id=12187)

        self.assertEqual(len(ep_list), 1)
        self.assertEqual(ep_list[0]["episodeNumber"], 2)
        self.assertEqual(ep_list[0]["episodeFile"]["releaseGroup"], "Komorebi")

    def test_anidb_wins_when_neither_slot_owned(self):
        """When the AniDB slot and the offset slot are both unowned (genuinely
        missing special), keep the AniDB mapping rather than silently switching."""
        anidb_xml = """
        <anime-list>
          <anime anidbid="8824" tvdbid="251047" defaulttvdbseason="0" episodeoffset="1">
            <mapping-list>
              <mapping anidbseason="1" tvdbseason="0">;1-5;</mapping>
            </mapping-list>
          </anime>
        </anime-list>
        """
        root = ElementTree.fromstring(anidb_xml)
        mapping = {
            "tvdb_id": 251047,
            "tvdb_season": 0,
            "tvdb_epoffset": 1,
            "anilist_id": 12187,
            "anidb_id": "8824",
        }
        episodes = [
            {"seasonNumber": 0, "episodeNumber": 2, "episodeFileId": 0, "monitored": True},
            {"seasonNumber": 0, "episodeNumber": 5, "episodeFileId": 0, "monitored": True},
        ]

        ep_list = self._run_get_ep_list(mapping, root, episodes=episodes, al_id=12187)

        self.assertEqual(len(ep_list), 1)
        self.assertEqual(ep_list[0]["episodeNumber"], 5)

    def test_plus_grouped_mapping_expands_to_all_tvdb_episodes(self):
        """A '+'-joined TVDB group ("1-1+2+3") maps one AniDB ep to several
        TVDB eps without crashing int() (Burn the Witch regression)."""
        root = ElementTree.fromstring(_ANIDB_XML_PLUS)
        mapping = {
            "tvdb_id": 389481,
            "tvdb_season": 0,
            "tvdb_epoffset": 0,
            "anilist_id": 116673,
            "anidb_id": "16321",
        }

        ep_list = self._run_get_ep_list(
            mapping, root, episodes=_SONARR_EPISODES_PLUS, al_id=116673
        )

        self.assertEqual(
            sorted(ep["episodeNumber"] for ep in ep_list), [1, 2, 3]
        )


if __name__ == "__main__":
    unittest.main()
