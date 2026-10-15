"""Canonical naming from approved references, never from a code's appearance.

An OEM reference and its provenance must already be in the reviewed library.
Manufacturer suffix rules identify a company, not interchangeability or an OEM.
From S4 the OEM list is the library's References (mall.references.library_q): exact_listed and disputed_codes read them, and
mark_exact_matches (which marked OEM the SKUs whose compact code was a declared or verified number of the list) is retired, since a
line holding its number as a Reference mirrors its main's tipo.

Since S2b a line's identity is its main Reference (mall.references): prefer_oem makes the chosen OEM Reference the main
(references.set_main, the codigo following it while the line is live) instead of renaming the line and keeping its old SKU as an
alterno, which the old main now is anyway; marking OEM retypes the main (references.retype_main).
"""
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import ValidationError
from .catalog_families import normalized_reference
from .lines import all_lines, storefront_lines
from . import references
from .models import Part, Reference


NON_ALNUM = '[^0-9A-Za-z]*'


def reference_pattern(key, *, anchored=True):
    """An iregex for every spelling of a normalized reference (MR968365: MR-968365, mr968365_); unanchored, it also finds the key
    inside a longer code. Callers re-check the hits with normalized_reference."""
    body = NON_ALNUM.join(key)
    return f'^{NON_ALNUM}{body}{NON_ALNUM}$' if anchored else body


def reference_owners(code, part, *, merged_here=True):
    """Other Parts (retired included) whose SKU, and other Parts' References, are this reference once normalized.
    merged_here=False ignores Parts already merged into this one: they share its identity."""
    key = normalized_reference(code)
    if not key:
        return []
    rx = reference_pattern(key)
    # A line in referencia (a library number no product carries, S4) owns nothing here: adding its number absorbs it (rule 3f).
    parts = all_lines().filter(sku__iregex=rx).exclude(pk=part.pk).exclude(estado='referencia')
    if not merged_here:
        parts = parts.exclude(merged_into=part.pk)
    return ([s for s in parts.values_list('sku', flat=True) if normalized_reference(s) == key]
            + [c for c in Reference.objects.filter(code__iregex=rx, is_main=False).exclude(part=part.pk).exclude(part__estado='referencia')
               .values_list('code', flat=True) if normalized_reference(c) == key])  # a main is its SKU


def rename_conflicts(part, code):
    """Why renaming part to code would make a reference ambiguous: the new MAIN, or the old SKU it keeps as an alterno, already
    identifies another Part (its SKU, retired included, or an alterno) under any punctuation (MR-968365 = MR968365 = MR968365_).
    A Part merged into this one shares its identity (Agrupar SKU, then the OEM as MAIN): from S4 its codigo is frozen and outside the
    codigo's uniqueness (line_codigo_unique_catalog), so not even its exact SKU blocks the MAIN (a library number's line in referencia
    absorbed by its code is one)."""
    return reference_owners(code, part, merged_here=False) + reference_owners(part.sku, part, merged_here=False)


def company_registry():
    """An explicit CATALOG_COMPANY_SUFFIXES setting wins; otherwise the admin suffix table (company_code TAGs, FEB/FEBEST)."""
    if hasattr(settings, 'CATALOG_COMPANY_SUFFIXES'):
        return settings.CATALOG_COMPANY_SUFFIXES
    from .catalog_suffixes import company_suffixes
    return company_suffixes()


def company_reference(sku):
    registry = company_registry()
    base, sep, suffix = sku.strip().upper().rpartition('-')
    if sep and base and suffix in registry:
        return {'code': base, 'brand': registry[suffix].strip().upper(), 'ref_type': 'company'}
    return None


def reference_type(code):
    return code.get('ref_type') or {'oem': 'oem', 'manufacturer': 'company'}.get(code.get('kind'), 'unknown')


def identity_status(part):
    """What Inventario's MAIN badge says (ManagedPart.identity), from the line's main Reference: its tipo as the legacy ref_type and
    its brand. A line without a main yet (before mall.refactor.s2b_main_refs) falls back to its codigo, as before S2b."""
    codes = list(part.codes.all())
    main = references.main_in(codes)
    oems = [c for c in codes if c.ref_type == 'oem']
    verified = [c for c in oems if c.brand and c.reference_source.strip()]
    status = ('oem' if part.is_OEM else 'choose_oem' if len({normalized_reference(c.code) for c in verified}) > 1
              else 'oem_available' if verified else 'needs_oem')
    if main is not None:
        brand = main.brand or ((company_reference(part.sku) or {}).get('brand', '') if main.ref_type == 'company' else '')
        return {'ref_type': 'oem' if part.is_OEM else main.ref_type if main.ref_type != 'unknown' else
                ('company' if company_reference(part.sku) else 'unknown'), 'status': status, 'brand': brand}
    current = [c for c in codes if normalized_reference(c.code) == normalized_reference(part.sku)]
    first = next((c for c in verified if c in current), None)
    return {
        'ref_type': 'oem' if part.is_OEM else next((c.ref_type for c in current if c.ref_type == 'company'),
                                          'company' if company_reference(part.sku) else 'unknown'),
        'status': status,
        'brand': first.brand if first else next((c.brand for c in current if c.ref_type == 'company'),
                                               (company_reference(part.sku) or {}).get('brand', '')),
    }


@transaction.atomic
def prefer_oem(part, *, actor=None, selected=None, confirm_current=False):
    """Make the line's verified OEM Reference its main (references.set_main), keeping the SAME line, its stock and every code: while
    the line is live its codigo follows the main, and the former main stays one of its References.

    An explicit reviewed choice can select among several approved OEMs. Otherwise keep an existing OEM main or select the sole
    verified OEM. Collisions require the existing merge review instead of silently combining unrelated catalog items.
    """
    part = Part.objects.select_for_update().get(pk=part.pk)
    if part.merged_into_id or not part.active:
        if selected:
            raise ValidationError('Selecciona un SKU principal activo.')
        return part
    # A main explicitly marked OEM is already canonical. Only a reviewed choice
    # can replace it with another OEM; its alternos do not own this flag.
    if part.is_OEM and not selected:
        return part
    oems = list(part.codes.filter(ref_type='oem').order_by('code', 'brand', 'pk'))
    eligible = [c for c in oems if c.brand and c.reference_source.strip()]
    if selected:
        target = next((c for c in eligible if c.code == selected['code'] and c.brand == selected['brand']), None)
        if not target:
            raise ValidationError({'main_reference': 'El MAIN debe ser una referencia OEM registrada con fabricante y fuente de equivalencia.'})
    else:
        target = next((c for c in eligible if c.code == part.sku), None)
        if not target and len({normalized_reference(c.code) for c in oems}) == 1:
            target = eligible[0] if eligible else None
    if not target:
        return part
    if target.code == part.sku:
        if (selected or confirm_current) and not part.is_OEM:
            references.set_main(part, target, actor=actor, reason='OEM confirmado')
        return part
    if rename_conflicts(part, target.code):
        if selected:
            raise ValidationError('El OEM ya identifica otro SKU. Usa Agrupar SKU para revisar y conservar todos sus vínculos.')
        holder = codigo_owner(part, target.code)
        if holder is not None:  # the OEM is another catalog line's codigo: a person decides through a merge case (S2b, set_main)
            references.open_main_case(part, holder, target, 'OEM preferido')
        return part
    try:
        references.set_main(part, target, actor=actor, reason='OEM preferido', open_case=not selected)
    except references.MainConflict:
        if selected:
            raise ValidationError('El OEM ya identifica otro SKU. Usa Agrupar SKU para revisar y conservar todos sus vínculos.')
    return part


def codigo_owner(part, code):
    """The catalog line (publicada or retirada, not merged) whose codigo is this code under any punctuation, other than part and the
    lines merged into it, or None."""
    key = normalized_reference(code)
    if not key:
        return None
    rows = catalog_lines().filter(sku__iregex=reference_pattern(key)).exclude(pk=part.pk).order_by('pk')
    return next((line for line in rows if normalized_reference(line.sku) == key), None)


def classify_company_main(part, actor=None):
    """A line whose codigo ends in a company suffix (-FEB: FEBEST): a brandless otro main becomes that company's aftermarket code, and
    the company's own code (the codigo without the suffix) is one of its References when no other line holds it (preserve_sku_reference
    filed both as alternos before S2b). Returns whether the main changed."""
    info = company_reference(part.sku)
    if not info:
        return False
    main = references.main_of(part)
    changed = False
    if main is not None and not main.brand and main.tipo == 'otro':
        after = references.retype_main(part, 'aftermarket', brand=info['brand'], actor=actor, reason='Sufijo de empresa')
        changed = bool(after and after.tipo == 'aftermarket')
    company_base_code(part, part.sku, actor)
    return changed


def company_base_code(part, sku, actor=None):
    """File the company's own code of `sku` (its codigo without a company suffix: 0110-GSU45A48 of 0110-GSU45A48-FEB, brand FEBEST) as
    an aftermarket Reference of `part`, unless another line holds it. Returns the Reference, or None."""
    info = company_reference(sku)
    if not info or all_lines().filter(sku__iexact=info['code']).exclude(pk=part.pk).exists():
        return None
    try:
        return references.add(part, info['code'], info['brand'], tipo='aftermarket', actor=actor)[0]
    except references.ReferenceConflict:
        return None  # another line holds the key (mall.references.validate): grouping review decides


def reconcile_identities(actor=None):
    """Shared by every feed: inexpensive local references before AI matching."""
    criteria = Q(codes__ref_type='oem', codes__estado__in=references.LIVE)
    for suffix in company_registry():
        criteria |= Q(sku__endswith='-' + suffix)
    # A main already marked OEM is left alone by prefer_oem and never takes a company alterno: skip it up front.
    ids = storefront_lines().filter(criteria, is_OEM=False).values_list('pk', flat=True).distinct()
    promoted = 0
    for pk in list(ids):
        with transaction.atomic():
            part = Part.objects.select_for_update().get(pk=pk)
            if not part.is_OEM and company_reference(part.sku):
                classify_company_main(part, actor)
            before = part.sku
            updated = prefer_oem(part, actor=actor)
            promoted += int(before != updated.sku)
    return promoted


# --- the OEM list (from S4: the library's References) ---------------------------------------------------------------------------------

EXACT_MIN = 8  # a shorter number can coincide with an unrelated code
EXACT_SOURCE = 'Su código está en la lista OEM'
COUNTING = ('declarado', 'verificado')  # a number only the OEM finder inferred, or one in dispute, says nothing on its own


def counts(code):
    """A compact number long and specific enough to name a line on its own."""
    return len(code) >= EXACT_MIN and any(char.isdigit() for char in code)


def exact_listed(sku):
    """The declared or verified numbers of the library (References, mall.references.library_q) whose clave is this SKU code, when the
    code counts on its own: such a line is OEM by definition."""
    from .models import Reference
    code = normalized_reference(sku)
    if not counts(code):
        return []
    return list(Reference.objects.filter(references.library_q(), clave=code, estado__in=COUNTING).order_by('brand'))


def disputed_codes(codes=None):
    """Numbers the library has only in dispute (every manufacturer that files them disputes them): the OEM finder never applies them."""
    from .models import Reference
    library = Reference.objects.filter(references.library_q())
    bad = library.filter(estado='disputa')
    if codes is not None:
        bad = bad.filter(clave__in=[normalized_reference(code) for code in codes])
    bad = set(bad.values_list('clave', flat=True))
    return bad - set(library.filter(clave__in=bad).exclude(estado='disputa').values_list('clave', flat=True)) if bad else set()


def exact_items(part_ids=None, codes=None):
    """Retired with S4 (with mark_exact_matches): a line whose codigo is a library number holds that number as a Reference, and its
    tipo mirrors its main (S2b). Returns {}."""
    return {}


def mark_exact_matches(actor=None, *, part_ids=None, codes=None, apply=True):
    """Retired with S4 (plan §3 S4): marking OEM the lines whose codigo is a number of the OEM list is moot once the library's numbers
    are References and a line's tipo mirrors its main; OEM_EXACT_MARK is ignored. Returns []."""
    return []


