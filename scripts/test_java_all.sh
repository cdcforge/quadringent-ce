#!/usr/bin/env sh
# Un seul build puis tous les contrats Java hors connexion source.
set -eu
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"
if ! command -v mvn >/dev/null 2>&1; then
  image=$(awk '/^FROM maven:/ { print $2; exit }' docker/Dockerfile)
  exec docker run --rm -v "$repo_root:/workspace" -w /workspace "$image" \
    sh scripts/test_java_all.sh
fi
mvn -q -B -f java/pom.xml -DskipTests package dependency:copy-dependencies \
  -DincludeScope=runtime -DoutputDirectory=target/dependency
export FLEET_CATALOG_SOURCE="$repo_root/java/src/main/java/io/quadringent/as400/FleetCatalogProbe.java"
export DIAGNOSTIC_WORKER_SOURCE="$repo_root/java/src/main/java/io/quadringent/as400/DiagnosticWorker.java"
for file in java/src/test/java/io/quadringent/as400/*Test.java; do
  class=$(basename "$file" .java)
  java -cp 'java/target/classes:java/target/test-classes:java/target/dependency/*' \
    "io.quadringent.as400.$class"
done
