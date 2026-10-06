from django.db import migrations, models
from django.db.models import Case, F, Q, Value, When
from django.db.models.functions import Abs


def positive_movements(apps, schema_editor):
    entries = apps.get_model('mall', 'StockEntry').objects.using(schema_editor.connection.alias)
    entries.update(
        quantity=Case(When(stock_delta=0, then=Abs(F('reserved_delta'))), default=Abs(F('stock_delta')), output_field=models.PositiveIntegerField()),
        direction=Case(
            When(stock_delta__lt=0, then=Value('debit')),
            When(stock_delta__gt=0, then=Value('credit')),
            When(reserved_delta__lt=0, then=Value('debit')),
            default=Value('credit'), output_field=models.CharField(),
        ),
        balance_type=Case(When(Q(stock_delta=0) & ~Q(reserved_delta=0), then=Value('reservation')), default=Value('stock'), output_field=models.CharField()),
        reserved_direction=Case(
            When(reserved_delta__gt=0, then=Value('credit')),
            When(reserved_delta__lt=0, then=Value('debit')),
            default=Value(''), output_field=models.CharField(),
        ),
        reserved_delta=Abs(F('reserved_delta')),
    )


def signed_movements(apps, schema_editor):
    entries = apps.get_model('mall', 'StockEntry').objects.using(schema_editor.connection.alias)
    entries.update(
        stock_delta=Case(
            When(balance_type='reservation', then=Value(0)),
            When(direction='debit', then=-F('quantity')),
            default=F('quantity'), output_field=models.IntegerField(),
        ),
        reserved_delta=Case(When(reserved_direction='debit', then=-F('reserved_delta')), default=F('reserved_delta'), output_field=models.IntegerField()),
    )


class Migration(migrations.Migration):
    dependencies = [('mall', '0009_reviewed_catalog_merges')]
    operations = [
        migrations.AddField(model_name='stockentry', name='quantity', field=models.PositiveIntegerField(default=0, verbose_name='cantidad del movimiento')),
        migrations.AddField(model_name='stockentry', name='direction', field=models.CharField(choices=[('credit', 'Crédito'), ('debit', 'Débito')], default='credit', max_length=6, verbose_name='dirección')),
        migrations.AddField(model_name='stockentry', name='balance_type', field=models.CharField(choices=[('stock', 'Existencias'), ('reservation', 'Reservas')], default='stock', max_length=11, verbose_name='saldo afectado')),
        migrations.AddField(model_name='stockentry', name='reserved_direction', field=models.CharField(blank=True, choices=[('credit', 'Crédito'), ('debit', 'Débito')], default='', max_length=6, verbose_name='dirección de la reserva')),
        migrations.RunPython(positive_movements, signed_movements),
        migrations.RemoveField(model_name='stockentry', name='stock_delta'),
        migrations.AlterField(model_name='stockentry', name='reserved_delta', field=models.PositiveIntegerField(default=0, verbose_name='cantidad del movimiento de reserva')),
        migrations.AddConstraint(model_name='stockentry', constraint=models.CheckConstraint(condition=Q(direction__in=['credit', 'debit']), name='stock_entry_valid_direction')),
        migrations.AddConstraint(model_name='stockentry', constraint=models.CheckConstraint(condition=Q(balance_type__in=['stock', 'reservation']), name='stock_entry_valid_balance_type')),
        migrations.AddConstraint(model_name='stockentry', constraint=models.CheckConstraint(condition=Q(reserved_delta=0, reserved_direction='') | Q(reserved_delta__gt=0, reserved_direction__in=['credit', 'debit']), name='stock_entry_reserved_direction')),
    ]
