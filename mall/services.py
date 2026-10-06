import hashlib
import json
from django.db import transaction
from rest_framework.exceptions import ValidationError
from django.db.models import Q
from .models import Account, InventoryUpdate, Part, StockEntry, SupplierItem
from .matching_queue import enqueue_matching

def matching_part(codigo, brand):
    matches = list(Part.objects.filter(active=True, merged_into__isnull=True).filter(
        Q(sku=codigo) | Q(codes__code=codigo, codes__brand__in=[brand, ''])
    ).distinct()[:2])
    # Ambiguous legacy code mappings require review instead of arbitrary matching.
    return matches[0] if len(matches) == 1 else None

@transaction.atomic
def ingest_inventory(*, supplier, actor, data):
    # Serialize writes per supplier, including creation and idempotency checks.
    Account.objects.select_for_update().get(pk=supplier.pk)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    previous = InventoryUpdate.objects.filter(supplier=supplier, key=data['update_id']).first()
    if previous:
        if previous.payload_hash != digest:
            raise ValidationError('El identificador de esta actualización ya se utilizó con datos diferentes.')
        return previous.item
    item, created = SupplierItem.objects.select_for_update().get_or_create(
        supplier=supplier, supplier_invent_id=data['supplier_invent_id'],
        defaults={'codigo': data['codigo'], 'brand': data['brand'], 'source': data['source']})
    if not created and item.source != data['source']:
        raise ValidationError('Este artículo pertenece a otro origen de inventario. Un administrador debe revisar el origen antes de actualizarlo.')
    if 'description' not in data:
        data = {**data, 'description': item.description}
    from .part_references import normalize_references
    data = {**data, 'references': normalize_references(data.get('references', item.references))}
    changed = created or any(getattr(item, field) != data[field] for field in ['codigo', 'brand', 'description', 'references'])
    match = matching_part(data['codigo'], data['brand'])
    item.part_id, item.matching_status = initial_match(None if created else item, data, match)
    delta = data['quantity'] - item.reported_quantity
    item.codigo = data['codigo']
    item.brand = data['brand']
    item.description = data.get('description', '')
    item.references = data['references']
    item.reported_quantity = data['quantity']
    item.save()
    if delta:
        StockEntry.objects.create(item=item, kind='sync', quantity=abs(delta), direction='credit' if delta > 0 else 'debit',
            reported_after=item.reported_quantity, reserved_after=item.reserved_quantity,
            actor=actor, reference=data['update_id'])
    InventoryUpdate.objects.create(supplier=supplier, key=data['update_id'], payload_hash=digest, item=item)
    if changed or item.matching_status != 'matched':
        enqueue_matching()
    return item


def initial_match(item, data, match):
    """Fast exact-code phase shared by API ingestion and Excel preview.

    Broader family matching runs after commit, never during stock preview.
    """
    from .matching_engine import description, spec_conflict
    part_id = item.part_id if item else None
    declared = data.get('references', item.references if item else [])
    if (item and item.references != declared) or (declared and (not item or
            item.matching_status != 'matched' or item.codigo != data['codigo'] or item.brand != data['brand'] or
            description(item.description) != description(data.get('description', '')))):
        # Resolve every declared reference together after commit. An exact own
        # code must not hide a conflicting reference from the same upload.
        return part_id, 'review' if part_id else 'pending'
    if item and part_id:
        unchanged = item.codigo == data['codigo'] and item.brand == data['brand'] and description(item.description) == description(data.get('description', ''))
        if unchanged and item.matching_status == 'matched':
            return part_id, 'matched'
        if (item.codigo != data['codigo'] or item.brand != data['brand'] or not unchanged):
            return part_id, 'matched' if match and match.pk == part_id and description(item.description) == description(data.get('description', '')) and not spec_conflict(data.get('description', ''), match.description) else 'review'
    if match and not spec_conflict(data.get('description', ''), match.description):
        if not part_id or part_id == match.pk:
            return match.pk, 'matched'
    return part_id, 'review' if part_id else 'pending'
