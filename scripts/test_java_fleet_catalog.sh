#!/usr/bin/env bash
# Compile only FleetCatalogProbe and its offline test with Java 21, then run it.
# Does not connect to IBM i, does not package the rest of the Java tree.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/java/src/main/java/io/quadringent/as400/FleetCatalogProbe.java"
TRUST="$ROOT/java/src/main/java/io/quadringent/as400/TlsTrust.java"
TEST="$ROOT/java/src/test/java/io/quadringent/as400/FleetCatalogProbeTest.java"
JT400="${HOME}/.m2/repository/net/sf/jt400/jt400/21.0.7/jt400-21.0.7.jar"
IMAGE="eclipse-temurin:21-jdk"

if [[ ! -f "$SRC" || ! -f "$TEST" || ! -f "$TRUST" ]]; then
  echo "missing FleetCatalogProbe or TlsTrust sources" >&2
  exit 1
fi
if [[ ! -f "$JT400" ]]; then
  echo "missing local jt400 21.0.7 jar" >&2
  exit 1
fi

OUT="$(mktemp -d "${TMPDIR:-/tmp}/fleet-catalog-test.XXXXXX")"
cleanup() {
  rm -rf "$OUT"
}
trap cleanup EXIT

export FLEET_CATALOG_SOURCE="$SRC"

compile_and_run() {
  local javac_bin="$1"
  local java_bin="$2"
  "$javac_bin" --release 21 -encoding UTF-8 -cp "$JT400" -d "$OUT" "$TRUST" "$SRC" "$TEST"
  FLEET_CATALOG_SOURCE="$SRC" "$java_bin" -cp "$OUT:$JT400" io.quadringent.as400.FleetCatalogProbeTest
}

if command -v javac >/dev/null 2>&1 && command -v java >/dev/null 2>&1; then
  version="$(javac -version 2>&1 || true)"
  if [[ "$version" == *21* ]]; then
    compile_and_run javac java
    exit 0
  fi
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Java 21 javac not found" >&2
  exit 1
fi

docker run --rm \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e FLEET_CATALOG_SOURCE="$SRC" \
  -v "$ROOT":"$ROOT":ro \
  -v "$JT400":"$JT400":ro \
  -v "$OUT":"$OUT" \
  -w "$OUT" \
  "$IMAGE" \
  bash -c "set -euo pipefail
    javac --release 21 -encoding UTF-8 -cp '$JT400' -d '$OUT' '$TRUST' '$SRC' '$TEST'
    FLEET_CATALOG_SOURCE='$SRC' java -cp '$OUT:$JT400' io.quadringent.as400.FleetCatalogProbeTest"
