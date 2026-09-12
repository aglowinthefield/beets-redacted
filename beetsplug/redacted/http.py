"""HTTP client implementation for Redacted API."""

import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Union

import requests
from backoff import expo, full_jitter, on_exception
from diskcache import Cache  # type: ignore[import-untyped]
from ratelimit import RateLimitException, limits, sleep_and_retry  # type: ignore[import-untyped]

from .exceptions import RedactedError, RedactedRateLimitError

# Constants for rate limiting. Redacted requires at most 0.5 qps.
PERIOD_SECONDS = 10
CALLS_PER_PERIOD = 5

# Cache configuration
# Persistent (not /tmp — that is tmpfs on many systems and wiped on reboot, which
# would re-hammer the RED API on the next `beet redacted` run).
CACHE_DIR = os.path.expanduser("~/.cache/redacted")
CACHE_EXPIRY = timedelta(days=7)


def _is_api_failure_body(response: requests.Response) -> bool:
    """True if the response body is a well-formed Redacted failure payload."""
    try:
        return bool(response.json().get("status") == "failure")
    except ValueError:
        return False


class HTTPClient(ABC):
    """Abstract base class for HTTP clients."""

    @abstractmethod
    def get(self, params: dict[str, str], headers: dict[str, str]) -> requests.Response:
        """Make a GET request.

        Args:
            params: Query parameters.
            headers: Request headers.

        Returns:
            Response object.
        """
        pass


class RequestsClient(HTTPClient):
    """HTTP client implementation using requests library."""

    def __init__(self, url: str, log: logging.Logger) -> None:
        """Initialize the client.

        Args:
            url: URL for API requests.
            log: Logger instance for logging messages.
        """
        self.url = url
        self.log = log
        self.session = requests.Session()

    def _get(self, params: dict[str, str], headers: dict[str, str]) -> requests.Response:
        """Make a GET request without rate limiting or retries.

        Args:
            params: Query parameters.
            headers: Request headers.

        Returns:
            Response object.

        Raises:
            RedactedError: If the request fails.
            RedactedRateLimitError: If rate limited by the Redacted API.
        """
        self.log.debug("Making GET request to {0}: {1}", self.url, params)
        try:
            response = self.session.get(self.url, params=params, headers=headers)

            # Redacted answers an unknown artist with HTTP 404 and a JSON body of
            # {"status": "failure", "error": "no artist found"}. That is a definitive
            # answer, not a transport fault: raising here would send it through the
            # retry decorator, costing eight requests and an exponential backoff for
            # every miss, and it would never be cached. Hand it back so the caller
            # parses the body and the cache stores the negative result.
            if response.status_code in (400, 404) and _is_api_failure_body(response):
                self.log.debug("Redacted API failure response: {0}", response.text)
                return response

            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                self.log.debug("Redacted rate limit exceeded response: {0}", e.response.text)
                raise RedactedRateLimitError("Rate limit exceeded") from e

            self.log.debug("Redacted API error response: {0}", e.response.text)
            raise RedactedError(f"HTTP error: {e}") from e
        except requests.exceptions.RequestException as e:
            self.log.debug("Redacted request error: {0}", e)
            raise RedactedError(f"Request error: {e}") from e

    @sleep_and_retry  # type: ignore[misc]
    @limits(calls=CALLS_PER_PERIOD, period=PERIOD_SECONDS)  # type: ignore[misc]
    @on_exception(
        expo,
        (RedactedError, RedactedRateLimitError, RateLimitException),
        max_tries=8,
        jitter=full_jitter,
    )
    def get(self, params: dict[str, str], headers: dict[str, str]) -> requests.Response:
        """Make a GET request with rate limiting and retries.

        Args:
            params: Query parameters.
            headers: Request headers.

        Returns:
            Response object.

        Raises:
            RedactedError: If the request fails.
            RedactedRateLimitError: If rate limited by the Redacted API.
            RateLimitException: If rate limited by the ratelimit library.
        """
        return self._get(params, headers)


class CachedRequestsClient(RequestsClient):
    """HTTP client implementation with disk caching."""

    def __init__(self, url: str, log: logging.Logger, cache_dir: str = CACHE_DIR) -> None:
        """Initialize the client.

        Args:
            url: URL for API requests.
            log: Logger instance for logging messages.
            cache_dir: Directory to store cache files. Defaults to CACHE_DIR.
        """
        super().__init__(url, log)
        os.makedirs(cache_dir, exist_ok=True)
        self.cache = Cache(cache_dir)
        self.cache_dir = cache_dir

    def close(self) -> None:
        """Close the cache connection."""
        self.log.debug("Closing cache connection")
        self.cache.close()

    def _get_cached_response(
        self, params: dict[str, str], headers: dict[str, str]
    ) -> Union[requests.Response, None]:
        """Get a cached response if available and not expired.

        Args:
            params: Query parameters.
            headers: Request headers.

        Returns:
            Cached response if available and valid, None otherwise.
        """
        cache_key = f"{self.url}:{params!s}:{headers!s}"
        cached_data = self.cache.get(cache_key)
        if not cached_data:
            return None

        timestamp, response_data = cached_data
        if datetime.fromtimestamp(timestamp) + CACHE_EXPIRY < datetime.now():
            return None

        response = requests.Response()
        response.status_code = response_data["status_code"]
        response.headers = response_data["headers"]
        response._content = response_data["content"]
        return response

    def _cache_response(
        self, params: dict[str, str], headers: dict[str, str], response: requests.Response
    ) -> None:
        """Cache a response.

        Args:
            params: Query parameters.
            headers: Request headers.
            response: Response to cache.
        """
        cache_key = f"{self.url}:{params!s}:{headers!s}"
        response_data = {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "content": response.content,
        }
        self.cache.set(cache_key, (datetime.now().timestamp(), response_data))

    def get(self, params: dict[str, str], headers: dict[str, str]) -> requests.Response:
        """Make a GET request with caching, rate limiting and retries.

        Args:
            params: Query parameters.
            headers: Request headers.

        Returns:
            Response object.

        Raises:
            RedactedError: If the request fails.
            RedactedRateLimitError: If rate limited by the Redacted API.
            RateLimitException: If rate limited by the ratelimit library.
        """
        # Check cache first.
        #
        # Must be an explicit None check: requests.Response.__bool__ is its .ok, so a
        # truthiness test silently discards every cached non-2xx response and refetches
        # it. That matters now that a 404 "no artist found" is a cached, definitive
        # answer rather than an error.
        cached_response = self._get_cached_response(params, headers)
        if cached_response is not None:
            return cached_response

        # If not in cache, make the request
        response: requests.Response = super().get(params, headers)
        self._cache_response(params, headers, response)
        return response
