"""Subgrupo-owned technical templates and reusable vehicle applications."""
import uuid

from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


class PartType(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.CharField(max_length=120, verbose_name='grupo')
    name = models.CharField(max_length=120, verbose_name='subgrupo')

    class Meta:
        ordering = ['category', 'name']
        constraints = [models.UniqueConstraint(Lower('category'), Lower('name'), name='unique_part_type_group_name')]

    def __str__(self):
        return f'{self.category} / {self.name}'


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
