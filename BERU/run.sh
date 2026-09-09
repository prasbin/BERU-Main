#!/usr/bin/env sh
# Run the BERU server locally / in production.
#
#   env from .env        ->  ./run.sh
#   default (no .env)    ->  python3 -m backend.main  (localhost, mock LLM)
#   production bind      ->  BERU_API_KEY=... HOST=0.0.0.0 ./run.sh
#
# BERU refuses to bind a non-localhost interface without a BERU_API_KEY.
# See docs/deployment.md.
set -eu

if [ -f .env ]; then
    # shellcheck disable=SC1091
    . ./.env
fi

if [ -n "${HOST+x}" ] && [ "${HOST:-}" != "127.0.0.1" ] && [ "${HOST:-}" != "localhost" ]; then
    exec uvicorn backend.main:app \
        --host "${HOST}" --port "${PORT:-8000}" \
        --timeout-graceful-shutdown 30
fi

exec python3 -m backend.main