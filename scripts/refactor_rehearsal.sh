#!/usr/bin/env bash
# Rehearsal of one inventory-refactor stage (plan §1.3, D2) on a restored copy of the database, never on the live one.
#
#   scripts/refactor_rehearsal.sh --dump docker_backups/before-0071-s1.dump --worktree refactor-s1 --stage s1 \
#       [--expand 0071_line_lifecycle] [--tighten 0072_line_lifecycle_checks] [--cli mall.refactor.s1] [--cli-user miguel] \
#       [--allow PATRÓN ...] [--allow-applied PATRÓN ...] [--time 'python -m ...' ...] [--old-code /app] \
#       [--db partsmall_rehearsal] [--replace] [--keep] [--print]
#
#  1. Restores the dump into the copy (createdb + pg_restore --no-owner) and snapshots it with the old code (--old-code, the main
#     checkout by default): the baseline.
#  2. Migrates the copy from the worktree up to --expand (every migration when omitted).
#  3. Snapshots the new code on the copy before any backfill: no server error, and against the baseline only the --allow keys change.
#  4. Runs the stage CLI (--dry-run, then --apply --user), migrates to --tighten when given (it needs --expand), runs each --time
#     command (sh -c in the new code's folder: a 500-row Excel batch in S3 and S6), and snapshots again: only --allow and
#     --allow-applied keys change.
#  5. Rolls back (tighten reversed, CLI --rollback, migrations back to where the dump was) and snapshots with the old code: it must
#     equal the baseline.
#  6. Drops the copy (kept with --keep, and on a failure so it can be inspected).
# Every step is timed; the window estimate is migrate + apply (+ tighten). Snapshots and the log go to output/refactor/<stage>/
# rehearsal-<time>/ in the main checkout; REFACTOR_OUTPUT points the stage CLI there, so its manifest never mixes with the live one's.
#
# Safety: every app command is a one-off container (docker compose run --rm --no-deps) with POSTGRES_DB set to the copy, whose name
# must start with partsmall_rehearsal, and with the outside services blank (Spaces, Resend, Gemini, WhatsApp, web push, Redis). It never
# touches the freeze file, the live database or the running containers. --print shows the commands without running any.
set -euo pipefail

REPO=${REPO:-/Users/migueltang/OratekPartsMall}
DB_CONTAINER=${DB_CONTAINER:-partsmall-db-1}
PGUSER_NAME=${POSTGRES_USER:-partsmall}
LIVE_DB=partsmall
MIN_FREE_KB=$((3 * 1024 * 1024))  # 3 GB in the Docker VM (plan D0)

DUMP='' WORKTREE='' STAGE='' EXPAND='' TIGHTEN='' CLI='' CLI_USER=miguel OLD_CODE=/app DB=partsmall_rehearsal
REPLACE=0 KEEP=0 PRINT=0
ALLOW=() ALLOW_APPLIED=() TIMED=()

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
die() { echo "ERROR: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case $1 in
        --dump) DUMP=$2; shift 2 ;;
        --worktree) WORKTREE=$2; shift 2 ;;
        --stage) STAGE=$2; shift 2 ;;
        --expand) EXPAND=$2; shift 2 ;;
        --tighten) TIGHTEN=$2; shift 2 ;;
        --cli) CLI=$2; shift 2 ;;
        --cli-user) CLI_USER=$2; shift 2 ;;
        --allow) ALLOW+=("$2"); shift 2 ;;
        --allow-applied) ALLOW_APPLIED+=("$2"); shift 2 ;;
        --time) TIMED+=("$2"); shift 2 ;;
        --old-code) OLD_CODE=$2; shift 2 ;;
        --db) DB=$2; shift 2 ;;
        --replace) REPLACE=1; shift ;;
        --keep) KEEP=1; shift ;;
        --print) PRINT=1; shift ;;
        -h|--help) usage ;;
        *) echo "Opción desconocida: $1" >&2; usage 2 ;;
    esac
done

[ -n "$DUMP" ] && [ -n "$WORKTREE" ] && [ -n "$STAGE" ] || { echo 'Faltan --dump, --worktree o --stage.' >&2; usage 2; }
[[ $DB =~ ^partsmall_rehearsal[a-z0-9_]*$ ]] || die "La copia debe llamarse partsmall_rehearsal…, no '$DB'."
[ "$DB" != "$LIVE_DB" ] || die 'La copia no puede ser la base de datos en vivo.'
[[ $STAGE =~ ^[a-z0-9_]+$ ]] || die "Etapa no válida: '$STAGE' (letras minúsculas, dígitos y _)."
[ -f "$DUMP" ] || die "No existe el respaldo $DUMP."
[ -d "$REPO/.claude/worktrees/$WORKTREE" ] || die "No existe el worktree $REPO/.claude/worktrees/$WORKTREE."
[ -z "$TIGHTEN" ] || [ -n "$EXPAND" ] || die '--tighten necesita --expand: la reversión vuelve a esa migración.'
[[ $OLD_CODE == /app || $OLD_CODE == /app/* ]] || die "--old-code es una ruta del contenedor bajo /app, no '$OLD_CODE'."
for code in "$OLD_CODE" "/app/.claude/worktrees/$WORKTREE"; do
    [ -f "$REPO${code#/app}/mall/refactor/snapshot.py" ] || die "El código de $code no tiene mall/refactor/snapshot.py (etapa S0.6)."
done

NEW_CODE=/app/.claude/worktrees/$WORKTREE
STAMP=$(date +%Y%m%d-%H%M%S)
HOST_OUT=$REPO/output/refactor/$STAGE/rehearsal-$STAMP
OUT=/app/output/refactor/$STAGE/rehearsal-$STAMP
COMPOSE=(docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" -p partsmall)
BLANK=(-e DO_SPACES_KEY= -e DO_SPACES_SECRET= -e DO_SPACES_BUCKET= -e RESEND_API_KEY= -e WHATSAPP_ACCESS_TOKEN= -e GEMINI_API_KEY=
       -e VAPID_PRIVATE_KEY= -e REDIS_URL=)
TIMINGS=()
FAILED_AT=''

run() {  # run <command...>: or only print it with --print
    if [ "$PRINT" = 1 ]; then printf '  $'; printf ' %q' "$@"; printf '\n'; return 0; fi
    "$@"
}

app() {  # app <code dir in the container> <command...>: a one-off app container on the copy, never the live database
    local code=$1; shift
    run "${COMPOSE[@]}" run --rm --no-deps -T -e POSTGRES_DB="$DB" -e REFACTOR_OUTPUT="$OUT" "${BLANK[@]}" -w "$code" app "$@"
}

psql_db() { run docker exec "$DB_CONTAINER" psql -U "$PGUSER_NAME" -d postgres -Atc "$1"; }

step() {  # step <label> <command...>: timed; a failure keeps the copy for inspection
    local label=$1 started status; shift
    echo "== $label"
    started=$(date +%s)
    set +e
    "$@"
    status=$?
    set -e
    TIMINGS+=("$(printf '%-60s %5s s' "$label" $(( $(date +%s) - started )))")
    if [ $status -ne 0 ]; then
        FAILED_AT=$label
        echo "FALLÓ: $label (salida $status)." >&2
        exit $status
    fi
}

snapshot() {  # snapshot <code dir> <name> [snapshot args...]
    local code=$1 name=$2; shift 2
    app "$code" python -m mall.refactor.snapshot --out "$OUT" --name "$name" "$@"
}

finish() {
    local status=$?
    echo
    echo 'Tiempos:'
    [ ${#TIMINGS[@]} -eq 0 ] || printf '  %s\n' "${TIMINGS[@]}"
    if [ "$PRINT" = 1 ]; then return; fi
    if [ $status -ne 0 ] || [ "$KEEP" = 1 ]; then
        echo "La copia $DB queda para revisarla. Bórrala con: docker exec $DB_CONTAINER dropdb -U $PGUSER_NAME --force $DB"
    fi
    [ $status -eq 0 ] || echo "El ensayo falló en: ${FAILED_AT:-preparación}. Instantáneas y registro: $HOST_OUT" >&2
}
trap finish EXIT

if [ "$PRINT" = 0 ]; then
    mkdir -p "$HOST_OUT"
    exec > >(tee -a "$HOST_OUT/rehearsal.log") 2>&1
fi
echo "Ensayo de la etapa $STAGE: respaldo $DUMP → copia $DB; código nuevo $NEW_CODE; código anterior $OLD_CODE"

# Preparation: room for the copy, and no copy left from another rehearsal unless --replace.
if [ "$PRINT" = 0 ]; then
    free_kb=$(docker exec "$DB_CONTAINER" df -Pk /var/lib/postgresql/data | awk 'NR == 2 {print $4}')
    [ "$free_kb" -ge "$MIN_FREE_KB" ] || die "Solo quedan $((free_kb / 1024)) MB en el volumen de PostgreSQL; se necesitan 3 GB."
    if [ "$(psql_db "SELECT 1 FROM pg_database WHERE datname = '$DB'")" = 1 ]; then
        [ "$REPLACE" = 1 ] || die "La copia $DB ya existe: usa --replace para borrarla y empezar de nuevo."
        run docker exec "$DB_CONTAINER" dropdb -U "$PGUSER_NAME" --force "$DB"
    fi
fi

restore() {
    run docker exec "$DB_CONTAINER" createdb -U "$PGUSER_NAME" "$DB"
    if [ "$PRINT" = 1 ]; then
        printf '  $ docker exec -i %q pg_restore -U %q -d %q --no-owner --no-privileges < %q\n' "$DB_CONTAINER" "$PGUSER_NAME" "$DB" "$DUMP"
    else
        docker exec -i "$DB_CONTAINER" pg_restore -U "$PGUSER_NAME" -d "$DB" --no-owner --no-privileges < "$DUMP"
    fi
}
step 'Restaurar el respaldo' restore
step 'Instantánea base (código anterior)' snapshot "$OLD_CODE" 01-base
BASE=$OUT/01-base.json.gz
if [ "$PRINT" = 1 ]; then
    START='<migración del respaldo>'
else
    START=$(docker exec "$DB_CONTAINER" psql -U "$PGUSER_NAME" -d "$DB" -Atc "SELECT max(name) FROM django_migrations WHERE app = 'mall'")
fi
echo "La copia está en la migración $START."

EXPAND_DIFF=(--diff "$BASE")
for pattern in ${ALLOW[@]+"${ALLOW[@]}"}; do EXPAND_DIFF+=(--allow "$pattern"); done
APPLIED_DIFF=("${EXPAND_DIFF[@]}")
for pattern in ${ALLOW_APPLIED[@]+"${ALLOW_APPLIED[@]}"}; do APPLIED_DIFF+=(--allow "$pattern"); done
TARGET=()
[ -z "$EXPAND" ] || TARGET=(mall "$EXPAND")

step "Migrar (expand${EXPAND:+ $EXPAND})" app "$NEW_CODE" python manage.py migrate --noinput ${TARGET[@]+"${TARGET[@]}"}
step 'Instantánea del código nuevo sin rellenar' snapshot "$NEW_CODE" 02-expand "${EXPAND_DIFF[@]}"

if [ -n "$CLI" ]; then
    step "CLI $CLI --dry-run" app "$NEW_CODE" python -m "$CLI" --dry-run
    step "CLI $CLI --apply" app "$NEW_CODE" python -m "$CLI" --apply --user "$CLI_USER"
fi
if [ -n "$TIGHTEN" ]; then
    step "Migrar (tighten $TIGHTEN)" app "$NEW_CODE" python manage.py migrate --noinput mall "$TIGHTEN"
fi
for command in ${TIMED[@]+"${TIMED[@]}"}; do
    step "Medir: $command" app "$NEW_CODE" sh -c "$command"
done
step 'Instantánea con los datos rellenados' snapshot "$NEW_CODE" 03-applied "${APPLIED_DIFF[@]}"

if [ -n "$TIGHTEN" ]; then
    step "Revertir el tighten (a $EXPAND)" app "$NEW_CODE" python manage.py migrate --noinput mall "$EXPAND"
fi
if [ -n "$CLI" ]; then
    step "CLI $CLI --rollback" app "$NEW_CODE" python -m "$CLI" --rollback
fi
step "Revertir las migraciones hasta $START" app "$NEW_CODE" python manage.py migrate --noinput mall "$START"
step 'Instantánea tras revertir (código anterior)' snapshot "$OLD_CODE" 04-rolled-back --diff "$BASE"

if [ "$KEEP" = 0 ]; then
    step 'Borrar la copia' run docker exec "$DB_CONTAINER" dropdb -U "$PGUSER_NAME" --force "$DB"
fi
echo
echo "Ensayo completo. Ventana estimada: migrar + aplicar${TIGHTEN:+ + tighten} (ver Tiempos). Instantáneas y registro: $HOST_OUT"
