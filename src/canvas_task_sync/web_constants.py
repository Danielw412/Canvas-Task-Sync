from __future__ import annotations

import os
from urllib.parse import urlsplit

DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8890
DEFAULT_SIMPLE_WEB_PORT = 8891
DEFAULT_WEB_URL = f"http://{DEFAULT_WEB_HOST}:{DEFAULT_WEB_PORT}"
DEFAULT_SIMPLE_WEB_URL = f"http://{DEFAULT_WEB_HOST}:{DEFAULT_SIMPLE_WEB_PORT}"

# Port used by the authoritative backend on the remote server.  Keep this separate from the
# laptop-facing dashboard ports above: the Windows proxy can move without changing the server
# deployment or its SSH tunnel target.
DEFAULT_REMOTE_BACKEND_SERVER_PORT = 8790

# Loopback port the laptop forwards to the authoritative backend over SSH:
#   ssh -N -L 8879:127.0.0.1:8790 daniel@<server>
DEFAULT_REMOTE_BACKEND_PORT = 8879
DEFAULT_REMOTE_BACKEND_URL = f"http://{DEFAULT_WEB_HOST}:{DEFAULT_REMOTE_BACKEND_PORT}"

# The backend still answers as though it were the dashboard origin, so its loopback
# host guard keeps working unchanged behind the tunnel.
DEFAULT_BACKEND_HOST_HEADER = f"{DEFAULT_WEB_HOST}:{DEFAULT_REMOTE_BACKEND_SERVER_PORT}"

LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})

# Origin the person's browser actually types.  It is the dashboard origin on the laptop,
# which is also where Google must redirect after consent, so a headless backend needs to
# be told about it when it differs from its own bound port.
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
