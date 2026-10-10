from django.conf import settings
from django.db import models


class ApiagConnection(models.Model):
    """The supplier's apiag-cloud server: the base URL of its API and the access token that apiag-cloud issued for MotionPartes.
    The token is stored encrypted (mall.apiag_connection) and no response ever carries it back."""
    account = models.OneToOneField('mall.Account', on_delete=models.CASCADE, related_name='apiag_connection', verbose_name='cuenta')
    server_url = models.CharField(max_length=300, verbose_name='URL del servidor de apiag-cloud')
    token_ciphertext = models.TextField(verbose_name='token cifrado')
    token_hint = models.CharField(max_length=4, verbose_name='últimos caracteres del token')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='fecha de creación')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='última actualización')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+', verbose_name='actualizada por')

    class Meta:
        verbose_name = 'conexión con apiag-cloud'
        verbose_name_plural = 'conexiones con apiag-cloud'
