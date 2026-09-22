from __future__ import annotations

import os
from urllib.parse import urlsplit

DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8890
DEFAULT_SIMPLE_WEB_PORT = 8891
DEFAULT_WEB_URL = f"http://{DEFAULT_WEB_HOST}:{DEFAULT_WEB_PORT}"
DEFAULT_SIMPLE_WEB_URL = f"http://{DEFAULT_WEB_HOST}:{DEFAULT_SIMPLE_WEB_PORT}"

# Port the authoritative backend binds on the server. The laptop forwards its own
# dashboard port here, so browsers there keep typing http://127.0.0.1:8890:
#   ssh -N -L 8890:127.0.0.1:8790 -L 8891:127.0.0.1:8891 daniel@<server>
# It stays 8790 rather than 8890 because School Dashboard's own tunnel targets it.
DEFAULT_SERVER_BACKEND_PORT = 8790

LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})

# Origin the person's browser actually types.  When an SSH tunnel maps the laptop's
# dashboard port onto a different backend port, the backend has to be told about it: Google
# must redirect there after consent, and the browser sends it as Host and Origin.
PUBLIC_ORIGIN_ENV = "CANVAS_TASK_SYNC_PUBLIC_ORIGIN"


def normalize_loopback_origin(value: str) -> str:
    """Return ``value`` as a bare loopback http origin, rejecting anything else."""
    parsed = urlsplit(value.strip())
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTNAMES:
        raise ValueError("The dashboard origin must use http:// on a loopback address.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("The dashboard origin must not carry credentials, a query, or a fragment.")
    if parsed.path not in {"", "/"}:
        raise ValueError("Enter only the dashboard origin, for example http://127.0.0.1:8890.")
    return f"http://{parsed.netloc}"


def resolve_public_origin(port: int) -> str:
    """Browser-visible dashboard origin for this backend."""
    configured = os.getenv(PUBLIC_ORIGIN_ENV, "").strip()
    if not configured:
        return f"http://{DEFAULT_WEB_HOST}:{port}"
    return normalize_loopback_origin(configured)


def origin_port(origin: str) -> int:
    return urlsplit(origin).port or 80


def loopback_host_headers(*ports: int) -> frozenset[str]:
    """``Host`` values a loopback browser sends for ``ports``, under either loopback name."""
    return frozenset(f"{name}:{port}" for name in ("127.0.0.1", "localhost") for port in ports)
