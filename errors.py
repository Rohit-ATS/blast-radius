"""What a caller is told when something fails.

An exception message is written for whoever is reading the logs. It routinely
carries the things that make an attacker's job easier — internal hostnames and
ports, filesystem paths, driver and library versions, fragments of SQL, the
shape of the schema — and putting it in an HTTP response publishes all of that
to anyone who can provoke the error.

Removing it entirely is the other failure. "Something went wrong" with no
handle turns every report into an interrogation, and it was the `detail` field
on these responses that made most of this project's own outages diagnosable
from a single curl.

So the split is: the full exception goes to the log, keyed by request id, and
the response carries a *classification* — one of a fixed set of strings chosen
here, never text derived from the exception. `reason()` is the only thing that
crosses that boundary, and because its vocabulary is closed it cannot leak
something new when an unfamiliar exception arrives.

    log:       ConnectionError: HTTPConnectionPool(host='hydradb-l2lg',
               port=10000): Max retries exceeded ... request_id=f9657b5e
    response:  {"error": "...", "reason": "connection refused",
                "request_id": "f9657b5e"}

The request id is what ties them together, so a user can quote eight
characters and an operator can find the whole thing.
"""

from __future__ import annotations

# The closed vocabulary. Every one of these describes a *class* of failure that
# a caller can act on — retry, check their input, check their own endpoint —
# without describing this system's internals.
UNREACHABLE = "connection refused"
DNS = "host could not be resolved"
TIMEOUT = "timed out"
TLS = "tls handshake failed"
AUTH = "authentication rejected"
NOT_FOUND = "not found"
RATE_LIMITED = "rate limited by the upstream service"
BAD_RESPONSE = "upstream returned an unusable response"
DATA = "the data did not match what was expected"
UNAVAILABLE = "temporarily unavailable"
INTERNAL = "internal error"

# Matched against the exception's class name and its text, lowercased. Ordered:
# the first hit wins, so the specific patterns come before the general ones.
_SIGNS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("nameresolutionerror", "getaddrinfo", "name or service not known",
      "nodename nor servname", "no address associated"), DNS),
    (("connectionrefused", "connection refused", "actively refused",
      "failed to establish a new connection", "connectionerror"), UNREACHABLE),
    (("timeout", "timed out", "readtimeout", "query_timeout"), TIMEOUT),
    (("sslerror", "certificate", "tlsv1", "ssl:"), TLS),
    (("unauthorized", "forbidden", "invalid token", "authenticationfailed",
      "permission denied", "401", "403"), AUTH),
    (("notfound", "no such", "does not exist", "404"), NOT_FOUND),
    (("toomanyrequests", "rate limit", "429"), RATE_LIMITED),
    (("jsondecodeerror", "decodeerror", "unicodedecodeerror",
      "invalid json", "unusable"), BAD_RESPONSE),
    (("valueerror", "keyerror", "typeerror", "validationerror"), DATA),
    (("operationalerror", "interfaceerror", "poolerror", "serviceunavailable",
      "503", "502"), UNAVAILABLE),
)


def reason(exc: BaseException | str | None) -> str:
    """A safe, fixed-vocabulary description of why something failed.

    Takes the exception rather than its message so the class name can be
    matched too — `NameResolutionError` says what went wrong even when its
    text is a wall of urllib3 internals.
    """
    if exc is None:
        return INTERNAL
    if isinstance(exc, str):
        blob = exc.lower()
    else:
        blob = f"{type(exc).__name__} {exc}".lower()

    for needles, label in _SIGNS:
        if any(n in blob for n in needles):
            return label
    return INTERNAL


def detail(exc: BaseException | str | None, limit: int = 300) -> str:
    """The full text, for logs only.

    Deliberately named so that a call site putting this in a response is
    obvious in review — `errors.detail(...)` next to a JSONResponse should
    read as a mistake, where `str(exc)` read as ordinary.
    """
    if exc is None:
        return ""
    if isinstance(exc, str):
        return exc[:limit]
    return f"{type(exc).__name__}: {exc}"[:limit]
