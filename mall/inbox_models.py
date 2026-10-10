"""What each person has read in their deal inbox: a conversation up to a message, the notifications up to a deal event."""
from django.conf import settings
from django.db import models


class ConversationRead(models.Model):
    """The newest message of a deal's conversation this person has seen (in the inbox panel or on the deal itself)."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='conversation_reads')
    order = models.ForeignKey('mall.SupplierRequest', on_delete=models.CASCADE, related_name='reads')
    last_message_id = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'order'], name='unique_conversation_read')]


class InboxCursor(models.Model):
    """Per person: the newest deal event their notification list has shown, and the message unread counts start after. Both start at
    the newest one when the person first polls the inbox, so the history from before the inbox existed never shows up as new."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, primary_key=True, related_name='inbox_cursor')
    seen_event_id = models.BigIntegerField(default=0)
    message_floor = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
