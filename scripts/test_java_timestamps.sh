#!/usr/bin/env sh
set -eu
# Run from the repository root, locally or in the Java 21 CI image.
mkdir -p java/target/timestamp-tests
javac --release 21 -d java/target/timestamp-tests \
  java/src/main/java/io/quadringent/as400/SourceClockMismatchException.java \
  java/src/main/java/io/quadringent/as400/JournalTimestamps.java \
  java/src/test/java/io/quadringent/as400/JournalTimestampsTest.java
for timestamp_jvm_zone in UTC America/Los_Angeles Asia/Tokyo; do
  java -Duser.timezone="$timestamp_jvm_zone" -cp java/target/timestamp-tests \
    io.quadringent.as400.JournalTimestampsTest
done
