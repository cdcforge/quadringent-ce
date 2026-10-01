#!/usr/bin/env sh
set -eu
mvn -q -B -f java/pom.xml -DskipTests package dependency:copy-dependencies \
  -DincludeScope=runtime -DoutputDirectory=target/dependency
java -cp 'java/target/classes:java/target/test-classes:java/target/dependency/*' \
  io.quadringent.as400.JournalAfterImageTest
