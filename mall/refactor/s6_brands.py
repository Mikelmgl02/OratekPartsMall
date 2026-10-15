"""Stage S6 of the inventory refactor, data step 5 (plan §3 S6, A6 deferred from S2a; I12 from S6): the brands References carry.

    python -m mall.refactor.s6_brands --dry-run|--apply --user USERNAME|--rollback [--report PATH] [--json]

1. Alias spellings: a Reference whose brand is a BrandAlias (TOY) takes its Brand.nombre (TOYOTA). Appendix C's I12 allows only a
   nombre from S6. Live, 2026-10-14: none.
2. Former merge aliases: a brandless public Reference that is the codigo of a line merged into its line (S2a kept their brand '': the
   2,472 merge aliases) and whose code ends in an identified brand TAG takes that brand (58411-07000-KSM → KSM, tipo aftermarket for a
   brand of parts, otro otherwise: a former SKU is never an OEM number). S2a left them brandless because services.matching_part and
   MatchIndex matched brand in [x, ''] for brandless GRP variants; from S6 a brandless item joins a branded Reference of its clave
   (mall.reference_matching rules 1 and 2), so the brand is safe now. The TAG is read as the S0.8 audit reads it: the trailing TAGs
   stripped while each is auto-eligible (mall.catalog_suffixes.auto_eligible), the outermost brand or house-brand TAG naming a Brand.
   A brand narrows the brandless reservation, so it never makes a new conflict.
A row whose new key its line already holds is listed and left. It runs before S6 lands (old code reads the brand as any other) and
again after as a catch-up. Every changed row goes, with its brand and tipo before, to output/refactor/s6/brands_manifest.jsonl;
--rollback gives them back. On the live database --apply and --rollback need the catalog freeze (mall.refactor.guard).
"""
import argparse
import json
import os
import sys
import uuid
from collections import Counter, defaultdict

from .s6_cases import recorded

STAGE = 's6'
MANIFEST = 'brands_manifest.jsonl'
# The plan counted (2026-10-13) 1,188 of the 2,472 merge aliases with a TAG brand. The live dry run 2026-10-14 finds 2,406 merge
# aliases, 763 of them with a TAG that names a Brand (the rest name none the brand table has, or end in no brand TAG), and no Reference
# under an alias spelling.
EXPECTED = {'alias': 0, 'alias_de_union': 2406, 'marca_tag': 763}
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
        return f"El manifiesto {manifest_path()} tiene líneas de otra base ({', '.join(others)}): muévelo antes de aplicar o deshacer en esta."
    return None


def write_manifest(lines):
    from .s6_cases import write_manifest as write
    return write(lines, manifest_path())  # the same atomic append, to this stage's own file


def tag_brand_of(code, lookups):
    """The brand an identified brand or house-brand TAG at the end of a code names (the TAGs stripped while each is auto-eligible:
    mall.reference_matching.strip_eligible), or ''."""
    from ..reference_matching import strip_eligible, tag_brand
    base, entries = strip_eligible(code, lookups)
    return tag_brand(entries, lookups) if base else ''


def plan():
    """The brand changes, read-only: [(Reference, brand, tipo, why)], the counts and what is listed."""
    from django.db.models import Exists, OuterRef
    from django.db.models.functions import Upper
    from .. import references
    from ..brand_models import BrandAlias
    from ..models import Part, Reference
    from ..reference_matching import Lookups
    from ..variants import new_reference_tipo
    lookups, counts, listed, changes = Lookups(), Counter(), defaultdict(list), []
    aliases = dict(BrandAlias.objects.select_related('brand').values_list('alias', 'brand__nombre'))
    for ref in Reference.all_objects.filter(brand__in=list(aliases), estado__in=references.LIVE).order_by('pk'):
        if aliases[ref.brand] != ref.brand:
            changes.append((ref, aliases[ref.brand], ref.tipo, 'alias'))
            counts['alias'] += 1
    merged = Part._base_manager.filter(merged_into_id=OuterRef('part_id')).annotate(codigo=Upper('sku')).filter(codigo=Upper(OuterRef('code')))
    rows = (Reference.all_objects.filter(brand='', supplier__isnull=True, is_main=False, estado__in=references.LIVE,
                                         part__merged_into__isnull=True).filter(Exists(merged)).order_by('pk'))
    for ref in rows.iterator(chunk_size=500):
        counts['alias_de_union'] += 1
        brand = tag_brand_of(ref.code, lookups)
        if not brand:
            continue
        changes.append((ref, brand, new_reference_tipo(brand) if ref.tipo != 'oem' else ref.tipo, 'marca_tag'))
        counts['marca_tag'] += 1
    return {'changes': changes, 'counts': counts, 'listed': listed}


def write(planned, log, run, user):
    from .. import references
    written = Counter()
    for ref, brand, tipo, why in planned['changes']:
        fresh = type(ref).all_objects.select_for_update().filter(pk=ref.pk).first()
        if fresh is None or fresh.brand == brand:
            continue
        before = {'brand': fresh.brand, 'tipo': fresh.tipo, 'ref_type': fresh.ref_type}
        try:
            references.update(fresh, brand=brand, tipo=tipo, actor=user)
        except references.ReferenceConflict as error:
            planned['listed']['no_cambiadas'].append(f'{fresh.code} → {brand}: {error.message}')
            continue
        log.append({**run, 'model': 'mall.Reference', 'id': str(fresh.pk), 'action': 'update', 'why': why, 'before': recorded(before),
                    'after': {'brand': brand, 'tipo': tipo}})
        written[why] += 1
    return dict(written)


def undo(log, run):
    from .. import references
    from ..models import Reference
    result = Counter()
    for line in reversed([line for line in manifest_lines() if line.get('action') == 'update']):
        ref = Reference.all_objects.filter(pk=line['id']).first()
        if ref is None or ref.brand != line['after']['brand']:
            result['cambiadas_despues'] += 1
            continue
        try:
            references.update(ref, brand=line['before']['brand'], tipo=line['before']['tipo'], actor=None)
        except references.ReferenceConflict:
            result['no_devueltas'] += 1
            continue
        result['devueltas'] += 1
    log.append({**run, 'model': 'manifest', 'action': 'rolled_back'})
    return dict(result)


def run(*, apply=False, rollback=False, username=None, report_path=None, stdout=None):
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.utils import timezone
    from ..matching_queue import matching_lock, matching_work
    stdout = stdout or sys.stdout
    user = None
    if apply or rollback:
        from .guard import refusal
        reason = refusal(True) or manifest_problem()
        if reason:
            stdout.write(reason + '\n')
            return None
        user = get_user_model().objects.filter(username=username or '').first()
        if apply and user is None:
            stdout.write('Indica con --user el usuario que figura como autor del cambio.\n' if not username else f'No existe el usuario {username}.\n')
            return None
    run_line = {'stage': 's6_brands', 'database': current_database(), 'run': str(uuid.uuid4()), 'actor': user.username if user else '',
                'at': timezone.now().isoformat()}

    def go():
        log = []
        with transaction.atomic():
            if rollback:
                result = {'mode': 'rollback', **undo(log, run_line)}
            else:
                planned = plan()
                result = {'mode': 'apply' if apply else 'dry_run', 'expected': EXPECTED, 'result': dict(planned['counts'])}
                if apply:
                    result['written'] = write(planned, log, run_line, user)
                result['listed'] = {key: rows for key, rows in planned['listed'].items()}
                stdout.write(f"{'Aplicado' if apply else 'Simulación'} · S6 marcas de las referencias\n")
                for key, label in (('alias', 'alias → nombre de la marca'), ('alias_de_union', 'alias de unión sin marca'),
                                   ('marca_tag', 'toman la marca de su sufijo')):
                    expected = EXPECTED.get(key)
                    stdout.write(f"  {label:40} {'' if expected is None else f'{expected:,}':>9} {planned['counts'].get(key, 0):>10,}\n")
                for ref, brand, _, why in planned['changes'][:15]:
                    stdout.write(f'    {ref.code}: {ref.brand or "sin marca"} → {brand} ({why})\n')
                for key, rows in sorted(planned['listed'].items()):
                    stdout.write(f'  {key.replace("_", " ").capitalize()}: {len(rows)}\n')
                    for row in rows[:15]:
                        stdout.write(f'    {row}\n')
                if report_path:
                    with open(report_path, 'w', encoding='utf-8') as handle:
                        json.dump({**result, 'changes': [{'id': ref.pk, 'code': ref.code, 'brand': brand, 'tipo': tipo, 'why': why}
                                                         for ref, brand, tipo, why in planned['changes']]},
                                  handle, ensure_ascii=False, indent=1, default=str)
            if log:
                result['manifest'] = str(write_manifest([{**run_line, 'action': 'run', 'mode': result['mode'], 'rows': len(log)}, *log]))
        if rollback:
            stdout.write('Rollback · S6 marcas: ' + ' · '.join(f'{key} {value:,}' for key, value in sorted(result.items())
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
    parser = argparse.ArgumentParser(description='S6: las referencias toman el nombre canónico de su marca y los alias de unión su marca.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Calcula los cambios sin escribir nada.')
    mode.add_argument('--apply', action='store_true', help='Escribe todo en una transacción (en vivo requiere el catálogo congelado).')
    mode.add_argument('--rollback', action='store_true', help='Deshace con el manifiesto lo aplicado en esta base.')
    parser.add_argument('--user', help='Usuario que figura como autor (obligatorio con --apply).')
    parser.add_argument('--report', help='Escribe en esta ruta (JSON) las cuentas y los cambios.')
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
