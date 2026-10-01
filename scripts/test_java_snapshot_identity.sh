#!/usr/bin/env sh
set -eu
mkdir -p java/target/snapshot-tests
javac --release 21 -d java/target/snapshot-tests \
  java/src/main/java/io/quadringent/as400/SnapshotIdentity.java \
  java/src/test/java/io/quadringent/as400/SnapshotIdentityTest.java
java -cp java/target/snapshot-tests io.quadringent.as400.SnapshotIdentityTest
