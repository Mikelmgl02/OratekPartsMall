"""OEM cross-references and SKU links (alternos, step 2a). Aftermarket codes hang off the OEM number they replace (OEMCrossReference),
and a SKU reaches them through its OEM numbers: its OEM alternos, the number its own code names and the numbers a supplier catalog
printed for the item matched to it (PartOEMLink).

    python -m mall.link_oem_numbers --dry-run|--apply

brings the name links of every canonical SKU in line with its code (16100-79445-NPW -> TOYOTA 1610079445). The supplier catalog
import writes the cross-references and the catalog links and refreshes the name links after an apply; saving a SKU refreshes its own.
Nothing here writes a PartCode, renames a SKU or changes a MAIN, and the company alternos already on the SKUs stay where they are.
"""
import logging
import re
from collections import defaultdict

from django.db import OperationalError, ProgrammingError, connection, transaction
from django.utils import timezone

from .oem_reference_models import OEMCrossReference, OEMReference, PartOEMLink, citation_list, compact, manufacturer_name

log = logging.getLogger(__name__)
CHUNK = 5000
MIN_KEY = 5  # shorter codes, or codes without a digit, name no OEM number
VIA_ORDER = ['oem', 'sku_name', 'catalog']  # how a SKU reaches a number, strongest first
# Catalog manufacturers the OEM finder's make tokens do not name, mapped to the finder's canonical make.
EXTRA_MAKES = {'SEAT': 'VAG', 'CUPRA': 'VAG', 'MINI': 'BMW', 'MERCURY': 'FORD', 'GM': 'CHEVROLET'}
_state = {'warned': False}


def tables_ready():
    """False until migration 0041 runs: the live app serves this code before the orchestrator migrates."""
    return {OEMCrossReference._meta.db_table, PartOEMLink._meta.db_table} <= set(connection.introspection.table_names())


def chunks(values, size=CHUNK):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def make_group(make):
    """The OEM finder's make group for a manufacturer or make name, through the finder's own make tokens: VOLKSWAGEN, AUDI and SEAT ->
    VAG, MERCEDES-BENZ -> MB, JEEP and DODGE -> MOPAR, CADILLAC -> GM. '' when there is no make evidence: the finder lumps PEUGEOT,
    VOLVO, OPEL... together as OTHER, which says nothing about agreement."""
    from .oem_finder import MAKE_GROUP, MAKE_TOKENS
    make = (make or '').strip().upper()
    tokens = [make, *re.split(r'[^0-9A-Z]+', make)]
    canonical = next((MAKE_TOKENS.get(x) or EXTRA_MAKES.get(x) for x in tokens if x in MAKE_TOKENS or x in EXTRA_MAKES), make)
    group = MAKE_GROUP.get(canonical, canonical)
    return '' if group == 'OTHER' else group


def make_groups(*texts):
    """The make groups a SKU's description (and name) spell out (TOY -> TOYOTA, HYU -> HMG); empty when they name none."""
    from .oem_finder import MAKE_GROUP, OEMFinder
    return {MAKE_GROUP.get(make, make) for make in OEMFinder.explicit_makes(' '.join(text for text in texts if text))} - {'OTHER'}


def name_key(sku, table=None):
    """The OEM number a SKU's own code names, compact: the whole code ('16100-29155') or the code before trailing brand and neutral
    tags ('16100-79445-NPW', '90919-01164 DENSO'). Blank for a retired SKU, and when the trailing tokens are a variant, an unknown
    token or an alternate number: those do not name the bare part."""
    from .catalog_suffixes import LIFECYCLE_RE, strip_tags, suffix_table
    sku = str(sku or '').strip().upper()
    if not sku or LIFECYCLE_RE.search(sku):
        return ''
    base, _, chain_class = strip_tags(sku, table=table or suffix_table())
    key = compact(base) if chain_class in ('bare', 'tag') else ''
    return key if len(key) >= MIN_KEY and re.search(r'\d', key) else ''


def name_targets(parts, table=None):
    """{part id (str): {reference id (str)}} for SKUs whose own code names a number in the library. parts: (id, sku, description,
    name) rows. A number filed under several manufacturers (CHRYSLER and DODGE) links under each, except those of a make the SKU's
    description rules out when it names one."""
    from .catalog_suffixes import suffix_table
    table = table or suffix_table()
    keys = {}
    for pk, sku, description, name in parts:
        key = name_key(sku, table)
        if key:
            keys[str(pk)] = (key, make_groups(description, name if name and name != sku else ''))
    found = defaultdict(list)
    for chunk in chunks({key for key, _ in keys.values()}):
        for pk, manufacturer, code in OEMReference.objects.filter(code__in=chunk).values_list('pk', 'manufacturer', 'code'):
            found[code].append((str(pk), make_group(manufacturer)))
    out = {}
    for part_id, (key, makes) in keys.items():
        refs = {ref for ref, group in found.get(key, []) if not makes or not group or group in makes}
        if refs:
            out[part_id] = refs
    return out


def refresh_name_links(part_ids=None, *, apply=True):
    """Bring the name links of these SKUs (every canonical SKU by default) in line with their codes: the missing ones are added and the
    stale ones dropped, a grouped (merged) SKU's included. Returns {'skus', 'links', 'new', 'removed'}."""
    from .models import Part
    ids = None if part_ids is None else [str(pk) for pk in part_ids]
    parts = Part.objects.filter(merged_into__isnull=True)
    links = PartOEMLink.objects.filter(source='sku_name')
    rows, have = [], {}
    for chunk in ([None] if ids is None else chunks(ids)):
        scope = parts if chunk is None else parts.filter(pk__in=chunk)
        rows += list(scope.values_list('pk', 'sku', 'description', 'name'))
        for pk, part_id, ref_id in (links if chunk is None else links.filter(part_id__in=chunk)).values_list('pk', 'part_id', 'reference_id'):
            have[(str(part_id), str(ref_id))] = pk
    wanted = {(part_id, ref) for part_id, refs in name_targets(rows).items() for ref in refs}
    new, stale = sorted(wanted - set(have)), [have[pair] for pair in set(have) - wanted]
    if apply and (new or stale):
        with transaction.atomic():
            PartOEMLink.objects.bulk_create([PartOEMLink(part_id=part_id, reference_id=ref, source='sku_name') for part_id, ref in new],
                                            batch_size=1000, ignore_conflicts=True)
            for chunk in chunks(stale):
                PartOEMLink.objects.filter(pk__in=chunk).delete()
    return {'skus': len({part_id for part_id, _ in wanted}), 'links': len(wanted), 'new': len(new), 'removed': len(stale)}


def store_cross_references(entries, *, apply=True):
    """entries: {(reference id, brand, code): citations}. One row per (number, brand, compact code): a new code is created and a known
    one gains the citations it lacks. A code without a digit or a brand is skipped. Returns {'new', 'cited', 'present'}."""
    plan = {}
    for (ref_id, brand, code), citations in entries.items():
        brand, code = manufacturer_name(brand), ' '.join(str(code or '').upper().split())
        number = compact(code)
        if not brand or len(brand) > 120 or not number or len(code) > 120 or not re.search(r'\d', number):
            continue
        entry = plan.setdefault((str(ref_id), brand, number), {'code': code, 'citations': []})
        entry['citations'] = citation_list(entry['citations'], *citations)
    existing = {}
    for chunk in chunks(sorted({ref_id for ref_id, _, _ in plan})):
        for row in OEMCrossReference.objects.filter(reference_id__in=chunk).only('pk', 'reference_id', 'brand', 'number', 'citations'):
            existing[(str(row.reference_id), row.brand, row.number)] = row
    new, cited, now = [], [], timezone.now()
    for key, entry in sorted(plan.items()):
        row = existing.get(key)
        if row is None:
            new.append(OEMCrossReference(reference_id=key[0], brand=key[1], code=entry['code'], number=key[2], citations=entry['citations']))
        elif citation_list(row.citations, *entry['citations']) != row.citations:
            row.citations, row.updated_at = citation_list(row.citations, *entry['citations']), now
            cited.append(row)
    if apply:
        with transaction.atomic():
            OEMCrossReference.objects.bulk_create(new, batch_size=1000, ignore_conflicts=True)
            OEMCrossReference.objects.bulk_update(cited, ['citations', 'updated_at'], batch_size=1000)
    return {'new': len(new), 'cited': len(cited), 'present': len(plan) - len(new) - len(cited)}


def store_catalog_links(entries, *, apply=True):
    """entries: {(part id, reference id): citations} from supplier catalogs. A new link is created and a known one gains the citations
    it lacks; nothing is ever removed (like the company alternos the catalogs write). Returns {'new', 'cited', 'present'}."""
    plan = {}
    for (part_id, ref_id), citations in entries.items():
        key = (str(part_id), str(ref_id))
        plan[key] = citation_list(plan.get(key), *citations)
    existing = {}
    for chunk in chunks(sorted({part_id for part_id, _ in plan})):
        for row in PartOEMLink.objects.filter(source='catalog', part_id__in=chunk).only('pk', 'part_id', 'reference_id', 'citations'):
            existing[(str(row.part_id), str(row.reference_id))] = row
    new, cited = [], []
    for key, citations in sorted(plan.items()):
        row = existing.get(key)
        if row is None:
            new.append(PartOEMLink(part_id=key[0], reference_id=key[1], source='catalog', citations=citations))
        elif citation_list(row.citations, *citations) != row.citations:
            row.citations = citation_list(row.citations, *citations)
            cited.append(row)
    if apply:
        with transaction.atomic():
            PartOEMLink.objects.bulk_create(new, batch_size=1000, ignore_conflicts=True)
            PartOEMLink.objects.bulk_update(cited, ['citations'], batch_size=1000)
    return {'new': len(new), 'cited': len(cited), 'present': len(plan) - len(new) - len(cited)}


def part_equivalents(part):
    """What a SKU reaches through its OEM numbers: {'numbers': [{reference, via, citations}], 'cross_references': [{brand, code,
    citations, numbers, on_sku}]}. via lists how the SKU reaches the number: oem (an OEM alterno), sku_name or catalog. Each code
    appears once with the numbers it is filed under; on_sku marks the codes the SKU also keeps as its own alterno. A SKU grouped into
    another reaches nothing: its codes moved to that SKU."""
    from .models import PartCode
    if part.merged_into_id:
        return {'numbers': [], 'cross_references': []}
    reached = defaultdict(lambda: {'via': [], 'citations': []})
    for ref_id in PartCode.objects.filter(part=part, ref_type='oem', oem_reference__isnull=False).values_list('oem_reference_id', flat=True):
        if 'oem' not in reached[ref_id]['via']:
            reached[ref_id]['via'].append('oem')
    for ref_id, source, citations in PartOEMLink.objects.filter(part=part).order_by('source', 'pk').values_list('reference_id', 'source', 'citations'):
        entry = reached[ref_id]
        if source not in entry['via']:
            entry['via'].append(source)
        entry['citations'] = citation_list(entry['citations'], *citations)
    for entry in reached.values():
        entry['via'].sort(key=VIA_ORDER.index)
    refs = OEMReference.objects.in_bulk(list(reached))
    numbers = [{'reference': refs[ref_id], **reached[ref_id]} for ref_id in sorted(refs, key=lambda pk: (refs[pk].manufacturer, refs[pk].code))]
    own = {(manufacturer_name(brand), compact(code)) for brand, code in PartCode.objects.filter(part=part).values_list('brand', 'code')}
    codes = {}
    for row in OEMCrossReference.objects.filter(reference_id__in=list(refs)).order_by('brand', 'number', 'pk'):
        entry = codes.setdefault((row.brand, row.number), {'brand': row.brand, 'code': row.code, 'citations': [], 'numbers': [],
                                                           'on_sku': (row.brand, row.number) in own})
        entry['citations'] = citation_list(entry['citations'], *row.citations)
        if row.reference_id not in entry['numbers']:
            entry['numbers'].append(row.reference_id)
    order = {entry['reference'].pk: index for index, entry in enumerate(numbers)}
    for entry in codes.values():
        entry['numbers'].sort(key=order.get)  # in the order the numbers are listed
    return {'numbers': numbers, 'cross_references': list(codes.values())}


def guarded(sync, *args):
    """Run a link refresh in a savepoint so it never breaks the SKU write that triggered it; while the tables are not migrated it is
    skipped quietly (one warning per process)."""
    try:
        with transaction.atomic():
            return sync(*args)
    except (ProgrammingError, OperationalError):
        try:
            ready = tables_ready()
        except Exception:
            ready = True
        if ready:
            log.exception('No se pudieron actualizar los números OEM del SKU.')
        elif not _state['warned']:
            _state['warned'] = True
            log.warning('Los vínculos de SKU con números OEM aún no están migrados (0041): se omite su actualización.')
    except Exception:
        log.exception('No se pudieron actualizar los números OEM del SKU.')
    return None


def part_pre_save(sender, instance, raw=False, update_fields=None, **kwargs):
    """The SKU and grouping an existing Part had before this save, so a rename or a grouping refreshes its name links."""
    if raw or instance._state.adding or instance.pk is None or (update_fields is not None and not {'sku', 'merged_into'} & set(update_fields)):
        return
    instance._oem_links_before = sender.objects.filter(pk=instance.pk).values_list('sku', 'merged_into_id').first()


def part_saved(sender, instance, created=False, raw=False, **kwargs):
    before = instance.__dict__.pop('_oem_links_before', None)
    if raw or not (created or (before is not None and before != (instance.sku, instance.merged_into_id))):
        return
    guarded(refresh_name_links, [instance.pk])

