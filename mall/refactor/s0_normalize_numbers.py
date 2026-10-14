"""S0.10, target data step 1: OEM numbers the library holds in two spellings become one.

    python -m mall.refactor.s0_normalize_numbers --dry-run|--apply [--user USERNAME] [--report PATH] [--manifest-dir DIR]

Daihatsu and Suzuki print a trailing -000 that the same number drops elsewhere (16100-87795-000 = 16100-87795; Toyota-badged
Daihatsus too), and an Opel catalog number has seven digits, which a leading zero too many or too few spells twice across the GM
family (0636160 and 636160). mall.supplier_catalog_import.canonical_number says which spelling stays. In every group of twins (59 on
2026-10-13), the OEMReference of the other spelling is absorbed by the make's canonical one: its sources, aftermarket codes
(OEMCrossReference), retired codes and SKU links (PartOEMLink) move over, merging with what the keeper has (citations and source
details add up, and each moved source records the absorbed number under absorbed_numbers), blank descriptive fields fill in and
the number goes. A supplier catalog's source joined with the keeper's own keeps the pages, item codes and product types of both,
as a re-import of the catalog would file them. A make that holds only the other spelling, while another make of the family holds
the canonical one, has it renamed in place. A twin that needs a person stays as it is and is listed: an OEM alterno files under it
(the alterno mirror would bring the spelling back), it is disputed or annotated, it chains a supersession, or a code is retired
from one twin and live on the other.
On the live database an apply needs the S0.11 guard (mall.refactor.guard: the catalog freeze is on and no writer is still busy).
It takes the matching worker's lock and writes every group in one transaction. It logs each row it changes or deletes, with its
values before, to DIR/manifest.jsonl before the transaction commits, so a failed write changes nothing; DIR (by default
mall.refactor.output.stage_dir('s0/normalize_numbers'): the main checkout's output/refactor/s0/normalize_numbers, /app/output/… in
the containers, or REFACTOR_OUTPUT in a rehearsal) must sit in an existing folder. To undo it,
restore the D1 dump while still frozen. Re-running changes nothing. Afterwards run python -m mall.link_oem_numbers --apply: a SKU's
name link follows the spelling its own code names.
"""
import argparse
import json
import os
import shutil
import sys
from collections import Counter, defaultdict

EXPECTED_GROUPS = 59  # the plan's count, 2026-10-13
STAGE = 's0/normalize_numbers'  # the manifest's folder under output/refactor/ (mall.refactor.output)
FILLABLE = ('description', 'part_type', 'applications', 'system', 'family')
# What a supplier catalog's source lists, added up when two spellings' sources join (the import unions them per number too).
CATALOG_LISTS = ('pages', 'brand_codes', 'product_types')


def default_manifest_dir():
    from .output import stage_dir
    return str(stage_dir(STAGE))


def refusal():
    """Why --apply may not write now, or None: the S0.11 guard (mall.refactor.guard: on the live database, the catalog freeze and no
    writer still busy; a test or rehearsal database needs nothing), and S4: once the library's numbers are References
    (mall.refactor.s4_library gave them the OEMReference ids as uids) the OEM table is frozen, and merging two of its rows would leave
    their References apart. main() exits on the guard first, as the other guarded CLIs do; run() checks both for a caller that skips
    main()."""
    from .guard import refusal as shared
    return shared(True) or library_moved()


def library_moved():
    """The refusal once S4 moved the library to References, or None."""
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute('SELECT EXISTS (SELECT 1 FROM mall_partcode r JOIN mall_oemreference o ON o.id = r.uid)')
        moved = cursor.fetchone()[0]
    return ('S4 ya pasó la biblioteca OEM a referencias (mall.refactor.s4_library): la tabla OEM está congelada y este paso ya no escribe; '
            'los números gemelos que queden se unen en S9.') if moved else None


def joined_detail(kept, source):
    """The keeper's source detail after the absorbed spelling's source of the same kind and citation joins it."""
    from ..oem_reference import merge_detail
    detail = merge_detail(kept.detail, source.detail)
    if kept.kind == 'aftermarket_catalog':
        for key in CATALOG_LISTS:
            values = {*((kept.detail or {}).get(key) or []), *((source.detail or {}).get(key) or [])}
            if values:
                detail[key] = sorted(values)
    return detail


def write_manifest(directory, lines):
    """Add the lines to DIR/manifest.jsonl through a flushed temporary copy renamed over it, so the file holds all of them or none.
    Called inside the transaction: a failed write rolls every group back."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, 'manifest.jsonl')
    temporary = f'{path}.{os.getpid()}.tmp'
    try:
        with open(temporary, 'w', encoding='utf-8') as handle:
            if os.path.exists(path):
                with open(path, encoding='utf-8') as old:
                    shutil.copyfileobj(old, handle)
            for line in lines:
                handle.write(json.dumps(line, ensure_ascii=False, default=str) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def plan():
    """The groups of twins and what happens to each spelling, decided read-only: [{'group', 'members', 'actions'}], each action
    {'action': merge|rename, 'ref': id, 'manufacturer', 'code', 'into': keeper id (merge) or the new code (rename), 'refused': ''}."""
    from ..oem_reference_models import OEMReference
    from ..supplier_catalog_import import canonical_number, twin_scope
    groups = defaultdict(list)
    for row in OEMReference.objects.values('pk', 'manufacturer', 'code').order_by('manufacturer', 'code'):
        groups[(twin_scope(row['manufacturer']), canonical_number(row['manufacturer'], row['code']))].append(row)
    out = []
    for (scope, canonical), members in sorted(groups.items()):
        if len({m['code'] for m in members}) < 2:  # one spelling, under one make or several: no twins
            continue
        actions, by_make = [], defaultdict(list)
        for m in members:
            by_make[m['manufacturer']].append(m)
        for make, rows in sorted(by_make.items()):
            keeper = next((m for m in rows if m['code'] == canonical), None)
            others = [m for m in rows if m['code'] != canonical]
            if not others:
                continue
            if keeper is None:  # only the other spelling under this make: it becomes the canonical one
                keeper, others = others[0], others[1:]
                actions.append({'action': 'rename', 'ref': str(keeper['pk']), 'manufacturer': make, 'code': keeper['code'], 'into': canonical})
            actions += [{'action': 'merge', 'ref': str(m['pk']), 'manufacturer': make, 'code': m['code'], 'into': str(keeper['pk'])} for m in others]
        out.append({'group': f'{scope} {canonical}', 'members': [f"{m['manufacturer']} {m['code']}" for m in members], 'actions': actions})
    refuse(out)
    return out


def refuse(groups):
    """Mark the spellings that need a person, and the merges into a keeper whose rename was refused."""
    from ..oem_reference_models import OEMReference
    refs = OEMReference.objects.in_bulk([a['ref'] for g in groups for a in g['actions']] + [a['into'] for g in groups for a in g['actions'] if a['action'] == 'merge'])
    refs = {str(pk): ref for pk, ref in refs.items()}
    for group in groups:
        stuck = set()
        for action in group['actions']:
            ref, why = refs[action['ref']], ''
            if ref.alternos.exists():
                why = 'alterno'  # an OEM alterno files under this spelling: the mirror would bring it back
            elif action['action'] == 'merge':
                keeper = refs[action['into']]
                if action['into'] in stuck:
                    why = 'keeper'
                elif ref.status == 'disputed' or ref.notes.strip():
                    why = 'curated'
                elif ref.superseded_by_id or ref.supersedes.exists():
                    why = 'supersession'
                elif conflicting_retirements(ref, keeper):
                    why = 'retired_conflict'
            if why:
                stuck.add(action['ref'])
            action['refused'] = why


def conflicting_retirements(loser, keeper):
    """A code retired from one twin and live on the other: which one is right needs a person."""
    live = lambda ref: set(ref.legacy_cross_references.values_list('brand', 'number'))
    retired = lambda ref: set(ref.legacy_retired_cross_references.values_list('brand', 'number'))
    return bool(retired(loser) & live(keeper) or live(loser) & retired(keeper))


def values(obj):
    return {field.attname: getattr(obj, field.attname) for field in obj._meta.concrete_fields}


def absorb(loser, keeper, counts, log, apply):
    """The keeper takes the loser's sources, codes, retired codes and links; then the loser goes."""
    from ..oem_reference_models import citation_list
    note = {'id': str(loser.pk), 'manufacturer': loser.manufacturer, 'code': loser.code, 'printed_forms': loser.printed_forms}

    def absorbed(*details):
        seen, out = set(), []
        for entry in [e for d in details for e in (d or {}).get('absorbed_numbers') or []] + [note]:
            if entry['id'] not in seen:
                seen.add(entry['id'])
                out.append(entry)
        return out

    def update(obj, **changes):
        log.append({'table': obj._meta.db_table, 'action': 'update', 'id': obj.pk, 'before': values(obj)})
        if apply:
            for field, value in changes.items():
                setattr(obj, field, value)
            obj.save(update_fields=list(changes) + (['updated_at'] if hasattr(obj, 'updated_at') and 'updated_at' not in changes else []))

    def delete(obj):
        log.append({'table': obj._meta.db_table, 'action': 'delete', 'id': obj.pk, 'before': values(obj)})
        if apply:
            obj.delete()

    mine = {(s.kind, s.citation): s for s in keeper.sources.all()}
    for source in loser.sources.all():
        counts['sources'] += 1
        kept = mine.get((source.kind, source.citation))
        if kept is not None:
            counts['sources_joined'] += 1
            update(kept, detail={**joined_detail(kept, source), 'absorbed_numbers': absorbed(kept.detail, source.detail)})
            delete(source)
        else:
            update(source, reference_id=keeper.pk, detail={**(source.detail or {}), 'absorbed_numbers': absorbed(source.detail)})
    mine = {(x.brand, x.number): x for x in keeper.legacy_cross_references.all()}
    for row in loser.legacy_cross_references.all():
        counts['codes'] += 1
        kept = mine.get((row.brand, row.number))
        if kept is not None:
            counts['codes_joined'] += 1
            if citation_list(kept.citations, *row.citations) != kept.citations:
                update(kept, citations=citation_list(kept.citations, *row.citations))
            delete(row)
        else:
            update(row, legacy_reference_id=keeper.pk)
    mine = {(x.brand, x.number) for x in keeper.legacy_retired_cross_references.all()}
    for row in loser.legacy_retired_cross_references.all():
        counts['retired_codes'] += 1
        if (row.brand, row.number) in mine:
            delete(row)
        else:
            update(row, legacy_reference_id=keeper.pk)
    mine = {(link.part_id, link.source): link for link in keeper.legacy_part_links.all()}
    for link in loser.legacy_part_links.all():
        counts['links'] += 1
        kept = mine.get((link.part_id, link.source))
        if kept is not None:
            counts['links_joined'] += 1
            if citation_list(kept.citations, *link.citations) != kept.citations:
                update(kept, citations=citation_list(kept.citations, *link.citations))
            delete(link)
        else:
            update(link, legacy_reference_id=keeper.pk)
    log.append({'table': keeper._meta.db_table, 'action': 'update', 'id': keeper.pk, 'before': values(keeper)})
    if apply:
        for field in FILLABLE:
            if not getattr(keeper, field) and getattr(loser, field):
                setattr(keeper, field, getattr(loser, field))
        refresh_status(keeper, keeper.updated_by, changed=True)  # takes the next version; an automatic merge edits no one's name in
    delete(loser)


def refresh_status(ref, actor=None, *, changed=False):
    """The OEM table's status rule on an OEMReference (this tool runs on the OEM table as it was before S4, which froze it): re-derived
    from its sources unless disputed, saved with the next version when it changed or the caller changed something else."""
    from ..oem_reference import derived_status
    before = ref.status
    if ref.status != 'disputed':
        ref.status = derived_status(ref.sources.values_list('kind', 'detail'))
    if changed or ref.status != before:
        ref.updated_by = actor
        ref.save()
    return ref.status


def rename(ref, code, log, apply):
    log.append({'table': ref._meta.db_table, 'action': 'update', 'id': ref.pk, 'before': values(ref)})
    if apply:
        ref.code = code  # save() keeps only the printed spellings of the new code
        ref.save()


def execute(groups, apply):
    """Run the plan (or count it, apply=False) inside the caller's transaction. Returns (counts, manifest rows)."""
    from ..oem_reference_models import OEMReference
    counts, log = Counter(), []
    for group in groups:
        for action in group['actions']:
            if action['refused']:
                counts[f"refused_{action['refused']}"] += 1
                continue
            ref = OEMReference.objects.select_for_update().get(pk=action['ref']) if apply else OEMReference.objects.get(pk=action['ref'])
            if action['action'] == 'rename':
                counts['renamed'] += 1
                rename(ref, action['into'], log, apply)
            else:
                counts['merged'] += 1
                keeper = OEMReference.objects.select_for_update().get(pk=action['into']) if apply else OEMReference.objects.get(pk=action['into'])
                absorb(ref, keeper, counts, log, apply)
    return counts, log


def run(*, apply=False, username=None, report_path=None, manifest_dir=None, stdout=None):
    from django.contrib.auth import get_user_model
    from django.db import connection, transaction
    from django.utils import timezone
    from ..oem_finder import matching_lock
    stdout = stdout or sys.stdout
    manifest_dir = manifest_dir or default_manifest_dir()
    actor = get_user_model().objects.get(username=username) if username else None
    if apply:
        reason = refusal()
        if reason:
            stdout.write(reason + '\n')
            return None
    if apply and not os.path.isdir(os.path.dirname(os.path.normpath(manifest_dir))):
        stdout.write(f'Falta la carpeta {os.path.dirname(os.path.normpath(manifest_dir))} del registro de cambios.\n')
        return None

    def go():
        try:
            with transaction.atomic():
                groups = plan()
                counts, log = execute(groups, apply)
                if apply:  # before the commit: a failed write rolls every group back
                    write_manifest(manifest_dir, [{'tool': 'mall.refactor.s0_normalize_numbers', 'database': connection.settings_dict['NAME'],
                                                   'user': actor.username if actor else '', 'at': timezone.now().isoformat(),
                                                   'groups': len(groups)}, *log])
        except OSError as error:
            stdout.write(f'No se pudo escribir el registro de cambios ({error}): no se cambia nada.\n')
            return None
        result = {'mode': 'apply' if apply else 'dry_run', 'groups': len(groups), 'expected_groups': EXPECTED_GROUPS, 'counts': dict(counts),
                  'rows_logged': len(log), 'plan': groups}
        refused = sum(v for k, v in counts.items() if k.startswith('refused_'))
        stdout.write(f"{'Aplicado' if apply else 'Simulación'} · {len(groups)} grupos de números gemelos (esperados {EXPECTED_GROUPS}) · "
                     f"{counts['merged']} absorbidos · {counts['renamed']} renombrados · {refused} sin tocar por necesitar revisión · "
                     f"fuentes {counts['sources']} ({counts['sources_joined']} unidas) · equivalencias {counts['codes']} "
                     f"({counts['codes_joined']} unidas) · retiradas {counts['retired_codes']} · vínculos {counts['links']} "
                     f"({counts['links_joined']} unidos)\n")
        if report_path:
            with open(report_path, 'w', encoding='utf-8') as handle:
                json.dump(result, handle, ensure_ascii=False, indent=1, default=str)
        return result

    if not apply:
        return go()
    with matching_lock() as acquired:
        if not acquired:
            stdout.write('El análisis de coincidencias está en curso; vuelve a intentarlo cuando termine.\n')
            return None
        return go()


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Une los números OEM que la biblioteca guarda con dos grafías (…000, cero inicial de Opel).')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Calcula los grupos y los cambios sin escribir nada.')
    mode.add_argument('--apply', action='store_true', help='Une cada grupo en una sola transacción (requiere el catálogo congelado).')
    parser.add_argument('--user', help='Usuario que figura en el registro de cambios.')
    parser.add_argument('--report', help='Escribe el plan y los conteos en esta ruta (JSON).')
    parser.add_argument('--manifest-dir', help='Carpeta del registro de cambios (manifest.jsonl); por omisión '
                                               'output/refactor/s0/normalize_numbers del checkout principal.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .guard import require_freeze
    require_freeze(options.apply)
    result = run(apply=options.apply, username=options.user, report_path=options.report, manifest_dir=options.manifest_dir, stdout=stdout)
    sys.exit(0 if result is not None else 1)


if __name__ == '__main__':
    main()
