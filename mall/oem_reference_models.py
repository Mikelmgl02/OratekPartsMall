"""OEM reference library: OEM part numbers stored as facts with provenance, independent of catalog SKUs. New tables only.

OEMReference is one number of one manufacturer, stored COMPACT (uppercase letters and digits: 17801-30070 -> 1780130070); the
spellings printed in sources stay as evidence in printed_forms. OEMReferenceSource is one piece of provenance (a distributor price
list, a catalog page, an approved OEM alterno of the catalog, an OEM finder run...), deduplicated by (reference, kind, citation).
The status follows the sources (mall.oem_reference.derived_status) unless someone disputes the number. Every save takes the next
row version. Rows never hold prices, stock or SKUs: the catalog SKUs that carry a number are computed on read.
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Upper

from .catalog_families import normalized_reference
from .catalog_suffix_models import values

REFERENCE_STATUSES = [('verified', 'Verificado'), ('declared', 'Declarado'), ('inferred', 'Inferido'), ('disputed', 'En disputa')]
SOURCE_KINDS = [('price_list', 'Lista de precios de distribuidor'), ('manufacturer_catalog', 'Catálogo del fabricante'),
                ('aftermarket_catalog', 'Catálogo de marca de repuesto'), ('ai_lookup', 'Búsqueda OEM con IA'),
                ('supplier_declaration', 'Declaración de proveedor'), ('catalog_approval', 'Aprobación en el catálogo'),
                ('oem_finder', 'Buscador OEM'), ('manual', 'Registro manual')]
# Sources a person registers (and may remove); the others follow the catalog's OEM alternos and are never edited by hand.
MANUAL_KINDS = ('manual', 'price_list', 'manufacturer_catalog', 'aftermarket_catalog', 'supplier_declaration')
AUTOMATIC_KINDS = ('catalog_approval', 'oem_finder', 'ai_lookup')
MAX_PRINTED = 20


def compact(code):
    """17801-30070 -> 1780130070, mr-968365 -> MR968365: the stored form of an OEM number (the catalog's normalized_reference)."""
    return normalized_reference(str(code or ''))


def manufacturer_name(value):
    return ' '.join(str(value or '').upper().split())


def spellings(forms, code):
    """Distinct uppercase spellings of this number other than the compact form itself; a spelling of another number is dropped."""
    out = []
    for form in forms or []:
        form = ' '.join(str(form or '').upper().split())[:80]
        if form and form != code and compact(form) == code and form not in out:
            out.append(form)
    return out[:MAX_PRINTED]


class OEMReference(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name='ID')
    manufacturer = models.CharField(max_length=120, verbose_name='fabricante')
    code = models.CharField(max_length=60, verbose_name='número OEM compacto')
    printed_forms = models.JSONField(default=list, blank=True, verbose_name='formas impresas')
    system = models.CharField(max_length=20, blank=True, default='', verbose_name='sistema de numeración')
    family = models.CharField(max_length=20, blank=True, default='', verbose_name='familia')
    part_type = models.CharField(max_length=60, blank=True, default='', verbose_name='tipo de pieza')
    description = models.CharField(max_length=300, blank=True, default='', verbose_name='descripción')
    applications = models.TextField(blank=True, default='', verbose_name='aplicaciones')
    status = models.CharField(max_length=10, choices=REFERENCE_STATUSES, default='inferred', db_index=True, verbose_name='estado')
    dispute_note = models.TextField(blank=True, default='', verbose_name='motivo de la disputa')
    superseded_by = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='supersedes',
                                      verbose_name='reemplazado por')
    notes = models.TextField(blank=True, default='', verbose_name='notas')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='fecha de creación')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='última actualización')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name='creada por')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name='actualizada por')
    version = models.PositiveIntegerField(default=1, verbose_name='versión')

    class Meta:
        verbose_name = 'referencia OEM'
        verbose_name_plural = 'referencias OEM'
        ordering = ['manufacturer', 'code']
        constraints = [
            models.UniqueConstraint(fields=['manufacturer', 'code'], name='oem_reference_unique_number'),
            models.CheckConstraint(condition=Q(code__regex=r'^[0-9A-Z]+$'), name='oem_reference_code_compact'),
            models.CheckConstraint(condition=Q(manufacturer=Upper('manufacturer')) & ~Q(manufacturer=''), name='oem_reference_manufacturer_upper'),
            models.CheckConstraint(condition=Q(status__in=values(REFERENCE_STATUSES)), name='oem_reference_valid_status'),
            models.CheckConstraint(condition=~Q(status='disputed') | ~Q(dispute_note=''), name='oem_reference_dispute_note'),
            models.CheckConstraint(condition=~Q(superseded_by=F('id')), name='oem_reference_not_own_superseder'),
        ]
        indexes = [models.Index(fields=['code'], name='oem_reference_code_idx')]

    def __str__(self):
        return f'{self.manufacturer} {self.code}'

    def save(self, *args, **kwargs):
        self.manufacturer, self.code = manufacturer_name(self.manufacturer), compact(self.code)
        self.printed_forms = spellings(self.printed_forms, self.code)
        if not self._state.adding:
            self.version = (self.version or 0) + 1
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = {*kwargs['update_fields'], 'version', 'updated_at'}
        super().save(*args, **kwargs)


class OEMReferenceSource(models.Model):
    reference = models.ForeignKey(OEMReference, on_delete=models.CASCADE, related_name='sources', verbose_name='referencia')
    kind = models.CharField(max_length=24, choices=SOURCE_KINDS, verbose_name='tipo de fuente')
    citation = models.CharField(max_length=500, blank=True, default='', verbose_name='cita')  # URL, or document name and page
    # run, rule and rules_version (OEM finder), part_codes [{id, part, code}] (the catalog alternos that back it), verified (manual)...
    detail = models.JSONField(default=dict, blank=True, verbose_name='detalle')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name='registrada por')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='fecha de registro')

    class Meta:
        verbose_name = 'fuente de referencia OEM'
        verbose_name_plural = 'fuentes de referencias OEM'
        ordering = ['created_at', 'id']
        constraints = [
            models.UniqueConstraint(fields=['reference', 'kind', 'citation'], name='oem_reference_source_unique'),
            models.CheckConstraint(condition=Q(kind__in=values(SOURCE_KINDS)), name='oem_reference_source_valid_kind'),
        ]

    def __str__(self):
        return f'{self.reference} · {self.get_kind_display()}'

    @property
    def removable(self):
        return self.kind in MANUAL_KINDS
