#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${BZHDBOT_ROOT:-/home/vadim/bzhd_bot}"
REPOSITORY="${BZHDBOT_REPOSITORY:-https://github.com/bzd-bot-booting/bzd-bot.git}"
BRANCH="${BZHDBOT_BRANCH:-main}"
COMPOSE_PROJECT="${BZHDBOT_COMPOSE_PROJECT:-bzhd-local}"
STATE_DIR="${BZHDBOT_STATE_DIR:-/var/lib/bzhd-bot-updater}"

cd "$ROOT_DIR"

if [[ ! -f .env || ! -f docker-compose.yml ]]; then
    echo "ERROR: .env or docker-compose.yml is missing." >&2
    exit 1
fi

if [[ ! -f deploy/update-bot.sh ]]; then
    echo "ERROR: deploy/update-bot.sh is missing." >&2
    exit 1
fi

if ! docker image inspect bzhd-bot:server >/dev/null 2>&1; then
    echo "ERROR: initial image bzhd-bot:server is missing." >&2
    exit 1
fi

PROJECT="$(docker inspect bzhd-bot \
    --format '{{ index .Config.Labels "com.docker.compose.project" }}' 2>/dev/null || true)"

if [[ "$PROJECT" != "$COMPOSE_PROJECT" ]]; then
    echo "ERROR: bzhd-bot does not belong to Compose project $COMPOSE_PROJECT." >&2
    echo "Detected project: ${PROJECT:-unknown}" >&2
    exit 1
fi

sudo install -m 0644 deploy/bzhd-bot-update.service \
    /etc/systemd/system/bzhd-bot-update.service
sudo install -m 0644 deploy/bzhd-bot-update.timer \
    /etc/systemd/system/bzhd-bot-update.timer

sudo install -d -m 0750 "$STATE_DIR"

SOURCE_DIR="${STATE_DIR}/source.git"

if [[ ! -d "$SOURCE_DIR" ]]; then
    sudo git clone --bare --filter=blob:none "$REPOSITORY" "$SOURCE_DIR"
fi

sudo git --git-dir="$SOURCE_DIR" fetch --quiet origin \
    "+refs/heads/${BRANCH}:refs/heads/${BRANCH}"

DEPLOYED_COMMIT="$(sudo git --git-dir="$SOURCE_DIR" rev-parse "refs/heads/${BRANCH}")"

printf '%s\n' "$DEPLOYED_COMMIT" | sudo tee "${STATE_DIR}/deployed_commit" >/dev/null
sudo chmod 0600 "${STATE_DIR}/deployed_commit"

sudo systemctl daemon-reload
sudo systemctl enable --now bzhd-bot-update.timer

echo "Updater installed."
echo "Branch: ${BRANCH}"
echo "Baseline commit: ${DEPLOYED_COMMIT}"
