#!/usr/bin/env bash
# Executed on the Lightsail box as ``ubuntu`` by .github/workflows/deploy.yml.
#
#     bash remote_deploy.sh <target-sha>
#
# Behavior:
#  - Detached-HEAD checkout by SHA, so the box's working tree is a
#    content-addressed cache rather than a branch with drift.
#  - ``pip install`` is unconditional but cheap when nothing changed.
#  - On any failure (service unhealthy or curl /healthz not "ok") the script
#    reverts to the previous SHA, reinstalls deps, restarts, and exits 1.
set -euo pipefail

SHA="${1:?missing target SHA}"
APP=/opt/iotagle/app
VENV=/opt/iotagle/venv
PREV_SHA="$(git -C "$APP" rev-parse HEAD)"

restart_and_probe() {
    sudo /bin/systemctl restart iotagle.service
    for _ in $(seq 1 10); do
        if sudo /bin/systemctl is-active --quiet iotagle.service \
           && curl -fsS --max-time 3 http://127.0.0.1/healthz | grep -q '^ok$'; then
            return 0
        fi
        sleep 1
    done
    return 1
}

echo ">> fetching"
git -C "$APP" fetch --quiet origin

echo ">> checking out $SHA (was $PREV_SHA)"
git -C "$APP" checkout --quiet --detach "$SHA"

echo ">> syncing deps"
"$VENV/bin/pip" install --quiet --disable-pip-version-check -r "$APP/requirements.txt"

echo ">> restart + probe"
if restart_and_probe; then
    echo ">> deployed $SHA"
    exit 0
fi

echo ">> health check failed; rolling back to $PREV_SHA"
git -C "$APP" checkout --quiet --detach "$PREV_SHA"
"$VENV/bin/pip" install --quiet --disable-pip-version-check -r "$APP/requirements.txt"
if restart_and_probe; then
    echo ">> rolled back to $PREV_SHA"
else
    echo ">> ROLLBACK ALSO FAILED — service may be down, manual fix required"
fi
exit 1
