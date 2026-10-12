import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class InventoryRun(models.Model):
    """One load of a supplier's inventory that changed something (mall.inventory_history): an Excel import (one per job), an
    apiag-cloud pull, or a manual update. A run is written only when it changes an item, so the apiag pull every few minutes leaves
    no rows while nothing moves. It belongs to the supplier account, so disconnecting apiag-cloud or deleting the uploader keeps it."""
    ORIGINS = [('excel', 'Carga Excel'), ('apiag', 'apiag-cloud'), ('manual', 'Actualización manual')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    supplier = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='inventory_runs', verbose_name='proveedor')
    origin = models.CharField(max_length=10, choices=ORIGINS, verbose_name='origen')
    label = models.CharField(max_length=120, verbose_name='referencia',
                             help_text='El prefijo de sus actualizaciones: excel-<carga>, apiag-cloud <hora> #<id> o el ID de la actualización manual.')
    import_job = models.ForeignKey('mall.SupplierInventoryImportJob', on_delete=models.SET_NULL, null=True, blank=True, related_name='runs',
                                   verbose_name='carga Excel')
    filename = models.CharField(max_length=255, blank=True, default='', verbose_name='archivo')
    mode = models.CharField(max_length=10, blank=True, default='', verbose_name='modo', help_text='apiag-cloud: full, changes o day.')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+', verbose_name='usuario')
    started_at = models.DateTimeField(default=timezone.now, verbose_name='inicio')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='fin')
    counts = models.JSONField(default=dict, blank=True, verbose_name='resumen',
                              help_text='created, updated, stock, withdrawn, credit_units, debit_units (y lo que el origen sume).')
    rebuilt = models.BooleanField(default=False, verbose_name='reconstruida',
                                  help_text='Reconstruida de los registros anteriores al libro: sin el detalle de los campos que cambiaron.')

    class Meta:
        verbose_name = 'carga o sincronización de inventario'
        verbose_name_plural = 'cargas y sincronizaciones de inventario'
        constraints = [models.UniqueConstraint(fields=['supplier', 'origin', 'label'], name='inventory_run_unique_label')]
        indexes = [models.Index(fields=['supplier', '-started_at'], name='inventory_run_supplier_idx')]


class InventoryChange(models.Model):
    """What one run did to one supplier item: created it, changed its fields (each with its value before and after), moved its units
    (before and after, and per sucursal for apiag-cloud) or withdrew it, with why."""
    ACTIONS = [('created', 'Creado'), ('updated', 'Modificado'), ('stock', 'Existencias'), ('withdrawn', 'Retirado')]

    run = models.ForeignKey(InventoryRun, on_delete=models.CASCADE, related_name='changes', verbose_name='carga o sincronización')
    item = models.ForeignKey('mall.SupplierItem', on_delete=models.PROTECT, related_name='inventory_changes', verbose_name='artículo del proveedor')
    action = models.CharField(max_length=10, choices=ACTIONS, verbose_name='qué cambió')
    fields = models.JSONField(default=dict, blank=True, verbose_name='campos', help_text='{campo: [antes, después]}; retirado: {"reason": ...}.')
    quantity_before = models.PositiveIntegerField(null=True, blank=True, verbose_name='existencias antes')
    quantity_after = models.PositiveIntegerField(null=True, blank=True, verbose_name='existencias después')
    branches = models.JSONField(default=list, blank=True, verbose_name='por sucursal', help_text='[{sucursal, nombre, antes, después}]')
    stock_entry = models.ForeignKey('mall.StockEntry', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                    verbose_name='movimiento de existencias')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='fecha')

    class Meta:
        verbose_name = 'cambio de un artículo del proveedor'
        verbose_name_plural = 'cambios de artículos del proveedor'
        constraints = [models.UniqueConstraint(fields=['run', 'item'], name='inventory_change_unique_item'),
                       models.CheckConstraint(condition=models.Q(action__in=['created', 'updated', 'stock', 'withdrawn']), name='inventory_change_valid_action')]
        indexes = [models.Index(fields=['item', '-created_at'], name='inventory_change_item_idx')]
