"""Subgrupo-owned technical templates and reusable vehicle applications."""
import uuid

from django.db import models, transaction
from django.db.models import Q
from django.db.models.functions import Lower


class Grupo(models.Model):
    """A grupo of the catalog's taxonomy (FRENOS, FILTROS...); its subgrupos are the PartTypes (inventory refactor S5). One per name,
    ignoring case: mall.technical_data.grupo_for finds or creates it. Renamed through save(), which renames the text that follows it."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    nombre = models.CharField(max_length=120, verbose_name='grupo')

    class Meta:
        ordering = ['nombre']
        verbose_name = 'grupo'
        verbose_name_plural = 'grupos'
        constraints = [models.UniqueConstraint(Lower('nombre'), name='grupo_nombre_unique')]

    def __str__(self):
        return self.nombre

    def save(self, *args, **kwargs):
        """The nombre is normalized, and a rename reaches the text that follows the grupo until S10, in the same transaction: its subgrupos'
        category and their lines' grupo text (mall.technical_data.project_grupo). Otherwise a line's next save would look its subgrupo up
        by the old text and bring the old grupo back. (A queryset update skips this; rename a grupo through save().)"""
        from .technical_data import normalized_label, project_grupo
        self.nombre = normalized_label(self.nombre)
        using = kwargs.get('using') or self._state.db or 'default'
        with transaction.atomic(using=using):
            super().save(*args, **kwargs)
            project_grupo(self, using=using)


class PartType(models.Model):
    """A subgrupo: a kind of part within its grupo, with its TechnicalTemplate (1:1). Until S10 `category` keeps the grupo's nombre as
    text: save() and the writers of mall.technical_data keep the two in step, and the code before S5 only knows the text."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grupo = models.ForeignKey(Grupo, on_delete=models.PROTECT, null=True, blank=True, related_name='subgrupos', verbose_name='grupo')
    category = models.CharField(max_length=120, verbose_name='grupo')
    name = models.CharField(max_length=120, verbose_name='subgrupo')

    class Meta:
        ordering = ['category', 'name']
        constraints = [models.UniqueConstraint(Lower('category'), Lower('name'), name='unique_part_type_group_name')]

    def __str__(self):
        return f'{self.category} / {self.name}'

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_grupo = (instance.__dict__.get('category'), instance.__dict__.get('grupo_id'))  # what save() compares against
        return instance

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        self._loaded_grupo = (self.__dict__.get('category'), self.__dict__.get('grupo_id'))

    def save(self, *args, **kwargs):
        """The grupo and its text move together (mall.technical_data.pair_grupo): a subgrupo saved with only the text, as the code
        before S5 and old tests write it, finds or creates its grupo."""
        from .technical_data import pair_grupo
        pair_grupo(self, using=kwargs.get('using') or self._state.db or 'default')
        fields = kwargs.get('update_fields')
        if fields is not None and {'category', 'grupo'} & set(fields):
            kwargs['update_fields'] = set(fields) | {'category', 'grupo'}
        super().save(*args, **kwargs)
        self._loaded_grupo = (self.__dict__.get('category'), self.__dict__.get('grupo_id'))


class TechnicalTemplate(models.Model):
    part_type = models.OneToOneField(PartType, primary_key=True, on_delete=models.CASCADE, related_name='template')
    revision = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)


class TechnicalField(models.Model):
    KINDS = [('number', 'Medida / decimal'), ('integer', 'Número entero'), ('boolean', 'Sí / No'), ('choice', 'Selección'), ('text', 'Texto')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(TechnicalTemplate, on_delete=models.CASCADE, related_name='fields')
    key = models.SlugField(max_length=60)
    label = models.CharField(max_length=120)
    section = models.CharField(max_length=120, default='CARACTERÍSTICAS')
    kind = models.CharField(max_length=12, choices=KINDS)
    unit = models.CharField(max_length=20, blank=True)
    options = models.JSONField(default=list, blank=True)
    required = models.BooleanField(default=False)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['position', 'id']
        constraints = [models.UniqueConstraint(fields=['template', 'key'], name='unique_template_field_key')]


class PartSpecification(models.Model):
    part = models.ForeignKey('mall.Part', on_delete=models.CASCADE, related_name='specifications')
    field = models.ForeignKey(TechnicalField, on_delete=models.PROTECT, related_name='values')
    number_value = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    boolean_value = models.BooleanField(null=True, blank=True)
    text_value = models.CharField(max_length=2000, null=True, blank=True)
    source = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['part', 'field'], name='unique_part_specification'),
            models.CheckConstraint(condition=(Q(number_value__isnull=False, boolean_value__isnull=True, text_value__isnull=True) |
                Q(number_value__isnull=True, boolean_value__isnull=False, text_value__isnull=True) |
                Q(number_value__isnull=True, boolean_value__isnull=True, text_value__isnull=False)), name='one_specification_value'),
        ]
        indexes = [models.Index(fields=['field', 'number_value'], name='spec_numeric_lookup_idx')]


class Application(models.Model):
    """A reusable vehicle configuration; it does not itself assert part fitment."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    make = models.CharField(max_length=120)
    model = models.CharField(max_length=120)
    generation = models.CharField(max_length=120, blank=True)
    year_from = models.PositiveSmallIntegerField()
    year_to = models.PositiveSmallIntegerField()
    engine = models.CharField(max_length=120, blank=True)
    trim = models.CharField(max_length=120, blank=True)
    transmission = models.CharField(max_length=120, blank=True)
    market = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ['make', 'model', 'year_from', 'engine', 'id']
        constraints = [
            models.CheckConstraint(condition=Q(year_to__gte=models.F('year_from')), name='application_year_order'),
            models.UniqueConstraint(fields=['make', 'model', 'generation', 'year_from', 'year_to', 'engine', 'trim', 'transmission', 'market'], name='unique_vehicle_application'),
        ]


class ItemApplication(models.Model):
    part = models.ForeignKey('mall.Part', on_delete=models.CASCADE, related_name='item_applications')
    application = models.ForeignKey(Application, on_delete=models.PROTECT, related_name='item_applications')
    position = models.CharField(max_length=120, blank=True)
    notes = models.CharField(max_length=2000, blank=True)
    source = models.CharField(max_length=500, blank=True)
    verified = models.BooleanField(default=False)

    class Meta:
        ordering = ['application__make', 'application__model', 'application__year_from', 'id']
        constraints = [models.UniqueConstraint(fields=['part', 'application', 'position'], name='unique_item_application_position')]
