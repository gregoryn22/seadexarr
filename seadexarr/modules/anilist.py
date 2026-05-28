import copy

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_URL = "https://graphql.anilist.co"
_TIMEOUT = 30

# AniList query
QUERY = """
query ($id: Int) {
  Media (id: $id, type: ANIME) {
    id
    title {
        english
        romaji
    }
    coverImage {
        extraLarge
        large
        medium
    }
    episodes
    format
  }
}
"""


def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        backoff_factor=1,
        allowed_methods={"POST"},
        status_forcelist={429, 500, 502, 503, 504},
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_SESSION = _make_session()


def get_query(al_id):
    """Do the AniList query

    Args:
        al_id (int): Anilist ID

    Raises:
        requests.exceptions.RequestException: on network failure after retries
    """

    variables = {"id": al_id}
    resp = _SESSION.post(
        API_URL,
        json={"query": QUERY, "variables": variables},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _media(j: dict) -> dict:
    """Safely extract the Media object from an AniList response.

    AniList returns {"data": null} for removed/incomplete entries.
    dict.get(key, {}) only uses the default when the key is absent,
    not when it exists with a null value — so we use `or {}` guards.
    """
    return ((j.get("data") or {}).get("Media") or {})


def get_anilist_n_eps(
    al_id,
    al_cache=None,
):
    """Query AniList to get number of episodes for an anime.

    Args:
        al_id (int): Anilist ID
        al_cache (dict): Cached Anilist requests. Defaults to None,
            which will create a dictionary
    """

    # Try and find query in cache
    if al_cache is None:
        al_cache = {}
    j = al_cache.get(al_id, None)

    # If we don't have it, do the query
    if j is None:
        try:
            j = get_query(al_id)
        except requests.exceptions.RequestException:
            return None, al_cache
        al_cache[al_id] = copy.deepcopy(j)

    # Pull out number of episodes
    n_eps = _media(j).get("episodes", None)

    return n_eps, al_cache


def get_anilist_title(
    al_id,
    al_cache=None,
):
    """Query AniList to get title for an anime.

    Args:
        al_id (int): Anilist ID
        al_cache (dict): Cached Anilist requests. Defaults to None,
            which will create a dictionary
    """

    # Try and find query in cache
    if al_cache is None:
        al_cache = {}
    j = al_cache.get(al_id, None)

    # If we don't have it, do the query
    if j is None:
        try:
            j = get_query(al_id)
        except requests.exceptions.RequestException:
            return None, al_cache
        al_cache[al_id] = copy.deepcopy(j)

    # Prefer the english title, but fall back to romaji
    title_obj = (_media(j).get("title") or {})
    title = title_obj.get("english") or title_obj.get("romaji")

    return title, al_cache


def get_anilist_thumb(
    al_id,
    al_cache=None,
):
    """Query AniList to get thumbnail URL for an anime.

    Args:
        al_id (int): Anilist ID
        al_cache (dict): Cached Anilist requests. Defaults to None,
            which will create a dictionary
    """

    # Try and find query in cache
    if al_cache is None:
        al_cache = {}
    j = al_cache.get(al_id, None)

    # If we don't have it, do the query
    if j is None:
        try:
            j = get_query(al_id)
        except requests.exceptions.RequestException:
            return None, al_cache
        al_cache[al_id] = copy.deepcopy(j)

    thumb = (_media(j).get("coverImage") or {}).get("large", None)

    return thumb, al_cache


def get_anilist_format(
    al_id,
    al_cache=None,
):
    """Query AniList to get format for an anime.

    Args:
        al_id (int): Anilist ID
        al_cache (dict): Cached Anilist requests. Defaults to None,
            which will create a dictionary
    """

    # Try and find query in cache
    if al_cache is None:
        al_cache = {}
    j = al_cache.get(al_id, None)

    # If we don't have it, do the query
    if j is None:
        try:
            j = get_query(al_id)
        except requests.exceptions.RequestException:
            return None, al_cache
        al_cache[al_id] = copy.deepcopy(j)

    al_format = _media(j).get("format", None)

    return al_format, al_cache
