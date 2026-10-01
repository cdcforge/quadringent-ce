#!/usr/bin/env sh
set -eu
mkdir -p java/target/oracle-tests
javac --release 21 -d java/target/oracle-tests \
  java/src/test/java/io/quadringent/as400/JournalWindowOracleTest.java
if test -f java/src/main/java/io/quadringent/as400/JournalWindowOracle.java; then
  javac --release 21 -d java/target/oracle-tests \
    java/src/main/java/io/quadringent/as400/TlsTrust.java \
    java/src/main/java/io/quadringent/as400/JournalWindowOracle.java
fi
java -cp java/target/oracle-tests io.quadringent.as400.JournalWindowOracleTest
