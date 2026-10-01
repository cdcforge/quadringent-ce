#!/usr/bin/env bash
# Reprise complète après une session AWS renouvelée.
#
# Une seule commande humaine reste nécessaire : `aws sso login --profile $QUADRINGENT_AWS_PROFILE`.
# Ce script enchaîne ensuite ce qui était différé : il régénère les identifiants
# utilisables, publie les copies historiques terminées, vérifie leur chargement
# et rend compte, sans jamais republier un lot déjà présent.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${QUADRINGENT_AWS_ENV:-/tmp/aws_env.sh}"
HIST_DIR="${QUADRINGENT_HIST_DIR:-/tmp/hist}"
RUNNER="${QUADRINGENT_PYTHON:-python3}"
PROFILE="${QUADRINGENT_AWS_PROFILE:?QUADRINGENT_AWS_PROFILE requis (profil AWS du site)}"
EXPECTED_ACCOUNT="${QUADRINGENT_AWS_ACCOUNT_ID:?QUADRINGENT_AWS_ACCOUNT_ID requis (compte AWS du site)}"

say() { printf '  %s\n' "$*"; }
die() { printf 'ECHEC: %s\n' "$*" >&2; exit 1; }

printf '=== 1. identifiants AWS ===\n'
if ! "$RUNNER" - "$ENV_FILE" "$PROFILE" <<'PY'
import json, subprocess, sys
out = subprocess.run(
    ["aws", "configure", "export-credentials", "--profile", sys.argv[2], "--format", "process"],
    capture_output=True, text=True)
if out.returncode != 0:
    print("Les identifiants ne sont pas disponibles.")
    print(f"Lancez d'abord : aws sso login --profile {sys.argv[2]}")
    sys.exit(1)
d = json.loads(out.stdout)
with open(sys.argv[1], "w") as handle:
    handle.write(f"export AWS_ACCESS_KEY_ID={d['AccessKeyId']}\n")
    handle.write(f"export AWS_SECRET_ACCESS_KEY={d['SecretAccessKey']}\n")
    handle.write(f"export AWS_SESSION_TOKEN={d['SessionToken']}\n")
print("identifiants régénérés")
PY
then
  die "session AWS indisponible"
fi
chmod 600 "$ENV_FILE"
mkdir -p "$HIST_DIR"
cp "$ENV_FILE" "$HIST_DIR/aws_env.sh"
chmod 644 "$HIST_DIR/aws_env.sh"
say "copie visible par les publieurs : $HIST_DIR/aws_env.sh"

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
unset AWS_PROFILE AWS_DEFAULT_PROFILE 2>/dev/null || true

printf '\n=== 2. identité effective ===\n'
account="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"
[ "$account" = "$EXPECTED_ACCOUNT" ] || die "compte inattendu : ${account:-aucun}"
say "compte : $account"

printf '\n=== 3. publication des copies disponibles ===\n'
for DIR in "$HIST_DIR"/*/; do
  [ -d "$DIR" ] || continue
  TABLE="$(basename "$DIR" | tr 'a-z' 'A-Z')"
  if ! ls "$DIR"/*.jsonl >/dev/null 2>&1; then
    say "$TABLE : aucun lot local, ignoré"
    continue
  fi
  count="$(ls "$DIR"/*.jsonl 2>/dev/null | wc -l | tr -d ' ')"
  say "$TABLE : $count lots locaux, publication"
  out="$("$RUNNER" "$ROOT/scripts/quadringent_fleet_history.py" \
        --table "$TABLE" --publish-only "$DIR" --count-published 2>&1 | tail -1)"
  if printf '%s' "$out" | grep -q '"status": "PUBLISHED"'; then
    printf '%s' "$out" | "$RUNNER" -c "import json,sys;d=json.load(sys.stdin);print('    publiees :',d.get('rows_published'),'| objets :',d.get('objects_published'),'| reutilises :',d.get('objects_reused'))"
  else
    say "    $TABLE : publication non aboutie — $(printf '%s' "$out" | cut -c1-120)"
  fi
done

printf '\n=== 4. rappel des verifications restantes ===\n'
say "reconciliation de valeurs : scripts/quadringent_fleet_reconcile.py --table <T> --source-dir <dir>"
say "continuite d'un run       : scripts/quadringent_fleet_continuity.py --run-prefix <s3 uri>"
say "provisionnement d'une voie: scripts/quadringent_fleet_destination_setup.py --verify-only"
