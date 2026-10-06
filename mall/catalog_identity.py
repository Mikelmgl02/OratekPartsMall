"""Canonical naming from approved references, never from a code's appearance.

An OEM reference and its provenance must already be in the reviewed library.
Manufacturer suffix rules identify a company, not interchangeability or an OEM.
"""
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import ValidationError
from .catalog_families import normalized_reference
from .models import CatalogIdentityChange, Part, PartCode


def company_reference(sku):
    # This registry is deliberately explicit and can be extended per deployment.
    registry = getattr(settings, 'CATALOG_COMPANY_SUFFIXES', {'FEB': 'FEBEST', 'FEBEST': 'FEBEST'})
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
    if info and not Part.objects.filter(sku__iexact=info['code']).exclude(pk=part.pk).exists():
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
    collision = (Part.objects.filter(sku__iexact=target.code).exclude(pk=part.pk).exists()
                 or PartCode.objects.filter(code__iexact=target.code).exclude(part=part).exists()
                 or PartCode.objects.filter(code__iexact=part.sku).exclude(part=part).exists())
    if collision:
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
    for suffix in getattr(settings, 'CATALOG_COMPANY_SUFFIXES', {'FEB': 'FEBEST', 'FEBEST': 'FEBEST'}):
        criteria |= Q(sku__endswith='-' + suffix)
    ids = Part.objects.filter(criteria, active=True, merged_into__isnull=True).values_list('pk', flat=True).distinct()
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
