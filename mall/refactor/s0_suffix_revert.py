"""D4 (owner decision of 2026-10-13): the 45 position, size and spec tokens of the code suffix table that were relabelled as brand
TAGs on 2026-10-07 go back to the label they had before. Stage S0.1 of the inventory refactor.

    python -m mall.refactor.s0_suffix_revert --dry-run|--apply --user USERNAME

Each token takes back the 11 audited fields of its relabel's `before` (class, kind, attribution, company flag, confidence, status,
owner confirmation, scopes, notes, alias) and the confirmation stamp it had, with a set_variant CatalogCodeSuffixChange. Every save
bumps the table version and invalidates the suffix cache, so the OEM finder re-tiers only the SKUs whose chain reads one of them.
A token already back is skipped; one edited after its relabel (or without a complete audit row) is reported and left alone: relabel
it from Sufijos. Aliases follow their token, as an owner label does. An apply takes the suffix table lock (SUFFIX_LOCK) and writes
in one transaction; on the live database it refuses unless the catalog freeze is on, and it appends every changed row with its
before-values to /app/output/refactor/s0/suffix-revert/manifest.jsonl. Re-running changes nothing. To undo it, confirm the tokens as
TAG again from Sufijos (a one-click relabel restores the previous TAG label). Afterwards run python -m mall.link_oem_numbers --apply
and python -m mall.oem_finder --dry-run; --dry-run --finder previews the finder's tiers with D4 first, storing nothing.
"""
import argparse
import json
import os
import sys
from pathlib import Path

TOKENS = ('0.25', '0.50', 'A', 'ABANICO', 'ABS', 'ALZADO', 'AS', 'ASB', 'AZUL', 'B', 'BASE', 'BOMBA', 'C', 'CONO', 'CR', 'D', 'DIESEL',
          'FL', 'FR', 'GL', 'GRAPA', 'GUIA', 'H', 'L', 'LED', 'LH', 'M', 'O', 'PUNTA', 'R', 'RH', 'RL', 'RR', 'SELLO', 'SF', 'SOCKET',
          'ST', 'STD', 'SYN', 'TAPA', 'USA', 'X', 'XEN', 'XENON', 'XX')
RELABELLED_ON = ('2026-10-06', '2026-10-07')  # the night of Oct 6-7 (America/Panama); 02:50 UTC on Oct 7
NOTE = 'Revertido: palabra de posición o medida (decisión del dueño, 2026-10-13)'
# The catalog write freeze of S0.11 (mall.maintenance once it lands): any level (catalog or all) counts.
FREEZE_FILE = Path('/app/.maintenance/freeze')
LIVE_DATABASES = ('partsmall',)
OUTPUT = Path('/app/output')  # the main checkout's git-ignored output/
MANIFEST = Path('refactor/s0/suffix-revert/manifest.jsonl')
STATUS_LABELS = {'revert': 'por revertir', 'already': 'ya revertidos', 'changed': 'cambiados después del relabel',
                 'unaudited': 'sin la auditoría del relabel'}


def catalog_frozen():
    return FREEZE_FILE.exists()


def live_database():
    from django.db import connection
    return connection.vendor == 'postgresql' and connection.settings_dict['NAME'] in LIVE_DATABASES


def relabels(tokens=None):
    """{token: the confirm_tag row of Oct 6-7 that moved it from VARIANT to a brand TAG}, the last one per token."""
    from ..catalog_suffix_models import CatalogCodeSuffixChange
    rows = CatalogCodeSuffixChange.objects.filter(action='confirm_tag', created_at__date__in=RELABELLED_ON, before__cls='VARIANT',
                                                  after__cls='TAG', after__tag_kind='brand')
    if tokens is not None:
        rows = rows.filter(token__in=tokens)
    return {row.token: row for row in rows.order_by('created_at', 'id')}


def stamp(row, change, before):
    """(confirmed_by id, confirmed_at) the token had before its relabel: the last audited confirmation, or none."""
    if not before.get('owner_confirmed'):
        return None, None
    prior = [c for c in row.changes.filter(created_at__lte=change.created_at).exclude(pk=change.pk).order_by('-created_at', '-id')
             if (c.after or {}).get('owner_confirmed') is True]
    return (prior[0].actor_id, prior[0].created_at) if prior else (row.confirmed_by_id, row.confirmed_at)


def plan(lock=False):
    """One entry per token: its row, its relabel and what an apply does with it (revert, already, changed, unaudited)."""
    from ..catalog_suffix_models import CatalogCodeSuffix
    from ..catalog_suffix_views import SNAPSHOT, snapshot
    keys = (*SNAPSHOT, 'alias_of')
    rows = CatalogCodeSuffix.objects.select_related('alias_of')
    rows = {row.token: row for row in (rows.select_for_update(of=('self',)) if lock else rows).filter(token__in=TOKENS)}
    found = relabels(TOKENS)
    entries = []
    for token in TOKENS:
        row, change = rows.get(token), found.get(token)
        before = (change.before or {}) if change else {}
        entry = {'token': token, 'row': row, 'change': change, 'before': before, 'status': 'unaudited'}
        entries.append(entry)
        if row is None or change is None or set(keys) - set(before):
            continue
        current = snapshot(row)
        if all(current[k] == before[k] for k in keys):
            entry['status'] = 'already'
        elif all(current[k] == (change.after or {}).get(k) for k in keys) and not row.changes.filter(created_at__gte=change.created_at).exclude(pk=change.pk).exists():
            entry['status'] = 'revert'
        else:
            entry['status'] = 'changed'
    return entries


def reverted_rows(rows, entries=None):
    """The suffix table rows (catalog_suffixes FIELDS dicts) with D4 applied in memory: what a read-only analysis sees before the apply.
    Rows already back, or edited since, stay as they are. entries: a plan() already made."""
    pending = {e['token']: e['before'] for e in (plan() if entries is None else entries) if e['status'] == 'revert'}
    return [dict(r, **{k: pending[r['token']][k] for k in r if k in pending[r['token']]}) if r['token'] in pending else r for r in rows]


def finder_preview():
    """{tier: [SKUs now, SKUs once D4 applies]} from two read-only OEM finder passes (nothing stored): what the --dry-run of
    mall.oem_finder after the apply will show, AUTO_RENAME_BASE above all."""
    from .. import catalog_suffixes as cs
    from ..oem_finder import TIER_ORDER, execute
    rows = cs._db_rows() or cs.fixture_rows()
    now, after = (execute(store=False, stage='s0_suffix_revert', table=cs.SuffixTable(r, source='db'))['counts']['tiers']
                  for r in (rows, reverted_rows(rows)))
    return {tier: [now.get(tier, [0])[0], after.get(tier, [0])[0]] for tier in TIER_ORDER if now.get(tier) or after.get(tier)}


def restore(entry, user):
    """Put one locked row back to its relabel's before-values (aliases follow) and audit it; returns the manifest lines."""
    from django.utils import timezone
    from ..catalog_suffix_models import CatalogCodeSuffix, CatalogCodeSuffixChange
    from ..catalog_suffix_views import SNAPSHOT, alias_label, save, snapshot
    row, before, now = entry['row'], entry['before'], timezone.now()
    audited, old = snapshot(row), stamped(row)
    for key in SNAPSHOT:
        setattr(row, key, before[key])
    row.alias_of = CatalogCodeSuffix.objects.get(token=before['alias_of']) if before['alias_of'] else None
    row.confirmed_by_id, row.confirmed_at = stamp(row, entry['change'], before)
    row.save()  # takes the next table version and invalidates the suffix cache
    audit = CatalogCodeSuffixChange.objects.create(suffix=row, token=row.token, action='set_variant', actor=user, before=audited,
                                                   after=snapshot(row), note=NOTE, version=row.version)
    lines = [manifest_line(row, old, audit, now, user)]
    for alias in CatalogCodeSuffix.objects.select_for_update().filter(alias_of=row).order_by('token'):
        certain, old = alias.status != 'needs_owner_label', stamped(alias)
        if (certain or alias.cls != row.cls) and save(alias, alias_label(row, certain), 'set_variant', user, note=f'Alias de {row.token}'):
            lines.append(manifest_line(alias, old, alias.changes.order_by('-created_at', '-id').first(), now, user))
    return lines


def stamped(row):
    """A row as the manifest records it: the audited fields plus its confirmation stamp."""
    from ..catalog_suffix_views import snapshot
    return {**snapshot(row), 'confirmed_by_id': row.confirmed_by_id, 'confirmed_at': row.confirmed_at}


def manifest_line(row, old, audit, now, user):
    from django.db import connection
    return {'stage': 's0.1', 'database': connection.settings_dict['NAME'], 'model': 'mall.CatalogCodeSuffix', 'id': row.pk,
            'token': row.token, 'action': 'update', 'before': old, 'after': stamped(row), 'audit_id': audit.pk,
            'version': row.version, 'actor': user.username, 'at': now}


def write_manifest(lines):
    path = OUTPUT / MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=False, default=str) + '\n')
    return path


def describe(entry):
    row, before = entry['row'], entry['before']
    if row is None:
        return f"{entry['token']}: no está en la tabla"
    now = f'{row.cls} {row.tag_kind or row.variant_kind}'.strip()
    if entry['status'] in ('revert', 'already'):
        return f"{row.token}: {now} → {before['cls']} {before['variant_kind']} ({before['status']}, {before['confidence']})"
    return f'{row.token}: {now} · {STATUS_LABELS[entry["status"]]}'


def run(*, apply=False, username=None, finder=False, stdout=None):
    """Plan, and with apply restore the tokens (finder: preview the OEM finder tiers). None when the apply is refused."""
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from ..catalog_suffix_views import lock_table, table_version
    stdout = stdout or sys.stdout
    user = None
    if apply:
        if live_database() and not catalog_frozen():
            stdout.write(f'Activa la congelación del catálogo ({FREEZE_FILE}) antes de aplicar en la base en vivo.\n')
            return None
        if not OUTPUT.is_dir():
            stdout.write(f'Falta la carpeta {OUTPUT} para el manifiesto; ejecútalo dentro del contenedor de la app.\n')
            return None
        user = get_user_model().objects.filter(username=username or '').first()
        if user is None:
            stdout.write('Indica con --user el usuario que figura como autor del cambio.\n')
            return None
    version = table_version()
    lines = []
    with transaction.atomic():
        if apply:
            lock_table()
        entries = plan(lock=apply)
        if apply:
            for entry in entries:
                if entry['status'] == 'revert':
                    lines += restore(entry, user)
    others = sorted(set(relabels()) - set(TOKENS))
    counts = {status: sum(e['status'] == status for e in entries) for status in STATUS_LABELS}
    result = {'mode': 'apply' if apply else 'dry_run', 'expected': len(TOKENS), **counts, 'rows_written': len(lines),
              'version_before': version, 'version_after': table_version(), 'other_relabels': others,
              'tokens': {status: [e['token'] for e in entries if e['status'] == status] for status in STATUS_LABELS}}
    if lines:
        result['manifest'] = str(write_manifest(lines))
    stdout.write(f"{'Aplicado' if apply else 'Simulación'} · D4: {len(TOKENS)} sufijos esperados · "
                 + ' · '.join(f'{counts[s]} {label}' for s, label in STATUS_LABELS.items())
                 + f" · versión de la tabla {version} → {result['version_after']}\n")
    for entry in entries:
        if entry['status'] != 'already' or not apply:
            stdout.write(f'  {describe(entry)}\n')
    if others:
        stdout.write(f"Otros sufijos pasados de variante a marca esa noche (no se tocan): {', '.join(others)}\n")
    if finder and not apply:
        result['finder'] = finder_preview()
        stdout.write('Buscador OEM (solo lectura), SKU por nivel hoy → con D4:\n'
                     + ''.join(f'  {tier}: {a} → {b}\n' for tier, (a, b) in result['finder'].items() if a != b or tier == 'AUTO_RENAME_BASE'))
    if apply and lines:
        stdout.write(f"Manifiesto: {result['manifest']}\n"
                     'Siguiente: python -m mall.link_oem_numbers --apply y python -m mall.oem_finder --dry-run\n')
    return result


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Devuelve a variante los 45 sufijos de posición o medida que se marcaron como marca el 2026-10-07.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Muestra qué sufijos cambiarían sin escribir nada.')
    mode.add_argument('--apply', action='store_true', help='Restaura cada sufijo con su auditoría (requiere la congelación en vivo).')
    parser.add_argument('--user', help='Usuario que figura como autor de los cambios (obligatorio con --apply).')
    parser.add_argument('--finder', action='store_true', help='Con --dry-run: compara los niveles del buscador OEM hoy y con D4 (solo lectura).')
    parser.add_argument('--json', action='store_true', help='Imprime también el resultado en JSON.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    stdout = stdout or sys.stdout
    result = run(apply=options.apply, username=options.user, finder=options.finder, stdout=stdout)
    if result is not None and options.json:
        stdout.write(json.dumps(result, ensure_ascii=False, default=str) + '\n')
    return 0 if result is not None else 1


if __name__ == '__main__':
    sys.exit(main())
