"""Replace what one supplier catalog filed with a corrected extract of it (a parser fix), then import the corrected extract.

    python -m mall.supplier_catalog_replace --dry-run|--apply FILE [--report DIR] [--user USERNAME]

FILE is a normalized extract (mall.supplier_catalog_import) of a catalog imported before, under the same title and year, so the
citations match. Only rows that cite that catalog change, and each ends as an import of FILE alone would leave it:
- Aftermarket codes under an OEM number (OEMCrossReference): a code FILE does not file under the number loses the catalog's citations;
  with none left it is retired the way Biblioteca OEM's Retirar does (OEMCrossReferenceRetired, by --user), so no import files it
  again. A code FILE files too keeps its other citations and takes FILE's pages.
- OEM numbers: a number FILE does not print loses the catalog's source. With no source left it is deleted, its codes included, when
  nothing else holds it: no alterno points at it, no tie of another source or of a retired SKU, and nobody disputed, annotated,
  chained or edited it. A number FILE prints takes FILE's source detail (its items and pages; the import keeps the first sighting's).
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
import re
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
    return {'catalog': job.catalog['key'], 'brand': job.brand, 'brand_codes': sorted(row['brand_codes']),
            'pages': sorted(p for p in row['pages'] if p), 'product_types': sorted(row['product_types']),
            **({'assembly_number_listed_for_component': True} if row['assembly'] else {})}


def filed_codes(job):
    """{((manufacturer, number), brand, compact code): {'code', 'citations'}}: what write_cross_references files, keyed as stored."""
    from .oem_reference_models import citation_list, compact, manufacturer_name
    out = {}
    for (manufacturer, number), row in job.oem_rows().items():
        for (brand, code), cited in row['cross'].items():
            brand, code = manufacturer_name(brand), ' '.join(str(code or '').upper().split())
            key = compact(code)
            if not brand or len(brand) > 120 or not key or len(code) > 120 or not re.search(r'\d', key):
                continue
            entry = out.setdefault(((manufacturer_name(manufacturer), number), brand, key), {'code': code, 'citations': []})
            entry['citations'] = citation_list(entry['citations'], *sorted(cited))
    return out


def held(ref, prefix):
    """Something besides the catalog keeps the number once the catalog's source goes."""
    if ref.status == 'disputed' or ref.notes.strip() or ref.superseded_by_id or ref.created_by_id or ref.updated_by_id:
        return True
    if ref.supersedes.exists() or ref.alternos.exists():
        return True
    for link in ref.part_links.exclude(source='sku_name').select_related('part'):
        if split(link.citations, prefix)[1] or not link.part.active or link.part.merged_into_id:
            return True
    return False


def plan(data):
    """Everything an apply would change, decided read-only: the import job for FILE and the changes per table."""
    from .catalog_suffixes import suffix_table
    from .models import PartCode
    from .oem_finder import OEMFinder, load_snapshot
    from .oem_reference_models import OEMCrossReference, OEMReferenceSource, PartOEMLink, citation_list, compact, manufacturer_name
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

    gone = set()
    for source in OEMReferenceSource.objects.filter(kind='aftermarket_catalog', citation=prefix).select_related('reference').order_by('reference__manufacturer', 'reference__code'):
        ref = source.reference
        row = numbers.get((ref.manufacturer, ref.code))
        if row is not None:
            detail = {**{k: v for k, v in (source.detail or {}).items() if k not in IMPORTER_DETAIL}, **source_detail(job, row)}
            if detail != source.detail:
                changes['numbers'].append({'action': 'detail', 'ref': ref, 'source': source, 'detail': detail})
        elif ref.sources.exclude(pk=source.pk).exists() or held(ref, prefix):
            changes['numbers'].append({'action': 'drop_source', 'ref': ref, 'source': source})
        else:
            gone.add(ref.pk)
            changes['numbers'].append({'action': 'delete', 'ref': ref, 'source': source,
                                       'name_links': ref.part_links.filter(source='sku_name').count()})

    for row in OEMCrossReference.objects.filter(citations__icontains=prefix).select_related('reference').order_by('reference__manufacturer', 'reference__code', 'brand', 'number'):
        mine, others = split(row.citations, prefix)
        if not mine:
            continue
        key = ((row.reference.manufacturer, row.reference.code), row.brand, row.number)
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

    for link in (PartOEMLink.objects.filter(source='catalog', citations__icontains=prefix, part__active=True, part__merged_into__isnull=True)
                 .select_related('reference', 'part').order_by('part__sku', 'reference__manufacturer', 'reference__code')):
        mine, others = split(link.citations, prefix)
        if not mine:
            continue
        key = (str(link.part_id), (link.reference.manufacturer, link.reference.code))
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
    for code in (PartCode.objects.filter(ref_type='company', reference_source__icontains=prefix, part__merged_into__isnull=True)
                 .select_related('part').order_by('part__sku', 'brand', 'code')):
        key = (manufacturer_name(code.brand), compact(code.code))
        mine = sorted(job.items[n]['brand_code'] for n in items_of.get(str(code.part_id), ()))
        if not printed_by.get(key, set()) & set(mine):
            changes['alternos'].append({'code': code, 'sku_items': mine, 'printed_by': sorted(printed_by.get(key, ()))})
    return job, changes


def apply_changes(job, changes, actor):
    from .oem_links import chunks
    from .oem_reference import refresh_status
    from .oem_reference_models import OEMCrossReference, OEMCrossReferenceRetired, OEMReference, PartOEMLink
    for change in changes['numbers']:
        if change['action'] == 'detail':
            change['source'].detail = change['detail']
            change['source'].save(update_fields=['detail'])
        elif change['action'] == 'drop_source':
            change['source'].delete()
            refresh_status(change['ref'], actor, changed=True)
    retired = [change['row'] for change in changes['codes'] if change['action'] == 'retire']
    OEMCrossReferenceRetired.objects.bulk_create([OEMCrossReferenceRetired(reference_id=row.reference_id, brand=row.brand, number=row.number, code=row.code,
                                                                           citations=row.citations, retired_by=actor) for row in retired],
                                                 batch_size=1000, ignore_conflicts=True)
    for chunk in chunks([row.pk for row in retired]):
        OEMCrossReference.objects.filter(pk__in=chunk).delete()
    for change in changes['codes']:
        if change['action'] in ('recite', 'drop_citation'):
            change['row'].citations = change['citations']
            change['row'].save(update_fields=['citations', 'updated_at'])
    for chunk in chunks([change['link'].pk for change in changes['ties'] if change['action'] == 'remove']):
        PartOEMLink.objects.filter(pk__in=chunk).delete()
    for change in changes['ties']:
        if change['action'] in ('recite', 'drop_citation'):
            change['link'].citations = change['citations']
            change['link'].save(update_fields=['citations'])
    for chunk in chunks([change['ref'].pk for change in changes['numbers'] if change['action'] == 'delete']):
        OEMReference.objects.filter(pk__in=chunk).delete()  # its codes, tombstones and name links go with it


def write_report(directory, changes):
    os.makedirs(directory, exist_ok=True)

    def table(name, header, rows):
        with open(os.path.join(directory, name), 'w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)

    table('numbers.csv', ['accion', 'fabricante', 'numero', 'id_numero', 'detalle_antes', 'detalle_despues'],
          [[c['action'], c['ref'].manufacturer, c['ref'].code, c['ref'].pk, json.dumps(c['source'].detail, ensure_ascii=False),
            json.dumps(c.get('detail', {}), ensure_ascii=False)] for c in changes['numbers']])
    table('codes.csv', ['accion', 'fabricante', 'numero', 'marca', 'codigo', 'id_equivalencia', 'citas_antes', 'citas_despues'],
          [[c['action'], c['row'].reference.manufacturer, c['row'].reference.code, c['row'].brand, c['row'].code, c['row'].pk,
            ' | '.join(c['row'].citations), ' | '.join(c['citations'])] for c in changes['codes']])
    table('ties.csv', ['accion', 'sku', 'fabricante', 'numero', 'id_vinculo', 'citas_antes', 'citas_despues'],
          [[c['action'], c['link'].part.sku, c['link'].reference.manufacturer, c['link'].reference.code, c['link'].pk,
            ' | '.join(c['link'].citations), ' | '.join(c['citations'])] for c in changes['ties']])
    table('alternos-review.csv', ['sku', 'descripcion', 'marca', 'codigo', 'fuente', 'articulos_del_sku', 'articulos_que_lo_imprimen', 'id_alterno', 'id_sku'],
          [[c['code'].part.sku, c['code'].part.description, c['code'].brand, c['code'].code, c['code'].reference_source,
            ' '.join(c['sku_items']), ' '.join(c['printed_by']), c['code'].pk, c['code'].part_id] for c in changes['alternos']])


def run(path, *, apply=False, report_dir=None, username=None, stdout=None):
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from .oem_finder import matching_lock
    stdout = stdout or sys.stdout
    data = json.load(open(path, encoding='utf-8'))
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
                     f"{n.get('drop_source', 0)} sin la fuente del catálogo, {n.get('detail', 0)} con detalle corregido, {imported['oem_new']} nuevos · "
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
    parser.add_argument('file', help='Extracto JSON normalizado y corregido.')
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
