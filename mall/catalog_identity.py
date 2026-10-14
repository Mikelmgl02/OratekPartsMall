"""Canonical naming from approved references, never from a code's appearance.

An OEM reference and its provenance must already be in the reviewed library.
Manufacturer suffix rules identify a company, not interchangeability or an OEM.
mark_exact_matches marks OEM (never renames) the SKUs whose compact code is a declared or verified number of the OEM list, 8 or more
characters with a digit: a SKU is OEM when its code is the OEM number.
"""
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import ValidationError
from .catalog_families import normalized_reference
from .lines import all_lines, catalog_lines, storefront_lines
from .models import CatalogIdentityChange, Part, PartCode


NON_ALNUM = '[^0-9A-Za-z]*'


def reference_pattern(key, *, anchored=True):
    """An iregex for every spelling of a normalized reference (MR968365: MR-968365, mr968365_); unanchored, it also finds the key
    inside a longer code. Callers re-check the hits with normalized_reference."""
    body = NON_ALNUM.join(key)
    return f'^{NON_ALNUM}{body}{NON_ALNUM}$' if anchored else body


def reference_owners(code, part, *, merged_here=True):
    """Other Parts (retired included) whose SKU, and other Parts' PartCodes, are this reference once normalized.
    merged_here=False ignores Parts already merged into this one: they share its identity."""
    key = normalized_reference(code)
    if not key:
        return []
    rx = reference_pattern(key)
    parts = all_lines().filter(sku__iregex=rx).exclude(pk=part.pk)
    if not merged_here:
        parts = parts.exclude(merged_into=part.pk)
    return ([s for s in parts.values_list('sku', flat=True) if normalized_reference(s) == key]
            + [c for c in PartCode.objects.filter(code__iregex=rx).exclude(part=part.pk).values_list('code', flat=True)
               if normalized_reference(c) == key])


def rename_conflicts(part, code):
    """Why renaming part to code would make a reference ambiguous: the new MAIN, or the old SKU it keeps as an alterno, already
    identifies another Part (its SKU, retired included, or an alterno) under any punctuation (MR-968365 = MR968365 = MR968365_).
    A Part merged into this one shares its identity (Agrupar SKU, then the OEM as MAIN); only its exact SKU still blocks the MAIN."""
    return (reference_owners(code, part, merged_here=False) + reference_owners(part.sku, part, merged_here=False)
            + list(all_lines().filter(sku__iexact=code, merged_into=part.pk).values_list('sku', flat=True)))


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
    codes = list(part.codes.all())
    oems = [c for c in codes if c.ref_type == 'oem']
    current = [c for c in codes if normalized_reference(c.code) == normalized_reference(part.sku)]
    verified = [c for c in oems if c.brand and c.reference_source.strip()]
    main = next((c for c in verified if c in current), None)
    return {
        'ref_type': 'oem' if part.is_OEM else next((c.ref_type for c in current if c.ref_type == 'company'),
                                          'company' if company_reference(part.sku) else 'unknown'),
        'status': 'oem' if part.is_OEM else 'choose_oem' if len({normalized_reference(c.code) for c in verified}) > 1
                  else 'oem_available' if verified else 'needs_oem',
        'brand': main.brand if main else next((c.brand for c in current if c.ref_type == 'company'),
                                             (company_reference(part.sku) or {}).get('brand', '')),
    }


def preserve_sku_reference(part, sku, *, is_oem=False):
    """Keep the old searchable code; only classify known suffixes as company."""
    info = None if is_oem else company_reference(sku)
    # A full supplier code remains a neutral alias for feeds with no brand.
    defaults = {'ref_type': 'company', 'brand': info['brand']} if info else {'ref_type': 'oem' if is_oem else 'unknown', 'brand': ''}
    existing = part.codes.filter(code=sku).first()
    if not existing and PartCode.objects.filter(code__iexact=sku).exclude(part=part).exists():
        return  # Conflicting library ownership must go through grouping review.
    if not existing:
        PartCode.objects.get_or_create(part=part, code=sku, brand=defaults['brand'],
                                       defaults={'ref_type': defaults['ref_type']})
    elif is_oem and existing.ref_type == 'unknown':
        existing.ref_type = 'oem'
        existing.save(update_fields=['ref_type'])
    elif info and existing.ref_type == 'unknown':
        if not PartCode.objects.filter(brand=info['brand'], code=sku).exclude(pk=existing.pk).exists():
            existing.brand, existing.ref_type = info['brand'], 'company'
            existing.save(update_fields=['brand', 'ref_type'])
    if info and not all_lines().filter(sku__iexact=info['code']).exclude(pk=part.pk).exists():
        if not PartCode.objects.filter(code__iexact=info['code']).exclude(part=part).exists():
            PartCode.objects.get_or_create(part=part, code=info['code'], brand=info['brand'],
                                           defaults={'ref_type': 'company'})


@transaction.atomic
def prefer_oem(part, *, actor=None, selected=None, confirm_current=False):
    """Rename the SAME master UUID, retaining supplier stock and every old code.

    An explicit reviewed choice can select among several approved OEMs. Otherwise
    keep an existing OEM or select the sole verified OEM. Collisions require the
    existing merge review instead of silently combining unrelated catalog items.
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
            part.is_OEM = True
            part.save(update_fields=['is_OEM'])
        return part
    if rename_conflicts(part, target.code):
        if selected:
            raise ValidationError('El OEM ya identifica otro SKU. Usa Agrupar SKU para revisar y conservar todos sus vínculos.')
        return part
    old = part.sku
    if len(old) > 120:
        if selected:
            raise ValidationError('El código anterior supera 120 caracteres y no puede conservarse como alterno.')
        return part
    preserve_sku_reference(part, old, is_oem=part.is_OEM)
    part.sku = target.code
    part.is_OEM = True
    if part.name == old:
        part.name = target.code
    part.save(update_fields=['sku', 'name', 'is_OEM'])
    CatalogIdentityChange.objects.create(part=part, previous_sku=old, sku=part.sku, actor=actor,
        reference={'code': target.code, 'brand': target.brand, 'ref_type': 'oem', 'reference_source': target.reference_source})
    return part


def reconcile_identities(actor=None):
    """Shared by every feed: inexpensive local references before AI matching."""
    criteria = Q(codes__ref_type='oem')
    for suffix in company_registry():
        criteria |= Q(sku__endswith='-' + suffix)
    # A main already marked OEM is left alone by prefer_oem and never takes a company alterno: skip it up front.
    ids = storefront_lines().filter(criteria, is_OEM=False).values_list('pk', flat=True).distinct()
    promoted = 0
    for pk in list(ids):
        with transaction.atomic():
            part = Part.objects.select_for_update().get(pk=pk)
            if not part.is_OEM and company_reference(part.sku):
                preserve_sku_reference(part, part.sku)
            before = part.sku
            updated = prefer_oem(part, actor=actor)
            promoted += int(before != updated.sku)
    return promoted


# --- a SKU whose code is in the OEM list is OEM -------------------------------------------------------------------------------------

EXACT_MIN = 8  # a shorter number can coincide with an unrelated code: those SKUs are marked by a person (Marcar como OEM)
EXACT_SOURCE = 'Su código está en la lista OEM'
COUNTING = ('declared', 'verified')  # an item only the OEM finder inferred, or one in dispute, marks nothing on its own


def counts(code):
    """A compact number long and specific enough to mark a SKU on its own."""
    return len(code) >= EXACT_MIN and any(char.isdigit() for char in code)


def exact_listed(sku):
    """The declared or verified items of the OEM list whose number is this SKU code, when the code counts for marking."""
    from .oem_reference_models import OEMReference
    code = normalized_reference(sku)
    return list(OEMReference.objects.filter(code=code, status__in=COUNTING).order_by('manufacturer')) if counts(code) else []


def disputed_codes(codes=None):
    """Numbers the OEM list has only in dispute (every manufacturer that files them disputes them): they make no SKU OEM and the OEM
    finder never applies them."""
    from .oem_reference_models import OEMReference
    bad = OEMReference.objects.filter(status='disputed')
    if codes is not None:
        bad = bad.filter(code__in=[normalized_reference(code) for code in codes])
    bad = set(bad.values_list('code', flat=True))
    return bad - set(OEMReference.objects.filter(code__in=bad).exclude(status='disputed').values_list('code', flat=True)) if bad else set()


def exact_items(part_ids=None, codes=None):
    """{part id: [OEMReference]} for active SKUs not yet OEM whose compact code is a declared or verified number of the OEM list."""
    from .oem_reference import compact_sql
    from .oem_reference_models import OEMReference
    refs = OEMReference.objects.filter(status__in=COUNTING)
    if codes is not None:
        refs = refs.filter(code__in=[normalized_reference(code) for code in codes])
    parts = (storefront_lines().filter(is_OEM=False).annotate(compact=compact_sql('sku'))
             .filter(compact__in=refs.values('code')))
    if part_ids is not None:
        parts = parts.filter(pk__in=list(part_ids))
    found = {}
    by_code = {}
    for pk, number in parts.values_list('pk', 'compact'):
        if counts(number):
            by_code.setdefault(number, []).append(pk)
    for ref in refs.filter(code__in=list(by_code)).order_by('manufacturer'):
        for pk in by_code[ref.code]:
            found.setdefault(pk, []).append(ref)
    return found


def mark_exact_matches(actor=None, *, part_ids=None, codes=None, apply=True):
    """Mark OEM every SKU whose code is a declared or verified number of the OEM list (MR-453383 = MITSUBISHI MR453383): the SKU uses
    the OEM number, so it is that OEM item's variant. Each mark leaves a CatalogIdentityChange naming the item. Returns
    [(part id, sku, [(manufacturer, number)])]."""
    from .oem_finder import OEMFinder
    from .oem_links import same_make
    from .oem_reference_models import OEMReference
    marked = []
    for pk, refs in exact_items(part_ids, codes).items():
        with transaction.atomic():
            part = storefront_lines(Part.objects.select_for_update()).filter(pk=pk, is_OEM=False).first()
            if part is None or normalized_reference(part.sku) != refs[0].code:
                continue
            # Part, then the item (the order of every writer): a dispute committed since the snapshot marks nothing.
            refs = list(OEMReference.objects.select_for_update().filter(pk__in=[ref.pk for ref in refs], status__in=COUNTING).order_by('manufacturer'))
            if not refs:
                continue
            makes = OEMFinder.explicit_makes(part.description or '')
            ref = (next((ref for ref in refs if ref.manufacturer in makes), None)
                   or next((ref for ref in refs if any(same_make(ref.manufacturer, make) for make in makes)), refs[0]))
            marked.append((part.pk, part.sku, [(item.manufacturer, item.code) for item in refs]))
            if not apply:
                continue
            part.is_OEM = True
            part.save(update_fields=['is_OEM'])
            CatalogIdentityChange.objects.create(part=part, previous_sku=part.sku, sku=part.sku, actor=actor, reference={
                'code': ref.code, 'brand': ref.manufacturer, 'ref_type': 'oem', 'reference_source': EXACT_SOURCE, 'action': 'exact_match'})
    return marked


def unmark_disputed(ref, actor=None):
    """A number put in dispute stops making its exact SKUs OEM, unless another item of the list not in dispute still has their code.
    The caller holds the transaction. Returns the SKUs unmarked."""
    from .oem_reference import compact_sql
    from .oem_reference_models import OEMReference
    if OEMReference.objects.filter(code=ref.code).exclude(status='disputed').exclude(pk=ref.pk).exists():
        return []
    unmarked = []
    for part in (catalog_lines(queryset=Part.objects.select_for_update()).filter(is_OEM=True).annotate(compact=compact_sql('sku'))
                 .filter(compact=ref.code)):
        part.is_OEM = False
        part.save(update_fields=['is_OEM'])
        CatalogIdentityChange.objects.create(part=part, previous_sku=part.sku, sku=part.sku, actor=actor, reference={
            'code': ref.code, 'brand': ref.manufacturer, 'ref_type': 'oem', 'reference_source': f'{ref.manufacturer} {ref.code} en disputa',
            'action': 'disputed'})
        unmarked.append(part.sku)
    return unmarked
