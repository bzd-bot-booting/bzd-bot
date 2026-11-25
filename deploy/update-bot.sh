#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${BZHDBOT_ROOT:-/home/vadim/bzhd_bot}"
BRANCH="${BZHDBOT_BRANCH:-main}"
REPOSITORY="${BZHDBOT_REPOSITORY:-https://github.com/bzd-bot-booting/bzd-bot.git}"
COMPOSE_PROJECT="${BZHDBOT_COMPOSE_PROJECT:-bzhd-local}"

IMAGE="bzhd-bot:server"
CANDIDATE_IMAGE="bzhd-bot:server-candidate"
ROLLBACK_IMAGE="bzhd-bot:server-rollback"

STATE_DIR="${BZHDBOT_STATE_DIR:-/var/lib/bzhd-bot-updater}"
STATE_FILE="${STATE_DIR}/deployed_commit"
SOURCE_DIR="${STATE_DIR}/source.git"
LOCK_FILE="${BZHDBOT_LOCK_FILE:-/run/lock/bzhd-bot-updater.lock}"

COMPOSE=(docker compose
    --project-name "$COMPOSE_PROJECT"
    --file "$ROOT_DIR/docker-compose.yml"
    --project-directory "$ROOT_DIR"
)

mkdir -p "$STATE_DIR" "$(dirname "$LOCK_FILE")"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "Updater is already running; skip."
    exit 0
fi

if [[ ! -f "$ROOT_DIR/.env" ]]; then
    echo "ERROR: local .env is missing; refusing update." >&2
    exit 1
fi

if [[ ! -f "$ROOT_DIR/docker-compose.yml" ]]; then
    echo "ERROR: Compose file is missing." >&2
    exit 1
fi

if [[ ! -d "$SOURCE_DIR" ]]; then
    git clone --bare --filter=blob:none "$REPOSITORY" "$SOURCE_DIR"
fi

git --git-dir="$SOURCE_DIR" fetch --quiet origin \
    "+refs/heads/${BRANCH}:refs/heads/${BRANCH}"

LATEST_COMMIT="$(git --git-dir="$SOURCE_DIR" rev-parse "refs/heads/${BRANCH}")"

if [[ ! -s "$STATE_FILE" ]]; then
    echo "ERROR: deployed commit state is missing. Install updater first." >&2
    exit 1
fi

DEPLOYED_COMMIT="$(cat "$STATE_FILE")"

if [[ "$LATEST_COMMIT" == "$DEPLOYED_COMMIT" ]]; then
    echo "No update: ${LATEST_COMMIT}"
    exit 0
fi

echo "Update found: ${DEPLOYED_COMMIT} -> ${LATEST_COMMIT}"

TMP_DIR="$(mktemp -d /tmp/bzhd-bot-update.XXXXXX)"
cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

git --git-dir="$SOURCE_DIR" archive "$LATEST_COMMIT" | tar -x -C "$TMP_DIR"

echo "Building candidate image..."
docker build --pull --tag "$CANDIDATE_IMAGE" "$TMP_DIR"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "ERROR: current image is missing; refusing automatic replacement." >&2
    exit 1
fi

docker image tag "$IMAGE" "$ROLLBACK_IMAGE"
docker image tag "$CANDIDATE_IMAGE" "$IMAGE"

wait_for_health() {
    local attempts="${1:-60}"
    local status=""

    for ((i=1; i<=attempts; i++)); do
        status="$(docker inspect bzhd-bot \
            --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
            2>/dev/null || true)"

        if [[ "$status" == "healthy" ]]; then
            return 0
        fi

        if [[ "$status" == "exited" || "$status" == "dead" ]]; then
            return 1
        fi

        sleep 5
    done

    return 1
}

rollback() {
    echo "Update failed. Restoring previous image..." >&2
    docker image tag "$ROLLBACK_IMAGE" "$IMAGE"

    "${COMPOSE[@]}" up -d --no-deps --force-recreate --no-build bot || true

    if wait_for_health 60; then
        echo "Previous version restored and healthy." >&2
    else
        echo "WARNING: rollback was attempted, but health was not confirmed." >&2
    fi
}

echo "Recreating bot container..."
if ! "${COMPOSE[@]}" up -d --no-deps --force-recreate --no-build bot; then
    rollback
    exit 1
fi

echo "Waiting for bot health check..."
if ! wait_for_health 60; then
    echo "ERROR: updated bot did not become healthy." >&2
    rollback
    exit 1
fi

printf '%s\n' "$LATEST_COMMIT" > "${STATE_FILE}.tmp"
chmod 0600 "${STATE_FILE}.tmp"
mv "${STATE_FILE}.tmp" "$STATE_FILE"

docker image rm "$CANDIDATE_IMAGE" >/dev/null 2>&1 || true

echo "Update successful: ${LATEST_COMMIT}"
