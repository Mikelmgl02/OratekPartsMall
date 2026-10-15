import hashlib
import json
from django.db import transaction
from rest_framework.exceptions import ValidationError
from django.db.models import Q
from .lines import storefront_lines
from .models import Account, InventoryUpdate, StockEntry, SupplierItem
from .references import live_q, own_interno_q
from .matching_queue import enqueue_matching

def matching_part(codigo, brand, supplier=None):
    # The storefront line a code names exactly (the code before S6 matched loads with it; from S6 mall.reference_matching does).
    # A live code matches as printed or as another spelling it keeps (printed_forms: a duplicate spelling S2a retired, 13780-61J00-NP-);
    # a retired code matches nothing. A supplier's own interno code (S3) matches that supplier's items only.
    shown = live_q('codes__')
    if supplier is not None:
        shown = shown | own_interno_q(supplier, 'codes__')
    matches = list(storefront_lines().filter(
        Q(sku=codigo) | Q(shown, codes__code=codigo, codes__brand__in=[brand, ''])
        | Q(shown, codes__printed_forms__contains=[codigo], codes__brand__in=[brand, ''])
    ).distinct()[:2])
    # Ambiguous legacy code mappings require review instead of arbitrary matching.
    return matches[0] if len(matches) == 1 else None

@transaction.atomic
def ingest_inventory(*, supplier, actor, data, queue_matching=True, recorder=None):
    """Create or update one supplier item from a load and move its count; recorder (mall.inventory_history.Recorder) keeps what changed
    for the supplier's ledger. The one path of Excel, the API and apiag-cloud, and from S6 the matching of a new or changed item too
    (mall.reference_matching, synchronous): an item without a Reference finds its line's Reference or gets one (rules 1 to 3), waits in a
    line case, or gets a line of its own in Productos nuevos (rule 4); an attached item's code change applies on its own when it keeps the
    item on its line, or waits in codigo_pendiente (review, out of stock) with a code_change case while codigo and brand stay; each
    declared code not on its line is a reference case. Lock order (plan §1.6.5): the supplier's Account, the item read unlocked (every
    writer of a supplier's items takes that lock first), the matching's catalog writes, the item locked and written."""
    from . import reference_matching, variants
    from .inventory_history import field_changes, snapshot
    from .part_references import normalize_references
    # Serialize writes per supplier, including creation and idempotency checks.
    Account.objects.select_for_update().get(pk=supplier.pk)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    previous = InventoryUpdate.objects.filter(supplier=supplier, key=data['update_id']).first()
    if previous:
        if previous.payload_hash != digest:
            raise ValidationError('El identificador de esta actualización ya se utilizó con datos diferentes.')
        return previous.item
    item = SupplierItem.objects.filter(supplier=supplier, supplier_invent_id=data['supplier_invent_id']).select_related('part').first()
    created = item is None
    if not created and item.source != data['source']:
        raise ValidationError('Este artículo pertenece a otro origen de inventario. Un administrador debe revisar el origen antes de actualizarlo.')
    if created:  # inserted unattached first, so the matching's cases and place() have its row
        item = SupplierItem.objects.create(supplier=supplier, supplier_invent_id=data['supplier_invent_id'], codigo=data['codigo'],
                                           brand=data['brand'], source=data['source'], description=data.get('description', ''))
    item.supplier = supplier
    before, quantity_before = (None, 0) if created else (snapshot(item), item.reported_quantity)
    if 'description' not in data:
        data = {**data, 'description': item.description}
    data = {**data, 'references': normalize_references(data.get('references', item.references))}
    changed = created or any(getattr(item, field) != data[field] for field in ['codigo', 'brand', 'description', 'references'])
    if not created:
        variants.ensure_attached(item, actor=actor)  # an item a queryset or the code before S3 left on a line without its Reference
    # What the load does (read-only): rules 1-5 for an item without a Reference, rule 5b for an attached one.
    load = reference_matching.plan_load(item, data, supplier=supplier)
    delta = data['quantity'] - item.reported_quantity
    item.description = data.get('description', '')
    item.references = data['references']
    reference_matching.carry_out_load(item, load, actor=actor)  # the catalog writes first (a Reference, a line in preparación)
    # Then the item itself, locked: a deal confirmation locks items without the Account, so its reserved units are read again here.
    item.reserved_quantity = SupplierItem.objects.select_for_update().values_list('reserved_quantity', flat=True).get(pk=item.pk)
    item.reported_quantity = data['quantity']
    stamp_variant_data(item, data)
    item._attached = True  # the matching hung it (or left it unattached) on purpose: SupplierItem.save's shim has nothing to do
    item.save()
    reference_matching.finish_load(item, load, actor=actor, changed=changed)
    entry = None
    if delta:
        entry = StockEntry.objects.create(item=item, kind='sync', quantity=abs(delta), direction='credit' if delta > 0 else 'debit',
            reported_after=item.reported_quantity, reserved_after=item.reserved_quantity,
            actor=actor, reference=data['update_id'])
    InventoryUpdate.objects.create(supplier=supplier, key=data['update_id'], payload_hash=digest, item=item)
    if recorder is not None:
        fields = {} if created else field_changes(before, item)
        action = 'created' if created else 'updated' if fields else 'stock' if delta else None
        if action:
            recorder.record(item, action, fields=fields, before=quantity_before, after=item.reported_quantity, entry=entry)
    # queue_matching=False: the caller knows only the quantity moved (the apiag-cloud pull), which changes nothing to match.
    if changed or (queue_matching and item.matching_status != 'matched'):
        enqueue_matching()
    return item


def stamp_variant_data(item, data):
    """The variant's own data a load sends (rule 10, S3.2: material, origen, garantia, calidad, condicion, unidades_por_empaque): only
    the keys present are written, each changed one stamped in field_sources with where it came from."""
    from django.utils import timezone
    from .serializers import VARIANT_DATA
    sources = dict(item.field_sources or {})
    for field in VARIANT_DATA:
        if field in data and getattr(item, field) != data[field]:
            setattr(item, field, data[field])
            sources[field] = {'source': 'proveedor', 'via': data.get('source', ''), 'update': data.get('update_id', ''),
                              'at': timezone.now().isoformat()}
    item.field_sources = sources
    return item


def initial_match(item, data, match):
    """The exact-code phase of the code before S6 (mall.variants.outcome still reads it); from S6 ingestion and the Excel preview take
    mall.reference_matching.plan_load instead."""
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
