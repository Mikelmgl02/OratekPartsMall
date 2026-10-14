# Written by hand as the expand migration of inventory refactor stage S2b (plan §1.2.1, §3 S2b): the line's tipo, with a database
# default, so the code before S2b keeps inserting lines (they get UNKNOWN until mall.refactor.s2b_main_refs or the S2b code sets it from
# the main Reference). Nothing else changes in the database: is_main and the References' other columns came with 0073. The tighten
# migration 0076 adds one main per line, the main never retired and the transition CHECK is_OEM = (tipo = 'OEM').

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
    ])
