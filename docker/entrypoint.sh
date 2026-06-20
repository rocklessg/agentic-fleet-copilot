#!/usr/bin/env bash
set -euo pipefail

python scripts/bootstrap.py
exec "$@"
