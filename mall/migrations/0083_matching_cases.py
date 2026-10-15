# Written by hand as the expand migration of inventory refactor stage S6.1 (plan §1.2.1, §3 S6): the matching case takes the target's
# shape (plan §2 MatchingCase). Every new column is nullable or has a database default, so the code before S6 keeps creating and
# updating cases (its get_or_create leaves them out and the database fills them):
# - reference (NULL, SET_NULL): the Reference a case is about (a code change's current Reference, a declared code's holder);
# - new_code and new_brand (''): the code and brand a code change or a declared code proposes (rule 5b);
# - lines (a new many-to-many table): the lines a case chooses between or would merge;
# - evidence ({}): numbers, codes, citations and hints;
# - guard (''): the check that stopped an automatic decision (prefix, noun, side, make, subgrupo, spec_conflict, separated, disputa...);
# - route (''): lote (decided in a batch) or caso (one by one); batch_key (''): the batch a lote case belongs to (legacy-auto-merge);
# - decided_by, decided_at (NULL) and created_at (NULL for the cases that existed before S6).
# key stays unique: every writer finds its case by it (reference:<item>:<clave>|<brand>, code_change:<item>, variant:<item>:<line>,
# line:<item>, merge:<sorted line ids> or merge:<batch_key>). kind and status keep their legacy values until S10 (supplier, catalog,
# catalog_reference, seed; unmatched, resolved). S6 has no tighten migration for cases.

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

from mall.refactor.migration_ops import with_lock_timeout


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('mall', '0080_library_lines_checks'),
    ]

    operations = with_lock_timeout([
        migrations.AddField(
            model_name='matchingcase',
            name='reference',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='cases',
                                    to='mall.reference', verbose_name='referencia'),
        ),
        migrations.AddField(
            model_name='matchingcase',
            name='new_code',
            field=models.CharField(blank=True, db_default='', default='', max_length=120, verbose_name='código propuesto'),
        ),
        migrations.AddField(
            model_name='matchingcase',
            name='new_brand',
            field=models.CharField(blank=True, db_default='', default='', max_length=120, verbose_name='marca propuesta'),
        ),
        migrations.AddField(
            model_name='matchingcase',
            name='lines',
            field=models.ManyToManyField(blank=True, related_name='matching_cases', to='mall.part', verbose_name='líneas'),
        ),
        migrations.AddField(
            model_name='matchingcase',
            name='evidence',
            field=models.JSONField(blank=True, db_default={}, default=dict, verbose_name='evidencia'),
        ),
        migrations.AddField(
            model_name='matchingcase',
            name='guard',
            field=models.CharField(blank=True, db_default='', default='', max_length=40, verbose_name='control que lo detuvo'),
        ),
        migrations.AddField(
            model_name='matchingcase',
            name='route',
            field=models.CharField(blank=True, choices=[('lote', 'Lote'), ('caso', 'Caso')], db_default='', default='', max_length=5,
                                   verbose_name='ruta'),
        ),
        migrations.AddField(
            model_name='matchingcase',
            name='batch_key',
            field=models.CharField(blank=True, db_default='', db_index=True, default='', max_length=120, verbose_name='lote'),
        ),
        migrations.AddField(
            model_name='matchingcase',
            name='decided_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+',
                                    to=settings.AUTH_USER_MODEL, verbose_name='decidido por'),
        ),
        migrations.AddField(
            model_name='matchingcase',
            name='decided_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='decidido el'),
        ),
        migrations.AddField(
            model_name='matchingcase',
            name='created_at',
            field=models.DateTimeField(blank=True, default=django.utils.timezone.now, null=True, verbose_name='creado el'),
        ),
        migrations.AddIndex(
            model_name='matchingcase',
            index=models.Index(fields=['kind', 'status'], name='matching_case_kind_status'),
        ),
    ])
