#!/bin/sh
set -eu
base=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Install Python 3.10+ from https://www.python.org/downloads/' >&2
    exit 1
fi
exec python3 "$base/app.py" setup "$@"
