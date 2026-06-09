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


class TestMappingIndexListValuedIds(unittest.TestCase):
    """Some AniBridge entries carry list-valued ids. Those can't be dict/set
    keys and a scalar series id never equalled a list under the old scan, so the
    index build must skip them instead of crashing on `unhashable type: list`."""

    def test_build_indexes_skips_list_valued_ids(self):
        arr = _make_arr()
        arr.anibridge_mappings = {
            "111": {"tvdb_id": [1, 2, 3], "imdb_id": "tt0001"},
            "222": {"tvdb_id": 555, "imdb_id": ["tt1", "tt2"]},
        }

        # Must not raise.
        arr._build_mapping_indexes()

        # The scalar ids are indexed and resolvable...
        self.assertIn(555, arr._anibridge_idx["tvdb"])
        self.assertEqual(arr.get_anilist_ids(tvdb_id=555), {222: arr.anibridge_mappings["222"]})
        self.assertEqual(arr.get_anilist_ids(imdb_id="tt0001"), {111: arr.anibridge_mappings["111"]})

        # ...while the list-valued ids are skipped, matching the old behaviour.
        self.assertEqual(arr.get_anilist_ids(tvdb_id=1), {})
        self.assertEqual(arr.get_anilist_ids(imdb_id="tt1"), {})


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


class TestCheckEpByAnibridge(unittest.TestCase):
    """AniBridge tvdb_mappings episode-range parsing."""

    def _check(self, season, episode, tvdb_mappings):
        from seadexarr.modules.seadex_sonarr import check_ep_by_anibridge
        ep = {"seasonNumber": season, "episodeNumber": episode}
        return check_ep_by_anibridge(ep=ep, tvdb_mappings=tvdb_mappings)

    def test_whole_season(self):
        self.assertTrue(self._check(1, 5, {"s1": ""}))
        self.assertFalse(self._check(2, 5, {"s1": ""}))

    def test_single_episode(self):
        self.assertTrue(self._check(0, 3, {"s0": "e3"}))
        self.assertFalse(self._check(0, 4, {"s0": "e3"}))

    def test_closed_range(self):
        self.assertTrue(self._check(1, 12, {"s1": "e1-e12"}))
        self.assertFalse(self._check(1, 13, {"s1": "e1-e12"}))

    def test_open_ended_range(self):
        # "e13-" used to crash with int('') — must mean episode 13 onwards.
        self.assertTrue(self._check(1, 13, {"s1": "e13-"}))
        self.assertTrue(self._check(1, 999, {"s1": "e13-"}))
        self.assertFalse(self._check(1, 12, {"s1": "e13-"}))

    def test_ratio_suffix_ignored(self):
        self.assertTrue(self._check(1, 2, {"s1": "e1-e12|2"}))


class TestGetOverlappingResults(unittest.TestCase):
    """rg1/rg2 copy-paste regression: each group must be compared against the
    OTHER group's episodes, not its own."""

    def _dict(self, eps_by_rg):
        return {
            rg: {"all_episodes": [{"season": s, "episode": e} for s, e in eps]}
            for rg, eps in eps_by_rg.items()
        }

    def test_disjoint_groups_do_not_overlap(self):
        from seadexarr.modules.seadex_sonarr import get_overlapping_results
        d = self._dict({
            "A": [(1, 1), (1, 2)],
            "B": [(1, 3), (1, 4)],
        })
        self.assertFalse(get_overlapping_results(d))

    def test_shared_episode_overlaps(self):
        from seadexarr.modules.seadex_sonarr import get_overlapping_results
        d = self._dict({
            "A": [(1, 1), (1, 2)],
            "B": [(1, 2), (1, 3)],
        })
        self.assertTrue(get_overlapping_results(d))

    def test_unparsed_group_assumed_overlapping(self):
        from seadexarr.modules.seadex_sonarr import get_overlapping_results
        d = self._dict({
            "A": [(1, 1)],
            "B": [],
        })
        self.assertTrue(get_overlapping_results(d))


class TestRgNormalisation(unittest.TestCase):
    """Sonarr strips punctuation from release groups ("-ZR-" parses as "ZR"),
    so comparisons must normalise — Kamisama Kiss regression."""

    def test_normalise_rg(self):
        from seadexarr.modules.seadex_arr import normalise_rg
        self.assertEqual(normalise_rg("-ZR-"), "zr")
        self.assertEqual(normalise_rg("ZR"), "zr")
        self.assertEqual(normalise_rg("Anime-Koi & MTBB"), "animekoimtbb")
        self.assertEqual(normalise_rg(None), "")
        self.assertEqual(normalise_rg(""), "")

    def _make_arr(self):
        from seadexarr.modules.seadex_arr import SeaDexArr
        arr = SeaDexArr.__new__(SeaDexArr)
        arr.audit_mode = True
        arr.use_torrent_hash_to_filter = False
        arr.log_line_length = 80
        arr.logger = MagicMock()
        return arr

    def test_owned_release_with_punctuated_seadex_name_not_flagged(self):
        # Library has "ZR" (Sonarr-normalised); SeaDex calls it "-ZR-".
        # Episode-by-episode comparison must treat them as the same group.
        arr = self._make_arr()
        seadex_dict = {
            "-ZR-": {
                "tags": [],
                "urls": {
                    "https://nyaa.si/view/1": {
                        "hash": "h1",
                        "size": [5_000_000_000],
                        "episodes": [
                            {"season": 2, "episode": 1, "size": 5_000_000_000},
                        ],
                        "download": False,
                    },
                },
            },
        }
        ep_list = [
            {
                "seasonNumber": 2,
                "episodeNumber": 1,
                "episodeFileId": 1,
                "episodeFile": {"releaseGroup": "ZR", "size": 5_000_000_000},
            },
        ]
        arr.filter_by_release_group(
            seadex_dict=seadex_dict,
            arr="sonarr",
            arr_release_dict={"ZR": {"size": [5_000_000_000]}},
            ep_list=ep_list,
        )
        url_item = seadex_dict["-ZR-"]["urls"]["https://nyaa.si/view/1"]
        self.assertFalse(url_item["download"])

    def test_blunt_branch_matches_punctuated_name(self):
        # No parsed episodes: the blunt release-group/size comparison must
        # also match "-ZR-" against a library "ZR" with the same sizes.
        arr = self._make_arr()
        seadex_dict = {
            "-ZR-": {
                "tags": [],
                "urls": {
                    "https://nyaa.si/view/1": {
                        "hash": "h1",
                        "size": [5_000_000_000],
                        "episodes": [],
                        "download": False,
                    },
                },
            },
        }
        arr.filter_by_release_group(
            seadex_dict=seadex_dict,
            arr="sonarr",
            arr_release_dict={"ZR": {"size": [5_000_000_000]}},
            ep_list=[],
        )
        url_item = seadex_dict["-ZR-"]["urls"]["https://nyaa.si/view/1"]
        self.assertFalse(url_item["download"])

    def test_movie_without_file_does_not_crash(self):
        # Radarr reports a movie with no file as {None: {"size": None}} —
        # the "Have:" log line used to crash joining a [None] group list.
        arr = self._make_arr()
        seadex_dict = {
            "BestGroup": {
                "tags": [],
                "urls": {
                    "https://nyaa.si/view/1": {
                        "hash": "h1",
                        "size": [5_000_000_000],
                        "episodes": [],
                        "download": False,
                    },
                },
            },
        }
        arr.filter_by_release_group(
            seadex_dict=seadex_dict,
            arr="radarr",
            arr_release_dict={None: {"size": None}},
            ep_list=None,
        )
        url_item = seadex_dict["BestGroup"]["urls"]["https://nyaa.si/view/1"]
        self.assertTrue(url_item["download"])
        logged = " ".join(
            str(c.args[0]) for c in arr.logger.info.call_args_list
        )
        self.assertIn("nothing", logged)

    def test_genuinely_different_group_still_flagged(self):
        arr = self._make_arr()
        seadex_dict = {
            "BestGroup": {
                "tags": [],
                "urls": {
                    "https://nyaa.si/view/1": {
                        "hash": "h1",
                        "size": [5_000_000_000],
                        "episodes": [],
                        "download": False,
                    },
                },
            },
        }
        arr.filter_by_release_group(
            seadex_dict=seadex_dict,
            arr="sonarr",
            arr_release_dict={"ZR": {"size": [9_000_000_000]}},
            ep_list=[],
        )
        url_item = seadex_dict["BestGroup"]["urls"]["https://nyaa.si/view/1"]
        self.assertTrue(url_item["download"])


if __name__ == "__main__":
    unittest.main()
