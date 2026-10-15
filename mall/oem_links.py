"""What a line reaches through its OEM numbers (inventory refactor S4, S9, S10). From S4 every number of the library is a Reference (tipo
oem with its manufacturer as brand, mall.references.add_library_number) on the line that carries it; from S9 an aftermarket code whose
numbers sit on one line is that line's aftermarket Reference (mall.merge_pass), and from S10 nothing else: the aftermarket codes filed under
numbers (OEMCrossReference), their tombstones and the SKUs' catalog and name links (PartOEMLink) are archived (archive schema, 0088). A
line's equivalents are its aftermarket References in an exclusive estado (equivalents_for, part_equivalents), and a line in referencia
(a library number no product carries yet) is never a catalog line. Nothing here writes a Reference, renames a line or changes a main.
"""
import logging
import re
from collections import defaultdict

from .oem_reference import compact, manufacturer_name

log = logging.getLogger(__name__)
CHUNK = 5000
MIN_SEARCH = 4  # letters and digits a search needs before it reads as a code (mall.catalog_search.code_key)
# Catalog manufacturers the OEM finder's make tokens do not name, mapped to the finder's canonical make.
EXTRA_MAKES = {'SEAT': 'VAG', 'CUPRA': 'VAG', 'MINI': 'BMW', 'MERCURY': 'FORD', 'GM': 'CHEVROLET'}


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


def part_equivalents(part):
    """What a line reaches through its OEM numbers: {'numbers': [{reference, via, citations}], 'cross_references': [{brand, code,
    citations, numbers, on_sku}]}. reference is the number as the Biblioteca shows it (mall.oem_reference.LibraryNumber); via is oem (one
    of its References). cross_references are the line's aftermarket References in an exclusive estado (from S10 nothing else: the codes
    filed under numbers are archived), each with its live sources' citations and the numbers (ids) the merge pass placed it under
    (detail s9 numbers), on_sku always true. A line grouped into another reaches nothing: its codes moved to that line."""
    from . import references
    from .models import Reference, ReferenceSource
    from .oem_reference import LibraryNumber
    if part.merged_into_id:
        return {'numbers': [], 'cross_references': []}
    refs = Reference.all_objects.select_related('reemplazado_por').in_bulk(
        list(Reference.objects.filter(references.library_q(), part=part).values_list('pk', flat=True)))
    order = sorted(refs, key=lambda pk: (refs[pk].brand, refs[pk].clave))
    numbers = [{'reference': LibraryNumber(refs[pk]), 'via': ['oem'], 'citations': []} for pk in order]
    by_code = {compact(refs[pk].code): refs[pk].uid for pk in order}
    codes = list(Reference.objects.filter(part=part, tipo='aftermarket', is_main=False, estado__in=references.EXCLUSIVE)
                 .order_by('brand', 'clave', 'pk'))
    citations = defaultdict(list)
    for ref_id, citation in (ReferenceSource.objects.filter(reference__in=codes, retirada=False).exclude(citation='')
                             .order_by('created_at', 'pk').values_list('reference_id', 'citation')):
        if citation not in citations[ref_id]:
            citations[ref_id].append(citation)
    rows = []
    for ref in codes:
        placed = ((ref.detail or {}).get('s9') or {}).get('numbers') or []
        filed = [by_code[key] for key in dict.fromkeys(compact(number) for number in placed) if key in by_code]
        rows.append({'brand': ref.brand, 'code': ref.code, 'citations': citations.get(ref.pk, []), 'numbers': filed, 'on_sku': True})
    return {'numbers': numbers, 'cross_references': rows}


def equivalents_for(part_ids):
    """{part id: [{'brand', 'code'}]}: what customers see next to a line's own codes, each code once, by brand and code, in one query
    for a whole page: the line's aftermarket References in an exclusive estado (never one in disputa: a step 0 copy waiting for its
    case, a code whose sources were retired). From S10 that is all: the codes filed under the library numbers the line holds are
    archived (S9 made every one whose numbers sit on one line that line's Reference). The JSON key stays."""
    from . import references
    from .models import Reference
    codes = defaultdict(dict)
    for chunk in chunks([str(pk) for pk in part_ids]):
        rows = (Reference.objects.filter(tipo='aftermarket', is_main=False, estado__in=references.EXCLUSIVE, part_id__in=chunk)
                .order_by('brand', 'clave', 'pk').values_list('part_id', 'brand', 'code'))
        for part_id, brand, code in rows:
            codes[str(part_id)].setdefault((brand, compact(code)), code)
    return {part_id: [{'brand': brand, 'code': code} for (brand, _), code in sorted(found.items())] for part_id, found in codes.items()}


def same_make(a, b):
    """Two manufacturer names agree: equal, or in the same OEM finder make group (LEXUS and TOYOTA, JEEP and DODGE)."""
    return manufacturer_name(a) == manufacturer_name(b) or bool(make_group(a)) and make_group(a) == make_group(b)
