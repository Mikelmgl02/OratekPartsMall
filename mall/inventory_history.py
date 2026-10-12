"""Libro de movimientos del proveedor: every movement of a supplier's stock and every load that changed its items.

Two records answer it:

- StockEntry, the stock ledger (one row per movement, with the balances after it). A confirmed quotation moves stock through its
  reservations: reserve at the handshake, shortfall, settle (the supplier's count includes the sale) and release (cancelled). An Excel
  import, an apiag-cloud pull or a manual update moves it through sync entries.
- InventoryRun and InventoryChange, written by the loads themselves (Recorder): per run, the items it created, the fields it changed
  (before and after), the units it moved (and per sucursal for apiag-cloud) and the items it withdrew, with why.

Both are read through the supplier's own account (/accounts/<id>/ledger/...) and, for superusers, through Administración
(/management/accounts/<id>/ledger/...). Neither shows prices: units only (owner decision D5 for MotionPartes' own view)."""
import logging
import uuid
from datetime import datetime, time, timedelta

from django.db.models import Case, CharField, Count, F, Q, Sum, Value, When
from django.db.models.functions import Greatest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.timezone import make_aware
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .inventory_history_models import InventoryChange, InventoryRun
from .models import Account, Membership, StockEntry

log = logging.getLogger(__name__)

FIELDS = ('codigo', 'brand', 'description', 'references')
FIELD_LABELS = {'codigo': 'Código', 'brand': 'Marca', 'description': 'Descripción', 'references': 'Referencias'}
PRECEDENCE = {'created': 3, 'withdrawn': 2, 'updated': 1, 'stock': 0}  # what a change says when one run touches an item twice
REASONS = {'retired': 'Retirado en apiag-cloud', 'refused': 'Datos de apiag-cloud no válidos', 'unseen': 'Ya no se publica en apiag-cloud'}
ORIGINS = {'quotation': 'Cotización confirmada', 'excel': 'Carga Excel', 'apiag': 'apiag-cloud', 'manual': 'Actualización manual'}
KINDS = dict(StockEntry._meta.get_field('kind').choices)
MODES = {'full': 'completa', 'changes': 'de cambios', 'day': 'de las últimas 24 horas'}
RESERVED_PREFIXES = ('excel-', 'apiag-cloud')  # update ids only the Excel import and the apiag pull write: a manual update cannot pose as one


# --- writing: the loads record what they changed ---------------------------------------------------------------------------------

class Recorder:
    """The run of one load (an Excel import, an apiag-cloud pull, a manual update) and its item changes. The run row is created with
    the first change, so a load that changes nothing leaves nothing. The caller holds the account lock, like every stock writer."""

    def __init__(self, supplier, origin, label, *, actor=None, import_job=None, filename='', mode=''):
        self.supplier, self.origin, self.label = supplier, origin, label[:120]
        self.actor, self.import_job, self.filename, self.mode = actor, import_job, filename[:255], mode
        self._run = None

    def run(self):
        if self._run is None:
            self._run, _ = InventoryRun.objects.get_or_create(supplier=self.supplier, origin=self.origin, label=self.label, defaults={
                'import_job': self.import_job, 'filename': self.filename, 'mode': self.mode,
                'actor': self.actor if getattr(self.actor, 'pk', None) else None})
        return self._run

    @property
    def touched(self):
        """This load recorded a change (a replayed manual update records nothing and leaves its run as it was)."""
        return self._run is not None

    def record(self, item, action, *, fields=None, before=None, after=None, branches=None, entry=None):
        run = self.run()
        change = InventoryChange.objects.filter(run=run, item=item).first()
        if change is None:
            InventoryChange.objects.create(run=run, item=item, action=action, fields=fields or {}, quantity_before=before, quantity_after=after,
                                           branches=branches or [], stock_entry=entry)
            return
        if PRECEDENCE[action] > PRECEDENCE[change.action]:
            change.action = action
        for field, value in (fields or {}).items():
            first = change.fields.get(field)
            change.fields[field] = [first[0], value[1]] if isinstance(first, list) and isinstance(value, list) else value
        if change.quantity_before is None:
            change.quantity_before = before
        if after is not None:
            change.quantity_after = after
        if branches:
            merged = {row['sucursal']: row for row in change.branches}
            for row in branches:
                merged[row['sucursal']] = {**row, 'before': merged[row['sucursal']]['before']} if row['sucursal'] in merged else row
            change.branches = sorted(merged.values(), key=lambda row: row['nombre'])
        change.stock_entry = entry or change.stock_entry
        change.save()

    def finish(self, *, done=True, mode=None):
        """Sum the run's changes into its counts (and close it when the load is over). Called after every committed page or batch, so a
        load stopped halfway keeps the counts of what it applied. The row is read again: one created in a page that rolled back is gone."""
        run = InventoryRun.objects.filter(supplier=self.supplier, origin=self.origin, label=self.label).first()
        self._run = run
        if run is None:
            return None
        counts = {action: 0 for action, _ in InventoryChange.ACTIONS}
        counts.update(dict(run.changes.values_list('action').annotate(total=Count('pk'))))
        units = run.changes.exclude(quantity_before=None).exclude(quantity_after=None).aggregate(
            credit=Sum(Greatest(F('quantity_after') - F('quantity_before'), Value(0))), debit=Sum(Greatest(F('quantity_before') - F('quantity_after'), Value(0))))
        values = {'counts': {**counts, 'credit_units': units['credit'] or 0, 'debit_units': units['debit'] or 0}}
        if mode:
            values['mode'] = mode
        if done:
            values['finished_at'] = timezone.now()
        InventoryRun.objects.filter(pk=run.pk).update(**values)
        return run


def snapshot(item):
    return {field: getattr(item, field) for field in FIELDS}


def field_changes(before, item):
    """{field: [before, after]} of the item fields that differ from the snapshot taken before the write."""
    return {field: [before[field], getattr(item, field)] for field in FIELDS if before[field] != getattr(item, field)}


def branch_changes(before, after, names):
    """[{sucursal, nombre, before, after}] of the sucursales whose units differ ({sucursal id: units} before and after)."""
    return sorted(({'sucursal': str(key), 'nombre': names.get(key, str(key)), 'before': before.get(key, 0), 'after': after.get(key, 0)}
                   for key in set(before) | set(after) if before.get(key, 0) != after.get(key, 0)), key=lambda row: row['nombre'])


# --- reading: the ledger of one supplier ----------------------------------------------------------------------------------------

def signed(quantity, direction):
    return quantity if direction == 'credit' else -quantity


def run_name(origin, label, filename):
    """How a load reads: the Excel file's name, the apiag-cloud pull's label (its time), or what a manual update is."""
    return filename or {'excel': 'Carga Excel', 'manual': 'Actualización manual'}.get(origin, label)


def entry_json(entry, runs, actor=None):
    reported = signed(entry.quantity, entry.direction) if entry.balance_type == 'stock' else 0
    reserved = signed(entry.reserved_delta, entry.reserved_direction) if entry.reserved_delta else 0
    # Available never goes below 0 (SupplierItem.available_quantity): the move is the change of the clamped balance, so a shortfall,
    # or a count below the reserved units, shows what really became available.
    available_after = max(0, entry.reported_after - entry.reserved_after)
    available_before = max(0, (entry.reported_after - reported) - (entry.reserved_after - reserved))
    reservation = entry.reservation if entry.reservation_id else None
    order = reservation.order if reservation else None
    item, run = entry.item, runs.get(entry.pk)
    return {
        'id': entry.pk, 'created_at': entry.created_at, 'kind': entry.kind, 'kind_label': KINDS.get(entry.kind, entry.kind),
        'origin': entry.origin, 'origin_label': ORIGINS[entry.origin],
        'item': {'id': str(item.pk), 'supplier_invent_id': item.supplier_invent_id, 'codigo': item.codigo, 'brand': item.brand,
                 'description': item.description, 'source': item.source},
        'stock_delta': reported, 'reserved_delta': reserved, 'available_delta': available_after - available_before,
        'reported_after': entry.reported_after, 'reserved_after': entry.reserved_after,
        'available_after': available_after, 'reference': entry.reference,
        'actor': actor(entry) if actor else (entry.actor.username if entry.actor_id else ''),
        'order': {'id': str(order.pk), 'reference': order.reference, 'client_name': order.client_name, 'status': order.status,
                  'status_label': order.get_status_display(), 'revision': reservation.quotation_line.quotation.revision} if order else None,
        'run': run,
    }


def ledger(supplier, params):
    origin = Case(When(reservation__isnull=False, then=Value('quotation')), When(reference__startswith='excel-', then=Value('excel')),
                  When(reference__startswith='apiag-cloud', then=Value('apiag')), default=Value('manual'), output_field=CharField())
    # An entry a process still running older code wrote has no supplier yet: it is found through its item.
    rows = (StockEntry.objects.filter(Q(supplier=supplier) | Q(supplier__isnull=True, item__supplier=supplier)).annotate(origin=origin)
            .select_related('item', 'actor', 'reservation__order', 'reservation__quotation_line__quotation'))
    if params.get('origin'):
        if params['origin'] not in ORIGINS:
            raise ValidationError({'origin': f'Usa {", ".join(ORIGINS)}.'})
        rows = rows.filter(origin=params['origin'])
    if params.get('kind'):
        if params['kind'] not in KINDS:
            raise ValidationError({'kind': f'Usa {", ".join(KINDS)}.'})
        rows = rows.filter(kind=params['kind'])
    if params.get('item'):
        try:
            rows = rows.filter(item_id=uuid.UUID(str(params['item'])))
        except ValueError:
            raise ValidationError({'item': 'Usa el id (uuid) de un artículo.'})
    search = (params.get('search') or '').strip()
    if search:
        rows = rows.filter(Q(item__codigo__icontains=search) | Q(item__supplier_invent_id__icontains=search) | Q(item__description__icontains=search)
                           | Q(reference__icontains=search) | Q(reservation__order__reference__icontains=search)
                           | Q(reservation__order__client_name__icontains=search))
    for key in ('after', 'before'):  # whole local days, both inclusive, as bounds the (supplier, date) index can range over
        if params.get(key):
            try:
                day = parse_date(params[key])
            except ValueError:
                day = None
            if day is None:
                raise ValidationError({key: 'Usa una fecha AAAA-MM-DD.'})
            rows = (rows.filter(created_at__gte=make_aware(datetime.combine(day, time.min))) if key == 'after'
                    else rows.filter(created_at__lt=make_aware(datetime.combine(day + timedelta(days=1), time.min))))
    return rows.order_by('-created_at', '-id')


def runs_of(entries):
    """{stock entry id: {id, label, name, origin}} for the sync entries a recorded load wrote."""
    return {entry_id: {'id': str(run_id), 'label': label, 'name': run_name(origin, label, filename), 'origin': origin}
            for entry_id, run_id, label, origin, filename in InventoryChange.objects.filter(stock_entry__in=[entry.pk for entry in entries])
            .values_list('stock_entry_id', 'run_id', 'run__label', 'run__origin', 'run__filename')}


def actor_mask(supplier):
    """Who a movement names in the supplier's own view: its members by name, the client's people as the client, the apiag-cloud pull as
    apiag-cloud, and anyone else (a MotionPartes administrator, a service) as MOTIONPARTES, as the deal timeline does."""
    from .apiag_sync import SERVICE_USER
    members = set(Membership.objects.filter(account=supplier).values_list('user_id', flat=True))
    clients = {}

    def label(user, order=None):
        if user is None:
            return ''
        if user.pk in members:
            return user.username
        if user.username == SERVICE_USER:
            return 'apiag-cloud'
        if order is not None:
            if order.client_id not in clients:
                clients[order.client_id] = set(Membership.objects.filter(account_id=order.client_id).values_list('user_id', flat=True))
            if user.pk in clients[order.client_id]:
                return order.client_name
        return 'MOTIONPARTES'

    return lambda entry: label(entry.actor if entry.actor_id else None, entry.reservation.order if entry.reservation_id else None), label


def run_json(run, actor=None):
    who = run.actor if run.actor_id else None
    return {'id': str(run.pk), 'origin': run.origin, 'origin_label': run.get_origin_display(), 'label': run.label, 'filename': run.filename,
            'name': run_name(run.origin, run.label, run.filename), 'mode': run.mode, 'mode_label': MODES.get(run.mode, run.mode),
            'started_at': run.started_at, 'finished_at': run.finished_at,
            'actor': actor(who) if actor else (who.username if who else ''),
            'counts': run.counts, 'rebuilt': run.rebuilt, 'import_job': str(run.import_job_id) if run.import_job_id else None}


def change_json(change):
    item = change.item
    reason = change.fields.get('reason') if change.action == 'withdrawn' else None
    fields = [] if reason else [{'field': field, 'label': FIELD_LABELS.get(field, field), 'before': pair[0], 'after': pair[1]}
                                for field, pair in change.fields.items() if isinstance(pair, list)]
    delta = None if change.quantity_before is None or change.quantity_after is None else change.quantity_after - change.quantity_before
    return {'id': change.pk, 'action': change.action, 'action_label': change.get_action_display(), 'created_at': change.created_at,
            'item': {'id': str(item.pk), 'supplier_invent_id': item.supplier_invent_id, 'codigo': item.codigo, 'brand': item.brand,
                     'description': item.description, 'source': item.source},
            'fields': fields, 'reason': reason, 'reason_label': REASONS.get(reason, reason or ''), 'quantity_before': change.quantity_before,
            'quantity_after': change.quantity_after, 'delta': delta, 'branches': change.branches}


# --- the API: the supplier's own account, and Administración for superusers -------------------------------------------------------

class SupplierScope:
    """The supplier account a ledger request reads: a member's own supplier account (accounts/<id>/...), or any supplier account for
    a superuser (management/accounts/<id>/...)."""
    management = False

    def supplier(self):
        account_id = self.kwargs['account_id']
        if self.management:
            if not self.request.user.is_superuser:
                raise PermissionDenied('Solo los superusuarios ven el libro de un proveedor.')
            account = get_object_or_404(Account, pk=account_id)
        else:
            from .views import account_for
            return account_for(self.request.user, account_id, 'supplier')
        if not account.roles.filter(capability='supplier').exists():
            raise PermissionDenied('La cuenta no tiene el rol de proveedor.')
        return account


class EntryPage(serializers.BaseSerializer):
    def to_representation(self, instance):
        return instance


LEDGER_PARAMS = [OpenApiParameter('origin', OpenApiTypes.STR, enum=list(ORIGINS)), OpenApiParameter('kind', OpenApiTypes.STR, enum=list(KINDS)),
                 OpenApiParameter('item', OpenApiTypes.UUID), OpenApiParameter('search', OpenApiTypes.STR),
                 OpenApiParameter('after', OpenApiTypes.DATE), OpenApiParameter('before', OpenApiTypes.DATE), OpenApiParameter('page', OpenApiTypes.INT)]


class LedgerEntries(SupplierScope, generics.ListAPIView):
    serializer_class = EntryPage

    def get_queryset(self):
        return ledger(self.supplier(), self.request.query_params)

    @extend_schema(operation_id='v1_accounts_ledger_list', responses=OpenApiTypes.OBJECT, parameters=LEDGER_PARAMS,
                   description='Los movimientos de existencias del proveedor, del más reciente al más antiguo: las cotizaciones confirmadas '
                               '(reserva, faltante, descuento, liberación) y las cargas (Excel, apiag-cloud, manual), con el saldo después de cada uno.')
    def get(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        runs, mask = runs_of(page), None if self.management else actor_mask(self.supplier())[0]
        return self.get_paginated_response([entry_json(entry, runs, mask) for entry in page])


class LedgerRuns(SupplierScope, generics.ListAPIView):
    serializer_class = EntryPage

    def get_queryset(self):
        rows = InventoryRun.objects.filter(supplier=self.supplier()).select_related('actor').order_by('-started_at', '-id')
        origin = self.request.query_params.get('origin')
        if origin:
            if origin not in dict(InventoryRun.ORIGINS):
                raise ValidationError({'origin': 'Usa excel, apiag o manual.'})
            rows = rows.filter(origin=origin)
        return rows

    @extend_schema(operation_id='v1_accounts_ledger_runs_list', responses=OpenApiTypes.OBJECT,
                   parameters=[OpenApiParameter('origin', OpenApiTypes.STR), OpenApiParameter('page', OpenApiTypes.INT)],
                   description='Las cargas Excel, sincronizaciones con apiag-cloud y actualizaciones manuales que cambiaron algo, con su resumen.')
    def get(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        mask = None if self.management else actor_mask(self.supplier())[1]
        return self.get_paginated_response([run_json(run, mask) for run in page])


class LedgerRun(SupplierScope, APIView):
    @extend_schema(operation_id='v1_accounts_ledger_run_retrieve', responses=OpenApiTypes.OBJECT, description='Una carga o sincronización con su resumen.')
    def get(self, request, account_id, run_id):
        supplier = self.supplier()
        run = get_object_or_404(InventoryRun.objects.select_related('actor'), pk=run_id, supplier=supplier)
        return Response(run_json(run, None if self.management else actor_mask(supplier)[1]))


class LedgerRunChanges(SupplierScope, generics.ListAPIView):
    serializer_class = EntryPage

    def get_queryset(self):
        run = get_object_or_404(InventoryRun, pk=self.kwargs['run_id'], supplier=self.supplier())
        rows = run.changes.select_related('item').order_by('item__supplier_invent_id', 'id')
        action = self.request.query_params.get('action')
        if action:
            if action not in dict(InventoryChange.ACTIONS):
                raise ValidationError({'action': 'Usa created, updated, stock o withdrawn.'})
            rows = rows.filter(action=action)
        search = (self.request.query_params.get('search') or '').strip()
        if search:
            rows = rows.filter(Q(item__codigo__icontains=search) | Q(item__supplier_invent_id__icontains=search) | Q(item__description__icontains=search))
        return rows

    @extend_schema(operation_id='v1_accounts_ledger_run_changes_list', responses=OpenApiTypes.OBJECT,
                   parameters=[OpenApiParameter('action', OpenApiTypes.STR), OpenApiParameter('search', OpenApiTypes.STR),
                                                              OpenApiParameter('page', OpenApiTypes.INT)],
                   description='Qué hizo la carga a cada artículo: creado, campos cambiados (antes y después), existencias (y por sucursal) o retirado.')
    def get(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response([change_json(change) for change in page])


def superusers():
    from .management import IsSuperuser
    return [IsSuperuser]


@extend_schema_view(get=extend_schema(operation_id='v1_management_accounts_ledger_list'))
class ManagementLedgerEntries(LedgerEntries):
    management, permission_classes = True, superusers()


@extend_schema_view(get=extend_schema(operation_id='v1_management_accounts_ledger_runs_list'))
class ManagementLedgerRuns(LedgerRuns):
    management, permission_classes = True, superusers()


@extend_schema_view(get=extend_schema(operation_id='v1_management_accounts_ledger_run_retrieve'))
class ManagementLedgerRun(LedgerRun):
    management, permission_classes = True, superusers()


@extend_schema_view(get=extend_schema(operation_id='v1_management_accounts_ledger_run_changes_list'))
class ManagementLedgerRunChanges(LedgerRunChanges):
    management, permission_classes = True, superusers()
