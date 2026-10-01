#!/bin/sh
# Real packaged HTTP server, no network, credentials, host ports or source mount.
set -eu
image=${1:?Usage: sh scripts/test_control_plane_image.sh IMAGE}
container=$(docker run -d --rm --env-file examples/local.env --network none --read-only "$image" --source historical:dev-sale:file:///missing-proof.json --environment dev)
trap 'docker stop "$container" >/dev/null 2>&1 || true' EXIT HUP INT TERM
docker exec -i "$container" python - <<'PY'
import json
import os
import shutil
import time
from pathlib import Path
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

base = "http://127.0.0.1:8844"
for attempt in range(100):
    try:
        with urlopen(base + "/", timeout=1) as response:
            assert response.headers.get_content_type() == "text/html"
            html = response.read().decode()
        break
    except (OSError, URLError):
        time.sleep(0.1)
else:
    raise AssertionError("packaged cockpit must serve HTML at /")

class Assets(HTMLParser):
    paths = []
    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {"src", "href"} and value.startswith("/assets/"):
                self.paths.append(value)

assets = Assets()
assets.feed(html)
assert any(path.endswith(".js") for path in assets.paths), "missing JS entry"
assert any(path.endswith(".css") for path in assets.paths), "missing CSS entry"
for path in assets.paths:
    with urlopen(base + path, timeout=2) as response:
        assert response.read(), "empty packaged asset"
        assert response.headers.get_content_type() != "text/html", "asset fell back to HTML"
with urlopen(base + "/assets/third-party-notices.txt", timeout=2) as response:
    assert response.headers.get_content_type() == "text/plain"
    notices = response.read().decode()
    assert "Copyright (c) Meta Platforms" in notices
    assert "SIL OPEN FONT LICENSE Version 1.1" in notices
    assert "react@" in notices and "@fontsource-variable/ibm-plex-sans@" in notices
    assert Path('/usr/share/quadringent/LICENSE').read_text().strip() in notices
    assert Path('/usr/share/quadringent/NOTICE').read_text().strip() in notices
licenses = Path('/usr/share/quadringent')
assert (licenses / 'licenses/JTOpen-IPL-1.0.html').is_file()
assert len(list((licenses / 'sources').glob('*-sources.jar'))) == 5
python_notices = (licenses / 'python-licenses/third-party-notices.txt').read_text()
assert 'boto3' in python_notices and 'botocore' in python_notices
with urlopen(base + "/v1/pipelines", timeout=2) as response:
    payload = json.load(response)
assert payload["pipelines"] == [], "missing proof must not fabricate a pipeline"
with urlopen(base + "/v1/overview", timeout=2) as response:
    overview = json.load(response)
assert overview["sources"][0]["status"] == "unavailable", "missing proof must stay unavailable"
for path in ("/assets/../../etc/passwd", "/%2e%2e/etc/passwd", "/fixtures/proof.json"):
    try:
        urlopen(base + path, timeout=2)
    except HTTPError as error:
        assert error.code == 404
    else:
        raise AssertionError("private path exposed: " + path)
assert os.getuid() == 10001
assert shutil.which("java") is not None, "les sondes de catalogue exigent le JRE"
assert shutil.which("javac") is None, "le compilateur reste dans le build"
assert not os.path.exists("/app/certs/ibmi-ca.pem"), "aucun CA de site dans l image"
assert shutil.which("node") is None, "Node belongs only in the build stage"
print("control-plane-image: HTML, assets, dependency notices, API missing-proof truth, path guards, nonroot PASS")
PY
docker exec "$container" python -S /app/quadringent_healthcheck.py 8844
if docker exec "$container" python -S /app/quadringent_healthcheck.py 1; then
    echo "healthcheck accepted an unavailable endpoint" >&2
    exit 1
fi
