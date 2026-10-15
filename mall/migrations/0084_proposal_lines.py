# The expand migration of inventory refactor stage S6.2 (plan §1.2.1, §3 S6.2): Productos nuevos become lines in preparación
# (mall.new_products), so a Revisión IA verdict hangs from its line. A new nullable column and a dropped NOT NULL: the code before S6
# keeps writing the proposal and never names the line. The column is new and empty, so its index and its uniqueness per run (a NULL
# line is distinct) come with it. mall.refactor.s6_new_products points every verdict at its line (a pending proposal's id is its line's).
# It follows S6.1's 0083_matching_cases, the other expand of the same deploy. Like 0083's keys, the line acts in the database too (ON
# DELETE SET NULL): the code before S6 does not know it, so a line it deletes (a code-only revert) leaves the verdict's line NULL instead
# of failing.

import django.db.models.deletion
from django.db import migrations, models

from mall.refactor.migration_ops import on_delete_in_database, with_lock_timeout


class Migration(migrations.Migration):

    dependencies = [
        ('mall', '0083_matching_cases'),
    ]

    operations = with_lock_timeout([
        migrations.AlterField(
            model_name='skureviewverdict',
            name='proposal',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='review_verdicts',
                                    to='mall.skuproposal', verbose_name='SKU propuesto'),
        ),
        migrations.AddField(
            model_name='skureviewverdict',
            name='line',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='review_verdicts',
                                    to='mall.part', verbose_name='línea'),
        ),
        migrations.AddIndex(
            model_name='skureviewverdict',
            index=models.Index(fields=['line', '-created_at'], name='sku_review_line_latest'),
        ),
        migrations.AddConstraint(
            model_name='skureviewverdict',
            constraint=models.UniqueConstraint(fields=('run', 'line'), name='sku_review_one_verdict_per_run_line'),
        ),
        on_delete_in_database((('mall_skureviewverdict', 'line_id', 'SET NULL'),)),
    ])
