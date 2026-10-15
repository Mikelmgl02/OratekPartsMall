"""Replace what one supplier catalog filed with a corrected extract of it (a parser fix), then import the corrected extract.

    python -m mall.supplier_catalog_replace --dry-run|--apply FILE [FILE ...] [--report DIR] [--user USERNAME]

FILE is a normalized extract (mall.supplier_catalog_import) of a catalog imported before, under the same title and year, so the
citations match, and with the catalog key of the extract it corrects. A catalog imported from several extracts under one citation
(ASVA CV and its OEM-only extract) is replaced with all of them at once, read as one and given in their original import order (a
number none of them printed before takes the first one's key; one already filed keeps its own). Replacing only some of them would
take the numbers only the others print for misreadings, so a run that leaves out an extract key the catalog's sources name is
refused, and so are extracts of different catalogs. Only rows that cite that catalog change, and each ends as an import of FILE
alone would leave it:
- Aftermarket codes under an OEM number (OEMCrossReference): a code FILE does not file under the number loses the catalog's citations;
  with none left it is retired the way Biblioteca OEM's Retirar does (OEMCrossReferenceRetired, by --user), so no import files it
  again. A code FILE files too keeps its other citations and takes FILE's pages.
- OEM numbers (from S4 the library's References): a number FILE does not print loses the catalog's source (retired, never deleted).
  With no source left, when nothing else holds it (no line keeps it as its own code, no tie of another source or of a retired SKU,
  and nobody disputed, annotated, chained or edited it) it is dropped the way S4 drops a number (mall.references.drop_library_number):
  it ends as the main of a line of its own in referencia, rejected (rechazada, bad_data), the number waiting there in disputa and
  tagged (it holds no key; a line's main is never retired, I1), and its codes are deleted with it (no tombstone). A catalog that prints
  it again brings the number and its line back and files its codes again. A number FILE prints takes FILE's source detail (its items and pages; the import keeps the
  first sighting's).
  When FILE turns it into a drive-shaft number listed for a CV component, or back, its description and part type follow (SEMIEJE
  (EJE COMPLETO) / SEMIEJES, or the component's), each only while it still holds what the catalog gave it.
- Catalog ties of active SKUs (PartOEMLink, source catalog): a tie FILE does not make loses the catalog's citations, and goes with none
  left; a kept one takes FILE's citations.
- Then the import files FILE's numbers, codes and ties. It writes no company alternos, OEM alternos, measurements or subgrupos and
  refreshes no name links: mall.supplier_catalog_import and mall.link_oem_numbers do those.
The report directory gets one CSV per table with every change, and alternos-review.csv: the company alternos citing the catalog that
FILE does not back on their SKU (the SKU matches no item of FILE that prints the code), for Alternos → Por revisar; nothing changes
them. An apply takes the matching worker's lock and writes in one transaction; back up the database first. Re-running changes nothing.
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict

# The source detail keys write_oem_table writes; any other key on the source is kept.
IMPORTER_DETAIL = ('catalog', 'brand', 'brand_codes', 'pages', 'product_types', 'assembly_number_listed_for_component')


def cites(text, prefix):
    return text == prefix or text.startswith(f'{prefix} · ')


def split(citations, prefix):
    """(this catalog's citations, the other ones)."""
    return [c for c in citations or [] if cites(c, prefix)], [c for c in citations or [] if not cites(c, prefix)]


def source_detail(job, row):
    """The detail write_oem_table gives the catalog's source of one number."""
    return {'catalog': row['catalog'], 'brand': job.brand, 'brand_codes': sorted(row['brand_codes']),
            'pages': sorted(p for p in row['pages'] if p), 'product_types': sorted(row['product_types']),
            **({'assembly_number_listed_for_component': True} if row['assembly'] else {})}


def filed_codes(job):
    """{((manufacturer, number), brand, compact code): {'code', 'citations'}}: what write_cross_references files, keyed as stored."""
    from .oem_reference_models import citation_list, manufacturer_name
    out = {}
    for (manufacturer, number), row in job.oem_rows().items():
        for (brand, code), cited in row['cross'].items():
            brand, code = manufacturer_name(brand), ' '.join(str(code or '').upper().split())
            key = job.filed_key(brand, code)  # the catalog's own code is filed without a digit too
            if not brand or len(brand) > 120 or not key or len(code) > 120:
                continue
            entry = out.setdefault(((manufacturer_name(manufacturer), number), brand, key), {'code': code, 'citations': []})
            entry['citations'] = citation_list(entry['citations'], *sorted(cited))
    return out


def held(ref, prefix):
    """Something besides the catalog keeps the number (a library Reference) once the catalog's source goes: a line holds it as its own
    code (its main, a code not placed there as library data by S4, or one whose clave the line keeps without a brand), supplier items
    hang from it, a person disputed, annotated, chained or edited it, another number names it as its replacement, or a tie of another
    source or of a retired SKU points at it. A number S4 put on a SKU line as library data (detail.origen biblioteca: the name links'
    numbers) is not held by that line: dropped, it leaves it as the OEM table's number left with its name link."""
    from . import references
    from .lines import on_storefront
    from .models import Reference
    if ref.part.estado != 'referencia':
        if ref.is_main or (ref.detail or {}).get('origen') != references.LIBRARY_ORIGIN or references.has_variants(ref):
            return True
        if Reference.objects.filter(part_id=ref.part_id, clave=ref.clave, brand='', supplier__isnull=True,
                                    estado__in=references.EXCLUSIVE).exclude(pk=ref.pk).exists():
            return True  # the line keeps the number under its own brandless code
    if ref.estado == 'disputa' or ref.notes.strip() or ref.reemplazado_por_id or ref.created_by_id or (ref.detail or {}).get('updated_by'):
        return True
    if ref.reemplaza.exists():
        return True
    for link in ref.part_links.exclude(source='sku_name').select_related('part'):
        if split(link.citations, prefix)[1] or not on_storefront(link.part):
            return True
    return False


def catalog_sources(citation_text):
    """The live library sources of one supplier catalog (ReferenceSource kind catalogo, the library's aftermarket_catalog)."""
    from django.db.models import Q
    from .models import ReferenceSource
    return (ReferenceSource.objects.filter(kind='catalogo', citation=citation_text, retirada=False)
            .filter(Q(detail__legacy_kind='aftermarket_catalog') | ~Q(detail__has_key='legacy_kind')))


def number_key(ref):
    """(manufacturer, compact number) of a library Reference: how the import keys the numbers it files."""
    from .oem_reference_models import compact
    return ref.brand, compact(ref.code)


def extract_keys(data):
    """The catalog keys of the extracts given (merged marks each item with its own)."""
    return {data['catalog']['key']} | {item['_extract'] for item in data['items'] if item.get('_extract')}


def missing_extracts(data):
    """The extract keys the catalog's sources name that none of the extracts given has: replacing without them would take every
    number only they print."""
    from .supplier_catalog_import import citation
    named = {(detail or {}).get('catalog') for detail in catalog_sources(citation(data['catalog'])).values_list('detail', flat=True)}
    return sorted(key for key in named - extract_keys(data) if key)


def retype(ref, source, row):
    """{field: value} the number takes when FILE turns its drive-shaft flag (assembly_number_listed_for_component) on or off: the
    description and part type an import of FILE gives it, each only while it still holds what the catalog gave it before."""
    from .supplier_catalog_import import ASSEMBLY_TEXT
    was = bool((source.detail or {}).get('assembly_number_listed_for_component'))
    if was == bool(row['assembly']):
        return {}
    before = ASSEMBLY_TEXT if was else (row['component'][0][:300], row['component'][1])
    after = (row['description'][:300], row['part_type'])
    detail = ref.detail or {}
    return {field: new for field, old, new in zip(('description', 'part_type'), before, after) if detail.get(field, '') == old and old != new}


def plan(data):
    """Everything an apply would change, decided read-only: the import job for FILE and the changes per table."""
    from .catalog_suffixes import suffix_table
    from .lines import catalog_q, storefront_q
    from .models import PartCode
    from .oem_finder import OEMFinder, load_snapshot
    from .oem_reference_models import OEMCrossReference, PartOEMLink, citation_list, compact, manufacturer_name
    from .supplier_catalog_import import CatalogImport, build_indexes, catalog_citations, citation
    finder = OEMFinder(load_snapshot(), suffix_table())
    job = CatalogImport(data, finder, build_indexes(finder, catalog_citations([data])))
    job.plan()
    job.write_links(False, codes=False)  # collects the catalog ties FILE makes
    prefix = citation(job.catalog)
    numbers = {(manufacturer_name(manufacturer), number): row for (manufacturer, number), row in job.oem_rows().items()}
    filed = filed_codes(job)
    ties = {(str(pid), (manufacturer_name(manufacturer), number)): sorted(cited) for (pid, (manufacturer, number)), cited in job.catalog_links.items()}
    changes = {'numbers': [], 'codes': [], 'ties': [], 'alternos': []}
    extracts = extract_keys(data)

    gone = set()
    for source in catalog_sources(prefix).select_related('reference', 'reference__part').order_by('reference__brand', 'reference__clave'):
        ref = source.reference
        row = numbers.get(number_key(ref))
        if row is not None:
            detail = {**{k: v for k, v in (source.detail or {}).items() if k not in IMPORTER_DETAIL}, **source_detail(job, row)}
            if (source.detail or {}).get('catalog') in extracts:  # the extract that filed it first, whatever order they come in
                detail['catalog'] = source.detail['catalog']
            if detail != source.detail:
                changes['numbers'].append({'action': 'detail', 'ref': ref, 'source': source, 'detail': detail})
            fields = retype(ref, source, row)
            if fields:
                changes['numbers'].append({'action': 'retype', 'ref': ref, 'source': source, 'fields': fields,
                                           'before': {field: (ref.detail or {}).get(field, '') for field in fields}})
        elif ref.sources.filter(retirada=False).exclude(pk=source.pk).exists() or held(ref, prefix):
            changes['numbers'].append({'action': 'drop_source', 'ref': ref, 'source': source})
        else:
            gone.add(ref.pk)
            changes['numbers'].append({'action': 'delete', 'ref': ref, 'source': source,  # retired: its line in referencia is rejected
                                       'name_links': ref.part_links.filter(source='sku_name').count()})

    for row in (OEMCrossReference.objects.filter(citations__icontains=prefix, reference__isnull=False).select_related('reference')
                .order_by('reference__brand', 'reference__clave', 'brand', 'number')):
        mine, others = split(row.citations, prefix)
        if not mine:
            continue
        key = (number_key(row.reference), row.brand, row.number)
        if key in filed:
            new = citation_list(others, *filed[key]['citations'])
            if sorted(new) != sorted(row.citations):
                changes['codes'].append({'action': 'recite', 'row': row, 'citations': new})
        elif row.reference_id in gone:
            changes['codes'].append({'action': 'with_number', 'row': row, 'citations': []})
        elif others:
            changes['codes'].append({'action': 'drop_citation', 'row': row, 'citations': others})
        else:
            changes['codes'].append({'action': 'retire', 'row': row, 'citations': []})

    for link in (PartOEMLink.objects.filter(storefront_q('part__'), source='catalog', citations__icontains=prefix, reference__isnull=False)
                 .select_related('reference', 'part').order_by('part__sku', 'reference__brand', 'reference__clave')):
        mine, others = split(link.citations, prefix)
        if not mine:
            continue
        key = (str(link.part_id), number_key(link.reference))
        if link.reference_id in gone:
            changes['ties'].append({'action': 'with_number', 'link': link, 'citations': []})
        elif key in ties:
            new = citation_list(others, *ties[key])
            if sorted(new) != sorted(link.citations):
                changes['ties'].append({'action': 'recite', 'link': link, 'citations': new})
        elif others:
            changes['ties'].append({'action': 'drop_citation', 'link': link, 'citations': others})
        else:
            changes['ties'].append({'action': 'remove', 'link': link, 'citations': []})

    # Company alternos the catalog wrote on a SKU that FILE does not back there: the SKU matches no item of FILE printing the code.
    items_of = defaultdict(set)
    for pid, n, _ in job.links:
        items_of[str(pid)].add(n)
    printed_by = defaultdict(set)
    for item in job.items:
        if (item.get('brand_code') or '').strip():
            printed_by[(manufacturer_name(job.brand), compact(item['brand_code']))].add(item['brand_code'])
            for xr in item.get('cross_refs', []):
                printed_by[(manufacturer_name(xr.get('brand') or ''), compact(xr.get('code') or ''))].add(item['brand_code'])
    for code in (PartCode.objects.filter(catalog_q(prefix='part__'), ref_type='company', reference_source__icontains=prefix, is_main=False)
                 .select_related('part').order_by('part__sku', 'brand', 'code')):
        key = (manufacturer_name(code.brand), compact(code.code))
        mine = sorted(job.items[n]['brand_code'] for n in items_of.get(str(code.part_id), ()))
        if not printed_by.get(key, set()) & set(mine):
            changes['alternos'].append({'code': code, 'sku_items': mine, 'printed_by': sorted(printed_by.get(key, ()))})
    return job, changes


def apply_changes(job, changes, actor):
    from django.utils import timezone
    from . import references
    from .models import ReferenceSource
    from .oem_links import chunks
    from .oem_reference_models import OEMCrossReference, OEMCrossReferenceRetired, PartOEMLink
    from .supplier_catalog_import import citation
    now = timezone.now()
    for change in changes['numbers']:
        ref = change['ref']
        if change['action'] == 'detail':
            change['source'].detail = change['detail']
            change['source'].save(update_fields=['detail'])
        elif change['action'] == 'retype':  # what the import writes: no one's edit
            ref = references.update(ref, detail={**(ref.detail or {}), **change['fields']})
            if ref.is_main and ref.part.estado == 'referencia':
                references.fill_line_from_library(ref.part, ref)
        elif change['action'] in ('drop_source', 'delete'):
            ReferenceSource.objects.filter(pk=change['source'].pk).update(retirada=True, retired_by=actor, retired_at=now)
            if change['action'] == 'drop_source':
                references.refresh_library_estado(ref, actor)
    gone = [change['ref'] for change in changes['numbers'] if change['action'] == 'delete']
    retired = [change['row'] for change in changes['codes'] if change['action'] == 'retire']
    OEMCrossReferenceRetired.objects.bulk_create([OEMCrossReferenceRetired(reference_id=row.reference_id, brand=row.brand, number=row.number, code=row.code,
                                                                           citations=row.citations, retired_by=actor) for row in retired],
                                                 batch_size=1000, ignore_conflicts=True)
    # A dropped number's codes go with it, without a tombstone (they went with the OEM table's number before S4), so a catalog that
    # prints the number again files them again.
    dropped = [change['row'].pk for change in changes['codes'] if change['action'] == 'with_number']
    # The lines that reached the codes and links that go stop finding them (their Search Documents' reach, S7).
    reach_refs = {row.reference_id for row in retired} | {change['row'].reference_id for change in changes['codes'] if change['action'] == 'with_number'}
    reach_lines = set()
    for chunk in chunks([row.pk for row in retired] + dropped):
        OEMCrossReference.objects.filter(pk__in=chunk).delete()
    # S9, rule 3f: the catalogo sources the retired citations gave the codes' References are retired (an inferido one left with none goes
    # to disputa, a merge they backed gets a case), before the rows change and while a dropped number still sits on its line.
    from .merge_pass import retire_citation
    for change in changes['codes']:
        row = change['row']
        if change['action'] in ('retire', 'with_number'):
            retire_citation(row.reference, row.brand, row.code, row.citations, actor=actor, exclude_row=row)
        elif change['action'] == 'drop_citation':
            retire_citation(row.reference, row.brand, row.code, [c for c in row.citations if c not in change['citations']], actor=actor,
                            exclude_row=row)
    for change in changes['codes']:
        if change['action'] in ('recite', 'drop_citation'):
            change['row'].citations = change['citations']
            change['row'].save(update_fields=['citations', 'updated_at'])
    removed = [change['link'].pk for change in changes['ties'] if change['action'] in ('remove', 'with_number')]
    removed += list(PartOEMLink.objects.filter(source='sku_name', reference__in=[ref.pk for ref in gone]).values_list('pk', flat=True))
    for chunk in chunks(removed):
        reach_lines |= set(PartOEMLink.objects.filter(pk__in=chunk).values_list('part_id', flat=True))
        PartOEMLink.objects.filter(pk__in=chunk).delete()
    from .search_document import refresh_reach
    refresh_reach(reach_refs, reach_lines)
    for change in changes['ties']:
        if change['action'] in ('recite', 'drop_citation'):
            change['link'].citations = change['citations']
            change['link'].save(update_fields=['citations'])
    note = f'El catálogo {job.report["title"]} ya no lo imprime (extracto corregido).'[:500]
    for ref in gone:  # S4: never deleted; each ends as the main of a rejected line of its own, in disputa, until a catalog prints it again
        references.drop_library_number(ref, note, actor=actor, catalog=citation(job.catalog))


def write_report(directory, changes):
    os.makedirs(directory, exist_ok=True)

    def table(name, header, rows):
        with open(os.path.join(directory, name), 'w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)

    table('numbers.csv', ['accion', 'fabricante', 'numero', 'id_numero', 'detalle_antes', 'detalle_despues'],
          [[c['action'], c['ref'].brand, number_key(c['ref'])[1], c['ref'].uid, json.dumps(c.get('before', c['source'].detail), ensure_ascii=False),
            json.dumps(c.get('detail', c.get('fields', {})), ensure_ascii=False)] for c in changes['numbers']])
    table('codes.csv', ['accion', 'fabricante', 'numero', 'marca', 'codigo', 'id_equivalencia', 'citas_antes', 'citas_despues'],
          [[c['action'], c['row'].reference.brand, number_key(c['row'].reference)[1], c['row'].brand, c['row'].code, c['row'].pk,
            ' | '.join(c['row'].citations), ' | '.join(c['citations'])] for c in changes['codes']])
    table('ties.csv', ['accion', 'sku', 'fabricante', 'numero', 'id_vinculo', 'citas_antes', 'citas_despues'],
          [[c['action'], c['link'].part.sku, c['link'].reference.brand, number_key(c['link'].reference)[1], c['link'].pk,
            ' | '.join(c['link'].citations), ' | '.join(c['citations'])] for c in changes['ties']])
    table('alternos-review.csv', ['sku', 'descripcion', 'marca', 'codigo', 'fuente', 'articulos_del_sku', 'articulos_que_lo_imprimen', 'id_alterno', 'id_sku'],
          [[c['code'].part.sku, c['code'].part.description, c['code'].brand, c['code'].code, c['code'].reference_source,
            ' '.join(c['sku_items']), ' '.join(c['printed_by']), c['code'].pk, c['code'].part_id] for c in changes['alternos']])


def merged(datasets):
    """Several extracts of one catalog (same citation) read as one: the first one's catalog and every item, each item marked with the
    extract it comes from (the source detail's catalog key, as the first import of each number left it)."""
    from .supplier_catalog_import import citation
    if len({citation(data['catalog']) for data in datasets}) > 1:
        raise ValueError('Los extractos deben ser del mismo catálogo (mismo título y año).')
    if len(datasets) == 1:
        return datasets[0]
    items = [{**item, '_extract': data['catalog']['key']} for data in datasets for item in data['items']]
    return {**datasets[0], 'items': items}


def run(path, *, apply=False, report_dir=None, username=None, stdout=None):
    """path: one extract, or a list of the extracts of one catalog. None when the extracts are refused or the lock is busy."""
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from .oem_finder import matching_lock
    stdout = stdout or sys.stdout
    paths = [path] if isinstance(path, (str, os.PathLike)) else list(path)
    try:
        data = merged([json.load(open(p, encoding='utf-8')) for p in paths])
    except ValueError as error:
        stdout.write(f'{error} No se cambia nada.\n')
        return None
    missing = missing_extracts(data)
    if missing:
        stdout.write(f"El catálogo también se importó de {', '.join(missing)}: pasa todos sus extractos juntos, en el orden en que se "
                     'importaron. Sin ellos se borrarían los números que solo esos imprimen. No se cambia nada.\n')
        return None
    actor = get_user_model().objects.get(username=username) if username else None

    def execute():
        job, changes = plan(data)
        if report_dir:
            write_report(report_dir, changes)
        with transaction.atomic():
            if apply:
                apply_changes(job, changes, actor)
            job.write_oem_table(apply)
            job.write_cross_references(apply)
            job.write_catalog_links(apply)
        counts = {table: dict(Counter(c['action'] for c in changes[table])) for table in ('numbers', 'codes', 'ties')}
        imported = {k: job.report['counts'].get(k, 0) for k in ('oem_numbers', 'oem_new', 'cross_reference_new', 'cross_reference_present',
                                                                  'catalog_link_new', 'catalog_link_present')}
        result = {'mode': 'apply' if apply else 'dry_run', 'catalog': job.catalog['key'], 'changes': counts, 'import': imported,
                  'alternos_to_review': len(changes['alternos']), 'alternos_skus': len({c['code'].part_id for c in changes['alternos']})}
        n, c, t = counts['numbers'], counts['codes'], counts['ties']
        stdout.write(f"{'Aplicado' if apply else 'Simulación'} · {job.report['title']} · números: {n.get('delete', 0)} borrados, "
                     f"{n.get('drop_source', 0)} sin la fuente del catálogo, {n.get('detail', 0)} con detalle corregido, "
                     f"{n.get('retype', 0)} con tipo corregido, {imported['oem_new']} nuevos · "
                     f"equivalencias: {c.get('retire', 0)} retiradas, {c.get('with_number', 0)} con su número, {c.get('drop_citation', 0)} sin la cita, "
                     f"{c.get('recite', 0)} con citas corregidas, {imported['cross_reference_new']} nuevas · vínculos: {t.get('remove', 0)} retirados, "
                     f"{t.get('drop_citation', 0)} sin la cita, {t.get('recite', 0)} con citas corregidas, {imported['catalog_link_new']} nuevos · "
                     f"alternos por revisar: {result['alternos_to_review']} en {result['alternos_skus']} SKU\n")
        return result

    if not apply:
        return execute()
    with matching_lock() as acquired:
        if not acquired:
            stdout.write('El análisis de coincidencias está en curso; vuelve a intentarlo cuando termine.\n')
            return None
        return execute()


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Reemplaza lo que un catálogo de proveedor registró por un extracto corregido del mismo catálogo.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Calcula todo sin escribir nada.')
    mode.add_argument('--apply', action='store_true', help='Retira lo que el extracto corregido no registra e importa el extracto.')
    parser.add_argument('file', nargs='+', help='Extracto JSON normalizado y corregido; si el catálogo se importó de varios extractos, '
                                                'todos, en el orden en que se importaron.')
    parser.add_argument('--report', help='Carpeta donde escribir los CSV del plan.')
    parser.add_argument('--user', help='Usuario que figura como autor de las equivalencias retiradas.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .refactor.guard import require_freeze
    require_freeze(options.apply)
    result = run(options.file, apply=options.apply, report_dir=options.report, username=options.user, stdout=stdout)
    if result is not None:
        (stdout or sys.stdout).write(json.dumps(result, ensure_ascii=False) + '\n')
    sys.exit(0 if result is not None else 1)


if __name__ == '__main__':
    main()
