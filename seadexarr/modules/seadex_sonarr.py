import copy
import time
import os
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import arrapi.exceptions
import requests
from arrapi import SonarrAPI

from .anilist import (
    get_anilist_n_eps,
    get_anilist_format,
)
from .discord import discord_push
from .log import centred_string, left_aligned_string
from .seadex_arr import SeaDexArr
from .seadex_radarr import SeaDexRadarr


TORRENT_FILENAMES_TO_SKIP = [
    "NCED",
    "NCOP",
    "Creditless Ending",
    "Creditless Opening",
    "Creditless ED",
    "Creditless OP",
]


def get_tvdb_id(mapping):
    """Get TVDB ID for a particular mapping

    Args:
        mapping (dict): Dictionary of SeaDex mappings

    Returns:
        int: TVDB ID
    """

    tvdb_id = mapping.get("tvdb_id", None)

    return tvdb_id


def get_tvdb_season(mapping):
    """Get TVDB season for a particular mapping

    Args:
        mapping (dict): Dictionary of SeaDex mappings

    Returns:
        int: TVDB season
    """

    tvdb_season = mapping.get("tvdb_season", -1)

    return tvdb_season


def get_overlapping_results(seadex_dict):
    """See if SeaDex releases have overlapping episodes

    Args:
        seadex_dict (dict): Dictionary of SeaDex releases
    """

    overlapping_results = False
    if len(seadex_dict) > 0:
        for rg1 in seadex_dict:

            rg1_all_eps = seadex_dict.get(rg1, {}).get("all_episodes", [])

            for rg2 in seadex_dict:

                if rg1 == rg2:
                    continue

                rg2_all_eps = seadex_dict.get(rg2, {}).get("all_episodes", [])

                if len(rg1_all_eps) == 0 or len(rg2_all_eps) == 0:
                    overlapping_results = True

                # Also, if we have an instance where one hasn't been parsed
                # but the other has, then just assume they overlap

                intersect = list(
                    filter(
                        lambda x: x in rg1_all_eps,
                        rg2_all_eps,
                    )
                )
                if len(intersect) > 0:
                    overlapping_results = True

    return overlapping_results


def check_ep_by_anime_ids(
    ep,
    tvdb_season,
):
    """Check whether to include an episode by Anime ID style

    Args:
        ep (dict): Dictionary of episode info
        tvdb_season (int): TVDB season number
    """

    include_episode = True

    # First, check by season
    season_number = ep.get("seasonNumber", None)

    # If the TVDB season is -1, this is anything but specials
    if tvdb_season == -1 and season_number == 0:
        include_episode = False

    # Else, if we have a season defined, and it doesn't match, don't include
    elif tvdb_season != -1 and season_number != tvdb_season:
        include_episode = False

    return include_episode


def check_ep_by_anibridge(
    ep,
    tvdb_mappings,
):
    """Check whether to include an episode by AniBridge style

    Args:
        ep (dict): Dictionary of episode info
        tvdb_mappings (dict): Dictionary of AniBridge-style TVDB mappings
    """

    ep_season = ep.get("seasonNumber", -1)
    ep_episode = ep.get("episodeNumber", -1)

    for season, episodes in tvdb_mappings.items():

        tvdb_season = int(season.strip("s"))

        # Simplest case, we have an empty string so just
        # match by season
        if episodes == "" and ep_season == tvdb_season:
            return True

        if ep_season != tvdb_season:
            continue

        # We may have multiple mappings per season,
        # so we need to split
        episodes_split = episodes.split(",")
        for episode_split in episodes_split:

            # There may be some ratio mapping that we
            # can ignore
            episode_split = episode_split.split("|")[0]

            # The simpler case here is a single episode
            if "-" not in episode_split:
                episode_split_exact = int(episode_split.strip("e"))

                if episode_split_exact == ep_episode:
                    return True

            # Or we need to split again, to get the start and
            # end points
            else:
                episode_split_start_end = episode_split.split("-")
                episode_split_start = int(episode_split_start_end[0].strip("e"))

                # An open-ended range ("e13-") splits to an empty end part —
                # treat it as unbounded
                end_str = (
                    episode_split_start_end[1].strip("e")
                    if len(episode_split_start_end) > 1
                    else ""
                )
                episode_split_end = int(end_str) if end_str else 9999

                if episode_split_start <= ep_episode <= episode_split_end:
                    return True

    # If after all that, we haven't found anything, just return False
    return False


class SeaDexSonarr(SeaDexArr):

    def __init__(
        self,
        config="config.yml",
        cache="cache.json",
        logger=None,
    ):
        """Sync Sonarr instance with SeaDex

        Args:
            config (str, optional): Path to config file.
                Defaults to "config.yml".
            cache (str, optional): Path to cache file.
                Defaults to "cache.json".
            logger. Logging instance. Defaults to None,
                which will create one.
        """

        SeaDexArr.__init__(
            self,
            arr="sonarr",
            config=config,
            cache=cache,
            logger=logger,
        )

        # Set up Sonarr
        self.sonarr_url = self.config.get("sonarr_url", None)
        if not self.sonarr_url:
            raise ValueError(f"sonarr_url needs to be defined in {config}")

        self.sonarr_api_key = self.config.get("sonarr_api_key", None)
        if not self.sonarr_api_key:
            raise ValueError(f"sonarr_api_key needs to be defined in {config}")

        self.sonarr = SonarrAPI(
            url=self.sonarr_url,
            apikey=self.sonarr_api_key,
        )

        self.ignore_movies_in_radarr = self.config.get("ignore_movies_in_radarr", False)

        # Sonarr runs locally, so episode-name parsing (one /parse call per file)
        # can be fanned out across threads without worrying about rate limits.
        self.parse_workers = self.config.get("sonarr_parse_workers", 8)

        # Also, if we have Radarr info, set up an instance there
        self.radarr = None
        self.all_radarr_movies = None
        radarr_url = self.config.get("radarr_url", None)
        radarr_api_key = self.config.get("radarr_api_key", None)

        if radarr_url is not None and radarr_api_key is not None:
            # Pass our cache path and already-synced mirror through. Without
            # these the sub-instance defaulted to cache.json in the cwd, putting
            # its mirror DB on an ephemeral path — which re-bootstrapped the
            # entire SeaDex catalogue from the API on every single run.
            self.radarr = SeaDexRadarr(
                config=config,
                cache=cache,
                logger=logger,
                mirror=self.mirror,
            )
            self.all_radarr_movies = self.radarr.get_all_radarr_movies()

    def run(self):
        """Run the SeaDex Sonarr Syncer"""

        # Get all the anime series
        all_sonarr_series = self.get_all_sonarr_series()
        n_sonarr = len(all_sonarr_series)

        self.log_arr_start(
            arr="sonarr",
            n_items=n_sonarr,
        )

        # Now start looping over these series, finding any potential mappings
        for sonarr_idx, sonarr_series in enumerate(all_sonarr_series):

            try:

                # Pull Sonarr and database info out
                tvdb_id = sonarr_series.tvdbId
                imdb_id = sonarr_series.imdbId
                sonarr_title = sonarr_series.title
                sonarr_series_id = sonarr_series.id

                self.log_arr_item_start(
                    arr="sonarr",
                    item_title=sonarr_title,
                    n_item=sonarr_idx + 1,
                    n_items=n_sonarr,
                )

                # If we're not monitored, then skip if ignore_unmonitored is switched on
                if not sonarr_series.monitored and self.ignore_unmonitored:
                    self.log_arr_item_unmonitored(
                        arr="sonarr",
                        item_title=sonarr_title,
                    )
                    continue

                # Get the mappings from the Sonarr series to AniList
                al_mappings = self.get_anilist_ids(
                    tvdb_id=tvdb_id,
                    imdb_id=imdb_id,
                )

                if len(al_mappings) == 0:
                    self.log_no_anilist_mappings(title=sonarr_title)
                    continue

                for al_id, mapping in al_mappings.items():

                    # Map the TVDB ID through to AniList
                    if al_id is None:
                        self.log_no_anilist_id()
                        continue

                    # Get the SeaDex entry if it exists
                    sd_entry = self.get_seadex_entry(al_id=al_id)
                    if sd_entry is None:
                        self.log_no_sd_entry(al_id=al_id)
                        continue
                    sd_url = sd_entry.url

                    # Check if we've already got this cached
                    al_id_in_cache = self.check_al_id_in_cache(
                        arr="sonarr",
                        al_id=al_id,
                        seadex_entry=sd_entry,
                    )

                    if al_id_in_cache and not self.ignore_seadex_update_times:
                        self.logger.info(
                            centred_string(
                                f"Cache time for AniList ID {al_id} matches SeaDex updated time",
                                total_length=self.log_line_length,
                            )
                        )
                        self.logger.info(
                            centred_string(
                                "-" * self.log_line_length,
                                total_length=self.log_line_length,
                            )
                        )
                        continue

                    # Also check if it's in the Radarr cache, if we have that option
                    if self.ignore_movies_in_radarr and not self.ignore_seadex_update_times:
                        al_id_in_radarr_cache = self.check_al_id_in_cache(
                            arr="radarr",
                            al_id=al_id,
                            seadex_entry=sd_entry,
                        )
                        if al_id_in_radarr_cache:
                            self.logger.info(
                                centred_string(
                                    f"Found AniList ID {al_id} in Radarr cache, "
                                    f"and cache time matches SeaDex updated time",
                                    total_length=self.log_line_length,
                                )
                            )
                            self.logger.info(
                                centred_string(
                                    "-" * self.log_line_length,
                                    total_length=self.log_line_length,
                                )
                            )
                            continue

                    # Get the AniList title
                    anilist_title = self.get_anilist_title(
                        al_id=al_id,
                        sd_entry=sd_entry,
                    )

                    # Setup info for cache
                    cache_details = {
                        "name": anilist_title,
                        "updated_at": sd_entry.updated_at,
                        "torrent_hashes": [],
                    }

                    # If we have a Radarr instance, and we don't want to add movies that
                    # are already in Radarr, do that now
                    if (
                        self.radarr is not None
                        and self.all_radarr_movies is not None
                        and self.ignore_movies_in_radarr
                    ):

                        radarr_movies = []

                        # Make sure these are flagged as specials since
                        # sometimes shows and movies are all lumped together
                        mapping_season = mapping.get("tvdb_season", -1)
                        if mapping_season == 0:

                            mapping_tmdb_id = mapping.get("tmdb_movie_id", None)
                            mapping_imdb_id = mapping.get("imdb_id", None)

                            for m in self.all_radarr_movies:

                                # Check by TMDB IDs
                                if mapping_tmdb_id is not None:
                                    if (
                                        m.tmdbId == mapping_tmdb_id
                                        and m not in radarr_movies
                                    ):
                                        radarr_movies.append(m)

                                # Check by IMDb IDs
                                if mapping_imdb_id is not None:
                                    if (
                                        m.imdbId == mapping_imdb_id
                                        and m not in radarr_movies
                                    ):
                                        radarr_movies.append(m)

                        if len(radarr_movies) > 0:

                            for movie in radarr_movies:
                                self.logger.info(
                                    centred_string(
                                        f"{movie.title} found in Radarr, will skip",
                                        total_length=self.log_line_length,
                                    )
                                )

                            self.logger.info(
                                centred_string(
                                    "-" * self.log_line_length,
                                    total_length=self.log_line_length,
                                )
                            )

                            time.sleep(self.sleep_time)
                            continue

                    # Get the episode list for all relevant episodes
                    ep_list = self.get_ep_list(
                        sonarr_series_id=sonarr_series_id,
                        al_id=al_id,
                        mapping=mapping,
                    )

                    if ep_list is None:
                        continue

                    # If all episodes are unmonitored, then skip if ignore_unmonitored is switched on
                    ep_list_monitored = [x.get("monitored", True) for x in ep_list]
                    if not any(ep_list_monitored) and self.ignore_unmonitored:
                        self.log_anilist_item_unmonitored(
                            arr="sonarr",
                            item_title=anilist_title,
                        )
                        time.sleep(self.sleep_time)
                        continue

                    sonarr_release_dict = self.get_sonarr_release_dict(ep_list=ep_list)
                    sonarr_release_groups = list(sonarr_release_dict.keys())

                    self.logger.debug(
                        centred_string(
                            f"Sonarr release group(s): {', '.join(sonarr_release_groups)}",
                            total_length=self.log_line_length,
                        )
                    )

                    # Produce a dictionary of info from the SeaDex request
                    seadex_dict = self.get_seadex_dict(sd_entry=sd_entry)

                    if len(seadex_dict) == 0:
                        self.log_no_seadex_releases()

                        self.update_cache(
                            arr="sonarr",
                            al_id=al_id,
                            cache_details=cache_details,
                        )

                        time.sleep(self.sleep_time)
                        continue

                    self.logger.debug(
                        centred_string(
                            f"SeaDex: {', '.join(seadex_dict)}",
                            total_length=self.log_line_length,
                        )
                    )

                    # Parse out filenames and check for overlaps
                    seadex_dict = self.parse_episodes_from_seadex(seadex_dict=seadex_dict)
                    overlapping_results = get_overlapping_results(seadex_dict=seadex_dict)

                    # If we're in interactive mode and there are multiple equivalent options here, then select
                    if self.interactive and len(seadex_dict) > 1 and overlapping_results:
                        seadex_dict = self.filter_seadex_interactive(
                            seadex_dict=seadex_dict,
                            sd_entry=sd_entry,
                        )

                    # Filter downloads by whether the episodes in each torrent match the release
                    # group we have in Sonarr
                    torrent_hashes, seadex_dict = self.filter_seadex_downloads(
                        al_id=al_id,
                        seadex_dict=seadex_dict,
                        arr="sonarr",
                        arr_release_dict=sonarr_release_dict,
                        ep_list=ep_list,
                    )

                    any_to_download = self.get_any_to_download(seadex_dict=seadex_dict)

                    if any_to_download:
                        self.log_arr_seadex_mismatch(
                            arr="sonarr",
                            seadex_dict=seadex_dict,
                        )
                        fields, anilist_thumb = self.get_seadex_fields(
                            arr="sonarr",
                            al_id=al_id,
                            release_group=sonarr_release_groups,
                            seadex_dict=seadex_dict,
                            arr_release_dict=sonarr_release_dict,
                        )

                        # If we've got stuff, time to do something!
                        if len(seadex_dict) > 0:

                            # Keep track of how many torrents we've added
                            n_torrents_added = 0

                            # Add torrents to qBittorrent
                            if self.qbit is not None:
                                n_torrents_added += self.add_torrent(
                                    torrent_dict=seadex_dict,
                                    torrent_client="qbit",
                                )

                            # Otherwise, increment by the number of torrents in the SeaDex dict
                            else:
                                n_torrents_added += len(seadex_dict)
                                self.torrents_added += len(seadex_dict)

                            # Push a message to Discord if we've added anything
                            if self.discord_url is not None and n_torrents_added > 0:
                                discord_push(
                                    url=self.discord_url,
                                    arr_title=sonarr_title,
                                    al_title=anilist_title,
                                    seadex_url=sd_url,
                                    fields=fields,
                                    thumb_url=anilist_thumb,
                                    logger=self.logger,
                                )

                            if self.max_torrents_to_add is not None:
                                if self.torrents_added >= self.max_torrents_to_add:
                                    self.log_max_torrents_added()
                                    return True

                    else:

                        self.logger.info(
                            centred_string(
                                f"You already have the recommended release(s) for this title",
                                total_length=self.log_line_length,
                            )
                        )

                    # Update and save out the cache
                    cache_details.update({"torrent_hashes": torrent_hashes})
                    self.update_cache(
                        arr="sonarr",
                        al_id=al_id,
                        cache_details=cache_details,
                    )

                    self.logger.info(
                        centred_string(
                            "-" * self.log_line_length,
                            total_length=self.log_line_length,
                        )
                    )

                    # Add in a wait, if required
                    time.sleep(self.sleep_time)

                self.logger.info(
                    centred_string(
                        self.log_line_sep * self.log_line_length,
                        total_length=self.log_line_length,
                    )
                )

                if self.max_torrents_to_add is not None:
                    if self.torrents_added >= self.max_torrents_to_add:
                        self.log_max_torrents_added()
                        return True

            except Exception as e:
                self.logger.error(f"Exception: {e}")
                self.logger.info(
                    centred_string(
                        self.log_line_sep * self.log_line_length,
                        total_length=self.log_line_length,
                    )
                )
                continue

            # Add in a blank line to break things up
            self.logger.info("")

        return True

    def get_all_sonarr_series(self):
        """Get all series in Sonarr with AniList mapping info"""

        sonarr_series = []

        # Sets so the per-series membership test below is O(1) rather than a
        # linear scan of every mapping id for every series in the library.
        all_tvdb_ids = set()
        all_imdb_ids = set()

        # Search through TVDB and IMDb IDs via Anime IDs and AniBridge mappings
        for mapping in [
            self.anime_mappings,
            self.anibridge_mappings,
        ]:
            if not mapping:
                continue

            # Some entries carry list-valued ids; a scalar series id never equals
            # a list, so they were unmatchable before and lists can't go in a set
            # anyway. Keep only hashable scalar ids.
            all_tvdb_ids.update(
                v
                for x in mapping
                if (v := mapping[x].get("tvdb_id", None)) is not None
                and not isinstance(v, (list, dict, set))
            )

            all_imdb_ids.update(
                v
                for x in mapping
                if (v := mapping[x].get("imdb_id", None)) is not None
                and not isinstance(v, (list, dict, set))
            )

        seen_ids = set()
        for s in self.sonarr.all_series():

            if s.id in seen_ids:
                continue

            # Include if either id matches a mapping; add once.
            if s.tvdbId in all_tvdb_ids or s.imdbId in all_imdb_ids:
                sonarr_series.append(s)
                seen_ids.add(s.id)

        sonarr_series.sort(key=lambda x: x.title)

        return sonarr_series

    def get_sonarr_series(self, tvdb_id):
        """Get Sonarr series for a given TVDB ID

        Args:
            tvdb_id (int): TVDB ID
        """

        try:
            series = self.sonarr.get_series(tvdb_id=tvdb_id)
        except arrapi.exceptions.NotFound:
            series = None

        return series

    def get_ep_list(
        self,
        sonarr_series_id,
        al_id,
        mapping,
    ):
        """Get a list of relevant episodes for an AniList mapping

        Args:
            sonarr_series_id (int): Series ID in Sonarr
            al_id (int): Anilist ID
            mapping (dict): Mapping dictionary between TVDB and AniList
        """

        # If we have any season info, pull that out now
        tvdb_season = get_tvdb_season(mapping)

        # Check we have a sensible AL ID
        if al_id == -1:
            raise ValueError("AniList ID not defined!")

        # Get the AniDB ID
        anidb_id = mapping.get("anidb_id", None)

        # Check what kind of mode we're in here,
        # it's either AniBridge or Anime IDs
        if "tvdb_mappings" in mapping:
            mapping_mode = "anibridge"
        else:
            mapping_mode = "anime_ids"

        # Get all the episodes for a season. Use the raw Sonarr API
        # call here to get details
        eps_req_url = (
            f"{self.sonarr_url}/api/v3/episode?"
            f"seriesId={sonarr_series_id}&"
            f"includeImages=false&"
            f"includeEpisodeFile=true&"
            f"apikey={self.sonarr_api_key}"
        )
        eps_req = requests.get(eps_req_url)

        if eps_req.status_code != 200:
            self.logger.warning("Failed get episodes data from Sonarr")
            return None

        ep_list = eps_req.json()

        # Sort by season/episode number for slicing later
        ep_list = sorted(
            ep_list,
            key=lambda x: (x.get("seasonNumber", None), x.get("episodeNumber", None)),
        )

        # Filter down here by various things
        final_ep_list = []
        for ep in ep_list:

            if mapping_mode == "anime_ids":
                include_episode = check_ep_by_anime_ids(
                    ep=ep,
                    tvdb_season=tvdb_season,
                )
            elif mapping_mode == "anibridge":
                tvdb_mappings = mapping.get("tvdb_mappings", {})
                include_episode = check_ep_by_anibridge(
                    ep=ep,
                    tvdb_mappings=tvdb_mappings,
                )
            else:
                raise ValueError(f"Invalid mapping mode {mapping_mode}")

            # If we've passed the vibe check, include things now
            if include_episode:
                final_ep_list.append(ep)

        # For OVAs and movies, the offsets can often be wrong, so if we have specific mappings
        # then take that into account here
        al_format, self.al_cache = get_anilist_format(
            al_id,
            al_cache=self.al_cache,
        )

        # Potentially pull out a bunch of mappings from AniDB. These should
        # be for anything not marked as TV, and specials as marked by
        # being in Season 0
        anidb_mapping_dict = {}
        if (
            self.anidb_mappings is not None
            and anidb_id is not None
            and (al_format not in ["TV"] or tvdb_season == 0)
        ):
            anidb_item = self.anidb_mappings.findall(
                f"anime[@anidbid='{anidb_id}']"
            )

            # The AniDB list can carry the same anidbid under more than one
            # tvdbid (e.g. a title split across TVDB entries). Disambiguate by
            # the TVDB ID we're actually working with before giving up.
            if len(anidb_item) > 1:
                tvdb_id = get_tvdb_id(mapping)
                if tvdb_id is not None:
                    anidb_item = [
                        a
                        for a in anidb_item
                        if a.get("tvdbid") == str(tvdb_id)
                    ]

            # If we still can't resolve to a single node, skip the AniDB
            # mapping rather than crashing the whole series — get_ep_list falls
            # back to the offset slice below.
            if len(anidb_item) > 1:
                self.logger.debug(
                    left_aligned_string(
                        f"Multiple AniDB mappings for anidbid {anidb_id}; "
                        f"skipping AniDB episode mapping",
                        total_length=self.log_line_length,
                    )
                )
                anidb_item = []

            if len(anidb_item) == 1:
                anidb_item = anidb_item[0]

                # We want things with mapping lists in, since more regular
                # mappings will have already been picked up
                anidb_mapping_list = anidb_item.findall("mapping-list")

                if len(anidb_mapping_list) > 0:
                    for ms in anidb_mapping_list:
                        m = ms.findall("mapping")
                        for i in m:

                            # If there's no text, continue
                            if not i.text:
                                continue

                            # Split at semicolons
                            i_split = i.text.strip(";").split(";")
                            i_split = [x.split("-") for x in i_split]

                            # Only match things if AniList and AniDB agree on the TVDB season
                            anidb_tvdbseason = int(i.attrib["tvdbseason"])
                            if not anidb_tvdbseason == tvdb_season:
                                continue

                            # For MOVIE entries mapped to TVDB season 0, anidbseason="0"
                            # mappings describe AniDB extras/specials, not the main movie.
                            # The movie itself lives at AniDB season 1 (handled by the
                            # offset slice).  Using these extra-episode mappings would
                            # override the offset and resolve to the wrong TVDB episode.
                            if al_format == "MOVIE" and i.attrib.get("anidbseason") == "0":
                                continue

                            # The TVDB side of a mapping can be a '+'-joined
                            # group (e.g. "1-1+2+3"), meaning one AniDB episode
                            # spans several TVDB episodes. Expand those so each
                            # TVDB episode points back at the AniDB episode.
                            mapping_pairs = {}
                            for x in i_split:
                                anidb_ep = int(x[0])
                                for tvdb_ep in x[1].split("+"):
                                    mapping_pairs[int(tvdb_ep)] = anidb_ep

                            anidb_mapping_dict[anidb_tvdbseason] = mapping_pairs

        # Work out the offset-based slice independently of the AniDB mapping so
        # we can fall back to it when the two disagree (see below).
        offset_ep_list = self._apply_offset_slice(
            final_ep_list=final_ep_list,
            mapping=mapping,
            mapping_mode=mapping_mode,
            tvdb_season=tvdb_season,
            al_id=al_id,
        )

        # Debug: show which episodes made it into the filtered list before AniDB
        # remapping. If this shows the wrong episode (e.g., an OVA instead of
        # the movie you actually own), the fix is upstream in the AniDB/anime-ids
        # offset — see the UPSTREAM NOTE in filter_by_release_group.
        if final_ep_list:
            ep_strs = [
                f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"
                + (f"[{(ep.get('episodeFile') or {}).get('releaseGroup', '?')}]"
                   if ep.get("episodeFileId", 0) != 0 else "[missing]")
                for ep in final_ep_list[:15]
            ]
            if len(final_ep_list) > 15:
                ep_strs.append(f"... ({len(final_ep_list)} total)")
            self.logger.debug(
                left_aligned_string(
                    f"Episode filter result: {', '.join(ep_strs)}",
                    total_length=self.log_line_length,
                )
            )

        if len(anidb_mapping_dict) > 0:

            # See if we have the AniDB mapping for each entry
            anidb_ep_list = [
                ep
                for ep in final_ep_list
                if anidb_mapping_dict.get(ep.get("seasonNumber", None), {}).get(
                    ep.get("episodeNumber", None), None
                )
                is not None
            ]

            # The AniDB anime-list and the Kometa Anime-IDs offset can resolve a
            # special/OVA to *different* TVDB episodes — each tracks a different
            # snapshot of TVDB's (frequently re-ordered) special numbering. When
            # the AniDB slot is empty but the offset slot holds a file, trust the
            # file we actually own. If the AniDB slot has a file, or neither does
            # (genuinely missing), keep the AniDB mapping.
            anidb_has_file = any(ep.get("episodeFileId", 0) for ep in anidb_ep_list)
            offset_has_file = any(ep.get("episodeFileId", 0) for ep in offset_ep_list)

            if not anidb_has_file and offset_has_file:
                self.logger.debug(
                    left_aligned_string(
                        "AniDB mapping resolved to an unowned episode but the "
                        "offset slice matches an owned file; using the offset slice",
                        total_length=self.log_line_length,
                    )
                )
                final_ep_list = copy.deepcopy(offset_ep_list)
            else:
                final_ep_list = copy.deepcopy(anidb_ep_list)

        else:
            final_ep_list = offset_ep_list

        return final_ep_list

    def _apply_offset_slice(
        self,
        final_ep_list,
        mapping,
        mapping_mode,
        tvdb_season,
        al_id,
    ):
        """Slice a season-filtered episode list down using the Anime-IDs offset

        Args:
            final_ep_list (list): Season-filtered episodes
            mapping (dict): Mapping dictionary between TVDB and AniList
            mapping_mode (str): Either "anime_ids" or "anibridge"
            tvdb_season (int): TVDB season number
            al_id (int): AniList ID
        """

        # First case, we've got Anime IDs
        if mapping_mode == "anime_ids":

            # Slice the list to get the correct episodes, so any potential offsets
            ep_offset = mapping.get("tvdb_epoffset", 0)
            n_eps, self.al_cache = get_anilist_n_eps(
                al_id,
                al_cache=self.al_cache,
            )

            # If we don't get a number of episodes, use them all
            if n_eps is None:
                n_eps = len(final_ep_list) - ep_offset

            # Check that we're including this by the episode number. This only
            # works for single-seasons, so be careful!
            if tvdb_season != -1:
                return [
                    ep
                    for ep in final_ep_list
                    if 1 <= ep.get("episodeNumber", None) - ep_offset <= n_eps
                ]

            return final_ep_list[ep_offset : n_eps + ep_offset]

        # Or, we've got AniBridge mappings so we don't need to do anything (hooray)
        if mapping_mode == "anibridge":
            return list(final_ep_list)

        raise ValueError(f"Invalid mapping mode {mapping_mode}")

    def get_sonarr_release_dict(
        self,
        ep_list,
    ):
        """Get a dictionary of useful info for a series in Sonarr

        Args:
            ep_list (list): List of episodes
        """

        # Look through, get release groups from the existing Sonarr files
        # and note any potential missing files
        sonarr_release_dict = {}
        missing_eps = 0
        n_eps = len(ep_list)
        for ep in ep_list:

            # Get missing episodes, then skip
            if ep.get("episodeFileId", 0) == 0:
                missing_eps += 1
                continue

            release_group = (ep.get("episodeFile") or {}).get("releaseGroup", None)
            if release_group is None or release_group == "":
                continue

            if release_group not in sonarr_release_dict:
                sonarr_release_dict[release_group] = {"size": []}
            size = (ep.get("episodeFile") or {}).get("size", None)
            sonarr_release_dict[release_group]["size"].append(size)

        if missing_eps > 0:
            self.logger.info(
                centred_string(
                    f"Missing episodes: {missing_eps}/{n_eps}",
                    total_length=self.log_line_length,
                )
            )

        # Debug: log which release groups are in the library for this mapping.
        # "nothing" here means either all episodes are missing OR Sonarr has the
        # files but releaseGroup is blank (common for manually imported files).
        # UPSTREAM NOTE: if the wrong release group appears here (e.g., an OVA's
        # group instead of the movie you own), the ep_list is pointing to the
        # wrong Sonarr episode — see UPSTREAM NOTE in filter_by_release_group.
        self.logger.debug(
            left_aligned_string(
                f"Library release group(s): {', '.join(sonarr_release_dict) or 'none'}",
                total_length=self.log_line_length,
            )
        )

        return sonarr_release_dict

    def parse_episodes_from_seadex(
        self,
        seadex_dict,
    ):
        """For files in a SeaDex release, parse this through Sonarr to get season/episode numbers

        This gets an overall episode list per-release group, and also episode lists per-torrent,
        if there are multiple

        Sonarr's /parse endpoint takes one file at a time, so a release with many
        files means many calls. They're independent and only depend on the
        filename, so we collect every file up front, parse the unique names
        concurrently (Sonarr is local — no rate limit to respect), then stitch
        the results back into the dict in their original order.

        Args:
            seadex_dict (dict): Dictionary of seadex releases
        """

        # Collect every (release_group, url, filename, size) we need to parse,
        # initialising the episode lists as we go. Skipping NCED/NCOP etc. here
        # keeps them out of the parse fan-out entirely.
        tasks = []
        for release_group, release_group_item in seadex_dict.items():

            # Set up an overall "all episodes" list
            release_group_item.update({"all_episodes": []})

            for url, url_item in release_group_item.get("urls", {}).items():

                # Set up a list to parse episodes from files
                url_item.update({"episodes": []})
                sizes = url_item.get("size", [])

                for sd_file_idx, seadex_file in enumerate(url_item.get("files", [])):

                    # Get basename from the file
                    f = os.path.basename(seadex_file)

                    # Skip filenames with things like "NCED", "NCOP"
                    if any([x in f for x in TORRENT_FILENAMES_TO_SKIP]):
                        continue

                    size = sizes[sd_file_idx] if sd_file_idx < len(sizes) else None
                    tasks.append((release_group, url, f, size))

        # Parse the unique filenames through Sonarr in parallel
        parsed = self._parse_filenames({f for (_, _, f, _) in tasks})

        # Stitch the parsed episodes back in, preserving collection order
        for release_group, url, f, size in tasks:

            episode_info = parsed.get(f)

            if not episode_info:
                self.logger.debug(
                    left_aligned_string(
                        f"Sonarr could not parse episode for {f}"
                    )
                )
                continue

            url_item = seadex_dict[release_group]["urls"][url]
            release_group_item = seadex_dict[release_group]

            # Add the season and episode numbers in
            for ep in episode_info:

                season = ep.get("seasonNumber", None)
                episode = ep.get("episodeNumber", None)

                if season is None or episode is None:
                    raise ValueError("Season or episode has come up None")

                self.logger.debug(
                    left_aligned_string(
                        f"{f} mapped to: S{season:02d}E{episode:02d}"
                    )
                )

                url_item["episodes"].append(
                    {
                        "season": season,
                        "episode": episode,
                        "size": size,
                    }
                )
                release_group_item["all_episodes"].append(
                    {
                        "season": season,
                        "episode": episode,
                        "size": size,
                    }
                )

        return seadex_dict

    def _parse_filenames(self, filenames):
        """Parse a set of filenames through Sonarr's /parse endpoint concurrently

        Args:
            filenames (set): Set of basename strings to parse

        Returns:
            dict: Maps each filename to its parsed ``episodes`` list, or None if
                the call failed (so one bad file can't sink the whole series).
        """

        def parse_one(f):
            d_enc = urlencode({"title": f, "apikey": self.sonarr_api_key})
            parse_req_url = f"{self.sonarr_url}/api/v3/parse?{d_enc}"
            try:
                parse_req = requests.get(parse_req_url)
                return f, parse_req.json().get("episodes", [])
            except Exception as e:
                self.logger.debug(
                    left_aligned_string(f"Failed to parse {f} through Sonarr: {e}")
                )
                return f, None

        filenames = list(filenames)
        if not filenames:
            return {}

        workers = max(1, min(self.parse_workers, len(filenames)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return {f: eps for f, eps in ex.map(parse_one, filenames)}
