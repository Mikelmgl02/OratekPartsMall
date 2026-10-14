"""Stage S5 of the inventory refactor: the catalog's grupos become a table (Grupo) and every subgrupo (PartType) points at its grupo.

    python -m mall.refactor.s5_grupos --dry-run|--apply --user USERNAME|--rollback [--report PATH] [--json]

One Grupo per distinct PartType.category, normalized (mall.technical_data.normalized_label) and matched ignoring case, as 0081's
UNIQUE Lower(nombre) does; a grupo that already exists (the code from S5 creates them on first use) is reused. Every subgrupo with no
grupo takes the grupo its text names, and its text takes that grupo's spelling. A subgrupo whose grupo is set but whose text names
another grupo (only a writer of the text alone could leave it, which no code from S5 is) and one with a blank text are listed for a
person, and so are two subgrupos of one grupo with the same name ignoring case: 0082 refuses to tighten while any exists. Lines keep
their part_type; the dry run also checks that the grupo and subgrupo text of every line with a subgrupo is that subgrupo's (so the
storefront facets, which read the FK from S5, stay identical) and counts the partial classifications (a grupo with no subgrupo).

It writes in bulk (no signals) under the matching worker's lock, in one transaction; on the live database --apply and --rollback need
the S0.11 guard (mall.refactor.guard: the catalog freeze is on and no writer is still busy). Every row it changes or creates goes, with
its values before, to mall.refactor.output.manifest_path('s5') (the main checkout's git-ignored output/refactor/s5/, /app/output/… in
the containers, REFACTOR_OUTPUT in a rehearsal) before the transaction commits; the folder must exist, and a manifest holding another
database's lines is refused. The dry run also works before 0081 is applied (every subgrupo read without a grupo, and no grupo yet).
Re-running changes nothing new: it runs before landing (the code before S5 never names the grupo) and again after, as the catch-up.

--rollback puts back what the applies on this database changed and no rollback has put back yet, unless a row changed since (listed):
each subgrupo's grupo and text as they were, and the grupos it created deleted while no subgrupo points at them. With 0082 in place a
subgrupo cannot go back to no grupo, and the code before S5 cannot create a subgrupo at all (it writes the text without the grupo). So
S5 rolls back in this order, from the worktree, under the freeze: reverse 0082 (python manage.py migrate mall 0081_grupo; S5's code
runs without it), then optionally --rollback, then revert the code (git revert, reload gunicorn, restart the workers), and last,
optionally, reverse 0081 (migrate mall to the migration 0081 depends on, from the worktree). Never revert the code while 0082 is
applied. --report writes every grupo with its subgrupos and everything listed, for the owner.
"""
import argparse
import json
import os
import shutil
import sys
import uuid
from collections import Counter, defaultdict

STAGE = 's5'
# The live dry run of 2026-10-14. Plan §3 S5 counted 13 grupos (41 subgrupos) on 2026-10-13: since then Revisión IA's fills of
# Productos nuevos (mall.sku_proposals, through part_type_for) created 65 subgrupos and the grupos COMBUSTIBLE, DIRECCIÓN and
# HERRAMIENTAS. The catalog keeps moving: D0 takes the deploy's own dry run as the expected counts.
EXPECTED = {'grupos': 16, 'subgrupos': 106, 'sin_grupo': 0, 'lineas': 614}
FIELDS = ('grupo_id', 'category')
NULLABLE = 'grupo_id'  # 0082 makes it NOT NULL: with it in place a subgrupo cannot go back to no grupo


def current_database():
    from django.db import connection
    return str(connection.settings_dict['NAME'])


def output_dir():
    from .output import main_checkout
    return main_checkout() / 'output'  # git-ignored; /app/output in the containers (a run from a worktree writes in the main checkout's)


def manifest_path():
    from .output import manifest_path as stage_manifest_path
    return stage_manifest_path(STAGE)


def manifest_lines():
    path = manifest_path()
    if not path.exists():
        return []
    with open(path, encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def manifest_problem():
    """Why the manifest cannot take this run's lines, or None: the output folder is missing (REFACTOR_OUTPUT aside), or the manifest
    holds another database's lines (a rehearsal's or a test run's), which a rollback here must never replay."""
    if not os.environ.get('REFACTOR_OUTPUT', '').strip() and not output_dir().is_dir():
        return f'Falta la carpeta {output_dir()} para el manifiesto; ejecútalo dentro del contenedor de la app.'
    others = sorted({str(line.get('database')) for line in manifest_lines()} - {current_database()})
    if others:
        return (f"El manifiesto {manifest_path()} tiene líneas de otra base ({', '.join(others)}): muévelo antes de aplicar o deshacer "
                'en esta.')
    return None


def refusal():
    """The S0.11 guard (mall.refactor.guard): on the live database, the catalog freeze and no writer still busy."""
    from .guard import refusal as shared
    return shared(True)


def write_manifest(lines):
    """Add the lines to the manifest through a flushed temporary copy renamed over it, so it holds all of them or none. Called inside
    the transaction: a failed write rolls every row back."""
    path = manifest_path()
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
    """Values as the manifest holds them (ids as text), for comparing a row now with what a run wrote."""
    return json.loads(json.dumps(values, default=str))


def columns():
    """{column: nullable} of mall_parttype, and whether the grupo table exists."""
    from django.db import connection
    with connection.cursor() as cursor:
        found = {column.name: column.null_ok for column in connection.introspection.get_table_description(cursor, 'mall_parttype')}
        tables = set(connection.introspection.table_names(cursor))
    return found, 'mall_grupo' in tables


def expanded():
    """Whether 0081 is applied here (the grupo table and PartType.grupo)."""
    found, table = columns()
    return table and NULLABLE in found


def tightened():
    """Whether 0082 is applied here (PartType.grupo NOT NULL)."""
    found, _ = columns()
    return found.get(NULLABLE) is False


# --- plan ----------------------------------------------------------------------------------------------------------------------------

def subgrupo_rows(schema):
    """Every subgrupo's pk, category, name, grupo_id and its grupo's nombre; before 0081, without a grupo."""
    from ..models import PartType
    if schema:
        return list(PartType.objects.order_by('pk').values('pk', 'category', 'name', 'grupo_id', 'grupo__nombre'))
    return [{**row, 'grupo_id': None, 'grupo__nombre': None} for row in PartType.objects.order_by('pk').values('pk', 'category', 'name')]


def plan(schema=True):
    """{'grupos': {key: {nombre, id, subgrupos}}, 'changes': {pk: (name, before, key)}, 'blank': [...], 'moved': [...], 'clashes': [...],
    'subgrupos': n, 'lines': {...}} (read-only). A key is a grupo's normalized name in lower case (0081's UNIQUE Lower(nombre))."""
    from ..models import Grupo, Part
    from ..technical_data import normalized_label
    existing = {grupo['nombre'].lower(): grupo for grupo in Grupo.objects.values('pk', 'nombre')} if schema else {}
    grupos = {key: {'nombre': grupo['nombre'], 'id': grupo['pk'], 'subgrupos': []} for key, grupo in existing.items()}
    changes, blank, moved, assigned = {}, [], [], {}
    rows = subgrupo_rows(schema)
    for row in rows:
        text = normalized_label(row['category'])
        before = {'grupo_id': row['grupo_id'], 'category': row['category']}
        if row['grupo_id']:
            key = row['grupo__nombre'].lower()
            grupos.setdefault(key, {'nombre': row['grupo__nombre'], 'id': row['grupo_id'], 'subgrupos': []})
            if text.lower() != key:  # its text names another grupo: which one is right needs a person
                moved.append({'id': str(row['pk']), 'name': row['name'], 'grupo': row['grupo__nombre'], 'texto': row['category']})
            elif row['category'] != row['grupo__nombre']:  # the same grupo spelled otherwise: the text takes the grupo's spelling
                changes[row['pk']] = (row['name'], before, key)
        elif not text:
            blank.append({'id': str(row['pk']), 'name': row['name']})
            continue
        else:
            key = text.lower()
            grupos.setdefault(key, {'nombre': text, 'id': None, 'subgrupos': []})
            changes[row['pk']] = (row['name'], before, key)
        grupos[key]['subgrupos'].append(row['name'])
        assigned[row['pk']] = key
    names = Counter((key, name.lower()) for key, info in grupos.items() for name in info['subgrupos'])
    clashes = [{'grupo': grupos[key]['nombre'], 'subgrupo': name, 'veces': count} for (key, name), count in sorted(names.items()) if count > 1]
    # Two subgrupos of one grupo with one name: neither changes (its text in the grupo's spelling would collide with the other's under
    # unique_part_type_group_name); a person keeps one.
    clashing = {(key, name) for (key, name), count in names.items() if count > 1}
    rows_by_pk = {row['pk']: row for row in rows}
    changes = {pk: change for pk, change in changes.items() if (change[2], rows_by_pk[pk]['name'].lower()) not in clashing}
    # The lines: their text against their subgrupo's planned grupo and name (the facets read the FK from S5).
    planned = {row['pk']: (grupos[assigned[row['pk']]]['nombre'] if row['pk'] in assigned else row['category'], row['name']) for row in rows}
    out_of_step, typed = [], 0
    for pk, sku, part_type_id, category, subcategory in (Part.objects.filter(part_type__isnull=False).order_by('pk')
                                                         .values_list('pk', 'sku', 'part_type_id', 'category', 'subcategory')):
        typed += 1
        if (category, subcategory) != planned.get(part_type_id):
            out_of_step.append({'id': str(pk), 'sku': sku, 'texto': f'{category} / {subcategory}',
                                'subgrupo': ' / '.join(planned.get(part_type_id, ('?', '?')))})
    partial = Part.objects.filter(part_type__isnull=True).exclude(category='', subcategory='').count()
    left = sum(1 for row in rows if row['grupo_id'] is None and row['pk'] not in changes)  # with no grupo after the run
    return {'grupos': grupos, 'changes': changes, 'blank': blank, 'moved': moved, 'clashes': clashes, 'subgrupos': len(rows), 'left': left,
            'lines': {'typed': typed, 'out_of_step': out_of_step, 'partial': partial}}


def kept_grupos(result):
    """The grupos after the run, by key: those that exist and those the run creates."""
    needed = {key for _, _, key in result['changes'].values()}
    return {key: info for key, info in sorted(result['grupos'].items()) if info['id'] is not None or key in needed}


# --- apply ---------------------------------------------------------------------------------------------------------------------------

def write(result, log, run):
    """Create the missing grupos and point each planned subgrupo at its grupo, its text in that grupo's spelling (one UPDATE per
    grupo). Returns {'grupos', 'subgrupos'}."""
    from django.db.models.functions import Lower
    from ..models import Grupo, PartType
    grupos, needed = result['grupos'], {key for _, _, key in result['changes'].values()}
    wanted = {key: Grupo(id=uuid.uuid4(), nombre=grupos[key]['nombre']) for key in sorted(needed) if grupos[key]['id'] is None}
    Grupo.objects.bulk_create(list(wanted.values()), ignore_conflicts=True)  # one the code created meanwhile is kept and read back
    stored = {grupo.nombre.lower(): grupo for grupo in Grupo.objects.alias(key=Lower('nombre')).filter(key__in=sorted(needed))}
    created = {grupo.pk for grupo in Grupo.objects.filter(pk__in=[grupo.pk for grupo in wanted.values()])}
    for key, grupo in sorted(stored.items()):
        if grupo.pk in created:
            log.append({**run, 'model': 'mall.Grupo', 'id': str(grupo.pk), 'action': 'create', 'after': {'nombre': grupo.nombre}})
    groups = defaultdict(list)
    for pk, (name, before, key) in sorted(result['changes'].items(), key=lambda item: str(item[0])):
        grupo = stored[key]
        after = {'grupo_id': str(grupo.pk), 'category': grupo.nombre}
        groups[key].append(pk)
        log.append({**run, 'model': 'mall.PartType', 'id': str(pk), 'name': name, 'action': 'update', 'before': recorded(before),
                    'after': after})
    written = 0
    for key, pks in groups.items():
        written += PartType.objects.filter(pk__in=pks).update(grupo=stored[key], category=stored[key].nombre)
    return {'grupos': len(created), 'subgrupos': written}


# --- rollback ------------------------------------------------------------------------------------------------------------------------

def applied_lines():
    """{(model, id): [lines]} of what the applies on this database did to each row and no rollback has put back yet, oldest first:
    a rollback line drops the updates before it (a created row keeps its create until it is deleted), a delete the whole row."""
    rows, database = defaultdict(list), current_database()
    for line in manifest_lines():
        if line.get('stage') != STAGE or line.get('database') != database or not line.get('model'):
            continue
        key, action = (line['model'], str(line['id'])), line.get('action')
        if action in ('update', 'create'):
            rows[key].append(line)
        elif action == 'rollback':
            rows[key] = [link for link in rows[key] if link['action'] == 'create']
        elif action == 'delete':
            rows.pop(key, None)
    return rows


def unbroken(chain):
    """The newest links of a row's chain of updates with no change from outside between them (each one's before is the after of the
    one before): a rollback puts back the before of the first of them, so a change made between two applies is kept."""
    run = [chain[-1]]
    for link in reversed(chain[:-1]):
        if link['after'] != run[0]['before']:
            break
        run.insert(0, link)
    return run


def undo(log, run):
    """Put back what the applies on this database changed: {'restored', 'deleted', 'changed': [...]}."""
    from ..models import Grupo, PartType
    by_model = defaultdict(dict)
    for (model, pk), chain in applied_lines().items():
        by_model[model][pk] = chain
    restored, deleted, changed = 0, 0, []
    chains = {pk: [link for link in chain if link['action'] == 'update'] for pk, chain in by_model.get('mall.PartType', {}).items()}
    chains = {pk: chain for pk, chain in chains.items() if chain}
    if chains:
        locked = {str(row['pk']): row for row in PartType.objects.select_for_update().filter(pk__in=list(chains)).order_by('pk')
                  .values('pk', 'name', *FIELDS)}
        for pk, chain in sorted(chains.items()):
            row = locked.get(pk)
            if row is None:
                continue
            now, links = recorded({field: row[field] for field in FIELDS}), unbroken(chain)
            if now == links[0]['before']:  # already back
                continue
            if now != chain[-1]['after']:
                changed.append(f"subgrupo {row['name']} ({pk}): cambió después de aplicar")
                continue
            if len(links) < len(chain):
                changed.append(f"subgrupo {row['name']} ({pk}): cambió entre dos aplicaciones; vuelve a como lo dejó ese cambio")
            PartType.objects.filter(pk=row['pk']).update(**links[0]['before'])
            restored += 1
            log.append({**run, 'model': 'mall.PartType', 'id': pk, 'name': row['name'], 'action': 'rollback', 'before': now,
                        'after': links[0]['before']})
    for pk, chain in sorted(by_model.get('mall.Grupo', {}).items()):
        created = next((line for line in chain if line['action'] == 'create'), None)
        grupo = Grupo.objects.select_for_update().filter(pk=pk).first()
        if created is None or grupo is None:
            continue
        if grupo.nombre != created['after']['nombre'] or PartType.objects.filter(grupo=grupo).exists():
            changed.append(f'grupo {grupo.nombre}: cambió o tiene subgrupos desde que se creó, se deja')
            continue
        grupo.delete()
        deleted += 1
        log.append({**run, 'model': 'mall.Grupo', 'id': pk, 'action': 'delete', 'before': created['after']})
    return {'restored': restored, 'deleted': deleted, 'changed': changed}


# --- run -----------------------------------------------------------------------------------------------------------------------------

def summary(result):
    """The counts after the run, as EXPECTED names them."""
    return {'grupos': len(kept_grupos(result)), 'subgrupos': result['subgrupos'], 'sin_grupo': result['left'],
            'lineas': result['lines']['typed']}


def changed_counts(result):
    new = len({key for _, _, key in result['changes'].values() if result['grupos'][key]['id'] is None})
    attached = sum(1 for _, before, _ in result['changes'].values() if before['grupo_id'] is None)
    return {'grupos': new, 'subgrupos': attached, 'texto': len(result['changes']) - attached}


def report(result, totals, counts):
    return {'expected': EXPECTED, 'result': totals, 'changes': counts,
            'grupos': [{'nombre': info['nombre'], 'nuevo': info['id'] is None, 'subgrupos': sorted(info['subgrupos'])}
                       for info in kept_grupos(result).values()],
            'sin_grupo': result['blank'], 'otro_grupo': result['moved'], 'repetidos': result['clashes'],
            'lineas_fuera_de_paso': result['lines']['out_of_step'], 'clasificaciones_parciales': result['lines']['partial']}


def print_summary(stdout, mode, totals, counts, result, writes=None):
    title = {'apply': 'Aplicado', 'dry_run': 'Simulación'}[mode]
    stdout.write(f'{title} · S5 grupos y subgrupos\n')
    stdout.write(f"  {'':22} {'esperado':>9} {'resultado':>10} {'cambian':>8}\n")
    for key, label, change in (('grupos', 'grupos', counts['grupos']), ('subgrupos', 'subgrupos', counts['subgrupos']),
                               ('sin_grupo', 'subgrupos sin grupo', None), ('lineas', 'líneas con subgrupo', None)):
        stdout.write(f"  {label:22} {EXPECTED[key]:>9,} {totals[key]:>10,} {'' if change is None else format(change, ','):>8}\n")
    if counts['texto']:
        stdout.write(f"  Subgrupos cuyo texto toma la grafía de su grupo: {counts['texto']}\n")
    stdout.write('  Grupos: ' + ' · '.join(f"{info['nombre']} {len(info['subgrupos'])}{' (nuevo)' if info['id'] is None else ''}"
                                          for info in kept_grupos(result).values()) + '\n')
    lines = result['lines']
    stdout.write(f"  Líneas cuyo texto difiere de su subgrupo: {len(lines['out_of_step'])}"
                 f"{' (las facetas no cambian)' if not lines['out_of_step'] else ' (las facetas cambiarían: revísalas)'} · "
                 f"clasificaciones parciales (grupo sin subgrupo): {lines['partial']}\n")
    for row in lines['out_of_step'][:20]:
        stdout.write(f"    {row['sku']}: texto {row['texto']}, subgrupo {row['subgrupo']}\n")
    if result['blank']:
        stdout.write(f"  Subgrupos sin texto de grupo ({len(result['blank'])}): necesitan una persona, 0082 no se aplica mientras existan\n")
        for row in result['blank'][:20]:
            stdout.write(f"    {row['name']} ({row['id']})\n")
    if result['moved']:
        stdout.write(f"  Subgrupos cuyo texto nombra otro grupo ({len(result['moved'])}): necesitan una persona, 0082 no se aplica mientras existan\n")
        for row in result['moved'][:20]:
            stdout.write(f"    {row['name']}: grupo {row['grupo']}, texto {row['texto']}\n")
    if result['clashes']:
        stdout.write(f"  Subgrupos repetidos en un grupo ({len(result['clashes'])}): necesitan una persona, 0082 no se aplica mientras existan\n")
        for row in result['clashes'][:20]:
            stdout.write(f"    {row['grupo']} / {row['subgrupo']}: {row['veces']}\n")
    if writes:
        stdout.write(f"  Escrito: {writes['grupos']} grupos · {writes['subgrupos']} subgrupos\n")


def run(*, apply=False, rollback=False, username=None, report_path=None, stdout=None):
    """Plan, and with apply write it, or with rollback undo the applies on this database. None when a write is refused."""
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.utils import timezone
    from ..matching_queue import matching_lock, matching_work
    from ..models import PartType
    stdout = stdout or sys.stdout
    user = None
    if apply or rollback:
        reason = refusal() or manifest_problem()
        if reason:
            stdout.write(reason + '\n')
            return None
        user = get_user_model().objects.filter(username=username or '').first()
        if user is None and (apply or username):  # --rollback names its actor when it can (plan §1.2.3: --rollback alone)
            stdout.write('Indica con --user el usuario que figura como autor del cambio.\n' if not username else
                         f'No existe el usuario {username}.\n')
            return None
        if not expanded():
            stdout.write('Falta la migración 0081_grupo en esta base: aplícala primero (la simulación funciona sin ella).\n')
            return None
        if rollback and tightened():
            stdout.write('0082 está aplicada: un subgrupo no puede volver a quedar sin grupo. Revierte primero 0082 '
                         '(python manage.py migrate mall 0081_grupo) y vuelve a ejecutar.\n')
            return None
    run_line = {'stage': STAGE, 'database': current_database(), 'run': str(uuid.uuid4()), 'actor': user.username if user else '',
                'at': timezone.now().isoformat()}

    def go():
        log = []
        with transaction.atomic():
            if rollback:
                result = {'mode': 'rollback', **undo(log, run_line)}
            else:
                schema = expanded()
                if apply:  # every subgrupo locked, so the plan and its writes see the same rows
                    list(PartType.objects.select_for_update().order_by('pk').values_list('pk', flat=True))
                elif not schema:
                    stdout.write('Sin 0081 en esta base: cada subgrupo se lee sin grupo y no hay grupos todavía.\n')
                planned = plan(schema)
                totals, counts = summary(planned), changed_counts(planned)
                result = {'mode': 'apply' if apply else 'dry_run', **report(planned, totals, counts)}
                writes = None
                if apply:
                    writes = result['written'] = write(planned, log, run_line)
                print_summary(stdout, result['mode'], totals, counts, planned, writes)
                if report_path:
                    with open(report_path, 'w', encoding='utf-8') as handle:
                        json.dump(result, handle, ensure_ascii=False, indent=1, default=str)
                    stdout.write(f'Informe: {report_path}\n')
            if log:  # before the commit: a failed write rolls every row back
                result['manifest'] = str(write_manifest([{**run_line, 'action': 'run', 'mode': result['mode'], 'rows': len(log)}, *log]))
        result['rows_logged'] = len(log)
        if rollback:
            stdout.write(f"Rollback · S5: {result['restored']} subgrupos restaurados · {result['deleted']} grupos borrados · "
                         f"{len(result['changed'])} cambiados después (se dejan)\n")
            for line in result['changed'][:30]:
                stdout.write(f'  {line}\n')
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
    parser = argparse.ArgumentParser(description='S5: la tabla de grupos y el grupo de cada subgrupo.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Calcula los cambios sin escribir nada (también antes de 0081).')
    mode.add_argument('--apply', action='store_true', help='Escribe todo en una transacción (en vivo requiere el catálogo congelado).')
    mode.add_argument('--rollback', action='store_true', help='Deshace con el manifiesto lo aplicado en esta base (requiere el congelamiento en vivo).')
    parser.add_argument('--user', help='Usuario que figura como autor (obligatorio con --apply; opcional con --rollback).')
    parser.add_argument('--report', help='Escribe en esta ruta (JSON) cada grupo con sus subgrupos y lo que necesita una persona.')
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
