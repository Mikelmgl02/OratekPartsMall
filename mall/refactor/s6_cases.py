"""Stage S6 of the inventory refactor, data step 1 (plan §3 S6, A5, OD18): the matching cases take the shape of matching on References.

    python -m mall.refactor.s6_cases --dry-run|--apply --user USERNAME|--rollback [--report PATH] [--json]

It runs after mall.refactor.s6_new_products, once 0083/0084 are applied (before S6 lands, under the freeze; again after, as a
catch-up: a case already converted or opened is left alone):
1. Legacy cases follow A5. A catalog or catalog_reference case becomes a merge case with its status (applied stays applied, review
   stays review, route caso): the lines its rows name (and the line its target_sku names) become its lines; its key, method, rows and
   fingerprint stay, and evidence['legacy'] keeps what it was. A supplier case that was applied becomes a line case, applied, on the
   line its target_sku names. A supplier case in review or unmatched whose item hangs from a line now is settled: on a line outside
   the catalog dismissed ('Colocado en Productos nuevos'), on a catalog line a line case, applied; one whose item has no line yet is
   listed (the worker's backlog places it or opens its line case).
2. The OD18 batch: each automatic merge of the code before S6 that the S0.8 audit (mall.refactor.s0_merge_audit) says the new rules
   would not make, whose failing sources are still merged into it (the 34 different-part merges were separated in S0), becomes one
   merge case: route lote, batch_key legacy-auto-merge, key merge:legacy-auto-merge:<merge id>, its lines the target and those
   sources. The audit runs again on the day's suffix table (it judged 217 in S0.8, 227 on 2026-10-14). Unificar keeps the merge, Mantener separadas takes it apart (mall.reference_matching.decide_merge).
3. Declared codes (rule 5b): each code an attached item declares that its line does not hold becomes a reference case
   (mall.reference_matching.sync_declared, one writer).
4. The worker's first pass, read-only, for D10's "open cases by kind": the merge cases the catalog stage would open
   (open_reference_merge_cases), what the backlog would do with each item without a Reference (attach, a new Reference on a line, a
   line case, a new product), and the code changes waiting.
Before 0083 is applied the dry run is a simulation: the conversions, the batch and the declared codes are counted; the catalog stage's
merge cases need 0083 and are not.

It writes in one transaction under the matching worker's lock; on the live database --apply and --rollback need the catalog freeze
(mall.refactor.guard). Every case it creates or changes goes, with its values before, to output/refactor/s6/cases_manifest.jsonl.
--rollback deletes the cases it created and gives every case it changed its kind, status, reason, route, evidence and lines back. Run
it before python -m mall.refactor.s6_new_products --rollback.
"""
import argparse
import json
import os
import shutil
import sys
import uuid
from collections import Counter, defaultdict

STAGE = 's6'
MANIFEST = 'cases_manifest.jsonl'
BATCH_KEY = 'legacy-auto-merge'
PLACED_REASON = 'Colocado en Productos nuevos'
# Live, read-only, 2026-10-14 (before 0083): legacy catalog cases 1,433 applied (1,380 base_and_spec, 53 family_review) and 107 in review
# (103 shared_oem_number, 4 compact_duplicate); catalog_reference 8 applied and 13 in review; supplier 134 applied, 9 in review and
# 4,185 unmatched (4,184 of them dismissed by s6_new_products for the items it places). The S0.8 audit judged 217 merges the new rules
# would not make; re-run today (the suffix table has moved since) it judges 227, 34 of them separated in S0 (OD18): 193 batch cases.
# The live dry run 2026-10-14 (before 0083, so a simulation) gave exactly these. The rehearsal gives the numbers of the day.
EXPECTED = {'merge_aplicados': 1441, 'merge_en_revision': 120, 'linea_aplicados': 134, 'lote': 193}
CHUNK = 2000


def current_database():
    from django.db import connection
    return str(connection.settings_dict['NAME'])


def manifest_path():
    from .output import stage_dir
    return stage_dir(STAGE) / MANIFEST


def manifest_lines():
    path = manifest_path()
    if not path.exists():
        return []
    with open(path, encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def manifest_problem():
    from .output import main_checkout
    if not os.environ.get('REFACTOR_OUTPUT', '').strip() and not (main_checkout() / 'output').is_dir():
        return f'Falta la carpeta {main_checkout() / "output"} para el manifiesto; ejecútalo dentro del contenedor de la app.'
    others = sorted({str(line.get('database')) for line in manifest_lines()} - {current_database()})
    if others:
        return (f"El manifiesto {manifest_path()} tiene líneas de otra base ({', '.join(others)}): muévelo antes de aplicar o deshacer "
                'en esta.')
    return None


def write_manifest(lines, path=None):
    """Add the lines through a flushed temporary copy renamed over the manifest (this stage's, or `path`): all or none, inside the
    transaction."""
    path = path or manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    try:
        with open(temporary, 'w', encoding='utf-8') as handle:
            if path.exists():
                with open(path, encoding='utf-8') as old:
                    shutil.copyfileobj(old, handle)
            for line in lines:
                handle.write(json.dumps(line, ensure_ascii=False, default=str) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def recorded(values):
    return json.loads(json.dumps(values, default=str))


def schema():
    """{'cases': 0083 applied (MatchingCase.new_code and its lines)}."""
    from django.db import connection
    with connection.cursor() as cursor:
        cases = {c.name for c in connection.introspection.get_table_description(cursor, 'mall_matchingcase')}
    return {'cases': 'new_code' in cases}


# --- plan -----------------------------------------------------------------------------------------------------------------------------

def row_ids(case):
    """The line ids a legacy case's rows name (sources and candidates), in order, once each."""
    from ..lines import is_uuid
    seen = []
    for row in [*(case.sources or []), *(case.candidates or [])]:
        pk = str((row or {}).get('id') or '')
        if pk and is_uuid(pk) and pk not in seen:
            seen.append(pk)
    return seen


def target_line(sku):
    """The catalog line a legacy case's target_sku names (its canonical line), or None."""
    from ..lines import canonical, lines_by_codigo
    if not sku:
        return None
    found = next(iter(lines_by_codigo(sku)), None)
    return canonical(found) if found is not None else None


NEW_FIELDS = ('reference', 'new_code', 'new_brand', 'evidence', 'guard', 'route', 'batch_key', 'decided_by', 'decided_at', 'created_at')


def legacy_cases(cases_ready):
    """MatchingCase rows as this database has them: before 0083 its new columns are left out (and evidence reads as {})."""
    from ..matching_models import MatchingCase
    return MatchingCase.objects.all() if cases_ready else MatchingCase.objects.defer(*NEW_FIELDS)


def evidence_of(case, cases_ready):
    return dict(case.evidence or {}) if cases_ready else {}


def legacy_plan(counts, listed, *, cases_ready=True):
    """The A5 conversions, read-only: [(case, changes, line ids)]."""
    from ..lines import CATALOG
    from ..models import Part
    changes = []
    catalog = legacy_cases(cases_ready).filter(kind__in=('catalog', 'catalog_reference')).order_by('pk')
    for case in catalog.iterator(chunk_size=500):
        ids = row_ids(case)
        target = target_line(case.target_sku) if case.status != 'review' else None
        if target is not None and str(target.pk) not in ids:
            ids.append(str(target.pk))
        existing = {str(pk) for pk in Part._base_manager.filter(pk__in=ids).values_list('pk', flat=True)}
        ids = [pk for pk in ids if pk in existing]
        status = {'resolved': 'applied', 'unmatched': 'review'}.get(case.status, case.status)
        legacy = {'kind': case.kind, 'method': case.method, 'status': case.status}
        changes.append((case, {'kind': 'merge', 'status': status, 'route': 'caso', 'evidence': {**evidence_of(case, cases_ready), 'legacy': legacy}},
                        ids))
        counts['merge_aplicados' if status == 'applied' else 'merge_en_revision' if status == 'review' else 'merge_otros'] += 1
    supplier = legacy_cases(cases_ready).filter(kind='supplier').select_related('item__part').order_by('pk')
    for case in supplier.iterator(chunk_size=500):
        if case.status == 'applied':
            target = target_line(case.target_sku)
            legacy = {'kind': 'supplier', 'method': case.method, 'status': case.status}
            changes.append((case, {'kind': 'line', 'route': 'caso', 'evidence': {**evidence_of(case, cases_ready), 'legacy': legacy}},
                            [str(target.pk)] if target is not None else []))
            counts['linea_aplicados'] += 1
        elif case.status in ('review', 'unmatched'):
            item = case.item
            if item is None or item.part_id is None or item.reference_id is None:
                counts['proveedor_sin_linea'] += 1
                listed['proveedor_sin_linea'].append(f'{case.key}: el artículo aún no tiene línea (la próxima pasada lo coloca)')
                continue
            if item.part.estado in CATALOG and not item.part.merged_into_id:
                legacy = {'kind': 'supplier', 'method': case.method, 'status': case.status}
                changes.append((case, {'kind': 'line', 'status': 'applied', 'route': 'caso', 'target_sku': item.part.sku,
                                       'evidence': {**evidence_of(case, cases_ready), 'legacy': legacy}}, [str(item.part_id)]))
                counts['proveedor_en_catalogo'] += 1
            else:
                changes.append((case, {'status': 'dismissed', 'reason': PLACED_REASON}, None))
                counts['proveedor_colocados'] += 1
    return changes


def batch_plan(counts, listed):
    """The OD18 batch, read-only: one merge case spec per doubtful legacy merge still in place (mall.refactor.s0_merge_audit)."""
    from ..lines import canonical
    from ..models import Part
    from ..references import is_live
    from .s0_merge_audit import REASONS, audit
    specs = []
    result = audit()
    for row in result['rows']:
        if not row['failing']:
            continue
        merge = row['merge']
        target = canonical(merge.target)
        if target is None or not is_live(target):
            listed['lote_sin_destino'].append(f'{row["target_sku"]}: su línea destino ya no existe o está unificada en otra')
            continue
        merged = [source for source in row['failing'] if source['state'] == 'unificado']
        if not merged:
            counts['lote_separadas'] += 1
            continue
        sources = list(Part._base_manager.filter(pk__in=[source['id'] for source in merged]).order_by('sku', 'pk'))
        reasons = sorted({source['reason'] for source in merged})
        spec = {'kind': 'merge', 'key': f'merge:{BATCH_KEY}:{merge.pk}', 'lines': [target.pk, *(part.pk for part in sources)],
                'line_rows': [target, *sources], 'batch_key': BATCH_KEY, 'route': 'lote', 'guard': '', 'reference': None,
                'evidence': {'audit': 'S0.8', 'merge_id': str(merge.pk), 'method': row['method'], 'motivos': reasons,
                             'codes': [{'code': source['sku'], 'brand': ''} for source in merged],
                             'different_parts': bool(row['different_parts'])},
                'reason': (f'Unificación automática anterior que las reglas nuevas no harían ({"; ".join(REASONS[r] for r in reasons)}): '
                           '«Unificar» la conserva; «Mantener separadas» la deshace con Separar SKU.')}
        specs.append(spec)
        counts['lote'] += 1
    return specs


def declared_plan(counts, *, cases_ready):
    """The reference cases the declared codes ask for, read-only: (item ids to sync, how many cases are new)."""
    from .. import references
    from ..matching_models import MatchingCase
    from ..models import SupplierItem
    from ..reference_matching import Lookups, case_key, declared_holders, reference_case
    from ..sku_proposals import merge_references
    lookups, items, new = Lookups(), [], 0
    rows = SupplierItem.objects.filter(reference__isnull=False).exclude(references=[]).select_related('part', 'reference').order_by('pk')
    for item in rows.iterator(chunk_size=500):
        seen, wanted = set(), []
        for row in merge_references(item.references or []):
            key = (references.clave(references.code_key(row['code'])), lookups.canonical(row.get('brand', '')))
            if key in seen:
                continue
            seen.add(key)
            holders = declared_holders(row, item, lookups)
            if any(holder.part_id == item.part_id for holder in holders):
                continue
            wanted.append(case_key(reference_case(item, row, holders), item))
        if wanted:
            items.append(item.pk)
            known = set(MatchingCase.objects.filter(key__in=wanted).values_list('key', flat=True)) if cases_ready else set()
            new += len(set(wanted) - known)
    counts['casos_declarados'] = new
    counts['articulos_con_declarados'] = len(items)
    return items


def first_pass(counts, listed, *, cases_ready):
    """What the matching worker's first pass would open, read-only (D10: open cases by kind)."""
    from .. import reference_matching as rm
    from ..models import SupplierItem
    if cases_ready:
        counts['paso_merge_catalogo'] = len(rm.open_reference_merge_cases(dry_run=True))
    else:
        listed['simulacion'].append('Sin 0083 no se cuentan los casos de unificación de la etapa del catálogo (covered lee sus líneas).')
    lookups, actions = rm.Lookups(), Counter()
    for item in SupplierItem.objects.filter(reference__isnull=True).select_related('supplier').order_by('pk').iterator(chunk_size=500):
        found = rm.resolve(rm.subject_of(item), lookups)
        actions[found.action] += 1
        actions['merge_3e'] += sum(1 for spec in found.cases if spec and spec['kind'] == 'merge')
        if found.action == 'case' and found.cases and found.cases[0].get('guard') == 'sin_codigo':
            listed['sin_codigo'].append(f'{item.supplier_invent_id} ({item.supplier.name}): código «{item.codigo}»')
    for action, number in actions.items():
        counts[f'paso_{action}'] = number
    counts['paso_cambios_de_codigo'] = SupplierItem.objects.filter(reference__isnull=False).exclude(
        codigo_pendiente__isnull=True, brand_pendiente__isnull=True).count()


def plan(*, cases_ready=True):
    counts, listed = Counter(), defaultdict(list)
    legacy = legacy_plan(counts, listed, cases_ready=cases_ready)
    batch = batch_plan(counts, listed)
    declared = declared_plan(counts, cases_ready=cases_ready)
    first_pass(counts, listed, cases_ready=cases_ready)
    return {'legacy': legacy, 'batch': batch, 'declared': declared, 'counts': counts, 'listed': listed}


# --- apply ----------------------------------------------------------------------------------------------------------------------------

FIELDS = ('kind', 'status', 'reason', 'route', 'evidence', 'target_sku', 'batch_key')


def write(planned, log, run):
    """Write the plan (inside the caller's transaction). Returns the counts written."""
    from ..matching_models import MatchingCase
    from ..models import SupplierItem
    from ..reference_matching import open_case, sync_declared
    written = Counter()
    for case, changes, ids in planned['legacy']:
        locked = MatchingCase.objects.select_for_update().filter(pk=case.pk).first()
        if locked is None:
            continue
        before = {name: getattr(locked, name) for name in FIELDS}
        before['lines'] = [str(pk) for pk in locked.lines.values_list('pk', flat=True)]
        updates = {name: value for name, value in changes.items() if getattr(locked, name) != value}
        if not updates and (ids is None or set(ids) == set(before['lines'])):
            continue
        if updates:
            MatchingCase.objects.filter(pk=locked.pk).update(**updates)
        if ids is not None:
            locked.lines.set(ids)
        log.append({**run, 'model': 'mall.MatchingCase', 'id': str(locked.pk), 'action': 'update', 'before': recorded(before)})
        written['convertidos'] += 1
    for spec in planned['batch']:
        if MatchingCase.objects.filter(key=spec['key']).exists():
            continue
        case = open_case(spec)
        log.append({**run, 'model': 'mall.MatchingCase', 'id': str(case.pk), 'action': 'create', 'key': case.key})
        written['lote'] += 1
    for start in range(0, len(planned['declared']), CHUNK):
        for item in SupplierItem.objects.filter(pk__in=planned['declared'][start:start + CHUNK]).select_related('part', 'reference', 'supplier'):
            before = set(MatchingCase.objects.filter(item_id=item.pk, kind='reference').values_list('pk', flat=True))
            sync_declared(item)
            for case in MatchingCase.objects.filter(item_id=item.pk, kind='reference').exclude(pk__in=before):
                log.append({**run, 'model': 'mall.MatchingCase', 'id': str(case.pk), 'action': 'create', 'key': case.key})
                written['casos_declarados'] += 1
    return dict(written)


def undo(log, run):
    """--rollback: the cases it created are deleted, the ones it changed get their values and lines back."""
    from ..matching_models import MatchingCase
    entries = [line for line in manifest_lines() if line.get('action') in ('create', 'update')]
    result = Counter()
    for line in reversed(entries):
        if line['action'] == 'create':
            result['borrados'] += MatchingCase.objects.filter(pk=line['id']).delete()[1].get('mall.MatchingCase', 0)
            continue
        case = MatchingCase.objects.filter(pk=line['id']).first()
        if case is None:
            continue
        before = dict(line['before'])
        lines = before.pop('lines', [])
        if case.decided_at is not None:  # decided since S6 landed (a legacy case has no decided_at): the decision stands
            before.pop('status', None)
        MatchingCase.objects.filter(pk=case.pk).update(**before)
        case.lines.set(lines)
        result['devueltos'] += 1
    log.append({**run, 'model': 'manifest', 'action': 'rolled_back', 'rows': len(entries)})
    return dict(result)


# --- run ------------------------------------------------------------------------------------------------------------------------------

ROWS = (('merge_aplicados', 'casos de catálogo aplicados → unificación'), ('merge_en_revision', 'casos de catálogo en revisión → unificación'),
        ('linea_aplicados', 'casos de proveedor aplicados → línea'), ('proveedor_colocados', 'de proveedor colocados en Productos nuevos'),
        ('proveedor_en_catalogo', 'de proveedor ya en una línea del catálogo'), ('proveedor_sin_linea', 'de proveedor sin línea aún'),
        ('lote', 'lote legacy-auto-merge (OD18)'), ('casos_declarados', 'casos de códigos declarados nuevos'),
        ('paso_merge_catalogo', '1.ª pasada: unificaciones del catálogo'), ('paso_attach', '1.ª pasada: se vinculan'),
        ('paso_new_reference_on_line', '1.ª pasada: referencia nueva en una línea'), ('paso_case', '1.ª pasada: casos de línea'),
        ('paso_no_line', '1.ª pasada: productos nuevos'), ('paso_merge_3e', '1.ª pasada: unificaciones por catálogo (3e)'),
        ('paso_cambios_de_codigo', '1.ª pasada: cambios de código'))


def print_summary(stdout, mode, planned, state, writes=None):
    title = {'apply': 'Aplicado', 'dry_run': 'Simulación'}[mode]
    counts = planned['counts']
    stdout.write(f'{title} · S6 casos de coincidencias\n')
    if not state['cases']:
        stdout.write('  (simulación: a esta base le falta 0083; los casos solo se cuentan)\n')
    stdout.write(f"  {'':46} {'esperado':>9} {'resultado':>10}\n")
    for key, label in ROWS:
        expected = EXPECTED.get(key)
        stdout.write(f"  {label:46} {'' if expected is None else f'{expected:,}':>9} {counts.get(key, 0):>10,}\n")
    for key, rows in sorted(planned['listed'].items()):
        stdout.write(f'  {key.replace("_", " ").capitalize()}: {len(rows)}\n')
        for row in rows[:15]:
            stdout.write(f'    {row}\n')
    if writes:
        stdout.write('  Escrito: ' + ' · '.join(f'{key} {value:,}' for key, value in sorted(writes.items())) + '\n')


def run(*, apply=False, rollback=False, username=None, report_path=None, stdout=None):
    """Plan, and with apply write it, or with rollback undo the applies on this database. None when a write is refused."""
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.utils import timezone
    from ..matching_queue import matching_lock, matching_work
    stdout = stdout or sys.stdout
    state = schema()
    user = None
    if apply or rollback:
        from .guard import refusal
        reason = refusal(True) or manifest_problem()
        if reason:
            stdout.write(reason + '\n')
            return None
        if not state['cases']:
            stdout.write('Falta la migración 0083_matching_cases en esta base: aplícala primero desde el worktree (la simulación funciona '
                         'sin ella).\n')
            return None
        user = get_user_model().objects.filter(username=username or '').first()
        if apply and user is None:
            stdout.write('Indica con --user el usuario que figura como autor del cambio.\n' if not username else f'No existe el usuario {username}.\n')
            return None
    run_line = {'stage': 's6_cases', 'database': current_database(), 'run': str(uuid.uuid4()), 'actor': user.username if user else '',
                'at': timezone.now().isoformat()}

    def go():
        log = []
        with transaction.atomic():
            if rollback:
                result = {'mode': 'rollback', **undo(log, run_line)}
            else:
                planned = plan(cases_ready=state['cases'])
                result = {'mode': 'apply' if apply else 'dry_run', 'result': dict(planned['counts']), 'expected': EXPECTED,
                          'listed': {key: rows for key, rows in planned['listed'].items()}}
                writes = None
                if apply:
                    writes = result['written'] = write(planned, log, run_line)
                print_summary(stdout, result['mode'], planned, state, writes)
                if report_path:
                    with open(report_path, 'w', encoding='utf-8') as handle:
                        json.dump(result, handle, ensure_ascii=False, indent=1, default=str)
                    stdout.write(f'Informe: {report_path}\n')
            if log:
                result['manifest'] = str(write_manifest([{**run_line, 'action': 'run', 'mode': result['mode'], 'rows': len(log)}, *log]))
        if rollback:
            stdout.write('Rollback · S6 casos: ' + ' · '.join(f'{key} {value:,}' for key, value in sorted(result.items())
                                                            if isinstance(value, int)) + '\n')
        if result.get('manifest'):
            stdout.write(f"Manifiesto: {result['manifest']}\n")
        return result

    if not (apply or rollback):
        return go()
    with matching_lock() as acquired, matching_work():
        if not acquired:
            stdout.write('El análisis de coincidencias está en curso; vuelve a intentarlo cuando termine.\n')
            return None
        try:
            return go()
        except OSError as error:
            stdout.write(f'No se pudo escribir el manifiesto ({error}): no se cambia nada.\n')
            return None


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='S6: los casos de coincidencias pasan a los tipos de S6 (A5, lote OD18, códigos declarados).')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Calcula los cambios sin escribir nada (antes de 0083: simulación).')
    mode.add_argument('--apply', action='store_true', help='Escribe todo en una transacción (en vivo requiere el catálogo congelado).')
    mode.add_argument('--rollback', action='store_true', help='Deshace con el manifiesto lo aplicado en esta base (requiere el congelamiento en vivo).')
    parser.add_argument('--user', help='Usuario que figura como autor (obligatorio con --apply).')
    parser.add_argument('--report', help='Escribe en esta ruta (JSON) las cuentas y las listas.')
    parser.add_argument('--json', action='store_true', help='Imprime también el resultado en JSON.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .guard import require_freeze
    require_freeze(options.apply or options.rollback)
    stdout = stdout or sys.stdout
    result = run(apply=options.apply, rollback=options.rollback, username=options.user, report_path=options.report, stdout=stdout)
    if result is not None and options.json:
        stdout.write(json.dumps(result, ensure_ascii=False, default=str) + '\n')
    return 0 if result is not None else 1


if __name__ == '__main__':
    sys.exit(main())
