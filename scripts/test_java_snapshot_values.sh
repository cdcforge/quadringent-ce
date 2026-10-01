#!/usr/bin/env sh
set -eu
# Maven compiles the real adapter and resolves its pinned JDBC dependencies.
mvn -q -B -f java/pom.xml -DskipTests package dependency:copy-dependencies \
  -DincludeScope=runtime -DoutputDirectory=target/dependency
for snapshot_test_zone in UTC Europe/Zurich America/Los_Angeles; do
  java -Duser.timezone="$snapshot_test_zone" \
    -cp 'java/target/classes:java/target/test-classes:java/target/dependency/*' \
    io.quadringent.as400.SnapshotValuesTest
done
