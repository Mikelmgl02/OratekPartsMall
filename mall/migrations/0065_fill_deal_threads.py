"""Every pair that has dealt gets its conversation (DealThread): its orders, their messages (each keeping its order as its tag) and
WhatsApp log rows join it, and each person's read marks of the pair's orders become one mark of the thread: the furthest read point, the
latest notice cooldowns, a pin if any order was pinned, and the group of the most recently placed one."""
from collections import defaultdict

from django.db import migrations
from django.db.models import Min, OuterRef, Subquery


def fill(apps, schema_editor):
    SupplierRequest = apps.get_model('mall', 'SupplierRequest')
    DealThread = apps.get_model('mall', 'DealThread')
    DealMessage = apps.get_model('mall', 'DealMessage')
    ConversationRead = apps.get_model('mall', 'ConversationRead')
    WhatsAppMessage = apps.get_model('mall', 'WhatsAppMessage')
    pairs = SupplierRequest.objects.values('client_id', 'supplier_id').annotate(first=Min('created_at')).order_by('first')
    for pair in pairs:
        thread, _ = DealThread.objects.get_or_create(client_id=pair['client_id'], supplier_id=pair['supplier_id'])
        DealThread.objects.filter(pk=thread.pk).update(created_at=pair['first'])
        SupplierRequest.objects.filter(client_id=pair['client_id'], supplier_id=pair['supplier_id']).update(thread_id=thread.pk)
    thread_of = SupplierRequest.objects.filter(pk=OuterRef('order_id')).values('thread_id')[:1]
    DealMessage.objects.filter(thread__isnull=True).update(thread_id=Subquery(thread_of))
    WhatsAppMessage.objects.filter(order__isnull=False, thread__isnull=True).update(thread_id=Subquery(thread_of))
    marks = defaultdict(list)
    for mark in ConversationRead.objects.select_related('order').order_by('id'):
        marks[(mark.user_id, mark.order.thread_id)].append(mark)
    for (user_id, thread_id), rows in marks.items():
        latest = lambda field: max((getattr(row, field) for row in rows if getattr(row, field)), default=None)
        keep = max(rows, key=lambda row: (row.last_message_id, row.updated_at))
        placed = sorted((row for row in rows if row.group_id), key=lambda row: row.updated_at, reverse=True)
        ConversationRead.objects.filter(pk=keep.pk).update(
            thread_id=thread_id, last_message_id=max(row.last_message_id for row in rows), emailed_at=latest('emailed_at'),
            whatsapp_at=latest('whatsapp_at'), pinned_at=latest('pinned_at'), group_id=placed[0].group_id if placed else None)
        ConversationRead.objects.filter(pk__in=[row.pk for row in rows if row.pk != keep.pk]).delete()


class Migration(migrations.Migration):
    dependencies = [('mall', '0064_deal_threads')]
    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
