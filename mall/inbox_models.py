"""What each person has read in their deal inbox: a conversation up to a message, the notifications up to a deal event."""
from django.conf import settings
from django.db import models


class ConversationRead(models.Model):
    """The newest message of a deal's conversation this person has seen (in the inbox panel or on the deal itself)."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='conversation_reads')
    order = models.ForeignKey('mall.SupplierRequest', on_delete=models.CASCADE, related_name='reads')
    last_message_id = models.BigIntegerField(default=0)
    # When the person was last emailed about unread messages of this deal (one email per conversation per cooldown).
    emailed_at = models.DateTimeField(null=True, blank=True)
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
