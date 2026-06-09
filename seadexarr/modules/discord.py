import time

from discordwebhook import Discord

# Discord allows 30 webhook messages per minute per channel; pacing posts two
# seconds apart stays under that without relying on 429 retries.
POST_SLEEP = 2


def discord_post_with_retry(url, embeds, logger=None, label=""):
    """Post embeds to a Discord webhook, retrying up to 3 times on 429.

    Args:
        url (str): Webhook URL
        embeds (list): List of embed dicts
        logger: Logging instance for rate-limit warnings. Defaults to None
        label (str): Item name to include in warnings. Defaults to ""

    Returns:
        The final requests response (None when the library returns none).
    """

    discord = Discord(url=url)
    response = None
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
            if logger is not None:
                logger.warning(
                    "Discord rate limited%s — sleeping %.1fs (attempt %d/3)",
                    f" for {label}" if label else "", sleep_for, attempt + 1,
                )
            time.sleep(sleep_for)
            discord = Discord(url=url)
            continue
        return response
    return response


def discord_push(
    url,
    arr_title,
    al_title,
    seadex_url,
    fields,
    thumb_url,
    logger=None,
):
    """Post a message to Discord

    Args:
        url (str): URL to post to
        arr_title (str): Title as in Arr instance
        al_title (str): Title as in AniList
        seadex_url (str): URL to SeaDex page
        fields (list): List of dicts containing links
            for the fields
        thumb_url (str): URL for thumbnail
        logger: Logging instance for failure/rate-limit warnings.
            Defaults to None

    Returns:
        bool: True when the post succeeded
    """

    embed = {
        "author": {
            "name": arr_title,
            "url": "https://github.com/bbtufty/seadexarr",
        },
        "title": al_title,
        "description": seadex_url,
        "fields": fields,
        "thumbnail": {"url": thumb_url},
    }

    response = discord_post_with_retry(url, [embed], logger=logger, label=arr_title)
    success = response is None or response.ok
    if not success and logger is not None:
        logger.warning(
            "Discord post failed for %s (%s): %s",
            arr_title, response.status_code, response.text[:200],
        )

    # Sleep for a bit to avoid rate limiting
    time.sleep(POST_SLEEP)

    return success
