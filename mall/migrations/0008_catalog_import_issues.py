import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('mall', '0007_reviewed_ai_classification')]

    operations = [
        migrations.CreateModel(
            name='CatalogImportIssue',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('row', models.PositiveIntegerField()),
                ('values', models.JSONField(default=dict)),
                ('original', models.JSONField(default=dict)),
                ('errors', models.JSONField(default=list)),
                ('recovery', models.JSONField(default=list)),
                ('status', models.CharField(choices=[('pending', 'Pendiente'), ('resolved', 'Corregida')], default='pending', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('applied_summary', models.JSONField(default=dict)),
                ('job', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='issues', to='mall.catalogimportjob')),
                ('part', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='mall.part')),
            ],
            options={
                'ordering': ['created_at', 'row'],
                'constraints': [models.UniqueConstraint(fields=('job', 'row'), name='unique_catalog_import_issue_row')],
            },
        ),
    ]
