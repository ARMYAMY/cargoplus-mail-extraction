#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$ROOT_DIR/scripts/cloud_deploy.sh" "$@"
