import uuid
from django.contrib.auth.models import AbstractUser
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from .media_storage import catalog_storage, catalog_image_path

class User(AbstractUser):
    email = models.EmailField(unique=True, verbose_name='correo electrónico')
    class Meta:
        verbose_name = 'usuario'
        verbose_name_plural = 'usuarios'

class Account(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name='ID')
    name = models.CharField(max_length=200, verbose_name='nombre')
    active = models.BooleanField(default=True, verbose_name='activo')
    roles = models.ManyToManyField("Role", blank=True, verbose_name='roles')
    def __str__(self):
        return self.name
    class Meta:
        verbose_name = 'cuenta'
        verbose_name_plural = 'cuentas'

class Role(models.Model):
    code = models.SlugField(unique=True, verbose_name='código')
    name = models.CharField(max_length=100, verbose_name='nombre')
    capability = models.CharField(max_length=20, choices=[("supplier", "Proveedor"), ("client", "Cliente")], verbose_name='tipo de acceso')
    def __str__(self):
        return self.name
    class Meta:
        verbose_name = 'rol'
        verbose_name_plural = 'roles'

class Membership(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, verbose_name='usuario')
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="memberships", verbose_name='cuenta')
    permission = models.CharField(max_length=20, choices=[("owner", "Propietario"), ("manager", "Administrador"), ("staff", "Empleado")], default="staff", verbose_name='permiso')
    class Meta:
        verbose_name = 'membresía'
        verbose_name_plural = 'membresías'
        constraints = [models.UniqueConstraint(fields=["user", "account"], name="unique_membership")]

class Invitation(models.Model):
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, verbose_name='código de invitación')
    email = models.EmailField(verbose_name='correo electrónico')
    expires_at = models.DateTimeField(verbose_name='fecha de vencimiento')
    accepted_at = models.DateTimeField(null=True, blank=True, verbose_name='fecha de aceptación')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name='creada por')
    class Meta:
        verbose_name = 'invitación'
        verbose_name_plural = 'invitaciones'

class Part(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name='ID')
    sku = models.CharField(max_length=200, unique=True, verbose_name='SKU interno')
    is_OEM = models.BooleanField(default=False, db_index=True, verbose_name='El SKU principal es OEM')
    name = models.CharField(max_length=200, blank=True, default='', verbose_name='nombre')
    description = models.TextField(blank=True, verbose_name='descripción')
    category = models.CharField(max_length=120, blank=True, default='', verbose_name='categoría')
    subcategory = models.CharField(max_length=120, blank=True, default='', verbose_name='subcategoría')
    part_type = models.ForeignKey('PartType', on_delete=models.PROTECT, null=True, blank=True, related_name='parts')
    technical_revision = models.PositiveIntegerField(default=0, editable=False)
    applications = models.ManyToManyField('Application', through='ItemApplication', related_name='parts', blank=True)
    legacy_brand = models.CharField(max_length=120, blank=True, default='', editable=False, verbose_name='marca histórica')
    active = models.BooleanField(default=True, verbose_name='activo')
    merged_into = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True,
                                    related_name='merged_parts', editable=False, verbose_name='SKU unificado')
    def __str__(self):
        return f"{self.sku} · {self.name}" if self.name and self.name != self.sku else self.sku
    def save(self, *args, **kwargs):
        self.sku = self.sku.strip().upper()
        fields = kwargs.get('update_fields')
        if fields is None or {'category', 'subcategory', 'part_type'} & set(fields):
            from .technical_data import bind_part_types
            if self._state.adding and self.part_type_id and not (self.category or self.subcategory):
                self.category, self.subcategory = self.part_type.category, self.part_type.name
            bind_part_types([self], using=kwargs.get('using') or self._state.db or 'default')
            if fields is not None:
                kwargs['update_fields'] = set(fields) | {'category', 'subcategory', 'part_type'}
        super().save(*args, **kwargs)
    class Meta:
        verbose_name = 'repuesto'
        verbose_name_plural = 'repuestos'
        constraints = [models.UniqueConstraint(Lower('sku'), name='unique_sku_case_insensitive'),
                       models.CheckConstraint(condition=~Q(id=models.F('merged_into')), name='different_merged_part')]


class PartImage(models.Model):
    """Ordered gallery for a canonical SKU. The first image is the cover."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(storage=catalog_storage, upload_to=catalog_image_path, max_length=500)
    thumbnail = models.ImageField(storage=catalog_storage, upload_to=catalog_image_path, max_length=500)
    alt_text = models.CharField(max_length=250, blank=True)
    position = models.PositiveIntegerField(default=0)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    size_bytes = models.PositiveIntegerField()
    original_filename = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position', 'created_at', 'id']
        indexes = [models.Index(fields=['part', 'position'], name='part_image_order_idx')]
        verbose_name = 'imagen del catálogo'
        verbose_name_plural = 'imágenes del catálogo'


class WishlistItem(models.Model):
    """A user's saved canonical SKU, independent of accounts and stock."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='wishlist_items')
    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name='wishlist_items')
    saved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-saved_at', '-id']
        constraints = [models.UniqueConstraint(fields=['user', 'part'], name='unique_user_wishlist_part')]


class CatalogMerge(models.Model):
    """An audit of reviewed canonical SKU changes, independent of stock history."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target = models.ForeignKey(Part, on_delete=models.PROTECT, related_name='catalog_merges')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    sources = models.JSONField(default=list)
    summary = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

class PartCode(models.Model):
    # Approved cross-references belong to the master, independently of stock.
    KINDS = [('alias', 'Equivalencia'), ('oem', 'Referencia OEM'), ('manufacturer', 'Referencia de fabricante')]
    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="codes", verbose_name='repuesto del catálogo')
    brand = models.CharField(max_length=120, blank=True, default='', verbose_name='marca opcional')
    code = models.CharField(max_length=120, verbose_name='código')
    REF_TYPES = [('unknown', 'SIN CLASIFICAR'), ('oem', 'OEM'), ('company', 'EMPRESA / FABRICANTE')]
    ref_type = models.CharField(max_length=20, choices=REF_TYPES, default='unknown', db_index=True)
    reference_source = models.CharField(max_length=500, blank=True, default='', verbose_name='fuente de la equivalencia')

    @property
    def kind(self):
        """Compatibility for older import jobs and API clients; one stored type."""
        return {'unknown': 'alias', 'company': 'manufacturer'}.get(self.ref_type, self.ref_type)

    @kind.setter
    def kind(self, value):
        self.ref_type = {'alias': 'unknown', 'manufacturer': 'company'}.get(value, value)
    class Meta:
        verbose_name = 'código de repuesto'
        verbose_name_plural = 'códigos de repuestos'
        constraints = [models.UniqueConstraint(fields=["brand", "code"], name="unique_brand_code")]
    def save(self, *args, **kwargs):
        self.brand = self.brand.strip().upper()
        self.code = self.code.strip().upper()
        super().save(*args, **kwargs)


class CatalogIdentityChange(models.Model):
    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name='identity_changes')
    previous_sku = models.CharField(max_length=200)
    sku = models.CharField(max_length=200)
    reference = models.JSONField(default=dict)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class OEMLookupCache(models.Model):
    fingerprint = models.CharField(max_length=64, primary_key=True)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class CatalogImportJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    filename = models.CharField(max_length=255)
    status = models.CharField(max_length=20, default='ready')
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    summary = models.JSONField(default=dict)
    applied_summary = models.JSONField(default=dict)
    preview = models.JSONField(default=dict)
    completed_batches = models.PositiveIntegerField(default=0)
    total_batches = models.PositiveIntegerField(default=0)
    total_skus = models.PositiveIntegerField(default=0)
    processed_skus = models.PositiveIntegerField(default=0)


class CatalogImportBatch(models.Model):
    job = models.ForeignKey(CatalogImportJob, on_delete=models.CASCADE, related_name='batches')
    index = models.PositiveIntegerField()
    data = models.JSONField(default=list)
    fingerprint = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['job', 'index'], name='unique_catalog_import_batch')]


class CatalogImportIssue(models.Model):
    """Rejected source rows retained until their owner can repair them."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(CatalogImportJob, on_delete=models.CASCADE, related_name='issues')
    row = models.PositiveIntegerField()
    values = models.JSONField(default=dict)
    original = models.JSONField(default=dict)
    errors = models.JSONField(default=list)
    recovery = models.JSONField(default=list)
    status = models.CharField(max_length=20, default='pending', choices=[('pending', 'Pendiente'), ('resolved', 'Corregida')])
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    part = models.ForeignKey(Part, on_delete=models.SET_NULL, null=True, blank=True)
    applied_summary = models.JSONField(default=dict)

    class Meta:
        ordering = ['created_at', 'row']
        constraints = [models.UniqueConstraint(fields=['job', 'row'], name='unique_catalog_import_issue_row')]

class Alternate(models.Model):
    # Historical compatibility records; the active alternos library uses PartCode.
    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="alternates", verbose_name='repuesto del catálogo')
    replacement = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="replaces", verbose_name='repuesto alterno')
    notes = models.TextField(blank=True, verbose_name='notas')
    class Meta:
        verbose_name = 'alterno'
        verbose_name_plural = 'alternos'
        constraints = [models.UniqueConstraint(fields=["part", "replacement"], name="unique_alternate"), models.CheckConstraint(condition=~Q(part=models.F("replacement")), name="different_alternate")]

class SupplierItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name='ID')
    supplier = models.ForeignKey(Account, on_delete=models.PROTECT, verbose_name='proveedor')
    supplier_invent_id = models.CharField(max_length=120, verbose_name='ID de inventario del proveedor')
    part = models.ForeignKey(Part, on_delete=models.PROTECT, null=True, blank=True, related_name="supplier_items", verbose_name='repuesto del catálogo')
    codigo = models.CharField(max_length=120, verbose_name='código del repuesto')
    brand = models.CharField(max_length=120, verbose_name='marca')
    description = models.TextField(blank=True, verbose_name='descripción')
    references = models.JSONField(default=list, blank=True, verbose_name='referencias declaradas por el proveedor')
    matching_status = models.CharField(max_length=20, choices=[("pending", "Pendiente"), ("matched", "Vinculado"), ("review", "Por revisar")], default="pending", verbose_name='estado de coincidencia')
    source = models.CharField(max_length=20, choices=[("upload", "Carga manual"), ("apiag", "apiag-cloud")], verbose_name='origen')
    reported_quantity = models.PositiveIntegerField(default=0, verbose_name='existencias reportadas')
    reserved_quantity = models.PositiveIntegerField(default=0, verbose_name='unidades reservadas')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='última actualización')
    class Meta:
        verbose_name = 'artículo del proveedor'
        verbose_name_plural = 'artículos del proveedor'
        constraints = [models.UniqueConstraint(fields=["supplier", "supplier_invent_id"], name="unique_supplier_invent")]
    @property
    def available_quantity(self) -> int:
        return max(0, self.reported_quantity - self.reserved_quantity)

class StockEntry(models.Model):
    item = models.ForeignKey(SupplierItem, on_delete=models.PROTECT, related_name="ledger", verbose_name='artículo del proveedor')
    kind = models.CharField(max_length=20, choices=[("sync", "Ajuste de existencias"), ("reserve", "Reserva"), ("release", "Liberación de reserva"), ("delivery", "Entrega")], verbose_name='tipo de movimiento')
    quantity = models.PositiveIntegerField(default=0, verbose_name='cantidad del movimiento')
    direction = models.CharField(max_length=6, choices=[('credit', 'Crédito'), ('debit', 'Débito')], default='credit', verbose_name='dirección')
    balance_type = models.CharField(max_length=11, choices=[('stock', 'Existencias'), ('reservation', 'Reservas')], default='stock', verbose_name='saldo afectado')
    reserved_delta = models.PositiveIntegerField(default=0, verbose_name='cantidad del movimiento de reserva')
    reserved_direction = models.CharField(max_length=6, choices=[('credit', 'Crédito'), ('debit', 'Débito')], default='', blank=True, verbose_name='dirección de la reserva')
    reported_after = models.PositiveIntegerField(verbose_name='saldo de existencias')
    reserved_after = models.PositiveIntegerField(verbose_name='saldo de unidades reservadas')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name='usuario responsable')
    reference = models.CharField(max_length=120, verbose_name='referencia')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='fecha de creación')
    class Meta:
        verbose_name = 'movimiento de existencias'
        verbose_name_plural = 'movimientos de existencias'
        constraints = [
            models.CheckConstraint(condition=Q(direction__in=['credit', 'debit']), name='stock_entry_valid_direction'),
            models.CheckConstraint(condition=Q(balance_type__in=['stock', 'reservation']), name='stock_entry_valid_balance_type'),
            models.CheckConstraint(condition=Q(reserved_delta=0, reserved_direction='') | Q(reserved_delta__gt=0, reserved_direction__in=['credit', 'debit']), name='stock_entry_reserved_direction'),
        ]

class InventoryUpdate(models.Model):
    supplier = models.ForeignKey(Account, on_delete=models.PROTECT, verbose_name='proveedor')
    key = models.CharField(max_length=120, verbose_name='ID de actualización')
    payload_hash = models.CharField(max_length=64, verbose_name='huella de la actualización')
    item = models.ForeignKey(SupplierItem, on_delete=models.PROTECT, verbose_name='artículo del proveedor')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='fecha de creación')
    class Meta:
        verbose_name = 'actualización de inventario'
        verbose_name_plural = 'actualizaciones de inventario'
        constraints = [models.UniqueConstraint(fields=["supplier", "key"], name="unique_inventory_update")]

# Keep AI proposals in their own module; importing here registers their models
# with Django without coupling stock records to the classification workflow.
from .classification_models import CatalogClassificationState, CatalogClassificationSuggestion
from .catalog_assistant_models import CatalogAssistantJob, CatalogAssistantSuggestion, CategorySuggestionCache
from .supplier_import_models import SupplierInventoryImportJob, SupplierInventoryImportBatch
from .request_models import (ClientRequestSubmission, SupplierRequest, SupplierRequestLine, RequestContribution,
                             DealQuotation, DealQuotationLine, DealEvent, DealMessage, DealCommand)
from .analytics_models import UsageEvent, UsageVisit
from .matching_models import MatchingQueue, MatchingCase, SupplierCodeMapping, MatchingDecision
from .technical_models import PartType, TechnicalTemplate, TechnicalField, PartSpecification, Application, ItemApplication
from .pricing_models import (SupplierPricingSettings, PricingAuditEvent, PriceList, PriceListEntry, SupplierItemPricing, PriceChange,
                             ClientPricingProfile, PricingRule)
from .quote_draft_models import DealQuotationDraft, DealQuotationDraftLine, DealQuotationAudit, DealQuotationLineAudit
from .price_import_models import PriceImportJob, PriceImportBatch
from .quote_assistant_models import QuoteAssistantRun, AIUsageRecord
