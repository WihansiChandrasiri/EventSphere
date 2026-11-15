#!/usr/bin/env bash
set -euo pipefail

# Create demo accounts only when explicitly enabled.
if [ "${SEED_DEMO:-false}" = "true" ]; then
    python -m app.seed
fi

# Run one application worker, including the waitlist timer.
exec python -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1