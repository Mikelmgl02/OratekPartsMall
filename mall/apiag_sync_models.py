from django.conf import settings
from django.db import models
from django.db.models import Q


class ApiagSucursal(models.Model):
    """A sucursal of a supplier's apiag-cloud, as its stock feed lists it (only the ones the supplier publishes there)."""
    account = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='apiag_sucursales', verbose_name='cuenta')
    apiag_id = models.CharField(max_length=40, verbose_name='ID en apiag-cloud')
    nombre = models.CharField(max_length=200, verbose_name='nombre')
    listed = models.BooleanField(default=True, verbose_name='publicada', help_text='La lista la última consulta a apiag-cloud.')
    first_seen_at = models.DateTimeField(auto_now_add=True, verbose_name='vista por primera vez')
    seen_at = models.DateTimeField(verbose_name='vista por última vez')

    class Meta:
        verbose_name = 'sucursal de apiag-cloud'
        verbose_name_plural = 'sucursales de apiag-cloud'
        constraints = [models.UniqueConstraint(fields=['account', 'apiag_id'], name='unique_apiag_sucursal')]

    def __str__(self):
        return self.nombre


class SupplierItemBranchStock(models.Model):
    """Units of an apiag-cloud item at one sucursal, kept only while above zero. The item's reported_quantity is their sum (the
    stock ledger records that total; check_stock_ledger proves the sum), and every pull rewrites an item's rows whole."""
    item = models.ForeignKey('mall.SupplierItem', on_delete=models.CASCADE, related_name='branch_stock', verbose_name='artículo del proveedor')
    sucursal = models.ForeignKey(ApiagSucursal, on_delete=models.PROTECT, related_name='stock', verbose_name='sucursal')
    quantity = models.PositiveIntegerField(verbose_name='unidades')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='última actualización')

    class Meta:
        verbose_name = 'existencias por sucursal'
        verbose_name_plural = 'existencias por sucursal'
        constraints = [models.UniqueConstraint(fields=['item', 'sucursal'], name='unique_item_branch_stock'),
                       models.CheckConstraint(condition=Q(quantity__gte=1), name='branch_stock_quantity_positive')]


class ApiagSync(models.Model):
    """The pull of one connection (mall.apiag_sync): its schedule, the run in progress (leased, so one at a time) and the last result.
    `since` is apiag-cloud's own server_time of the last complete pull, kept as it sent it."""
    STAGES = [('idle', 'Sin sincronizar'), ('running', 'Sincronizando'), ('completed', 'Sincronizada'), ('failed', 'Con error'),
              ('paused', 'En pausa')]
    MODES = [('full', 'Completa'), ('changes', 'Cambios'), ('day', 'Últimas 24 horas')]

    connection = models.OneToOneField('mall.ApiagConnection', on_delete=models.CASCADE, related_name='sync', verbose_name='conexión')
    server_url = models.CharField(max_length=300, blank=True, default='', verbose_name='servidor de la última consulta')
    requested = models.PositiveIntegerField(default=0, verbose_name='sincronizaciones pedidas')
    completed = models.PositiveIntegerField(default=0, verbose_name='sincronizaciones atendidas')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                     verbose_name='pedida por')
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name='próxima sincronización')
    lease = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    stage = models.CharField(max_length=10, choices=STAGES, default='idle', verbose_name='estado')
    mode = models.CharField(max_length=10, choices=MODES, blank=True, default='', verbose_name='tipo de consulta')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='inicio')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='fin')
    last_success_at = models.DateTimeField(null=True, blank=True, verbose_name='última sincronización correcta')
    last_full_at = models.DateTimeField(null=True, blank=True, verbose_name='última consulta completa')
    last_day_at = models.DateTimeField(null=True, blank=True, verbose_name='última consulta de 24 horas')
    since = models.CharField(max_length=40, blank=True, default='', verbose_name='server_time de la última consulta')
    sucursales_version = models.CharField(max_length=40, blank=True, default='', verbose_name='versión de sucursales')
    progress = models.JSONField(default=dict, blank=True, verbose_name='avance')
    summary = models.JSONField(default=dict, blank=True, verbose_name='resultado')
    error = models.TextField(blank=True, default='', verbose_name='error')
    error_code = models.CharField(max_length=30, blank=True, default='', verbose_name='código del error')
    failures = models.PositiveIntegerField(default=0, verbose_name='fallos seguidos')

    class Meta:
        verbose_name = 'sincronización con apiag-cloud'
        verbose_name_plural = 'sincronizaciones con apiag-cloud'
