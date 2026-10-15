# Written by hand as the expand migration of inventory refactor stage S7 (plan §1.2.1, §2 SearchDocument, §3 S7): the storefront's Search
# Document. Only new things, so the code before S7 never notices them:
# - the pg_trgm extension (TrigramExtension: CREATE EXTENSION IF NOT EXISTS, trusted since PostgreSQL 13, 1.6 on the live server);
# - mall_searchdocument, one row per line on sale (publicada and not merged, I8), keyed by the line, with its GIN trigram indexes on the
#   space-joined columns a search reads with LIKE (codes, compact, reach, text);
# - mall_searchdocumentrebuild, one row per rebuild (the matching worker's stage every six hours, or python -m mall.refactor.s7_search).
# Both tables start empty: the S7 CLI builds the documents before landing, from the worktree, and nothing reads them until
# CATALOG_SEARCH_SOURCE=document. The line's key also acts in the database (ON DELETE CASCADE): the code before S7 does not know the
# table, so a line it deletes (a code-only revert, the S6 rollback deleting the lines it made) takes its document with it. S7 has no
# tighten migration. Reversing it drops both tables and leaves the extension (DROP EXTENSION would fail on any other index using it).

import django.contrib.postgres.indexes
import django.db.models.deletion
import django.utils.timezone
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations, models

from mall.refactor.migration_ops import on_delete_in_database, with_lock_timeout


class KeepTrigramExtension(TrigramExtension):
    """CREATE EXTENSION IF NOT EXISTS pg_trgm; reversing it leaves the extension in place."""

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        pass


class Migration(migrations.Migration):

    dependencies = [
        ('mall', '0084_proposal_lines'),
    ]

    operations = with_lock_timeout([
        KeepTrigramExtension(),
        migrations.CreateModel(
            name='SearchDocumentRebuild',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('started_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now, verbose_name='iniciada el')),
                ('finished_at', models.DateTimeField(blank=True, null=True, verbose_name='terminada el')),
                ('origin', models.CharField(choices=[('worker', 'Trabajador de coincidencias'), ('cli', 'Línea de comandos')], default='worker',
                                            max_length=10, verbose_name='origen')),
                ('summary', models.JSONField(blank=True, default=dict, verbose_name='resumen')),
                ('error', models.TextField(blank=True, default='', verbose_name='error')),
            ],
            options={
                'verbose_name': 'reconstrucción de la búsqueda',
                'verbose_name_plural': 'reconstrucciones de la búsqueda',
                'ordering': ['-started_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='SearchDocument',
            fields=[
                ('line', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name='search', serialize=False,
                                              to='mall.part', verbose_name='línea')),
                ('codes', models.TextField(blank=True, default='', help_text='Las referencias públicas no retiradas y sus otras grafías; una en '
                                           'disputa solo si es la principal.', verbose_name='códigos')),
                ('compact', models.TextField(blank=True, default='', help_text='Los mismos códigos sin separadores.', verbose_name='códigos compactos')),
                ('reach', models.TextField(blank=True, default='', help_text='Hasta S9: los números OEM vinculados a la línea y los códigos '
                                           'posventa archivados bajo sus números, compactos.', verbose_name='números alcanzados')),
                ('internal', models.JSONField(blank=True, default=dict, help_text='{id del proveedor: [códigos]}: sus códigos internos, solo para '
                                              'su propio catálogo.', verbose_name='códigos internos')),
                ('text', models.TextField(blank=True, default='', help_text='Descripción, nombre, grupo, subgrupo, marcas y la marca canónica de '
                                          'cada alias (TOY → TOY TOYOTA).', verbose_name='texto')),
                ('brand', models.CharField(blank=True, db_index=True, default='', help_text='La marca de la referencia principal.', max_length=120,
                                           verbose_name='marca')),
                ('calidades', models.JSONField(blank=True, default=list, help_text='Las calidades de sus artículos vinculados.',
                                               verbose_name='calidades')),
                ('grupo', models.CharField(blank=True, default='', max_length=120, verbose_name='grupo')),
                ('subgrupo', models.CharField(blank=True, default='', max_length=120, verbose_name='subgrupo')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='última actualización')),
            ],
            options={
                'verbose_name': 'documento de búsqueda',
                'verbose_name_plural': 'documentos de búsqueda',
                'indexes': [
                    django.contrib.postgres.indexes.GinIndex(fields=['codes'], name='search_document_codes_trgm', opclasses=['gin_trgm_ops']),
                    django.contrib.postgres.indexes.GinIndex(fields=['compact'], name='search_document_compact_trgm', opclasses=['gin_trgm_ops']),
                    django.contrib.postgres.indexes.GinIndex(fields=['reach'], name='search_document_reach_trgm', opclasses=['gin_trgm_ops']),
                    django.contrib.postgres.indexes.GinIndex(fields=['text'], name='search_document_text_trgm', opclasses=['gin_trgm_ops']),
                ],
            },
        ),
        on_delete_in_database((('mall_searchdocument', 'line_id', 'CASCADE'),)),
    ])
