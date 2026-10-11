import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class SkuProposal(models.Model):
    """A new catalog SKU proposed from supplier items that match nothing in the catalog (mall.sku_proposals): one product, however many
    suppliers carry it. Nothing reaches the catalog until an administrator approves it. Each field remembers who filled it last
    (`field_sources`: the supplier's data, the OEM library, an administrator or, later, an AI investigation) so the investigation can
    fill what is missing without overwriting what a person decided."""
    STATUSES = [('pending', 'Por revisar'), ('approved', 'Aprobado'), ('linked', 'Vinculado a un SKU existente'), ('rejected', 'Rechazado'),
                ('resolved', 'Resuelto por el emparejamiento')]
    REJECT_REASONS = [('duplicate', 'Ya existe en el catálogo'), ('not_a_part', 'No es un repuesto'), ('bad_data', 'Datos del proveedor incorrectos'),
                      ('other', 'Otro motivo')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=400, db_index=True, verbose_name='clave del producto',
                           help_text='Código compacto y marca: el mismo producto de varios proveedores. Una propuesta abierta por producto.')
    status = models.CharField(max_length=10, choices=STATUSES, default='pending', db_index=True, verbose_name='estado')
    sku = models.CharField(max_length=200, verbose_name='SKU propuesto')
    description = models.TextField(blank=True, default='', verbose_name='descripción')
    brand = models.CharField(max_length=120, blank=True, default='', verbose_name='marca del proveedor')
    oem_code = models.CharField(max_length=120, blank=True, default='', verbose_name='número OEM')
    oem_brand = models.CharField(max_length=120, blank=True, default='', verbose_name='fabricante del número OEM')
    part_type = models.ForeignKey('mall.PartType', on_delete=models.PROTECT, null=True, blank=True, related_name='+', verbose_name='grupo y subgrupo')
    references = models.JSONField(default=list, blank=True, verbose_name='alternos', help_text='[{code, brand, ref_type}]')
    field_sources = models.JSONField(default=dict, blank=True, verbose_name='origen de cada campo')
    fingerprint = models.CharField(max_length=64, verbose_name='huella de los artículos')
    version = models.PositiveIntegerField(default=1, verbose_name='versión')
    part = models.ForeignKey('mall.Part', on_delete=models.PROTECT, null=True, blank=True, related_name='proposals', verbose_name='SKU creado o vinculado')
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+', verbose_name='decidido por')
    decided_at = models.DateTimeField(null=True, blank=True, verbose_name='decidido el')
    reject_reason = models.CharField(max_length=20, choices=REJECT_REASONS, blank=True, default='', verbose_name='motivo del rechazo')
    reject_note = models.TextField(blank=True, default='', verbose_name='nota del rechazo')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='creada el')
    updated_at = models.DateTimeField(auto_now=True, db_index=True, verbose_name='última actualización')

    class Meta:
        verbose_name = 'SKU propuesto'
        verbose_name_plural = 'SKU propuestos'
        constraints = [
            models.CheckConstraint(condition=Q(status__in=['pending', 'approved', 'linked', 'rejected', 'resolved']), name='sku_proposal_valid_status'),
            models.CheckConstraint(condition=~Q(status__in=['approved', 'linked']) | Q(part__isnull=False, decided_at__isnull=False), name='sku_proposal_decided_part'),
            models.CheckConstraint(condition=~Q(status='rejected') | (~Q(reject_reason='') & Q(decided_at__isnull=False)), name='sku_proposal_reject_reason'),
            models.CheckConstraint(condition=Q(oem_code='', oem_brand='') | (~Q(oem_code='') & ~Q(oem_brand='')), name='sku_proposal_oem_pair'),
            # Approved and linked proposals are history: the same product can come back as a new proposal (a supplier added later).
            models.UniqueConstraint(fields=['key'], condition=Q(status__in=['pending', 'rejected', 'resolved']), name='sku_proposal_open_key'),
        ]


class SkuProposalItem(models.Model):
    """A supplier item behind a proposal (each item is behind one at a time). `snapshot` is the item as the proposal saw it: approving
    refuses when the item changed since. `seen_at` is the item's own updated_at when it was read, so a save after that read is picked
    up again even if it lands while the proposal is being written."""
    proposal = models.ForeignKey(SkuProposal, on_delete=models.CASCADE, related_name='items', verbose_name='SKU propuesto')
    item = models.OneToOneField('mall.SupplierItem', on_delete=models.CASCADE, related_name='sku_proposal', verbose_name='artículo del proveedor')
    snapshot = models.JSONField(default=dict, verbose_name='artículo al proponerlo')
    added_at = models.DateTimeField(auto_now_add=True, verbose_name='agregado el')
    seen_at = models.DateTimeField(default=timezone.now, verbose_name='visto por última vez', help_text='Un artículo guardado después vuelve a revisarse.')

    class Meta:
        verbose_name = 'artículo de un SKU propuesto'
        verbose_name_plural = 'artículos de SKU propuestos'
