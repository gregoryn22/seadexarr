"""
Unit tests for concurrent Sonarr filename parsing.

parse_episodes_from_seadex fans the per-file /parse calls out across a thread
pool (Sonarr is local, no rate limit). These tests pin the behaviour that must
survive that change: NCED/NCOP extras are skipped, every file's season/episode
lands in both the per-URL and the per-group "all_episodes" lists with the right
size, identical filenames are only parsed once, and a failed parse is tolerated
rather than sinking the whole series.

Run with: python -m pytest tests/test_parse.py -v
"""

import re
import threading
import unittest
from unittest.mock import MagicMock, patch

from seadexarr.modules.seadex_sonarr import SeaDexSonarr


def _make_sonarr(workers=8):
    s = SeaDexSonarr.__new__(SeaDexSonarr)
    s.sonarr_url = "http://sonarr.test"
    s.sonarr_api_key = "key"
    s.parse_workers = workers
    s.logger = MagicMock()
    return s


_SXXEYY = re.compile(r"S(\d+)E(\d+)", re.IGNORECASE)


def _fake_parse_response(url):
    """Mimic Sonarr /parse: pull SxxEyy out of the title query param."""
    title = url.split("title=", 1)[1].split("&", 1)[0]
    resp = MagicMock()
    m = _SXXEYY.search(title)
    if m:
        resp.json.return_value = {
            "episodes": [
                {"seasonNumber": int(m.group(1)), "episodeNumber": int(m.group(2))}
            ]
        }
    else:
        resp.json.return_value = {"episodes": []}
    return resp


def _seadex_dict():
    return {
        "Moxie": {
            "urls": {
                "https://nyaa.si/view/1": {
                    "files": [
                        "[Moxie] Show - NCED [ABC].mkv",          # skipped
                        "[Moxie] Show - S01E01 [DEF].mkv",
                        "[Moxie] Show - S01E02 [GHI].mkv",
                    ],
                    "size": [100, 200, 300],
                },
            },
        },
    }


class TestParseEpisodes(unittest.TestCase):

    def test_skips_extras_and_maps_episodes_with_sizes(self):
        s = _make_sonarr()
        with patch(
            "seadexarr.modules.seadex_sonarr.requests.get",
            side_effect=lambda url: _fake_parse_response(url),
        ):
            out = s.parse_episodes_from_seadex(_seadex_dict())

        url_item = out["Moxie"]["urls"]["https://nyaa.si/view/1"]
        eps = url_item["episodes"]

        # NCED dropped; E01 and E02 mapped with their sizes (note the NCED slot's
        # size index, 100, must NOT leak onto E01 — E01 keeps index 1 → 200).
        self.assertEqual(
            [(e["season"], e["episode"], e["size"]) for e in eps],
            [(1, 1, 200), (1, 2, 300)],
        )
        # all_episodes mirrors the per-url episodes for this single-url group
        self.assertEqual(out["Moxie"]["all_episodes"], eps)

    def test_identical_filenames_parsed_once(self):
        s = _make_sonarr()
        sd = {
            "Moxie": {
                "urls": {
                    "u1": {"files": ["[Moxie] Show - S01E01.mkv"], "size": [10]},
                    "u2": {"files": ["[Moxie] Show - S01E01.mkv"], "size": [20]},
                },
            },
        }
        calls = []
        lock = threading.Lock()

        def tracking_get(url):
            with lock:
                calls.append(url)
            return _fake_parse_response(url)

        with patch(
            "seadexarr.modules.seadex_sonarr.requests.get", side_effect=tracking_get
        ):
            out = s.parse_episodes_from_seadex(sd)

        # Same basename across two urls → only one /parse call
        self.assertEqual(len(calls), 1)
        # But both urls still get the episode, each with its own size
        self.assertEqual(out["Moxie"]["urls"]["u1"]["episodes"][0]["size"], 10)
        self.assertEqual(out["Moxie"]["urls"]["u2"]["episodes"][0]["size"], 20)

    def test_failed_parse_is_tolerated(self):
        s = _make_sonarr()

        def boom_for_e02(url):
            if "S01E02" in url:
                raise RuntimeError("Sonarr hiccup")
            return _fake_parse_response(url)

        with patch(
            "seadexarr.modules.seadex_sonarr.requests.get", side_effect=boom_for_e02
        ):
            out = s.parse_episodes_from_seadex(_seadex_dict())

        eps = out["Moxie"]["urls"]["https://nyaa.si/view/1"]["episodes"]
        # E02 failed → dropped; E01 still present, no exception raised
        self.assertEqual([(e["episode"]) for e in eps], [1])

    def test_no_files_returns_empty_structures(self):
        s = _make_sonarr()
        sd = {"Grp": {"urls": {"u": {"files": [], "size": []}}}}
        with patch(
            "seadexarr.modules.seadex_sonarr.requests.get",
            side_effect=AssertionError("should not be called"),
        ):
            out = s.parse_episodes_from_seadex(sd)
        self.assertEqual(out["Grp"]["all_episodes"], [])
        self.assertEqual(out["Grp"]["urls"]["u"]["episodes"], [])


if __name__ == "__main__":
    unittest.main()
