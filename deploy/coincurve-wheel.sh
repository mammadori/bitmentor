#!/usr/bin/env bash
# Builds the coincurve wheel that requirements.lock downloads from the GitHub release deps/coincurve-2d11b11.
# coincurve 22.0.0, the first release with Python 3.14 wheels, is not on PyPI yet: this wheel comes from upstream
# commit 2d11b11, which supports Python 3.14. The build is reproducible: run it and compare the sha256 with the one
# pinned in requirements.lock. Needs docker. Usage: bash deploy/coincurve-wheel.sh [output folder, default ./dist]
set -euo pipefail
COMMIT=2d11b1160c75ae8fd94fe8fe3f226aec176bf9bf
IMAGE=quay.io/pypa/manylinux_2_28_x86_64@sha256:9555ba6c479fa69a2aa80ad1dc57154a60d562b19df159361d9379a02beb1e36
OUT=$(mkdir -p "${1:-dist}" && cd "${1:-dist}" && pwd)
docker run --rm -e COMMIT="$COMMIT" -e HOST_UID="$(id -u)" -v "$OUT:/out" "$IMAGE" bash -euo pipefail -c '
  git clone -q https://github.com/ofek/coincurve /src && cd /src && git checkout -q "$COMMIT"
  # Commit time as the timestamp of every file in the wheel, so two builds give the same bytes
  export SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) PYTHONHASHSEED=0
  # A development version (22.0.0.dev0): it cannot be mistaken for the official release, which comes after it, and
  # its file name has only letters, digits, dots and dashes, which a GitHub release keeps as they are (a "+" may not be)
  sed -i "s/^__version__ = \"\([^\"+]*\)\"/__version__ = \"\1.dev0\"/" src/coincurve/__init__.py
  grep "^__version__" src/coincurve/__init__.py
  /opt/python/cp314-cp314/bin/pip wheel -q --no-deps -w /tmp/raw .
  auditwheel repair -w /tmp/fixed /tmp/raw/*.whl > /dev/null
  /opt/python/cp314-cp314/bin/python -m venv /tmp/t && /tmp/t/bin/pip install -q /tmp/fixed/*.whl pytest
  cd /tmp && /tmp/t/bin/python -m pytest -q -p no:cacheprovider /src/tests
  cp /tmp/fixed/*.whl /out/ && chown "$HOST_UID" /out/*.whl'
cd "$OUT" && sha256sum coincurve-*.whl
