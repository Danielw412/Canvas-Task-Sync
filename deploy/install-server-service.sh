#!/usr/bin/env bash
# Install (or reinstall) the systemd user service that runs the backend and both
# dashboards.
#
# Run this on the server, from the repository root:
#     ./deploy/install-server-service.sh
#
# Everything binds 127.0.0.1 only. A laptop reaches the dashboards at its usual addresses,
# http://127.0.0.1:8890 and :8891, with:
#     ssh -N -L 127.0.0.1:8890:127.0.0.1:8790 -L 127.0.0.1:8891:127.0.0.1:8891 daniel@<server>
# On Windows, scripts/install-windows-startup.ps1 -ServerHost daniel@<server> keeps that
# tunnel open from sign-in.
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

# Lingering is what makes this start at boot rather than at first login, and what keeps
# scheduled syncs running after you log out. Try it directly; polkit may allow it.
if [[ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null || echo no)" != "yes" ]]; then
  loginctl enable-linger "$USER" 2>/dev/null || true
fi
if [[ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null || echo no)" == "yes" ]]; then
  echo "user lingering is on: the backend and dashboards start at boot and survive logout."
else
  echo
  echo "note: user lingering is off, so the backend and dashboards start only at login and stop at logout."
  echo "      Enable it once with:  sudo loginctl enable-linger $USER"
fi

echo
systemctl --user --no-pager status "${UNIT_NAME}" || true
