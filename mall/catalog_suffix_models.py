"""Admin-managed code suffix table (OEM finder phase 1): what a trailing token of a SKU means.

TAG tokens never change the part (brand, origin, genuine, supplier marker...), VARIANT tokens may change it, UNKNOWN tokens block
until the owner labels them. Every save takes the next table-wide version, so readers can key their caches by the max version;
every reviewed edit writes a CatalogCodeSuffixChange. Rows describe tokens only, never prices or stock.
"""
from django.conf import settings
from django.db import models
from django.db.models import F, Max, Q
from django.db.models.functions import Upper

SUFFIX_CLASSES = [('TAG', 'Etiqueta'), ('VARIANT', 'Variante'), ('UNKNOWN', 'Desconocido')]
TAG_KINDS = [('company_code', 'Código de empresa'), ('brand', 'Marca'), ('house_brand', 'Marca propia'), ('origin', 'Origen'),
             ('genuine', 'Genuino'), ('supplier_marker', 'Marcador de proveedor'), ('commercial', 'Comercial'), ('lifecycle', 'Ciclo de vida')]
VARIANT_KINDS = [('position', 'Posición'), ('size', 'Medida'), ('kit', 'Kit'), ('material', 'Material'), ('spec', 'Especificación'),
                 ('version', 'Versión'), ('color', 'Color'), ('package', 'Empaque'), ('descriptor', 'Descriptor'), ('market', 'Mercado'),
                 ('alt_oem_tail', 'OEM alterno'), ('compound', 'Compuesto')]
SUFFIX_STATUSES = [('seed_confirmed', 'Confirmado en la semilla'), ('needs_owner_confirmation', 'Por confirmar'),
                   ('needs_owner_label', 'Por etiquetar'), ('owner_confirmed', 'Confirmado por el propietario')]
CONFIDENCES = [('low', 'Baja'), ('medium', 'Media'), ('high', 'Alta')]
MATCHES = [('exact', 'Exacto'), ('shape', 'Forma')]
SUFFIX_ACTIONS = [('seed_confirm', 'Confirmación del propietario en la semilla'), ('confirm_tag', 'Confirmar etiqueta'),
                  ('set_variant', 'Marcar variante'), ('set_unknown', 'Marcar desconocido'), ('note', 'Notas'),
                  ('add_alias', 'Agregar alias'), ('set_alias', 'Alias de otra etiqueta'),
                  ('add_scope_override', 'Agregar alcance'), ('remove_scope_override', 'Quitar alcance')]


def values(choices):
    return [value for value, _ in choices]


def normalized_token(value):
    return ' '.join((value or '').upper().split())


class CatalogCodeSuffix(models.Model):
    token = models.CharField(max_length=60, unique=True)
    match = models.CharField(max_length=5, choices=MATCHES, default='exact')
    alias_of = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='aliases')
    cls = models.CharField(max_length=7, choices=SUFFIX_CLASSES, default='UNKNOWN')
    tag_kind = models.CharField(max_length=20, choices=TAG_KINDS, blank=True, default='')
    variant_kind = models.CharField(max_length=20, choices=VARIANT_KINDS, blank=True, default='')
    attribution = models.CharField(max_length=200, blank=True, default='')
    creates_company_reference = models.BooleanField(default=False)
    # [{heads: [...], cls, kind, attribution, confidence}]: the same token read per description head noun (DAI, HD, IN, M).
    scope_overrides = models.JSONField(default=list, blank=True)
    confidence = models.CharField(max_length=6, choices=CONFIDENCES, default='low')
    status = models.CharField(max_length=30, choices=SUFFIX_STATUSES, default='needs_owner_label')
    owner_confirmed = models.BooleanField(default=False)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    confirmed_at = models.DateTimeField(null=True, blank=True)
    proposed_label = models.TextField(blank=True, default='')
    legacy_company_registry = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default='')
    # Seed counts/examples/evidence; an OEM finder run adds stats['run'] (chain rows per tier, unlock and confirmation effects).
    stats = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1, db_index=True)

    class Meta:
        ordering = ['token']
        constraints = [
            models.CheckConstraint(condition=Q(token=Upper('token')) & ~Q(token=''), name='catalog_suffix_token_upper'),
            models.CheckConstraint(condition=Q(match__in=values(MATCHES)), name='catalog_suffix_valid_match'),
            models.CheckConstraint(condition=Q(status__in=values(SUFFIX_STATUSES)), name='catalog_suffix_valid_status'),
            models.CheckConstraint(condition=Q(confidence__in=values(CONFIDENCES)), name='catalog_suffix_valid_confidence'),
            models.CheckConstraint(condition=Q(cls='TAG', tag_kind__in=values(TAG_KINDS), variant_kind='')
                                   | Q(cls='VARIANT', variant_kind__in=values(VARIANT_KINDS), tag_kind='')
                                   | Q(cls='UNKNOWN', tag_kind='', variant_kind=''), name='catalog_suffix_class_kind'),
            models.CheckConstraint(condition=~Q(alias_of=F('id')), name='catalog_suffix_not_own_alias'),
        ]

    def __str__(self):
        return self.token

    @property
    def kind(self):
        return self.tag_kind or self.variant_kind

    @property
    def auto_eligible(self):
        """A TAG an auto-applied rename may strip: owner-confirmed, or a confident seed row. Lifecycle tags only hold the Part."""
        return self.cls == 'TAG' and self.tag_kind != 'lifecycle' and (
            self.owner_confirmed or (self.status == 'seed_confirmed' and self.confidence in ('high', 'medium')))

    def save(self, *args, **kwargs):
        self.token = normalized_token(self.token)
        # Table-wide monotonic version: any edit raises the max, which keys every reader's cache.
        self.version = (type(self).objects.aggregate(top=Max('version'))['top'] or 0) + 1
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = {*kwargs['update_fields'], 'version', 'updated_at'}
        super().save(*args, **kwargs)
        from .catalog_suffixes import invalidate
        invalidate()

    def delete(self, *args, **kwargs):
        result = super().delete(*args, **kwargs)
        from .catalog_suffixes import invalidate
        invalidate()
        return result


class CatalogCodeSuffixChange(models.Model):
    suffix = models.ForeignKey(CatalogCodeSuffix, on_delete=models.PROTECT, related_name='changes')
    token = models.CharField(max_length=60)
    action = models.CharField(max_length=30, choices=SUFFIX_ACTIONS)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    note = models.CharField(max_length=300, blank=True, default='')
    version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.CheckConstraint(condition=Q(action__in=values(SUFFIX_ACTIONS)), name='catalog_suffix_change_valid_action')]
        indexes = [models.Index(fields=['suffix', '-created_at'], name='catalog_suffix_change_idx')]
