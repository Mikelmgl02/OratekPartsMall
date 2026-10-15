"""The storefront's Search Document (inventory refactor S7, plan §2): one row per line on sale (publicada and not merged, invariant I8),
holding what a catalog search reads, already folded (upper case, no accents) so every word is one LIKE on an indexed column (GIN trigram,
pg_trgm). mall.search_document is its only writer: refresh(line_ids) after a write that changes what a line shows, and the matching
worker's rebuild every six hours. Stock and the items' calidades are never in it: availability and the calidad filter stay live. The
storefront search reads nothing else (mall.catalog_search; from S10 the legacy search is gone)."""
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.utils import timezone


class SearchDocument(models.Model):
    # The space-joined columns start and end with a space, so ' KEY ' finds one whole code and 'KEY' a code containing it; a search word
    # has no spaces, so it never spans two codes.
    line = models.OneToOneField('mall.Part', on_delete=models.CASCADE, primary_key=True, related_name='search', verbose_name='línea')
    codes = models.TextField(blank=True, default='', verbose_name='códigos',
                             help_text='Las referencias públicas no retiradas y sus otras grafías; una en disputa solo si es la principal.')
    compact = models.TextField(blank=True, default='', verbose_name='códigos compactos', help_text='Los mismos códigos sin separadores.')
    # S10 contract, step 1 (0086): nothing fills or reads reach any more (the aftermarket codes it held were OEMCrossReference rows, which
    # S10 archives); a database default fills it until step 2 takes it out of the state and 0089 drops it with its index.
    reach = models.TextField(blank=True, default='', db_default='', verbose_name='números alcanzados',
                             help_text='Hasta S9: los números OEM vinculados a la línea y los códigos posventa archivados bajo sus números, compactos.')
    internal = models.JSONField(default=dict, blank=True, verbose_name='códigos internos',
                                help_text='{id del proveedor: [códigos]}: sus códigos internos, solo para su propio catálogo.')
    text = models.TextField(blank=True, default='', verbose_name='texto',
                            help_text='Descripción, nombre, grupo, subgrupo, marcas y la marca canónica de cada alias (TOY → TOY TOYOTA).')
    brand = models.CharField(max_length=120, blank=True, default='', db_index=True, verbose_name='marca',
                             help_text='La marca de la referencia principal.')
    grupo = models.CharField(max_length=120, blank=True, default='', verbose_name='grupo')
    subgrupo = models.CharField(max_length=120, blank=True, default='', verbose_name='subgrupo')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='última actualización')

    class Meta:
        verbose_name = 'documento de búsqueda'
        verbose_name_plural = 'documentos de búsqueda'
        indexes = [GinIndex(fields=['codes'], opclasses=['gin_trgm_ops'], name='search_document_codes_trgm'),
                   GinIndex(fields=['compact'], opclasses=['gin_trgm_ops'], name='search_document_compact_trgm'),
                   GinIndex(fields=['reach'], opclasses=['gin_trgm_ops'], name='search_document_reach_trgm'),
                   GinIndex(fields=['text'], opclasses=['gin_trgm_ops'], name='search_document_text_trgm')]

    def __str__(self):
        return f'{self.line_id}'


class SearchDocumentRebuild(models.Model):
    """One rebuild of every document (mall.search_document.rebuild): the matching worker's stage every six hours, or the S7 CLI. Its
    summary keeps what it wrote and the I8 check that followed; error, why it stopped."""
    ORIGINS = [('worker', 'Trabajador de coincidencias'), ('cli', 'Línea de comandos')]
    started_at = models.DateTimeField(default=timezone.now, db_index=True, verbose_name='iniciada el')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='terminada el')
    origin = models.CharField(max_length=10, choices=ORIGINS, default='worker', verbose_name='origen')
    summary = models.JSONField(default=dict, blank=True, verbose_name='resumen')
    error = models.TextField(blank=True, default='', verbose_name='error')

    class Meta:
        verbose_name = 'reconstrucción de la búsqueda'
        verbose_name_plural = 'reconstrucciones de la búsqueda'
        ordering = ['-started_at', '-id']
