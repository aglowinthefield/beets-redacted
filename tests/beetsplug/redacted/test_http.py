# type: ignore
"""Tests for the HTTP client."""

import tempfile
from unittest.mock import patch

import pytest
import requests

from beetsplug.redacted.exceptions import RedactedError, RedactedRateLimitError
from beetsplug.redacted.http import CachedRequestsClient, HTTPClient, RequestsClient
from beetsplug.redacted.utils.test_utils import FakeLogger


@pytest.fixture
def log():
    """Create a fake logger."""
    return FakeLogger()


def test_abstract_base_class():
    """Test that HTTPClient is an abstract base class."""
    with pytest.raises(TypeError):
        HTTPClient()  # type: ignore


def test_requests_client_get_success(log: FakeLogger):
    """Test successful GET request."""
    client = RequestsClient("https://redacted.ch/api", log)
    mock_response = requests.Response()
    mock_response.status_code = 200
    mock_response._content = b'{"test": "data"}'
    mock_response.headers["content-type"] = "application/json"

    with patch("requests.Session.get", return_value=mock_response):
        response = client.get({"param": "value"}, {"header": "value"})
        assert response.status_code == 200
        assert response.json() == {"test": "data"}


def test_requests_client_get_rate_limit(log: FakeLogger):
    """Test rate limited GET request."""
    client = RequestsClient("https://redacted.ch/api", log)
    mock_response = requests.Response()
    mock_response.status_code = 429
    mock_error = requests.exceptions.HTTPError(response=mock_response)

    with patch("requests.Session.get", side_effect=mock_error):
        with pytest.raises(RedactedRateLimitError, match="Rate limit exceeded"):
            client._get({"param": "value"}, {"header": "value"})


def test_requests_client_get_http_error(log: FakeLogger):
    """Test HTTP error in GET request."""
    client = RequestsClient("https://redacted.ch/api", log)
    mock_response = requests.Response()
    mock_response.status_code = 500
    mock_error = requests.exceptions.HTTPError(response=mock_response)

    with patch("requests.Session.get", side_effect=mock_error):
        with pytest.raises(RedactedError, match="HTTP error"):
            client._get({"param": "value"}, {"header": "value"})


def test_cached_client_basic_functionality(log: FakeLogger):
    """Test basic functionality of the cached client."""
    # Get a temporary directory for the test
    with tempfile.TemporaryDirectory(prefix="redacted_test_cache") as temp_dir:
        # Set up mock response
        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_response._content = b'{"test": "data"}'
        mock_response.headers["content-type"] = "application/json"

        with patch("requests.Session.get", return_value=mock_response):
            client = CachedRequestsClient("https://redacted.ch/api", log, cache_dir=temp_dir)
            params = {"test": "value"}
            headers = {"Authorization": "test"}

            # First request should work
            response1 = client.get(params, headers)
            assert response1.status_code == mock_response.status_code
            assert response1.content == mock_response.content

            # Second request should also work (whether from cache or not)
            response2 = client.get(params, headers)
            assert response2.status_code == mock_response.status_code
            assert response2.content == mock_response.content

            # Make sure to close the cache connection before the temporary directory is cleaned up
            client.cache.close()


def _response(status_code: int, body: bytes) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = body
    response.headers["Content-Type"] = "application/json"
    response.url = "https://redacted.sh/ajax.php"
    return response


NO_ARTIST_BODY = b'{"status":"failure","error":"no artist found"}'


def test_api_failure_404_is_returned_not_retried(log: FakeLogger):
    """Redacted answers an unknown artist with 404 plus a JSON failure body.

    That is a definitive answer. Raising would send it through the retry decorator,
    costing eight requests and an exponential backoff for every miss.
    """
    client = RequestsClient("https://redacted.sh/ajax.php", log)
    with patch.object(client.session, "get", return_value=_response(404, NO_ARTIST_BODY)) as get:
        response = client.get(params={"action": "artist"}, headers={})

    assert response.status_code == 404
    assert get.call_count == 1


def test_genuine_http_error_still_raises(log: FakeLogger):
    """A 404 without a Redacted failure body is a real error, not an answer."""
    client = RequestsClient("https://redacted.sh/ajax.php", log)
    with patch.object(client.session, "get", return_value=_response(404, b"<html>nope</html>")):
        with pytest.raises(RedactedError):
            client._get(params={"action": "artist"}, headers={})


def test_rate_limit_still_raises(log: FakeLogger):
    client = RequestsClient("https://redacted.sh/ajax.php", log)
    with patch.object(client.session, "get", return_value=_response(429, b"slow down")):
        with pytest.raises(RedactedRateLimitError):
            client._get(params={"action": "artist"}, headers={})


def test_cached_failure_response_is_reused(log: FakeLogger):
    """requests.Response.__bool__ is its .ok, so a truthiness check on the cached
    response silently discards every cached non-2xx response and refetches it."""
    with tempfile.TemporaryDirectory() as cache_dir:
        client = CachedRequestsClient("https://redacted.sh/ajax.php", log, cache_dir=cache_dir)
        params, headers = {"action": "artist", "artistname": "Nobody"}, {"Authorization": "k"}

        with patch.object(
            client.session, "get", return_value=_response(404, NO_ARTIST_BODY)
        ) as get:
            first = client.get(params=params, headers=headers)
            second = client.get(params=params, headers=headers)

        assert first.status_code == 404
        assert second.status_code == 404
        assert second.content == NO_ARTIST_BODY
        # The second lookup must come from the cache.
        assert get.call_count == 1
        client.close()


def test_cached_success_response_is_reused(log: FakeLogger):
    with tempfile.TemporaryDirectory() as cache_dir:
        client = CachedRequestsClient("https://redacted.sh/ajax.php", log, cache_dir=cache_dir)
        params, headers = {"action": "artist", "id": "1"}, {"Authorization": "k"}

        with patch.object(
            client.session, "get", return_value=_response(200, b'{"status":"success"}')
        ) as get:
            client.get(params=params, headers=headers)
            client.get(params=params, headers=headers)

        assert get.call_count == 1
        client.close()
