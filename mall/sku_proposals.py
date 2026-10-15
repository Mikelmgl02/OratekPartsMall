"""Proposed new catalog SKUs (Productos nuevos before inventory refactor S6) and what the lines in preparación still read from them.

Until S6 the matching worker turned supplier items no SKU took into one SkuProposal per product (the same compact code and brand,
whatever the supplier), and an administrator approved, linked or rejected it. From S6 a new product is a line in preparación
(mall.new_products, which replaced this module's write side: place, publish, reject, link, edit, ai_fill), and
mall.refactor.s6_new_products turned the pending proposals into those lines with their ids kept. The SkuProposal rows are history,
never written again (S10 archives them).

What stays here, read by mall.new_products, mall.sku_review, the matching engine and the stage CLIs: vehicle_makes() (supplier brands
that are a vehicle make, each with the manufacturer its OEM numbers are filed under), the placeholder brands, library_oem() (the OEM
number the library files an aftermarket code under), merge_references() and printed_forms() (a product's codes from its items),
catalog_owners() (the catalog SKUs a code already is, for the duplicate checks), the two refusals the views answer, and
proposals_stage(), the matching worker's stage, which now places the backlog (mall.new_products.backlog_stage)."""
from collections import Counter

from django.conf import settings
from rest_framework import status as http
from rest_framework.exceptions import APIException, ValidationError

from .catalog_families import normalized_reference
from .catalog_identity import reference_pattern
from .lines import CATALOG, catalog_lines, catalog_q
from .models import PartCode
from .part_references import normalize_references

MAX_REFERENCES = 100
PROPOSAL_LOCK = 724631112  # pg advisory lock class of an approval's codes (matching pass 724631109 ... suffix table 724631111)
PLACEHOLDER_BRANDS = {'GENERICO', 'GENERICA', 'GENERIC', 'S/M', 'SM', 'S/MARCA', 'SIN MARCA', 'N/A', 'NA', 'VARIOS', 'VARIAS', 'OTRO',
                      'OTROS', 'OTRA', 'OTRAS', 'NINGUNA', 'ALTERNO', 'ALTERNA', 'IMPORTADO', 'CHINA', 'TAIWAN'}
# Make tokens other words share (TOYO tires, HON, MIT, CHE...): as a supplier brand they say nothing about the make.
NOT_A_MAKE = {'TOYO', 'DAI', 'HON', 'MIT', 'MIST', 'CHE', 'CHEX', 'MAZ', 'ISU', 'SUZ', 'DAE', 'INFINITY', 'CHANG', 'CHERRY', 'WULIN',
              'VOLK', 'SSANG', 'BENZ', 'INTERNATIONAL', 'INTERNACIONAL', 'MINI'}
MAKE_NAMES = {'VW': 'VOLKSWAGEN', 'VOLKWAGEN': 'VOLKSWAGEN'}


class ProposalConflict(APIException):
    status_code = http.HTTP_409_CONFLICT
    default_detail = 'La propuesta cambió mientras la revisabas: vuelve a abrirla.'


class DuplicateProposal(Exception):
    """Approving would create a second SKU for a code the catalog already has: why, and the SKUs (id, sku, active) its items belong
    to. The views answer 400 with both, so the drawer can offer Vincular."""

    def __init__(self, detail, owners):
        super().__init__(detail)
        self.detail, self.owners = detail, owners


def clean(value):
    """Upper case, single spaces."""
    return ' '.join((value or '').upper().split())


# --- what both read: makes, codes, the catalog ------------------------------------------------------------------------------------

def vehicle_makes():
    """Supplier brands that are a vehicle make (a dealer's genuine parts), each with the manufacturer its OEM numbers are filed under:
    the OEM finder's make names and their unambiguous abbreviations (TOY and TOYTA → TOYOTA, NIS → NISSAN, VW → VOLKSWAGEN), plus
    CATALOG_EXTRA_MAKES. A make inside a group keeps its own name (AUDI, JEEP, GMC), not the group's."""
    from .oem_finder import MAKE_TOKENS
    from .oem_links import EXTRA_MAKES
    makes = {}
    for token, canonical in {**MAKE_TOKENS, **EXTRA_MAKES}.items():
        if token in NOT_A_MAKE:
            continue
        abbreviation = token != canonical and len(token) < len(canonical) and canonical.startswith(token[:3])
        makes[token] = MAKE_NAMES.get(token) or (canonical if abbreviation else token)
    for make in settings.CATALOG_EXTRA_MAKES:
        make = clean(make)
        if make:
            makes.setdefault(make, make)
    return makes


def library_oem(code, brand):
    """(OEM number, manufacturer, citation) the OEM library files this aftermarket code under, when it names exactly one."""
    from .oem_reference_models import OEMCrossReference
    rows = list(OEMCrossReference.objects.filter(number=normalized_reference(code), brand__iexact=brand, reference__isnull=False)
                .select_related('reference')[:2])
    if len(rows) != 1:
        return '', '', ''
    reference, citations = rows[0].reference, rows[0].citations or []  # the number's Reference (S4): its code is the first printed form
    return reference.code[:120], reference.brand[:120], str(citations[0])[:300] if citations else ''


def merge_references(declared):
    """The items' references one at a time: a malformed one is skipped, and a typed one (oem, company) wins over an untyped copy."""
    merged = {}
    for reference in declared:
        try:
            [row] = normalize_references([reference])
        except (ValidationError, ValueError):
            continue
        key = (row['brand'], row['code'])
        if key not in merged and len(merged) >= MAX_REFERENCES:
            continue
        if key not in merged or merged[key].get('ref_type', 'unknown') == 'unknown':
            merged[key] = row
    return [merged[key] for key in sorted(merged)]


def printed_forms(items):
    """The items' codes as printed, the best first: the form most suppliers use, then one without spaces, then the one with its
    separators (04465-02220 over 0446502220). Stock plays no part: it changes all the time."""
    codes = Counter(item.codigo.strip().upper() for item in items)
    return sorted(codes, key=lambda code: (-codes[code], ' ' in code, -len(code), code))


OWNERS = (*CATALOG, 'unificada')  # the lines catalog_owners reads: the catalog's, and those merged into one


def catalog_owners(code, brands=()):
    """Catalog SKUs this code already is once normalized (MR-968365 = MR968365): their own SKU, retired and merged ones included, or an
    alterno of theirs under one of these brands or with none. A merged SKU answers with the one it was merged into. Lines off the
    catalog (mall.lines: referencia, preparación, rechazada) own nothing here.
    [(id, sku, active)]: a retired one still blocks a second SKU, but items can only be linked to an active one."""
    key = normalized_reference(code)
    if not key:
        return []
    rx, brands = reference_pattern(key), {clean(brand) for brand in brands if brand}
    owners = {(merged_id or pk, merged or sku, merged_active if merged_id else active) for pk, sku, active, merged_id, merged, merged_active in
              catalog_lines(OWNERS).filter(sku__iregex=rx).values_list('pk', 'sku', 'active', 'merged_into_id', 'merged_into__sku', 'merged_into__active')
              if normalized_reference(sku) == key}
    owners |= {(merged_id or pk, merged or sku, merged_active if merged_id else active)
               for found, brand, pk, sku, active, merged_id, merged, merged_active in PartCode.objects.filter(catalog_q(OWNERS, 'part__'), code__iregex=rx)
               .values_list('code', 'brand', 'part_id', 'part__sku', 'part__active', 'part__merged_into_id', 'part__merged_into__sku', 'part__merged_into__active')
               if normalized_reference(found) == key and (not brand or not brands or clean(brand) in brands)}
    return sorted(owners, key=lambda owner: owner[1])


def owner_label(owner):
    return f'{owner[1]}{"" if owner[2] else " (retirado)"}'


# --- the matching worker's stage -----------------------------------------------------------------------------------------------------

def proposals_stage():
    """The matching worker's Productos nuevos stage (settings.SKU_PROPOSALS_STAGE): from S6 it places the backlog on lines in
    preparación (mall.new_products.backlog_stage) instead of refreshing proposals; a failure is logged and the pass goes on."""
    from .new_products import backlog_stage
    return backlog_stage()
