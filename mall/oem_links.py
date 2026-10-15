"""OEM cross-references and the lines that reach them (alternos, step 2a; inventory refactor S4, S9). Aftermarket codes hang off the OEM
number they replace (OEMCrossReference), and from S4 every number of the library is a Reference (tipo oem with its manufacturer as
brand, mall.references.add_library_number): a line reaches the codes of the numbers it holds. From S9 (mall.merge_pass) a code whose
numbers sit on one line is that line's aftermarket Reference, and the SKUs' catalog and name links (PartOEMLink, sources catalog and
sku_name) are no longer read: the merge pass put a catalog item's numbers on its SKU's line, or opened a case. Catalog search (search_q)
and supplier matching (reach_index) find lines through those numbers and codes (step 2b). A line in referencia (a library number no
product carries yet) is never a target: the readers take catalog lines only.

The supplier catalog import writes the cross-references and the catalog links (store_cross_references, store_catalog_links, keyed by
the number's Reference); the links stay as evidence the merge pass reads (a group's SKU) until S10 archives them. The name links of
S2a's time are retired with S4. Nothing here writes a Reference, renames a line or changes a main.
"""
import logging
import re
from collections import defaultdict

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from .oem_reference_models import OEMCrossReference, OEMCrossReferenceRetired, PartOEMLink, citation_list, compact, manufacturer_name

log = logging.getLogger(__name__)
CHUNK = 5000
MIN_KEY = 5  # shorter codes, or codes without a digit, name no OEM number
MIN_SEARCH = 4  # letters and digits a search needs before it looks through OEM numbers and aftermarket codes
# Catalog manufacturers the OEM finder's make tokens do not name, mapped to the finder's canonical make.
EXTRA_MAKES = {'SEAT': 'VAG', 'CUPRA': 'VAG', 'MINI': 'BMW', 'MERCURY': 'FORD', 'GM': 'CHEVROLET'}


def tables_ready():
    """False until migration 0079 runs: the live app serves this code before the orchestrator migrates."""
    from .oem_reference import tables_ready as library_ready
    return {OEMCrossReference._meta.db_table, PartOEMLink._meta.db_table} <= set(connection.introspection.table_names()) and library_ready()


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


def name_targets(parts, table=None, library=None):
    """{part id (str): {number id (str)}} for lines whose own code names a number of the library. parts: (id, sku, description, name)
    rows. A number filed under several manufacturers (CHRYSLER and DODGE) counts under each, except those of a make the line's
    description rules out when it names one. library: {compact number: [(id, manufacturer)]}; by default the library's References
    (their ids). Read-only since S4: the name links are retired (refresh_name_links), and S4's backfill lists what they would add."""
    from .catalog_suffixes import suffix_table
    table = table or suffix_table()
    keys = {}
    for pk, sku, description, name in parts:
        key = name_key(sku, table)
        if key:
            keys[str(pk)] = (key, make_groups(description, name if name and name != sku else ''))
    found = defaultdict(list)
    if library is not None:
        for key, _ in keys.values():
            found[key] = [(str(pk), make_group(manufacturer)) for pk, manufacturer in library.get(key, ())]
    else:
        from . import references
        from .models import Reference
        for chunk in chunks({key for key, _ in keys.values()}):
            for pk, manufacturer, code in Reference.objects.filter(references.library_q(), references.compact_q(chunk)).values_list('pk', 'brand', 'code'):
                found[compact(code)].append((str(pk), make_group(manufacturer)))
    out = {}
    for part_id, (key, makes) in keys.items():
        refs = {ref for ref, group in found.get(key, []) if not makes or not group or group in makes}
        if refs:
            out[part_id] = refs
    return out


def refresh_name_links(part_ids=None, *, apply=True):
    """Retired with S4 (plan §3 S4): a library number a line's codigo names is one of that line's References, and rule 3 (S6) proposes
    the rest, so no name link is added or dropped any more. Kept until S10 for the callers that report its counts."""
    return {'skus': 0, 'links': 0, 'new': 0, 'removed': 0, 'retired': True}


def store_cross_references(entries, *, apply=True, bare=()):
    """entries: {(reference id, brand, code): citations}. One row per (number, brand, compact code): a new code is created and a known
    one gains the citations it lacks. A code without a brand, or one a superuser retired from the number, is skipped, and so is a code
    without a digit unless bare lists its (brand, compact code): a catalog's own item code (ASVA FDWH-FOCMF). Returns {'new', 'cited',
    'present'}."""
    plan = {}
    for (ref_id, brand, code), citations in entries.items():
        brand, code = manufacturer_name(brand), ' '.join(str(code or '').upper().split())
        number = compact(code)
        if not brand or len(brand) > 120 or not number or len(code) > 120 or not (re.search(r'\d', number) or (brand, number) in bare):
            continue
        entry = plan.setdefault((str(ref_id), brand, number), {'code': code, 'citations': []})
        entry['citations'] = citation_list(entry['citations'], *citations)
    existing = {}
    for chunk in chunks(sorted({ref_id for ref_id, _, _ in plan})):
        for row in OEMCrossReference.objects.filter(reference_id__in=chunk).only('pk', 'reference_id', 'brand', 'number', 'citations'):
            existing[(str(row.reference_id), row.brand, row.number)] = row
        # A code a superuser retired from the number stays retired, whatever catalog prints it again.
        for key in OEMCrossReferenceRetired.objects.filter(reference_id__in=chunk).values_list('reference_id', 'brand', 'number'):
            plan.pop((str(key[0]), key[1], key[2]), None)
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
            if new:  # the lines that reach these numbers find the new codes (their Search Documents' reach, S7)
                from .search_document import refresh_reach
                refresh_reach({row.reference_id for row in new})
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
            if new:  # the linked lines reach the numbers and their codes (their Search Documents' reach, S7)
                from .search_document import refresh_reach
                refresh_reach(part_ids={row.part_id for row in new})
    return {'new': len(new), 'cited': len(cited), 'present': len(plan) - len(new) - len(cited)}


def library_of(part_ids):
    """[(number id, line id)] of the library's References on these lines (from S9 the SKU links are not read)."""
    from . import references
    from .models import Reference
    rows = []
    for chunk in chunks([str(pk) for pk in part_ids]):
        rows += list(Reference.objects.filter(references.library_q(), part_id__in=chunk).values_list('pk', 'part_id'))
    return rows


def canonical_brands(spellings):
    """{spelling: canonical Brand.nombre} for these brand spellings (by nombre or alias, else the spelling normalized), in one query:
    what mall.brands.resolve says, for a whole page at once."""
    from .brand_models import Brand
    from .brands import normalize
    spellings = {spelling for spelling in spellings if spelling}
    found = {}
    for nombre, alias in Brand.objects.filter(Q(nombre__in=spellings) | Q(aliases__alias__in=spellings)).values_list('nombre', 'aliases__alias'):
        if nombre in spellings:
            found[nombre] = nombre
        if alias in spellings:
            found.setdefault(alias, nombre)
    return {spelling: found.get(spelling) or normalize(spelling) for spelling in spellings}


def held_elsewhere(codes):
    """{clave: {(brand, line id)}} of the exclusive public References of these codes' claves on live lines: which lines hold a code
    (rule 8: the other lines do not show it)."""
    from . import references
    from .models import Reference
    out = defaultdict(set)
    for chunk in chunks(sorted({references.clave(references.code_key(code)) for code in codes if code})):
        rows = (Reference.all_objects.filter(clave__in=chunk, estado__in=references.EXCLUSIVE, supplier__isnull=True,
                                             part__merged_into__isnull=True).exclude(part__estado='unificada')
                .values_list('clave', 'brand', 'part_id'))
        for key, brand, part_id in rows:
            out[key].add((brand, str(part_id)))
    return out


def part_equivalents(part):
    """What a line reaches through its OEM numbers: {'numbers': [{reference, via, citations}], 'cross_references': [{brand, code,
    citations, numbers, on_sku}]}. reference is the number as the Biblioteca shows it (mall.oem_reference.LibraryNumber); via is oem (one
    of its References: from S9 the SKU links are not read). Each code appears once with the numbers (ids) it is filed under; on_sku marks
    the codes the line also keeps as its own Reference. A line grouped into another reaches nothing: its codes moved to that line."""
    from . import references
    from .models import Reference
    from .oem_reference import LibraryNumber
    if part.merged_into_id:
        return {'numbers': [], 'cross_references': []}
    refs = Reference.all_objects.select_related('reemplazado_por').in_bulk(
        list(Reference.objects.filter(references.library_q(), part=part).values_list('pk', flat=True)))
    order = sorted(refs, key=lambda pk: (refs[pk].brand, refs[pk].clave))
    numbers = [{'reference': LibraryNumber(refs[pk]), 'via': ['oem'], 'citations': []} for pk in order]
    own = {(manufacturer_name(brand), compact(code)) for brand, code in Reference.objects.filter(part=part).values_list('brand', 'code')}
    codes = {}
    for row in OEMCrossReference.objects.filter(reference_id__in=list(refs)).order_by('brand', 'number', 'pk'):
        entry = codes.setdefault((row.brand, row.number), {'brand': row.brand, 'code': row.code, 'citations': [], 'numbers': [],
                                                           'on_sku': (row.brand, row.number) in own})
        entry['citations'] = citation_list(entry['citations'], *row.citations)
        uid = refs[row.reference_id].uid
        if uid not in entry['numbers']:
            entry['numbers'].append(uid)
    position = {refs[pk].uid: index for index, pk in enumerate(order)}
    for entry in codes.values():
        entry['numbers'].sort(key=position.get)  # in the order the numbers are listed
    return {'numbers': numbers, 'cross_references': list(codes.values())}


def equivalents_for(part_ids):
    """{part id: [{'brand', 'code'}]}: what customers see next to a line's own codes, each code once, by brand and code, in a few
    queries for a whole page. From S9 (plan §3 S9: equivalents stop reading PartOEMLink and become the line's aftermarket References):
    the line's aftermarket References in an exclusive estado (never one in disputa), and, for the codes the merge pass left as catalog
    rows, the codes filed under the library numbers the line holds that no other line holds as a Reference (rule 8: a code kept on
    another line is not shown here). The JSON key stays."""
    from . import references
    from .models import Reference
    ids = [str(pk) for pk in part_ids]
    codes, reached = defaultdict(dict), defaultdict(set)
    for chunk in chunks(ids):  # one query: the lines' aftermarket References and their library numbers
        # an aftermarket Reference in disputa is not shown (a step 0 copy waiting for its case, a code whose sources were retired)
        rows = (Reference.objects.filter(Q(tipo='aftermarket', is_main=False, estado__in=references.EXCLUSIVE) | references.library_q(),
                                         part_id__in=chunk)
                .order_by('brand', 'clave', 'pk').values_list('pk', 'part_id', 'tipo', 'brand', 'code'))
        for pk, part_id, tipo, brand, code in rows:
            if tipo == 'aftermarket':
                codes[str(part_id)].setdefault((brand, compact(code)), code)
            else:
                reached[pk].add(str(part_id))
    filed = []
    for chunk in chunks(sorted(reached)):
        filed += list(OEMCrossReference.objects.filter(reference_id__in=chunk).order_by('brand', 'number', 'pk')
                      .values_list('reference_id', 'brand', 'number', 'code'))
    if filed:
        names = canonical_brands({brand for _, brand, _, _ in filed})
        holders = held_elsewhere({code for _, _, _, code in filed})
        for ref_id, brand, number, code in filed:
            name = names.get(brand, brand)
            key = references.clave(references.code_key(code))
            for part_id in reached[ref_id]:
                if any(line != part_id and held in (name, '') for held, line in holders.get(key, ())):
                    continue
                codes[part_id].setdefault((name, number), code)
    return {part_id: [{'brand': brand, 'code': code} for (brand, _), code in sorted(found.items())] for part_id, found in codes.items()}


def search_q(text, exact=False):
    """Q for lines that hold an OEM number of the library (from S9 a SKU link no longer ties one) whose compact form contains the text (16100 39315), or
    to which an aftermarket code filed under such a number belongs (gwt41a); equal to it when exact. None for text with fewer than
    MIN_SEARCH letters or digits. Uncorrelated subqueries, no Python lists; the caller's queryset decides which lines count (a line in
    referencia is never a catalog or storefront line)."""
    from . import references
    from .models import Reference
    key = compact(text)
    if len(key) < MIN_SEARCH:
        return None
    library = Reference.objects.filter(references.library_q())
    numbers = library.filter(references.compact_match_q(key, exact=exact)).values('pk')  # a short number's clave is its spelling
    crossed = OEMCrossReference.objects.filter(**{'number' if exact else 'number__contains': key}).values('reference_id')
    return Q(pk__in=library.filter(Q(pk__in=numbers) | Q(pk__in=crossed)).values('part_id'))


def reach_index(part_ids):
    """The OEM library as supplier matching reads it, for these canonical lines (string ids): {number id: {part id}} through the
    library References they hold; {compact number: {(number id, manufacturer)}} of the numbers some line reaches;
    {(brand, compact code): ({number id}, {printed code})} of the aftermarket codes filed under those numbers. Number ids are the
    References' ids (text)."""
    from . import references
    from .models import Reference
    part_ids = {str(pk) for pk in part_ids}
    reaching = defaultdict(set)
    rows = Reference.objects.filter(references.library_q()).values_list('pk', 'part_id')  # from S9 the SKU links are not read
    for ref_id, part_id in rows:
        if str(part_id) in part_ids:
            reaching[str(ref_id)].add(str(part_id))
    numbers, aftermarket = defaultdict(set), {}
    for chunk in chunks(sorted(reaching)):
        for pk, manufacturer, code in Reference.all_objects.filter(pk__in=chunk).values_list('pk', 'brand', 'code'):
            numbers[compact(code)].add((str(pk), manufacturer))
        for ref_id, brand, number, code in OEMCrossReference.objects.filter(reference_id__in=chunk).values_list('reference_id', 'brand', 'number', 'code'):
            refs, printed = aftermarket.setdefault((brand, number), (set(), set()))
            refs.add(str(ref_id))
            printed.add(code)
    return dict(reaching), dict(numbers), aftermarket


def same_make(a, b):
    """Two manufacturer names agree: equal, or in the same OEM finder make group (LEXUS and TOYOTA, JEEP and DODGE)."""
    return manufacturer_name(a) == manufacturer_name(b) or bool(make_group(a)) and make_group(a) == make_group(b)
