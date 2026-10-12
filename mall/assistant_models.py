"""The WhatsApp assistant's memory of each conversation (mall.whatsapp_assistant)."""
from django.conf import settings
from django.db import models


class WhatsAppAssistantSession(models.Model):
    """The assistant's conversation with one number: the client account it shops for, the options it last numbered (so «la 2» or a tap
    picks one), what it is waiting for, and the last turns, for context. Forgotten after a long pause (mall.whatsapp_assistant.IDLE)."""
    STEPS = [('idle', 'Sin pendiente'), ('account', 'Eligiendo cuenta'), ('parts', 'Eligiendo repuesto'), ('offers', 'Eligiendo proveedor'),
             ('quantity', 'Esperando cantidad'), ('cart', 'Viendo la cesta')]
    number = models.CharField(max_length=32, unique=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    account = models.ForeignKey('mall.Account', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    step = models.CharField(max_length=10, choices=STEPS, default='idle')
    options = models.JSONField(default=list, blank=True)  # [{'id': 'part:<uuid>', 'label': ...}] as last numbered
    # The article waiting for its quantity, a quantity said in advance, a request waiting for the account to be chosen.
    pending = models.JSONField(default=dict, blank=True)
    history = models.JSONField(default=list, blank=True)  # the last turns: [{'rol': 'cliente' | 'asistente', 'texto': ...}]
    updated_at = models.DateTimeField(auto_now=True)
