"""What each person has read in their deal inbox: a conversation up to a message, the notifications up to a deal event. Also how they
organize their conversations list: the ones they pinned and the groups they made."""
import uuid

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower


class InboxGroup(models.Model):
    """A group the person made in their conversations list, shown in the order they made them. Deleting it leaves its conversations
    ungrouped."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='inbox_groups')
    name = models.CharField(max_length=60)
    position = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position', 'created_at']
        constraints = [models.UniqueConstraint(Lower('name'), 'user', name='unique_inbox_group_name')]


class ConversationRead(models.Model):
    """This person's state of a deal's conversation: the newest message they have seen (in the inbox panel or on the deal itself), and
    where they keep it in their list (pinned on top, or in one of their groups)."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='conversation_reads')
    order = models.ForeignKey('mall.SupplierRequest', on_delete=models.CASCADE, related_name='reads')
    last_message_id = models.BigIntegerField(default=0)
    # When the person was last emailed (or sent a WhatsApp) about unread messages of this deal (one per conversation per cooldown).
    emailed_at = models.DateTimeField(null=True, blank=True)
    whatsapp_at = models.DateTimeField(null=True, blank=True)
    # A pinned conversation lists under Fijadas whatever its group; unpinned, it goes back to its group.
    pinned_at = models.DateTimeField(null=True, blank=True)
    group = models.ForeignKey(InboxGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name='conversations')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'order'], name='unique_conversation_read')]


class InboxCursor(models.Model):
    """Per person: the newest deal event their notification list has shown, and the message unread counts start after. Both start at
    the newest one when the person first polls the inbox, so the history from before the inbox existed never shows up as new. Also
    when they last polled it (someone active in the app gets no email about a message) and which emails they want."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, primary_key=True, related_name='inbox_cursor')
    seen_event_id = models.BigIntegerField(default=0)
    message_floor = models.BigIntegerField(default=0)
    active_at = models.DateTimeField(null=True, blank=True)
    email_messages = models.BooleanField(default=True, verbose_name='correo por mensajes nuevos')
    email_updates = models.BooleanField(default=True, verbose_name='correo por cambios en las órdenes')
    # WhatsApp needs the person's explicit consent (Meta's opt-in rule): nothing is sent until whatsapp_opt_in_at is set, and replying
    # BAJA or STOP clears it.
    whatsapp_number = models.CharField(max_length=20, blank=True, default='', verbose_name='número de WhatsApp')
    whatsapp_opt_in_at = models.DateTimeField(null=True, blank=True, verbose_name='aceptó avisos por WhatsApp')
    whatsapp_messages = models.BooleanField(default=True, verbose_name='WhatsApp por mensajes nuevos')
    whatsapp_updates = models.BooleanField(default=True, verbose_name='WhatsApp por cambios en las órdenes')
    # The last automatic answer to a message the person wrote to the notices number (at most one per WHATSAPP_REPLY_EVERY).
    whatsapp_replied_at = models.DateTimeField(null=True, blank=True)
    # A number is used only once the person proves it is theirs: a code sent there by WhatsApp (mall.whatsapp_verification), kept only as
    # a hash, valid for a few minutes and a few attempts. Until then the number waits here, and an earlier verified number keeps working.
    whatsapp_verified_at = models.DateTimeField(null=True, blank=True, verbose_name='número de WhatsApp verificado')
    whatsapp_pending_number = models.CharField(max_length=20, blank=True, default='')
    whatsapp_code_hash = models.CharField(max_length=64, blank=True, default='')
    whatsapp_code_sent_at = models.DateTimeField(null=True, blank=True)
    whatsapp_code_attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class PushSubscription(models.Model):
    """A browser that agreed to receive this person's deal notifications (Web Push). The endpoint names the browser: when another person
    signs in there and turns notifications on, the subscription moves to them. Push services drop an endpoint with 404/410; so do we."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='push_subscriptions')
    endpoint = models.URLField(max_length=1000, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class WhatsAppMessage(models.Model):
    """Every WhatsApp notice sent, with what Meta's webhook later said about it (sent → delivered → read, or failed)."""
    STATUSES = [('queued', 'En cola'), ('accepted', 'Aceptado por WhatsApp'), ('sent', 'Enviado'), ('delivered', 'Entregado'), ('read', 'Leído'),
                ('failed', 'Falló')]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='whatsapp_messages')
    order = models.ForeignKey('mall.SupplierRequest', on_delete=models.SET_NULL, null=True, blank=True, related_name='whatsapp_messages')
    number = models.CharField(max_length=20)
    template = models.CharField(max_length=80)
    kind = models.CharField(max_length=40)
    wamid = models.CharField(max_length=128, blank=True, default='', db_index=True, verbose_name='id del mensaje en WhatsApp')
    status = models.CharField(max_length=10, choices=STATUSES, default='queued')
    error = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-id']
