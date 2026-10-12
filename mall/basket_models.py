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
