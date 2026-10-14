import uuid
from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.functions import RandomUUID
from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Length, Lower, Trim, Upper
from django.db.models.lookups import GreaterThanOrEqual
from django.utils import timezone
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
    # The OEM line's lifecycle (inventory refactor S1; mall.lines is its writer and the readers' querysets). Until S10 `active` is
    # kept equal to estado == 'publicada' (save() below; a CHECK from 0072) and merged_into is set exactly on unificada lines.
    ESTADOS = [('referencia', 'Referencia'), ('preparacion', 'En preparación'), ('publicada', 'Publicada'), ('rechazada', 'Rechazada'),
               ('retirada', 'Retirada'), ('unificada', 'Unificada')]
    ORIGENES = [('catalogo', 'Catálogo'), ('biblioteca', 'Biblioteca OEM'), ('proveedor', 'Proveedor')]
    REASONS = [('duplicate', 'Ya existe en el catálogo'), ('not_a_part', 'No es un repuesto'), ('bad_data', 'Datos del proveedor incorrectos'),
               ('other', 'Otro motivo')]  # SkuProposal.REJECT_REASONS
    # The line's tipo mirrors its main Reference (inventory refactor S2b: oem → OEM, aftermarket → AFTERMARKET, interno and otro →
    # UNKNOWN; mall.references.set_main writes both). Until S10 is_OEM is kept equal to tipo == 'OEM' (save() below; a CHECK from 0076).
    TIPOS = [('OEM', 'OEM'), ('AFTERMARKET', 'Posventa'), ('UNKNOWN', 'Sin clasificar')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name='ID')
    sku = models.CharField(max_length=200, unique=True, verbose_name='SKU interno')
    is_OEM = models.BooleanField(default=False, db_index=True, verbose_name='El SKU principal es OEM')
    name = models.CharField(max_length=200, blank=True, default='', verbose_name='nombre')
    description = models.TextField(blank=True, verbose_name='descripción')
    tipo = models.CharField(max_length=12, choices=TIPOS, default='UNKNOWN', db_default='UNKNOWN', db_index=True, verbose_name='tipo')
    category = models.CharField(max_length=120, blank=True, default='', verbose_name='categoría')
    subcategory = models.CharField(max_length=120, blank=True, default='', verbose_name='subcategoría')
    part_type = models.ForeignKey('PartType', on_delete=models.PROTECT, null=True, blank=True, related_name='parts')
    technical_revision = models.PositiveIntegerField(default=0, editable=False)
    applications = models.ManyToManyField('Application', through='ItemApplication', related_name='parts', blank=True)
    legacy_brand = models.CharField(max_length=120, blank=True, default='', editable=False, verbose_name='marca histórica')
    active = models.BooleanField(default=True, verbose_name='activo')
    merged_into = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True,
                                    related_name='merged_parts', editable=False, verbose_name='SKU unificado')
    estado = models.CharField(max_length=12, choices=ESTADOS, default='publicada', db_default='publicada', db_index=True, verbose_name='estado')
    origen = models.CharField(max_length=10, choices=ORIGENES, default='catalogo', db_default='catalogo', verbose_name='origen')
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   editable=False, verbose_name='decidido por')
    decided_at = models.DateTimeField(null=True, blank=True, editable=False, verbose_name='decidido el')
    decision_reason = models.CharField(max_length=20, choices=REASONS, blank=True, default='', db_default='', verbose_name='motivo')
    decision_note = models.TextField(blank=True, default='', db_default='', verbose_name='nota')
    aplicaciones_texto = models.TextField(blank=True, default='', db_default='', verbose_name='aplicaciones (texto)')
    field_sources = models.JSONField(default=dict, blank=True, db_default={}, verbose_name='origen de cada campo')
    version = models.PositiveIntegerField(default=1, db_default=1, editable=False, verbose_name='versión')
    # NULL on the lines that existed before S1 (0071 adds the columns without filling them).
    created_at = models.DateTimeField(null=True, blank=True, default=timezone.now, editable=False, verbose_name='creada el')
    updated_at = models.DateTimeField(null=True, blank=True, auto_now=True, verbose_name='última actualización')
    changed_at = models.DateTimeField(null=True, blank=True, editable=False, verbose_name='último cambio visible',
                                      help_text='Solo cambios que una persona ve (mall.lines.touch_visible), no los rellenos de la IA.')
    def __str__(self):
        return f"{self.sku} · {self.name}" if self.name and self.name != self.sku else self.sku
    IDENTITY = ('sku', 'is_OEM', 'tipo', 'estado', 'merged_into_id')  # what the S2b shim compares against (mall.references.follow_line)
    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._lifecycle = (instance.__dict__.get('active'), instance.__dict__.get('estado'))  # what save() compares against
        instance._identity = {name: instance.__dict__.get(name) for name in cls.IDENTITY}
        return instance
    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        loaded = dict(zip(('active', 'estado'), getattr(self, '_lifecycle', (None, None))))
        identity = dict(getattr(self, '_identity', {}))
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        for name in loaded:
            if fields is None or name in fields:
                loaded[name] = self.__dict__.get(name)
        for name in self.IDENTITY:
            if fields is None or name in fields or name.removesuffix('_id') in fields:
                identity[name] = self.__dict__.get(name)
        self._lifecycle = (loaded['active'], loaded['estado'])
        self._identity = identity
    def save(self, *args, **kwargs):
        self.sku = self.sku.strip().upper()
        fields = kwargs.get('update_fields')
        adding = self._state.adding
        before = dict(getattr(self, '_identity', {})) if not adding else {}
        # estado and active stay in step (whichever changed since load wins; merged_into forces unificada) and are written together.
        if fields is None or {'active', 'estado', 'merged_into'} & set(fields):
            from .lines import pair_lifecycle
            pair_lifecycle(self)
            if fields is not None:
                fields = kwargs['update_fields'] = set(fields) | {'active', 'estado'}
        # tipo and is_OEM likewise (whichever changed since load wins), so the code that only knows is_OEM keeps working until S10.
        if fields is None or {'tipo', 'is_OEM'} & set(fields):
            from .references import pair_line_tipo
            pair_line_tipo(self, before)
            if fields is not None:
                fields = kwargs['update_fields'] = set(fields) | {'tipo', 'is_OEM'}
        if fields:  # a partial save stamps updated_at too (auto_now only writes it when listed); update_fields=[] still saves nothing
            fields = kwargs['update_fields'] = set(fields) | {'updated_at'}
        if fields is None or {'category', 'subcategory', 'part_type'} & set(fields):
            from .lines import set_subgrupo
            from .technical_data import bind_part_types
            if self._state.adding and self.part_type_id and not (self.category or self.subcategory):
                set_subgrupo(self, self.part_type)  # a new line given only its subgrupo takes its text
            bind_part_types([self], using=kwargs.get('using') or self._state.db or 'default')
            if fields is not None:
                kwargs['update_fields'] = set(fields) | {'category', 'subcategory', 'part_type'}
        super().save(*args, **kwargs)
        self._lifecycle = (self.__dict__.get('active'), self.__dict__.get('estado'))  # a deferred one stays unloaded
        # Transition shim until S10 (plan §1.4): a live line keeps one main Reference holding its codigo, whose tipo it mirrors. A line
        # inserted without one gets it here (unless mall.lines.create_line makes it), and so does a line renamed, given its tipo or
        # brought back from a merge by the code that writes sku, is_OEM or merged_into directly.
        from .references import follow_line
        follow_line(self, before, adding=adding, fields=fields, using=kwargs.get('using'))
        self._identity = {name: self.__dict__.get(name) for name in self.IDENTITY}
    class Meta:
        verbose_name = 'repuesto'
        verbose_name_plural = 'repuestos'
        constraints = [models.UniqueConstraint(Lower('sku'), name='unique_sku_case_insensitive'),
                       models.CheckConstraint(condition=~Q(id=models.F('merged_into')), name='different_merged_part'),
                       models.CheckConstraint(condition=Q(estado__in=['referencia', 'preparacion', 'publicada', 'rechazada', 'retirada', 'unificada']),
                                              name='line_valid_estado'),
                       models.CheckConstraint(condition=Q(origen__in=['catalogo', 'biblioteca', 'proveedor']), name='line_valid_origen'),
                       models.CheckConstraint(condition=Q(merged_into__isnull=True) & ~Q(estado='unificada') | Q(merged_into__isnull=False, estado='unificada'),
                                              name='line_unificada_iff_merged'),
                       models.CheckConstraint(condition=~Q(estado='rechazada') | (~Q(decision_reason='') & Q(decided_at__isnull=False)),
                                              name='line_rejected_has_reason'),
                       # Transition until S10, when active becomes a generated mirror of estado.
                       models.CheckConstraint(condition=Q(active=True, estado='publicada') | Q(active=False) & ~Q(estado='publicada'),
                                              name='line_active_iff_publicada'),
                       # From 0076 (S2b's tighten): tipo mirrors the main Reference; is_OEM mirrors tipo until S10.
                       models.CheckConstraint(condition=Q(tipo__in=['OEM', 'AFTERMARKET', 'UNKNOWN']), name='line_valid_tipo'),
                       models.CheckConstraint(condition=Q(is_OEM=True, tipo='OEM') | Q(is_OEM=False) & ~Q(tipo='OEM'), name='line_oem_iff_tipo')]


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

def _regexp_replace(expression, pattern, replacement=''):
    return models.Func(expression, models.Value(pattern), models.Value(replacement), models.Value('g'), function='REGEXP_REPLACE',
                       output_field=models.CharField())


# A Reference's clave (plan §2): the compact code ([A-Z0-9] only) when the code is OEM-like (8+ such characters, 3+ digits), otherwise
# the code as printed, upper case, trimmed, with its inner spaces collapsed. Every function is immutable, so PostgreSQL stores it as a
# generated column that no writer can get wrong (old code and bulk paths included); mall.references.clave() is the same rule in Python.
_COMPACT_CODE = _regexp_replace(Upper('code'), '[^A-Z0-9]')
CLAVE = models.Case(
    models.When(GreaterThanOrEqual(Length(_COMPACT_CODE), 8) & GreaterThanOrEqual(Length(_regexp_replace(F('code'), '[^0-9]')), 3),
                then=_COMPACT_CODE),
    default=_regexp_replace(Upper(Trim('code')), r'\s+', ' '), output_field=models.CharField(max_length=120))
REFERENCE_LIVE = ('verificado', 'declarado', 'inferido', 'disputa')  # every estado but retirada
REFERENCE_DELETE_REFUSED = ('Una referencia no se borra: mall.references.retire la retira de su línea (estado retirada) y queda como '
                            'historial.')


class ReferenceDeleteRefused(RuntimeError):
    """A delete of a Reference (inventory refactor S2a: nothing deletes a Reference, mall.references.retire takes it off its line)."""


class ReferenceQuerySet(models.QuerySet):
    def delete(self):
        raise ReferenceDeleteRefused(REFERENCE_DELETE_REFUSED)
    delete.alters_data = True
    delete.queryset_only = True


class LiveReferenceManager(models.Manager.from_queryset(ReferenceQuerySet)):
    """Reference.objects, and so every line's `codes` and their prefetches: the References that are not retirada, and not a supplier's
    own interno code (S3: one supplier's name for the line, which no other supplier, client or matching pass may read; a line's main
    stays even when interno). Readers skip those rows through it; Reference.all_objects reads every row (mall.references,
    mall.variants, the stage CLIs, the admin). A filter that reaches codes through a relation (codes__code=...) joins every row instead,
    so it names the same rows too (mall.references.live_q)."""
    def get_queryset(self):
        return super().get_queryset().filter(estado__in=REFERENCE_LIVE).exclude(tipo='interno', is_main=False)


class AllReferenceManager(models.Manager.from_queryset(ReferenceQuerySet)):
    pass


class Reference(models.Model):
    # One code of an OEM line (inventory refactor S2a; PartCode until then, and PartCode stays an alias of it until S10). It keeps
    # PartCode's integer ids, its table (mall_partcode) and the line's `codes` relation, which the API, the split payloads and other
    # code depend on. mall.references is its only writer: it creates (build/add), changes (update) and retires (retire) References,
    # and nothing deletes one. Until S10 ref_type is kept in step with tipo (save() below; a CHECK from 0074) and the JSON keeps
    # ref_type/kind; brand stays text holding the canonical Brand.nombre, '' meaning any brand.
    KINDS = [('alias', 'Equivalencia'), ('oem', 'Referencia OEM'), ('manufacturer', 'Referencia de fabricante')]
    TIPOS = [('oem', 'OEM'), ('aftermarket', 'Posventa'), ('interno', 'Código interno del proveedor'), ('otro', 'Otro')]
    ESTADOS = [('verificado', 'Verificada'), ('declarado', 'Declarada'), ('inferido', 'Inferida'), ('disputa', 'En disputa'),
               ('retirada', 'Retirada')]
    EXCLUSIVE = ('verificado', 'declarado', 'inferido')  # the estados that hold their key for one line (reference_one_live_line, 0074)
    LIVE = REFERENCE_LIVE
    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="codes", verbose_name='repuesto del catálogo')
    brand = models.CharField(max_length=120, blank=True, default='', verbose_name='marca opcional')
    code = models.CharField(max_length=120, verbose_name='código')
    REF_TYPES = [('unknown', 'SIN CLASIFICAR'), ('oem', 'OEM'), ('company', 'EMPRESA / FABRICANTE')]
    ref_type = models.CharField(max_length=20, choices=REF_TYPES, default='unknown', db_index=True)
    reference_source = models.CharField(max_length=500, blank=True, default='', verbose_name='fuente de la equivalencia')
    # The OEM table row an OEM alterno files under (brand as manufacturer, compact code); kept by the oem_reference signal handlers.
    oem_reference = models.ForeignKey('mall.OEMReference', on_delete=models.SET_NULL, null=True, blank=True, related_name='alternos',
                                      verbose_name='referencia OEM')
    # The public id: PostgreSQL fills it for every row, old code's inserts included. No API exposes it before S4, which sets it to the
    # OEMReference id of each library number.
    uid = models.UUIDField(db_default=RandomUUID(), unique=True, editable=False, verbose_name='identificador público')
    printed_forms = models.JSONField(default=list, db_default=[], blank=True, verbose_name='otras formas impresas',
                                     help_text='Grafías del mismo código vistas en otras fuentes (13780-61J00-NP-).')
    clave = models.GeneratedField(expression=CLAVE, output_field=models.CharField(max_length=120), db_persist=True, verbose_name='clave')
    tipo = models.CharField(max_length=12, choices=TIPOS, default='otro', db_default='otro', verbose_name='tipo')
    supplier = models.ForeignKey(Account, on_delete=models.PROTECT, null=True, blank=True, related_name='internal_references',
                                 verbose_name='proveedor', help_text='Solo en un código interno: el proveedor que lo usa.')
    is_main = models.BooleanField(default=False, db_default=False, verbose_name='principal')
    estado = models.CharField(max_length=10, choices=ESTADOS, default='declarado', db_default='declarado', verbose_name='estado')
    dispute_note = models.TextField(blank=True, default='', db_default='', verbose_name='motivo de la disputa')
    notes = models.TextField(blank=True, default='', db_default='', verbose_name='notas')
    detail = models.JSONField(default=dict, db_default={}, blank=True, verbose_name='detalle')
    reemplazado_por = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='reemplaza',
                                        verbose_name='reemplazada por')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   editable=False, verbose_name='creada por')
    # NULL on the References that existed before S2a (0073 adds the columns without filling them).
    created_at = models.DateTimeField(null=True, blank=True, default=timezone.now, editable=False, verbose_name='creada el')
    updated_at = models.DateTimeField(null=True, blank=True, auto_now=True, verbose_name='última actualización')
    version = models.PositiveIntegerField(default=1, db_default=1, editable=False, verbose_name='versión')

    objects = LiveReferenceManager()
    all_objects = AllReferenceManager()

    @property
    def kind(self):
        """Compatibility for older import jobs and API clients; one stored type."""
        return {'unknown': 'alias', 'company': 'manufacturer'}.get(self.ref_type, self.ref_type)

    @kind.setter
    def kind(self, value):
        self.ref_type = {'alias': 'unknown', 'manufacturer': 'company'}.get(value, value)

    class Meta:
        db_table = 'mall_partcode'
        verbose_name = 'referencia'
        verbose_name_plural = 'referencias'
        # From 0074 (S2a's tighten). The brandless reservation across lines (a brandless code reserves it for every brand) is
        # mall.references.validate's, under the clave's advisory lock.
        constraints = [
            # One exclusive Reference per key: a code retired or disputed holds nothing (plan §2, rule 8).
            models.UniqueConstraint(fields=['clave', 'brand', 'supplier'], condition=Q(estado__in=['verificado', 'declarado', 'inferido']),
                                    nulls_distinct=False, name='reference_one_live_line'),
            models.CheckConstraint(condition=Q(tipo__in=['oem', 'aftermarket', 'interno', 'otro']), name='reference_valid_tipo'),
            models.CheckConstraint(condition=Q(estado__in=['verificado', 'declarado', 'inferido', 'disputa', 'retirada']),
                                   name='reference_valid_estado'),
            # Transition until S10, when ref_type goes.
            models.CheckConstraint(condition=Q(tipo='oem', ref_type='oem') | Q(tipo='aftermarket', ref_type='company')
                                   | Q(tipo__in=['interno', 'otro'], ref_type='unknown'), name='reference_tipo_matches_ref_type'),
            models.CheckConstraint(condition=Q(tipo='interno', supplier__isnull=False) | (~Q(tipo='interno') & Q(supplier__isnull=True)),
                                   name='reference_interno_has_supplier'),
            models.CheckConstraint(condition=~Q(code=''), name='reference_code_set'),
            models.CheckConstraint(condition=~Q(estado='disputa') | ~Q(dispute_note=''), name='reference_dispute_note'),
            models.CheckConstraint(condition=~Q(reemplazado_por=F('id')), name='reference_not_own_replacement'),
            # From 0076 (S2b's tighten): one main per line, never retired (rule 3g; I1 is the main on every live line, which
            # mall.references.ensure_mains keeps until S10).
            models.UniqueConstraint(fields=['part'], condition=Q(is_main=True) & ~Q(estado='retirada'), name='reference_one_main_per_line'),
            models.CheckConstraint(condition=~(Q(is_main=True) & Q(estado='retirada')), name='reference_main_not_retired'),
            # From 0077 (S3's expand): the target of the composite foreign key that keeps each variant on its Reference's line
            # (variant_line_follows_reference, 0078).
            models.UniqueConstraint(fields=['id', 'part'], name='reference_id_line'),
        ]
        indexes = [models.Index(fields=['clave'], name='reference_clave_idx'), models.Index(fields=['part', 'is_main'], name='reference_part_main_idx')]

    def __str__(self):
        return f'{self.brand} {self.code}'.strip()

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._tipo_pair = (instance.__dict__.get('tipo'), instance.__dict__.get('ref_type'))  # what save() compares against
        return instance

    def save(self, *args, **kwargs):
        self.brand = self.brand.strip().upper()
        self.code = self.code.strip().upper()
        fields = kwargs.get('update_fields')
        # tipo and ref_type stay in step (whichever changed since load wins) and are written together, so the code that only knows
        # ref_type keeps working until S10.
        if fields is None or {'tipo', 'ref_type'} & set(fields):
            from .references import pair_tipo
            pair_tipo(self)
            if fields is not None:
                fields = kwargs['update_fields'] = set(fields) | {'tipo', 'ref_type'}
        if self._state.adding and not self.__dict__.pop('_checked', False):
            # Transition shim until S10 (S2b): an insert by code outside mall.references whose key the line's main already holds
            # updates the main instead, and one whose key another line's main holds waits in disputa (mall.references.adopt_insert).
            from .references import adopt_insert
            if adopt_insert(self):
                kwargs.pop('force_insert', None)
                fields = None
        if not self._state.adding:
            self.version = (self.version or 0) + 1
            if fields:  # a partial save stamps updated_at and the version too (update_fields=[] still saves nothing)
                fields = kwargs['update_fields'] = set(fields) | {'version', 'updated_at'}
        super().save(*args, **kwargs)
        self._tipo_pair = (self.__dict__.get('tipo'), self.__dict__.get('ref_type'))
        if self.__dict__.pop('_adopted_main', False):
            from .references import sync_line_tipo
            sync_line_tipo(self.part, self)  # the line's tipo mirrors its main (I11)

    def delete(self, *args, **kwargs):
        raise ReferenceDeleteRefused(REFERENCE_DELETE_REFUSED)


PartCode = Reference  # the class's name until S10 (plan §1.1); mall.test_refactor_guards keeps new code from using it


class ReferenceSource(models.Model):
    """Where a Reference comes from (inventory refactor S2a): a catalog page, the OEM library, a supplier, an administrator, an AI
    lookup or the OEM finder. Writers upsert on (reference, kind, citation) and merge detail (mall.references.cite). Until S10 the
    Reference keeps its reference_source text too; S10 derives it from the newest source."""
    KINDS = [('catalogo', 'Catálogo de marca'), ('biblioteca', 'Biblioteca OEM'), ('proveedor', 'Proveedor'), ('admin', 'Administrador'),
             ('ia', 'Investigación IA'), ('buscador', 'Buscador OEM')]
    reference = models.ForeignKey(Reference, on_delete=models.CASCADE, related_name='sources', verbose_name='referencia')
    kind = models.CharField(max_length=12, choices=KINDS, verbose_name='tipo de fuente')
    citation = models.CharField(max_length=500, blank=True, default='', verbose_name='cita')
    marks = models.JSONField(default=list, blank=True, verbose_name='marcas', help_text="Lo que la fuente anota junto al código: '*', 'W/HOUSING'...")
    retirada = models.BooleanField(default=False, verbose_name='retirada')
    retired_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name='retirada por')
    retired_at = models.DateTimeField(null=True, blank=True, verbose_name='retirada el')
    # legacy_kind, run, rule, part_codes, pages, brand_codes...
    detail = models.JSONField(default=dict, blank=True, verbose_name='detalle')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name='registrada por')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='registrada el')

    class Meta:
        verbose_name = 'fuente de referencia'
        verbose_name_plural = 'fuentes de referencias'
        ordering = ['created_at', 'id']
        constraints = [
            models.UniqueConstraint(fields=['reference', 'kind', 'citation'], name='reference_source_unique'),
            models.CheckConstraint(condition=Q(kind__in=['catalogo', 'biblioteca', 'proveedor', 'admin', 'ia', 'buscador']),
                                   name='reference_source_valid_kind'),
        ]

    def __str__(self):
        return f'{self.reference} · {self.get_kind_display()}'


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
    # Historical compatibility records; the active alternos library uses Reference.
    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="alternates", verbose_name='repuesto del catálogo')
    replacement = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="replaces", verbose_name='repuesto alterno')
    notes = models.TextField(blank=True, verbose_name='notas')
    class Meta:
        verbose_name = 'alterno'
        verbose_name_plural = 'alternos'
        constraints = [models.UniqueConstraint(fields=["part", "replacement"], name="unique_alternate"), models.CheckConstraint(condition=~Q(part=models.F("replacement")), name="different_alternate")]

class SupplierItem(models.Model):
    # A variant (inventory refactor S3): a supplier's item hangs from one Reference of its line, and part is always that Reference's
    # line (mall.variants.attach is the only writer of both; from 0078 a composite foreign key keeps them in step, so moving a
    # Reference moves its variants). reference stays nullable for good (OD21): an item no code places yet waits without one, pending.
    # A code change that cannot apply on its own waits in codigo_pendiente/brand_pendiente (status review, out of stock: OD7) while
    # codigo and brand keep what order and cesta lines were made with.
    CALIDADES = [('original', 'Original'), ('oes', 'OES (equipo original)'), ('alternativo', 'Alternativo')]
    CONDICIONES = [('nuevo', 'Nuevo'), ('remanufacturado', 'Remanufacturado')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name='ID')
    supplier = models.ForeignKey(Account, on_delete=models.PROTECT, verbose_name='proveedor')
    supplier_invent_id = models.CharField(max_length=120, verbose_name='ID de inventario del proveedor')
    part = models.ForeignKey(Part, on_delete=models.PROTECT, null=True, blank=True, related_name="supplier_items", verbose_name='repuesto del catálogo')
    reference = models.ForeignKey(Reference, on_delete=models.PROTECT, null=True, blank=True, related_name='variants', db_index=False,
                                  verbose_name='referencia', help_text='El código de la línea del que cuelga este artículo.')
    codigo = models.CharField(max_length=120, verbose_name='código del repuesto')
    brand = models.CharField(max_length=120, verbose_name='marca')
    codigo_pendiente = models.CharField(max_length=120, null=True, blank=True, verbose_name='código pendiente',
                                        help_text='El código nuevo que envió el proveedor y que espera revisión.')
    brand_pendiente = models.CharField(max_length=120, null=True, blank=True, verbose_name='marca pendiente',
                                       help_text='La marca nueva que envió el proveedor y que espera revisión.')
    description = models.TextField(blank=True, verbose_name='descripción')
    references = models.JSONField(default=list, blank=True, verbose_name='referencias declaradas por el proveedor')
    matching_status = models.CharField(max_length=20, choices=[("pending", "Pendiente"), ("matched", "Vinculado"), ("review", "Por revisar")], default="pending", verbose_name='estado de coincidencia')
    source = models.CharField(max_length=20, choices=[("upload", "Carga manual"), ("apiag", "apiag-cloud")], verbose_name='origen')
    reported_quantity = models.PositiveIntegerField(default=0, verbose_name='existencias reportadas')
    reserved_quantity = models.PositiveIntegerField(default=0, verbose_name='unidades reservadas')
    # What the supplier says about this variant (rule 10; filled from S3.2): optional, '' when unknown.
    material = models.CharField(max_length=120, blank=True, default='', db_default='', verbose_name='material')
    origen = models.CharField(max_length=120, blank=True, default='', db_default='', verbose_name='país de origen')
    garantia = models.CharField(max_length=120, blank=True, default='', db_default='', verbose_name='garantía')
    calidad = models.CharField(max_length=12, choices=CALIDADES, blank=True, default='', db_default='', verbose_name='calidad')
    condicion = models.CharField(max_length=16, choices=CONDICIONES, blank=True, default='', db_default='', verbose_name='condición')
    unidades_por_empaque = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name='unidades por empaque')
    field_sources = models.JSONField(default=dict, blank=True, db_default={}, verbose_name='origen de cada campo')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='última actualización')
    class Meta:
        verbose_name = 'artículo del proveedor'
        verbose_name_plural = 'artículos del proveedor'
        constraints = [models.UniqueConstraint(fields=["supplier", "supplier_invent_id"], name="unique_supplier_invent")]
        indexes = [models.Index(fields=['reference', 'part'], name='variant_reference_line_idx'),
                   models.Index(fields=['supplier', 'codigo'], name='variant_supplier_codigo_idx')]
    @property
    def available_quantity(self) -> int:
        return max(0, self.reported_quantity - self.reserved_quantity)
    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._hung = (instance.__dict__.get('part_id'), instance.__dict__.get('reference_id'))  # what save() compares against
        return instance
    def save(self, *args, **kwargs):
        # Transition shim until S10 (plan §1.4): code that writes part without a Reference (old code, tests) gets the line's Reference
        # for the item's code attached here, and a part changed under an unchanged Reference is a Vincular (mall.variants.follow_item).
        from .variants import follow_item
        follow_item(self, kwargs)
        super().save(*args, **kwargs)
        self._hung = (self.__dict__.get('part_id'), self.__dict__.get('reference_id'))

class StockEntry(models.Model):
    item = models.ForeignKey(SupplierItem, on_delete=models.PROTECT, related_name="ledger", verbose_name='artículo del proveedor')
    # The item's supplier, kept on the entry so a supplier's whole ledger is one indexed read (mall.inventory_history).
    supplier = models.ForeignKey(Account, on_delete=models.PROTECT, null=True, blank=True, related_name='+', verbose_name='proveedor')
    kind = models.CharField(max_length=20, choices=[("sync", "Ajuste de existencias"), ("reserve", "Reserva"), ("release", "Liberación de reserva"), ("settle", "Pedido descontado por el proveedor"), ("shortfall", "Faltante físico"), ("delivery", "Entrega")], verbose_name='tipo de movimiento')
    quantity = models.PositiveIntegerField(default=0, verbose_name='cantidad del movimiento')
    direction = models.CharField(max_length=6, choices=[('credit', 'Crédito'), ('debit', 'Débito')], default='credit', verbose_name='dirección')
    balance_type = models.CharField(max_length=11, choices=[('stock', 'Existencias'), ('reservation', 'Reservas')], default='stock', verbose_name='saldo afectado')
    reserved_delta = models.PositiveIntegerField(default=0, verbose_name='cantidad del movimiento de reserva')
    reserved_direction = models.CharField(max_length=6, choices=[('credit', 'Crédito'), ('debit', 'Débito')], default='', blank=True, verbose_name='dirección de la reserva')
    reported_after = models.PositiveIntegerField(verbose_name='saldo de existencias')
    reserved_after = models.PositiveIntegerField(verbose_name='saldo de unidades reservadas')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name='usuario responsable')
    reference = models.CharField(max_length=120, verbose_name='referencia')
    # The confirmed order's reservation a reserve or settle entry moves (mall.stock_reservations).
    reservation = models.ForeignKey('mall.StockReservation', on_delete=models.PROTECT, null=True, blank=True, related_name='entries',
                                    verbose_name='reserva de pedido')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='fecha de creación')

    def save(self, *args, **kwargs):
        if self.supplier_id is None and self.item_id:
            self.supplier_id = self.item.supplier_id
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = 'movimiento de existencias'
        verbose_name_plural = 'movimientos de existencias'
        indexes = [models.Index(fields=['supplier', '-created_at', '-id'], name='stock_entry_supplier_idx')]
        # Every entry moves at least one unit of one balance. A sync moves the reported count only; reserve, release and settle move
        # reserved units only, by their quantity; every entry but a sync points to the reservation it moves (mall.check_stock_ledger).
        constraints = [
            models.CheckConstraint(condition=Q(direction__in=['credit', 'debit']), name='stock_entry_valid_direction'),
            models.CheckConstraint(condition=Q(balance_type__in=['stock', 'reservation']), name='stock_entry_valid_balance_type'),
            models.CheckConstraint(condition=Q(reserved_delta=0, reserved_direction='') | Q(reserved_delta__gt=0, reserved_direction__in=['credit', 'debit']), name='stock_entry_reserved_direction'),
            models.CheckConstraint(condition=Q(quantity__gte=1), name='stock_entry_quantity_positive'),
            # A shortfall or delivery takes units out of the count and the reservation together (a reservation-only entry when the
            # count has nothing left to take), so available stock does not move.
            models.CheckConstraint(condition=Q(kind='sync', balance_type='stock', reserved_delta=0)
                                   | Q(kind='reserve', balance_type='reservation', direction='credit')
                                   | Q(kind__in=['release', 'settle'], balance_type='reservation', direction='debit')
                                   | Q(kind__in=['shortfall', 'delivery'], direction='debit', reserved_direction='debit'), name='stock_entry_kind_shape'),
            models.CheckConstraint(condition=Q(balance_type='stock') | Q(reserved_delta=F('quantity'), reserved_direction=F('direction')),
                                   name='stock_entry_reservation_amount'),
            models.CheckConstraint(condition=Q(kind='sync', reservation__isnull=True) | (~Q(kind='sync') & Q(reservation__isnull=False)),
                                   name='stock_entry_reservation_link'),
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
from .inventory_history_models import InventoryRun, InventoryChange
from .request_models import (ClientRequestSubmission, SupplierRequest, SupplierRequestLine, RequestContribution,
                             DealQuotation, DealQuotationLine, DealEvent, DealMessage, DealAttachment, DealThread, DealCommand, StockReservation,
                             DealShortfall, DealShortfallLine)
from .analytics_models import UsageEvent, UsageVisit
from .assistant_models import WhatsAppAssistantSession
from .basket_models import BasketEvent, BasketItem
from .inbox_models import AccountWhatsApp, ConversationRead, InboxCursor, InboxGroup, PushSubscription, WhatsAppContact, WhatsAppMessage
from .matching_models import MatchingQueue, MatchingCase, SupplierCodeMapping, MatchingDecision
from .technical_models import Grupo, PartType, TechnicalTemplate, TechnicalField, PartSpecification, Application, ItemApplication
from .pricing_models import (SupplierPricingSettings, PricingAuditEvent, PriceList, PriceListEntry, SupplierItemPricing, PriceChange,
                             ClientPricingProfile, PricingRule)
from .quote_draft_models import DealQuotationDraft, DealQuotationDraftLine, DealQuotationAudit, DealQuotationLineAudit
from .price_import_models import PriceImportJob, PriceImportBatch
from .quote_assistant_models import QuoteAssistantRun, AIUsageRecord
from .catalog_suffix_models import CatalogCodeSuffix, CatalogCodeSuffixChange
from .oem_finder_models import OEMFinderRun, OEMReviewCase, OEMFinderChange, OEMApplyHalt, OEMReviewDecision, OEMAutoCandidate
from .oem_reference_models import OEMReference, OEMReferenceSource, OEMCrossReference, OEMCrossReferenceRetired, PartOEMLink
from .catalog_split_models import CatalogSplit, CatalogSeparation
from .apiag_models import ApiagConnection
from .apiag_sync_models import ApiagSucursal, ApiagSync, SupplierItemBranchStock
from .sku_proposal_models import SkuProposal, SkuProposalItem
from .sku_review_models import SkuReviewRun, SkuReviewVerdict
from .brand_models import Brand, BrandAlias
