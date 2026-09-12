import logging
import re
from typing import Union


def normalize_query(
    artist: Union[str, list[str]], album: str, log: logging.Logger
) -> Union[str, None]:
    """Normalize a query string for searching.

    Args:
        query: The query string to normalize

    Returns:
        The normalized query string
    """
    if isinstance(artist, list):
        artist = " ".join(artist)

    # Remove featuring artists to improve search results
    artist = re.sub(
        r"\s+(and|ft\.?|feat\.?|featuring|\+|\&) .*", " ", artist, flags=re.IGNORECASE
    ).strip()

    # Deliberately no longer strips words like "EP", "Vol", "Single" or "Collection"
    # from the title. They are ordinary title words as often as they are format noise,
    # and removing them searches for the wrong record: "The Singles Collection" became
    # "The Singles", "MIKUHOP EP" became "MIKUHOP", and "ANNIVERSARY VOL.10" became
    # "ANNIVERSARY 10", which returns a different release that then scores well enough
    # to be accepted. Edition noise in brackets is still removed below, and the scoring
    # side tolerates the rest.

    query = f"{artist} {album}"

    # Remove text within parentheses, such as "(2015 Remaster)"
    query = re.sub(r"\(.*\)", " ", query, flags=re.IGNORECASE)
    query = re.sub(r"\[.*\]", " ", query, flags=re.IGNORECASE)

    # Remove non-alphanumeric characters
    query = re.sub(r"[^\w\d]", " ", query)

    # Normalize whitespace: replace multiple spaces with a single space and strip
    query = re.sub(r"\s+", " ", query).strip()

    log.debug("Search query: {0}", query)
    if not query:
        return None
    return query
