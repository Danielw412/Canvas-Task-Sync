#!/usr/bin/env bash
# Install (or reinstall) the systemd user service for the authoritative backend.
#
# Run this on the server, from the repository root:
#     ./deploy/install-server-service.sh
#
# The backend binds 127.0.0.1 only. Reach it from a laptop with:
#     ssh -N -L 8879:127.0.0.1:8790 daniel@<server>
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="canvas-task-sync.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [[ ! -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  echo "error: ${REPO_ROOT}/.venv/bin/python is missing. Create the venv first:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

for required in config/courses.yaml credentials.json .env; do
  if [[ ! -f "${REPO_ROOT}/${required}" ]]; then
    echo "warning: ${required} is missing; the backend will report setup as incomplete." >&2
  fi
done

mkdir -p "${UNIT_DIR}"
# %h expands to the running user's home, so the unit stays portable across accounts.
sed "s|%h/projects/Canvas-Task-Sync|${REPO_ROOT}|g" \
  "${REPO_ROOT}/deploy/${UNIT_NAME}" > "${UNIT_DIR}/${UNIT_NAME}"

systemctl --user daemon-reload
systemctl --user enable "${UNIT_NAME}"
systemctl --user restart "${UNIT_NAME}"

# Without lingering the service stops when the last login session ends, which defeats
# scheduled syncs. This needs root, so it is reported rather than forced.
if [[ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null || echo no)" != "yes" ]]; then
  echo
  echo "note: user lingering is off, so the backend stops when you log out."
  echo "      Enable it once with:  sudo loginctl enable-linger $USER"
fi

echo
systemctl --user --no-pager status "${UNIT_NAME}" || true
