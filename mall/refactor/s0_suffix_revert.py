"""D4 (owner decision of 2026-10-13): the 45 position, size and spec tokens of the code suffix table that were relabelled as brand
TAGs on 2026-10-07 go back to the label they had before. Stage S0.1 of the inventory refactor.

    python -m mall.refactor.s0_suffix_revert --dry-run|--apply|--rollback --user USERNAME

Each token takes back the 11 audited fields of its relabel's `before` (class, kind, attribution, company flag, confidence, status,
owner confirmation, scopes, notes, alias) and the confirmation stamp it had, with a set_variant CatalogCodeSuffixChange. Every save
bumps the table version and invalidates the suffix cache, so the OEM finder re-tiers only the SKUs whose chain reads one of them.
A token already back is skipped; one edited after its relabel, or without a complete audit row (its `before`, and the confirmation it
had when it was owner-confirmed), is reported and left alone: relabel it from Sufijos. Aliases follow their token, as an owner label
does. --apply and --rollback take the suffix table lock (SUFFIX_LOCK) and write in one transaction; on the live database they refuse
unless the S0.11 guard passes (the catalog freeze is on and no writer is still busy). Every changed row goes with its before-values to
the manifest, the main checkout's git-ignored output/refactor/s0/suffix-revert/manifest.jsonl (/app/output/… in the containers),
written and flushed before the transaction commits, so a failed write changes nothing; a manifest holding another database's lines
(a test run's) is refused, never appended to. Re-running changes nothing. --rollback puts back the rows the last apply on this
database changed, stamps included, unless one was edited since. After either, run python -m mall.link_oem_numbers --apply and
python -m mall.oem_finder --dry-run; --dry-run --finder previews the finder's tiers with D4 first, storing nothing.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

TOKENS = ('0.25', '0.50', 'A', 'ABANICO', 'ABS', 'ALZADO', 'AS', 'ASB', 'AZUL', 'B', 'BASE', 'BOMBA', 'C', 'CONO', 'CR', 'D', 'DIESEL',
          'FL', 'FR', 'GL', 'GRAPA', 'GUIA', 'H', 'L', 'LED', 'LH', 'M', 'O', 'PUNTA', 'R', 'RH', 'RL', 'RR', 'SELLO', 'SF', 'SOCKET',
          'ST', 'STD', 'SYN', 'TAPA', 'USA', 'X', 'XEN', 'XENON', 'XX')
RELABELLED_ON = ('2026-10-06', '2026-10-07')  # the night of Oct 6-7 (America/Panama); 02:50 UTC on Oct 7
NOTE = 'Revertido: palabra de posición o medida (decisión del dueño, 2026-10-13)'
ROLLBACK_NOTE = 'Deshecho: vuelve la etiqueta que tenía antes de la reversión D4 (rollback de S0.1)'
MANIFEST = Path('refactor/s0/suffix-revert/manifest.jsonl')
STATUS_LABELS = {'revert': 'por revertir', 'already': 'ya revertidos', 'changed': 'cambiados después del relabel',
                 'unaudited': 'sin la auditoría del relabel'}
ROLLBACK_LABELS = {'rollback': 'por deshacer', 'already': 'ya deshechos', 'changed': 'cambiados después de aplicar'}
CLASS_ACTIONS = {'TAG': 'confirm_tag', 'VARIANT': 'set_variant', 'UNKNOWN': 'set_unknown'}  # the audit action of a restored label


def main_checkout():
    """The main checkout (settings.MAIN_CHECKOUT once S0.11 lands, else BASE_DIR): the repository a .claude/worktrees/<name>
    checkout belongs to, so a run from the worktree writes where a run from the main checkout does. A test copy is its own."""
    from django.conf import settings
    base = Path(getattr(settings, 'MAIN_CHECKOUT', None) or settings.BASE_DIR)
    return base.parents[2] if (base.parent.name, base.parent.parent.name) == ('worktrees', '.claude') else base


def output_dir():
    return main_checkout() / 'output'  # git-ignored; /app/output in the containers


def manifest_path():
    return output_dir() / MANIFEST


def current_database():
    from django.db import connection
    return str(connection.settings_dict['NAME'])


def refusal():
    """Why --apply or --rollback may not write now, or None: the S0.11 guard (mall.refactor.guard: on the live database, the catalog
    freeze and no writer still busy). Until S0.11 lands the live database is refused outright, since a freeze file would hold back
    neither the screens nor the workers; a test or rehearsal database needs nothing."""
    try:
        from .guard import refusal as shared
    except ImportError:  # S0.11 has not landed yet
        from django.conf import settings
        from django.db import connection
        live = getattr(settings, 'LIVE_DATABASE_NAME', 'partsmall')
        if connection.vendor == 'postgresql' and connection.settings_dict['NAME'] == live:
            return (f'Base de datos en vivo ({live}) sin la seguridad de operaciones de S0.11 (mall.refactor.guard): sin ella el '
                    'congelamiento del catálogo no detiene las pantallas ni los trabajadores. No se escribe nada; integra S0.11 primero.')
        return None
    return shared(True)


def manifest_lines():
    """The manifest's lines, oldest first ([] while there is none)."""
    path = manifest_path()
    if not path.exists():
        return []
    with open(path, encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def foreign_lines():
    """The message for a manifest that holds another database's lines (a test run's), which a rollback here must never read; or None."""
    others = sorted({str(line.get('database')) for line in manifest_lines()} - {current_database()})
    if others:
        return (f"El manifiesto {manifest_path()} tiene líneas de otra base ({', '.join(others)}): muévelo o bórralo antes de aplicar "
                'o deshacer en esta.')
    return None


def manifest_problem():
    """Why the manifest cannot take this run's lines, or None: its folder is missing, or it holds another database's lines."""
    if not output_dir().is_dir():
        return f'Falta la carpeta {output_dir()} para el manifiesto; ejecútalo dentro del contenedor de la app.'
    return foreign_lines()


def relabels(tokens=None):
    """{token: the confirm_tag row of Oct 6-7 that moved it from VARIANT to a brand TAG}, the last one per token."""
    from ..catalog_suffix_models import CatalogCodeSuffixChange
    rows = CatalogCodeSuffixChange.objects.filter(action='confirm_tag', created_at__date__in=RELABELLED_ON, before__cls='VARIANT',
                                                  after__cls='TAG', after__tag_kind='brand')
    if tokens is not None:
        rows = rows.filter(token__in=tokens)
    return {row.token: row for row in rows.order_by('created_at', 'id')}


def stamp(row, change, before):
    """(confirmed_by id, confirmed_at) the token had before its relabel: none when it was not owner-confirmed, else its last audited
    confirmation. None when that confirmation has no audit row: the relabel's own stamp is all the row has, so it is left alone."""
    if not before.get('owner_confirmed'):
        return None, None
    prior = [c for c in row.changes.filter(created_at__lte=change.created_at).exclude(pk=change.pk).order_by('-created_at', '-id')
             if (c.after or {}).get('owner_confirmed') is True]
    return (prior[0].actor_id, prior[0].created_at) if prior else None


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
        # This tool's own audit rows (an apply undone by --rollback) are not edits: the token can be reverted again.
        later = [c for c in row.changes.filter(created_at__gte=change.created_at).exclude(pk=change.pk) if c.note not in (NOTE, ROLLBACK_NOTE)]
        if all(current[k] == before[k] for k in keys):
            entry['status'] = 'already'
        elif all(current[k] == (change.after or {}).get(k) for k in keys) and not later:
            entry['stamp'] = stamp(row, change, before)
            entry['status'] = 'revert' if entry['stamp'] is not None else 'unaudited'
        else:
            entry['status'] = 'changed'
    return entries


def reverted_rows(rows, entries=None):
    """The suffix table rows (catalog_suffixes FIELDS dicts, alias_of as a token) with D4 applied in memory, aliases following their
    token as restore() writes them: what a read-only analysis sees before the apply. Rows already back, or edited since, stay as they
    are. entries: a plan() already made."""
    from ..catalog_suffix_views import alias_label
    pending = {e['token']: e['before'] for e in (plan() if entries is None else entries) if e['status'] == 'revert'}
    result = []
    for r in rows:
        if r['token'] in pending:
            r = dict(r, **{k: v for k, v in pending[r['token']].items() if k in r})
        elif r.get('alias_of') in pending:
            root, certain = SimpleNamespace(**pending[r['alias_of']]), r['status'] != 'needs_owner_label'
            if certain or r['cls'] != root.cls:
                r = dict(r, **alias_label(root, certain))
        result.append(r)
    return result


def finder_preview():
    """{tier: [SKUs now, SKUs once D4 applies]} from two read-only OEM finder passes over one catalog read (nothing stored): what the
    --dry-run of mall.oem_finder after the apply will show, AUTO_RENAME_BASE above all."""
    from .. import catalog_suffixes as cs
    from ..oem_finder import TIER_ORDER, execute, load_snapshot
    rows, catalog = cs._db_rows() or cs.fixture_rows(), load_snapshot()
    now, after = (execute(store=False, stage='s0_suffix_revert', snapshot=catalog, table=cs.SuffixTable(r, source='db'))['counts']['tiers']
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
    row.confirmed_by_id, row.confirmed_at = entry['stamp']
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


def recorded(values):
    """A stamped row as the manifest compares it: JSON values, the confirmation time as one ISO text."""
    from django.utils.dateparse import parse_datetime
    data = json.loads(json.dumps(values, default=str))
    return {**data, 'confirmed_at': parse_datetime(data['confirmed_at']).isoformat() if data.get('confirmed_at') else None}


def manifest_line(row, old, audit, now, user, action='update'):
    return {'stage': 's0.1', 'database': current_database(), 'model': 'mall.CatalogCodeSuffix', 'id': row.pk, 'token': row.token,
            'action': action, 'before': old, 'after': stamped(row), 'audit_id': audit.pk, 'version': row.version,
            'actor': user.username, 'at': now}


def write_manifest(lines):
    """Append the lines and flush them to disk. Called inside the transaction: a failed write rolls the rows back."""
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=False, default=str) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    return path


def applied_lines():
    """{row id: the line of the last apply on this database that changed it}, in the order the apply wrote them."""
    lines, database = {}, current_database()
    for line in manifest_lines():
        if (line.get('stage'), line.get('database'), line.get('action')) == ('s0.1', database, 'update'):
            lines[line['id']] = line
    return lines


def rollback_plan(lock=False):
    """One entry per row the last apply changed: rollback (as the apply left it), already (back at its before-values) or changed
    (edited since, or gone: left alone)."""
    from ..catalog_suffix_models import CatalogCodeSuffix
    lines = applied_lines()
    rows = CatalogCodeSuffix.objects.select_related('alias_of').filter(pk__in=list(lines)).order_by('pk')
    rows = {row.pk: row for row in (rows.select_for_update(of=('self',)) if lock else rows)}
    entries = []
    for pk, line in lines.items():
        row = rows.get(pk)
        current = recorded(stamped(row)) if row else None
        status = 'rollback' if current == recorded(line['after']) else 'already' if current == recorded(line['before']) else 'changed'
        entries.append({'token': line['token'], 'row': row, 'line': line, 'status': status})
    return entries


def roll_back(entry, user):
    """Put one locked row back to the before-values its apply recorded, stamp included, and audit it; returns its manifest line."""
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime
    from ..catalog_suffix_models import CatalogCodeSuffix, CatalogCodeSuffixChange
    from ..catalog_suffix_views import SNAPSHOT, snapshot
    row, before, now = entry['row'], entry['line']['before'], timezone.now()
    audited, old = snapshot(row), stamped(row)
    for key in SNAPSHOT:
        setattr(row, key, before[key])
    row.alias_of = CatalogCodeSuffix.objects.get(token=before['alias_of']) if before['alias_of'] else None
    row.confirmed_by_id = before['confirmed_by_id']
    row.confirmed_at = parse_datetime(before['confirmed_at']) if before['confirmed_at'] else None
    row.save()
    audit = CatalogCodeSuffixChange.objects.create(suffix=row, token=row.token, action=CLASS_ACTIONS[row.cls], actor=user, before=audited,
                                                   after=snapshot(row), note=ROLLBACK_NOTE, version=row.version)
    return manifest_line(row, old, audit, now, user, action='rollback')


def describe(entry):
    row, before = entry['row'], entry['before']
    if row is None:
        return f"{entry['token']}: no está en la tabla"
    now = f'{row.cls} {row.tag_kind or row.variant_kind}'.strip()
    if entry['status'] in ('revert', 'already'):
        return f"{row.token}: {now} → {before['cls']} {before['variant_kind']} ({before['status']}, {before['confidence']})"
    return f'{row.token}: {now} · {STATUS_LABELS[entry["status"]]}'


def undo(user, stdout):
    """--rollback: the rows of the last apply on this database back to their before-values, in one transaction."""
    from django.db import transaction
    from ..catalog_suffix_views import lock_table, table_version
    version, lines = table_version(), []
    with transaction.atomic():
        lock_table()
        entries = rollback_plan(lock=True)
        for entry in entries:
            if entry['status'] == 'rollback':
                lines.append(roll_back(entry, user))
        path = write_manifest(lines) if lines else None  # before the commit
    counts = {status: sum(e['status'] == status for e in entries) for status in ROLLBACK_LABELS}
    result = {'mode': 'rollback', 'rows': len(entries), **counts, 'rows_written': len(lines), 'version_before': version,
              'version_after': table_version(),
              'tokens': {status: [e['token'] for e in entries if e['status'] == status] for status in ROLLBACK_LABELS}}
    stdout.write(f'Rollback · S0.1: {len(entries)} filas aplicadas en esta base · '
                 + ' · '.join(f'{counts[s]} {label}' for s, label in ROLLBACK_LABELS.items())
                 + f" · versión de la tabla {version} → {result['version_after']}\n")
    if not entries:
        stdout.write('El manifiesto no tiene ninguna aplicación en esta base: no hay nada que deshacer.\n')
    for entry in entries:
        if entry['status'] == 'changed':
            stdout.write(f"  {entry['token']}: {ROLLBACK_LABELS['changed']}, se deja como está\n")
    if path:
        result['manifest'] = str(path)
        stdout.write(f'Manifiesto: {path}\n'
                     'Siguiente: python -m mall.link_oem_numbers --apply y python -m mall.oem_finder --dry-run\n')
    return result


def run(*, apply=False, rollback=False, username=None, finder=False, stdout=None):
    """Plan, and with apply restore the tokens, or with rollback undo the last apply (finder: preview the OEM finder tiers).
    None when a write is refused."""
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from ..catalog_suffix_views import lock_table, table_version
    stdout = stdout or sys.stdout
    user = None
    if apply or rollback:
        reason = refusal() or manifest_problem()
        if reason:
            stdout.write(reason + '\n')
            return None
        user = get_user_model().objects.filter(username=username or '').first()
        if user is None:
            stdout.write('Indica con --user el usuario que figura como autor del cambio.\n')
            return None
    if rollback:
        return undo(user, stdout)
    version, lines, path = table_version(), [], None
    with transaction.atomic():
        if apply:
            lock_table()
        entries = plan(lock=apply)
        if apply:
            for entry in entries:
                if entry['status'] == 'revert':
                    lines += restore(entry, user)
            if lines:
                path = write_manifest(lines)  # before the commit: a failed write rolls every row back
    others = sorted(set(relabels()) - set(TOKENS))
    counts = {status: sum(e['status'] == status for e in entries) for status in STATUS_LABELS}
    result = {'mode': 'apply' if apply else 'dry_run', 'expected': len(TOKENS), **counts, 'rows_written': len(lines),
              'version_before': version, 'version_after': table_version(), 'other_relabels': others,
              'tokens': {status: [e['token'] for e in entries if e['status'] == status] for status in STATUS_LABELS}}
    if path:
        result['manifest'] = str(path)
    stdout.write(f"{'Aplicado' if apply else 'Simulación'} · D4: {len(TOKENS)} sufijos esperados · "
                 + ' · '.join(f'{counts[s]} {label}' for s, label in STATUS_LABELS.items())
                 + f" · versión de la tabla {version} → {result['version_after']}\n")
    for entry in entries:
        if entry['status'] != 'already' or not apply:
            stdout.write(f'  {describe(entry)}\n')
    if others:
        stdout.write(f"Otros sufijos pasados de variante a marca esa noche (no se tocan): {', '.join(others)}\n")
    warning = not apply and output_dir().is_dir() and foreign_lines()  # an apply would refuse it
    if warning:
        stdout.write(f'Aviso: {warning}\n')
    if finder and not apply:
        result['finder'] = finder_preview()
        stdout.write('Buscador OEM (solo lectura), SKU por nivel hoy → con D4:\n'
                     + ''.join(f'  {tier}: {a} → {b}\n' for tier, (a, b) in result['finder'].items() if a != b or tier == 'AUTO_RENAME_BASE'))
    if path:
        stdout.write(f'Manifiesto: {path}\n'
                     'Siguiente: python -m mall.link_oem_numbers --apply y python -m mall.oem_finder --dry-run\n')
    return result


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Devuelve a variante los 45 sufijos de posición o medida que se marcaron como marca el 2026-10-07.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Muestra qué sufijos cambiarían sin escribir nada.')
    mode.add_argument('--apply', action='store_true', help='Restaura cada sufijo con su auditoría (requiere la congelación en vivo).')
    mode.add_argument('--rollback', action='store_true', help='Deshace la última aplicación con el manifiesto (requiere la congelación en vivo).')
    parser.add_argument('--user', help='Usuario que figura como autor de los cambios (obligatorio con --apply y --rollback).')
    parser.add_argument('--finder', action='store_true', help='Con --dry-run: compara los niveles del buscador OEM hoy y con D4 (solo lectura).')
    parser.add_argument('--json', action='store_true', help='Imprime también el resultado en JSON.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    stdout = stdout or sys.stdout
    result = run(apply=options.apply, rollback=options.rollback, username=options.user, finder=options.finder, stdout=stdout)
    if result is not None and options.json:
        stdout.write(json.dumps(result, ensure_ascii=False, default=str) + '\n')
    return 0 if result is not None else 1


if __name__ == '__main__':
    sys.exit(main())
