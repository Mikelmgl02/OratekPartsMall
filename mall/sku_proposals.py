"""Proposed new catalog SKUs (Productos nuevos before inventory refactor S6) and what the lines in preparación still read from them.

Until S6 the matching worker turned supplier items no SKU took into one SkuProposal per product (the same compact code and brand,
whatever the supplier), and an administrator approved, linked or rejected it. From S6 a new product is a line in preparación
(mall.new_products, which replaced this module's write side: place, publish, reject, link, edit, ai_fill), and the S6 stage CLI turned
the pending proposals into those lines with their ids kept. S10 archived the SkuProposal rows (archive schema).

What stays here, read by mall.new_products, mall.sku_review and the matching engine: vehicle_makes() (supplier brands
that are a vehicle make, each with the manufacturer its OEM numbers are filed under), the placeholder brands, merge_references() and
printed_forms() (a product's codes from its items),
catalog_owners() (the catalog SKUs a code already is, for the duplicate checks) and the two refusals the views answer. The matching
worker's proposals stage is gone: its backlog stage (mall.reference_matching.backlog) places the items no line takes."""
from collections import Counter

from rest_framework import status as http
from rest_framework.exceptions import APIException, ValidationError

from .catalog_families import normalized_reference
from .catalog_identity import reference_pattern
from .lines import CATALOG, catalog_lines, catalog_q
from .models import Reference
from .part_references import normalize_references

MAX_REFERENCES = 100
# Productos nuevos' states, as the proposals had them before S6 (the JSON keeps them: mall.sku_proposal_views maps a line's estado).
STATUSES = [('pending', 'Por revisar'), ('approved', 'Aprobado'), ('linked', 'Vinculado a un SKU existente'), ('rejected', 'Rechazado'),
            ('resolved', 'Resuelto por el emparejamiento')]
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
    every brand of tipo vehiculo (KAIYI, SERES: inventory refactor S10 replaced the CATALOG_EXTRA_MAKES setting with the Marcas table).
    A make inside a group keeps its own name (AUDI, JEEP, GMC), not the group's."""
    from .oem_finder import MAKE_TOKENS
    from .oem_links import EXTRA_MAKES
    makes = {}
    for token, canonical in {**MAKE_TOKENS, **EXTRA_MAKES}.items():
        if token in NOT_A_MAKE:
            continue
        abbreviation = token != canonical and len(token) < len(canonical) and canonical.startswith(token[:3])
        makes[token] = MAKE_NAMES.get(token) or (canonical if abbreviation else token)
    from .brand_models import Brand
    for make in Brand.objects.filter(tipo='vehiculo').values_list('nombre', flat=True):
        make = clean(make)
        if make:
            makes.setdefault(make, make)
    return makes


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
               for found, brand, pk, sku, active, merged_id, merged, merged_active in Reference.objects.filter(catalog_q(OWNERS, 'part__'), code__iregex=rx)
               .values_list('code', 'brand', 'part_id', 'part__sku', 'part__active', 'part__merged_into_id', 'part__merged_into__sku', 'part__merged_into__active')
               if normalized_reference(found) == key and (not brand or not brands or clean(brand) in brands)}
    return sorted(owners, key=lambda owner: owner[1])


def owner_label(owner):
    return f'{owner[1]}{"" if owner[2] else " (retirado)"}'


