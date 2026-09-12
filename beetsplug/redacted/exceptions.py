"""Custom exceptions for the Redacted client."""


class RedactedError(Exception):
    """Base exception for Redacted API errors."""

    pass


class RedactedRateLimitError(RedactedError):
    """Exception raised when the API rate limit is exceeded."""

    def __init__(self, message: str) -> None:
        """Initialize the rate limit error.

        Args:
            message: Error message.
        """
        super().__init__(message)


class RedactedAPIFailure(RedactedError):
    """Raised when Redacted answers successfully but reports a failure status.

    Distinct from a transport error: the tracker was reached and said no. For an artist
    lookup that means "no such artist here", which is a real answer worth recording,
    whereas an HTTP or network error leaves the album's status genuinely unknown.
    """

    pass
