"""The cesta (mall.basket): what a person is gathering to ask quotations for, per client account, kept on the server so every device (and
the WhatsApp assistant) sees the same one."""
from django.conf import settings
from django.db import models


class BasketItem(models.Model):
    """One supplier's article in a person's cesta for a client account, as it was when added: its part, code and brand go with the
    solicitud as the expected ones, so a change since is caught when sending (mall.request_views.submit_request)."""
    SOURCES = [('web', 'MotionPartes'), ('whatsapp', 'WhatsApp')]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='basket_items')
    account = models.ForeignKey('mall.Account', on_delete=models.CASCADE, related_name='basket_items')
    supplier_item = models.ForeignKey('mall.SupplierItem', on_delete=models.CASCADE, related_name='basket_items')
    part = models.ForeignKey('mall.Part', on_delete=models.CASCADE, related_name='basket_items')
    codigo = models.CharField(max_length=120)
    brand = models.CharField(max_length=120, blank=True, default='')
    quantity = models.PositiveIntegerField()
    source = models.CharField(max_length=10, choices=SOURCES, default='web')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at', 'id']
        constraints = [models.UniqueConstraint(fields=['user', 'account', 'supplier_item'], name='unique_basket_item')]


class BasketEvent(models.Model):
    """The cesta's ledger: one change to a person's cesta for a client account, with the article as it was (so the history reads the same
    after the article changes or is gone). Written where every change passes (mall.basket.apply_changes and settle): the site, WhatsApp,
    a browser's old cesta moving in, and a solicitud taking units out."""
    KINDS = [('added', 'Agregado'), ('changed', 'Cantidad cambiada'), ('removed', 'Quitado'), ('moved_in', 'Traído del navegador'),
             ('sent', 'Enviado en una solicitud')]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='basket_events')
    account = models.ForeignKey('mall.Account', on_delete=models.CASCADE, related_name='basket_events')
    supplier_item = models.ForeignKey('mall.SupplierItem', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    part = models.ForeignKey('mall.Part', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    sku = models.CharField(max_length=120, blank=True, default='')
    codigo = models.CharField(max_length=120)
    brand = models.CharField(max_length=120, blank=True, default='')
    supplier_name = models.CharField(max_length=255, blank=True, default='')
    kind = models.CharField(max_length=10, choices=KINDS)
    quantity_before = models.PositiveIntegerField(default=0)
    quantity_after = models.PositiveIntegerField(default=0)
    source = models.CharField(max_length=10, choices=BasketItem.SOURCES, default='web')
    reference = models.CharField(max_length=40, blank=True, default='')  # the order a sent line went in
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-id']
        indexes = [models.Index(fields=['user', 'account', '-id'], name='basket_event_cesta'), models.Index(fields=['created_at'], name='basket_event_time')]
