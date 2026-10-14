"""Brands as a table (inventory refactor S1, plan §2): one row per brand, vehicle makes and parts makers alike, with the spellings
that name it (BrandAlias: TOY → TOYOTA) and, for a few makes, the familia it belongs to (KAIYI under CHERY, LEXUS under TOYOTA; one
level). Reference.brand (today PartCode.brand) stays text holding the canonical nombre until S10, so today's lookups and JSON keep
working; mall.brands is the only writer of `nombre` (rename/merge re-key those codes in the same transaction)."""
from django.db import models
from django.db.models import F, Q


def normalize_name(name):
    """A brand spelling as the table keeps it: upper case, trimmed, single spaces (CENTURY  / HKT → CENTURY / HKT)."""
    return ' '.join((name or '').upper().split())


class Brand(models.Model):
    TIPOS = [('vehiculo', 'Marca de vehículo'), ('repuesto', 'Marca de repuestos')]

    nombre = models.CharField(max_length=120, unique=True, verbose_name='nombre',
                              help_text='En mayúsculas y con espacios simples. Solo cambia con mall.brands.rename o merge.')
    tipo = models.CharField(max_length=10, choices=TIPOS, blank=True, default='', verbose_name='tipo')
    familia = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='miembros', verbose_name='familia',
                                help_text='La marca madre (KAIYI → CHERY): un mismo código es la misma pieza en toda la familia. Un solo nivel.')
    notas = models.TextField(blank=True, default='', verbose_name='notas')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='creada el')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='última actualización')

    def __str__(self):
        return self.nombre

    def save(self, *args, **kwargs):
        self.nombre = normalize_name(self.nombre)
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = 'marca'
        verbose_name_plural = 'marcas'
        ordering = ['nombre']
        constraints = [
            models.CheckConstraint(condition=~Q(nombre=''), name='brand_nombre_set'),
            models.CheckConstraint(condition=Q(tipo__in=['', 'vehiculo', 'repuesto']), name='brand_valid_tipo'),
            models.CheckConstraint(condition=~Q(familia=F('id')), name='brand_not_own_familia'),
        ]


class BrandAlias(models.Model):
    """Another spelling of one brand: a make abbreviation (TOY, NIS), a suffix token (DG → DONGIL) or a whitespace variant as the text
    columns hold it (MOTION  MOTORS), so the spelling is kept as written, only trimmed and upper case."""
    alias = models.CharField(max_length=120, unique=True, verbose_name='alias')
    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name='aliases', verbose_name='marca')

    def __str__(self):
        return f'{self.alias} → {self.brand.nombre}'

    def save(self, *args, **kwargs):
        self.alias = (self.alias or '').strip().upper()
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = 'alias de marca'
        verbose_name_plural = 'alias de marcas'
        ordering = ['alias']
        constraints = [models.CheckConstraint(condition=~Q(alias=''), name='brand_alias_set')]
