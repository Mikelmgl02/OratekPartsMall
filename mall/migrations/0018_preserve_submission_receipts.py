from django.db import migrations


def freeze_receipts(apps, schema_editor):
    Submission = apps.get_model('mall', 'ClientRequestSubmission')
    Contribution = apps.get_model('mall', 'RequestContribution')
    for submission in Submission.objects.filter(result__isnull=True).iterator(chunk_size=200):
        receipts = []
        for order in submission.requests.order_by('supplier_name', 'id'):
            lines = list(order.lines.all())
            captured = [{'supplier_item_id': str(line.supplier_item_id), 'codigo': line.codigo,
                         'sku': line.sku, 'quantity': line.quantity} for line in lines]
            Contribution.objects.get_or_create(submission=submission, order=order, defaults={'lines': captured})
            receipts.append({'id': str(order.pk), 'reference': order.reference,
                'supplier': {'id': str(order.supplier_id), 'name': order.supplier_name},
                'line_count': len(lines), 'unit_count': sum(line.quantity for line in lines)})
        submission.result = {'submission_id': str(submission.submission_id),
                             'created_at': submission.created_at.isoformat(), 'requests': receipts}
        submission.save(update_fields=['result'])


class Migration(migrations.Migration):
    dependencies = [('mall', '0017_deal_commands')]
    operations = [migrations.RunPython(freeze_receipts, migrations.RunPython.noop)]
