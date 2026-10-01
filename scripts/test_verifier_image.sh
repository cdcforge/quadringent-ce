#!/bin/sh
# Offline packaging gate, not a source-to-destination certification.
set -eu
image=${1:?Usage: sh scripts/test_verifier_image.sh IMAGE}

help=$(docker run --rm --env-file examples/local.env --network none --read-only "$image" --help)
for option in --run-id --snowflake-oidc-token-file --aws-default-credentials --proof-output --slo-policy --previous-alert-state; do
    printf '%s\n' "$help" | grep -q -- "$option"
done

refresh_help=$(docker run --rm --env-file examples/local.env --network none --read-only --entrypoint python "$image" /app/scripts/quadringent_observability_refresh.py --help)
for option in --policy --out --publish-confirm --snowflake-oidc-token-file --aws-default-credentials; do
    printf '%s\n' "$refresh_help" | grep -q -- "$option"
done

docker run --rm --env-file examples/local.env --network none --read-only --entrypoint python "$image" -c '
import os
import shutil
import io
import json
from pathlib import Path
import boto3
import snowflake.connector
import quadringent.verification_window
import quadringent.snowflake_autonomous
import quadringent.observability_snapshot
import quadringent_control_plane.projection
from quadringent.site_config import current
site = current()
assert os.getuid() == 10001, "verifier must run as UID 10001"
assert shutil.which("java") is None, "verifier must not include a JVM"
licenses = Path("/usr/share/quadringent")
assert "Apache License" in (licenses / "LICENSE").read_text()
notices = (licenses / "python-licenses/third-party-notices.txt").read_text()
assert "snowflake-connector-python" in notices and "Mozilla Public License" in notices
assert callable(quadringent.observability_snapshot.read_previous_observability)
class PreviousRunProof:
    def get_object(self, **kwargs):
        payload = json.dumps({"format_version": "as400-console-v1", "flux": {"id": site.stream_prefix + "/runs/test0001"}}).encode()
        return {"Body": io.BytesIO(payload), "ContentLength": len(payload), "ETag": "offline-version"}
assert quadringent.observability_snapshot.read_previous_observability(PreviousRunProof(), site=site) == (None, "offline-version")
try:
    quadringent.snowflake_autonomous.publish_autonomous_proof(
        None, "s3://other/key", b"{}", expected="s3://declared/key"
    )
except ValueError:
    pass
else:
    raise AssertionError("verifier must reject unconditional proof publication")
print("verifier-offline-runtime-ok")
'
