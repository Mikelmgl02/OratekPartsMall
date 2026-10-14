# Written by hand as the expand migration of inventory refactor stage S2b (plan §1.2.1, §3 S2b): the line's tipo, with a database
# default, so the code before S2b keeps inserting lines (they get UNKNOWN until mall.refactor.s2b_main_refs or the S2b code sets it from
# the main Reference). The References need nothing more: is_main and their other columns came with 0073. The tighten
# migration 0076 adds one main per line, the main never retired and the transition CHECK is_OEM = (tipo = 'OEM').
# Part B (mall.line_merge): a CatalogSeparation may come from a dismissed case instead of a split, so its split loses its NOT NULL and it
# gains the case, who decided and why (nullable, or with a database default), which the code before S2b never writes. Reversing this
# migration drops tipo, case, actor and reason and leaves split nullable (the pairs a dismissed case wrote stay, kept apart).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from mall.refactor.migration_ops import with_lock_timeout


class Migration(migrations.Migration):

    dependencies = [
        ('mall', '0082_grupo_checks'),
    ]

    operations = with_lock_timeout([
        migrations.AddField(
            model_name='part',
            name='tipo',
            field=models.CharField(choices=[('OEM', 'OEM'), ('AFTERMARKET', 'Posventa'), ('UNKNOWN', 'Sin clasificar')], db_default='UNKNOWN',
                                   db_index=True, default='UNKNOWN', max_length=12, verbose_name='tipo'),
        ),
        # Dropping the NOT NULL is never undone in the database: a case dismissed under S2b writes pairs with no split, which reversing
        # this migration would otherwise refuse (SET NOT NULL), and the code before S2b reads only the pair columns.
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterField(
                model_name='catalogseparation',
                name='split',
                field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='separations',
                                        to='mall.catalogsplit'),
            )],
            database_operations=[migrations.RunSQL('ALTER TABLE mall_catalogseparation ALTER COLUMN split_id DROP NOT NULL',
                                                   reverse_sql=migrations.RunSQL.noop)],
        ),
        migrations.AddField(
            model_name='catalogseparation',
            name='case',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='separations',
                                    to='mall.matchingcase', verbose_name='caso descartado'),
        ),
        migrations.AddField(
            model_name='catalogseparation',
            name='actor',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+',
                                    to=settings.AUTH_USER_MODEL, verbose_name='decidido por'),
        ),
        migrations.AddField(
            model_name='catalogseparation',
            name='reason',
            field=models.CharField(blank=True, db_default='', default='', max_length=40, verbose_name='motivo'),
        ),
    ])
