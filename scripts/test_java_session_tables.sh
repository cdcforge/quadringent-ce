#!/usr/bin/env sh
set -eu
# Run from the repository root. Compiles the session against already-fetched
# Debezium/JTOpen jars when Maven is unavailable.
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEBEZIUM="$HOME/.m2/repository/io/debezium/ibmi-journal-parsing/3.6.1.Final/ibmi-journal-parsing-3.6.1.Final.jar"
JT400="$HOME/.m2/repository/net/sf/jt400/jt400/21.0.7/jt400-21.0.7.jar"
SLF4J="$HOME/.m2/repository/org/slf4j/slf4j-api/2.0.12/slf4j-api-2.0.12.jar"
OUT="$ROOT/java/target/session-tables-tests"
mkdir -p "$OUT"
CP="$DEBEZIUM:$JT400:$SLF4J"
javac --release 21 -cp "$CP" -d "$OUT" \
  "$ROOT/java/src/main/java/io/quadringent/as400/TlsTrust.java" \
  "$ROOT/java/src/main/java/io/quadringent/as400/JournalTimestamps.java" \
  "$ROOT/java/src/main/java/io/quadringent/as400/MultiFormatJournalDate.java" \
  "$ROOT/java/src/main/java/io/quadringent/as400/MultiFormatJournalTime.java" \
  "$ROOT/java/src/main/java/io/quadringent/as400/RawCaptureWriter.java" \
  "$ROOT/java/src/main/java/io/quadringent/as400/ReadOnlyReceiverCatalog.java" \
  "$ROOT/java/src/main/java/io/quadringent/as400/JournalSession.java" \
  "$ROOT/java/src/test/java/io/quadringent/as400/JournalSessionTablesTest.java"
java -cp "$OUT:$CP" io.quadringent.as400.JournalSessionTablesTest
