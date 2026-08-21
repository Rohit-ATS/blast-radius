"""Configuration, from one place.

Every tunable in this project is an environment variable, which is correct for
containers and hostile to a human trying to run it. So this module reads a
`.env` file sitting next to it and exports its keys into the environment
*before* anything else imports, then hands back a description of what it found.

Real environment variables always win over the file — a deploy that sets
SUPABASE_URL in its own way is not overridden by a stale checkout.

Import this first, from the process entrypoint:

    import config            # noqa: F401  (side effect: loads .env)

`python setup_check.py` validates whatever ends up here.
"""

from __future__ import annotations

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.environ.get("BLAST_ENV_FILE", os.path.join(HERE, ".env"))

_LINE = re.compile(r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$""")


def load_env(path: str = ENV_PATH) -> dict[str, str]:
    """Parse a .env file into os.environ without clobbering real env vars.

    Supports `KEY=value`, `export KEY=value`, `#` comments, and single or
    double quoted values. Deliberately not a full dotenv implementation: no
    interpolation, no multiline. If you need those, you are past the point
    where a .env file is the right answer.
    """
    found: dict[str, str] = {}
    if not os.path.exists(path):
        return found

    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = _LINE.match(line)
            if not m:
                continue
            key, value = m.group(1), m.group(2)

            # strip a trailing comment only when the value is unquoted
            if value and value[0] not in "\"'":
                value = value.split(" #", 1)[0].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]

            found[key] = value
            os.environ.setdefault(key, value)     # real env wins
    return found


LOADED = load_env()


# --------------------------------------------------------------------------
# accessors
# --------------------------------------------------------------------------

def get(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def flag(name: str, default: bool = False) -> bool:
    raw = get(name, "1" if default else "0").lower()
    return raw in ("1", "true", "yes", "on")


def number(name: str, default: float) -> float:
    try:
        return float(get(name) or default)
    except ValueError:
        return default


# --------------------------------------------------------------------------
# what is configured
# --------------------------------------------------------------------------

SUPABASE_URL = get("SUPABASE_URL").rstrip("/")
SUPABASE_ANON_KEY = get("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = get("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_ON = bool(SUPABASE_URL and SUPABASE_ANON_KEY)

PUBLIC_URL = get("PUBLIC_URL").rstrip("/")
SECURE_COOKIES = flag("SECURE_COOKIES", False)

SMTP_HOST = get("SMTP_HOST")
SMTP_PORT = int(number("SMTP_PORT", 587))
SMTP_USER = get("SMTP_USER")
SMTP_PASSWORD = get("SMTP_PASSWORD")
SMTP_FROM = get("SMTP_FROM") or SMTP_USER
SMTP_TLS = flag("SMTP_STARTTLS", True)
EMAIL_ON = bool(SMTP_HOST and SMTP_FROM)

MONITOR_INTERVAL = number("MONITOR_INTERVAL", 300)
MONITOR_STALE = number("MONITOR_STALE", 3600)
WEBHOOK_TIMEOUT = number("WEBHOOK_TIMEOUT", 10)


def redact(value: str, keep: int = 6) -> str:
    if not value:
        return ""
    return value[:keep] + "…" + f"({len(value)} chars)"


def describe(public: bool = False) -> dict:
    """What is configured. Never contains a secret's value.

    `public=True` is the version served over HTTP. It drops the things that are
    not secrets but are still nobody's business from outside: the filesystem
    path of the env file, and the list of which variables are set. Neither is a
    credential, but together they hand a stranger a map of the deployment —
    and a runtime health check does not need either. `setup_check.py` runs
    locally and gets the full picture.
    """
    common = {
        "auth_provider": "supabase" if SUPABASE_ON else "local",
        "supabase_configured": SUPABASE_ON,
        "public_url": PUBLIC_URL or None,
        "secure_cookies": SECURE_COOKIES,
        "email_delivery": "smtp" if EMAIL_ON else "off",
        "monitor_interval_s": MONITOR_INTERVAL,
        "monitor_stale_s": MONITOR_STALE,
    }
    if public:
        return common

    return {
        **common,
        "env_file": ENV_PATH if os.path.exists(ENV_PATH) else None,
        "env_keys_loaded": sorted(LOADED.keys()),
        "supabase_url": SUPABASE_URL or None,
        "supabase_anon_key": redact(SUPABASE_ANON_KEY) or None,
        "supabase_service_key_set": bool(SUPABASE_SERVICE_KEY),
        "smtp_host": SMTP_HOST or None,
    }
