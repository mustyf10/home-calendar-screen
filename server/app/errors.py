"""One place that turns failures into words for the household, and into detail for the log.

Screens show `user_message(...)`: what failed, in plain language, and what to try.
`log_failure(...)` writes the developer view: exception type, upstream text and traceback.
"""
import logging

import anthropic
import httpx

from app.claude import NoResult

SEE_LOG = "Something went wrong on the Pi. The details are in the server log."


class UserFacingError(Exception):
    """A failure whose message is written for the household and safe to show as is.

    `detail` is the developer's version (status codes, upstream text) and only goes to the log.
    """

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message)
        self.detail = detail


def reason(exc: BaseException) -> str:
    """Why it failed, in plain language, with a next step where there is one."""
    if isinstance(exc, UserFacingError):
        return str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "The other site took too long to answer. Try again in a minute."
    if isinstance(exc, httpx.HTTPError):
        return "Couldn't connect. Check the address and the Pi's internet connection."
    if isinstance(exc, anthropic.AuthenticationError):
        return "The AI service rejected the API key. Check ANTHROPIC_API_KEY."
    if isinstance(exc, anthropic.RateLimitError):
        return "The AI service is busy or out of credit. Try again later."
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.InternalServerError)):
        return "Couldn't reach the AI service. Try again in a few minutes."
    if isinstance(exc, anthropic.APIError):
        return "The AI service returned an error. Try again later."
    if isinstance(exc, NoResult):
        return "The AI couldn't make sense of it. Try again, or add the details by hand."
    if isinstance(exc, PermissionError):
        return "The Pi isn't allowed to write there. Check the folder's permissions."
    if isinstance(exc, OSError):
        return "The Pi couldn't read or write a file. Check the disk has space."
    return SEE_LOG


def user_message(action: str, exc: BaseException) -> str:
    """`action` names what failed ("Couldn't load that calendar"); the reason follows."""
    return f"{action}. {reason(exc)}"


def log_failure(logger: logging.Logger, what: str, exc: BaseException) -> None:
    """Known failures get one line with their detail; anything else gets its traceback."""
    if isinstance(exc, UserFacingError):
        logger.warning("%s: %s: %s", what, type(exc).__name__, exc.detail or exc)
    else:
        logger.warning("%s", what, exc_info=exc)
