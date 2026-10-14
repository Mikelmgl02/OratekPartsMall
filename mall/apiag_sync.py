"""The pull from a supplier's apiag-cloud: its published items become that account's inventory (source «apiag»), with their units per
sucursal. Runs in the apiag-sync service (mall.apiag_sync_runner); the supplier panel only asks for a run.

Each run reads apiag-cloud's GET /api/motionpartes/stock/ page by page (see its feed.py) and applies every page in one short
transaction, under the same locks as an Excel upload: the supplier account, then the page's items in id order. Items go through
ingest_inventory, the one write path of stock: a changed total writes a «sync» entry in the stock ledger, signed by the disabled
service identity, and new or edited items are queued for matching. Unchanged items write nothing. The units per sucursal live in
SupplierItemBranchStock and always add up to the item's total; MotionPartes' reservations stay on the item.

What a run asks for:
- full: everything published, on the first pull, when the supplier changes which sucursales they publish, or when the last good
  pull is too old for apiag-cloud's `since`. Items a complete full pull did not list drop to 0 (withdrawn).
- changes: what changed since the last good pull (apiag-cloud adds its own overlap), every APIAG_SYNC_MINUTES.
- day: once a day after APIAG_SYNC_DAILY_AT, everything that changed in the 24 hours before the last good pull, which repairs
  anything a run missed.
Withdrawn items (retired, or no longer ACTIVO there) drop to 0 and keep their history. A failed run changes no cursor: the next
one asks again from the same point, and values are absolute, so applying a page twice changes nothing."""
import json
import logging
import time as clock
import uuid
from collections import Counter
from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Case, F, IntegerField, Q, Value, When
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError

from . import apiag_client
from .apiag_models import ApiagConnection
from .apiag_sync_models import ApiagSucursal, ApiagSync, SupplierItemBranchStock
from .apiag_tokens import TokenUnreadable, token
from .matching_runner import WorkerStopping, checkpoint, no_cut
from .models import Account, SupplierItem
from .serializers import InventorySerializer
from .services import ingest_inventory

logger = logging.getLogger(__name__)

FEED_PATH = '/api/motionpartes/stock/'
PAGE_SIZE = 500
MAX_PAGE_BYTES = 32 * 1024 * 1024
MAX_QUANTITY = 2147483647
MAX_REFERENCES = 100
LEASE = timedelta(minutes=30)
RUN_LIMIT = timedelta(minutes=25)  # a run gives up before its lease can expire under it
FULL_AFTER = timedelta(days=6)  # apiag-cloud refuses a `since` older than 7 days
MAX_BACKOFF = timedelta(hours=1)
MAX_SAMPLES = 20
SERVICE_USER = 'motionpartes-apiag-sync-service'
# Answers that will not change by asking again: wait until the connection is saved again or someone asks for a run.
PERMANENT = {'token_rejected', 'disabled', 'not_found', 'license', 'redirect', 'internal', 'token_unreadable'}


class SyncFailed(Exception):
    def __init__(self, status, message, *, resync=False):
        super().__init__(message)
        self.status, self.message, self.resync = status, message, resync


def service_user():
    """The disabled, non-login identity that signs the stock ledger entries of a pull (like the matching worker's)."""
    user, created = get_user_model().objects.get_or_create(username=SERVICE_USER, defaults={
        'email': 'apiag-sync-service@motionpartes.invalid', 'is_active': False})
    if created:
        user.set_unusable_password()
        user.save(update_fields=['password'])
    if user.is_active or user.has_usable_password():
        raise RuntimeError('La identidad de servicio de la sincronización con apiag-cloud debe estar desactivada.')
    return user


def interval():
    return timedelta(minutes=settings.APIAG_SYNC_MINUTES)


# --- reading the feed -----------------------------------------------------------------------------------------------------------

def fetch(connection, secret, params):
    try:
        code, body, headers = apiag_client.get(connection.server_url, f'{FEED_PATH}?{urlencode(params)}', secret, max_bytes=MAX_PAGE_BYTES)
    except apiag_client.Unreachable as error:
        raise SyncFailed(error.status, error.message)
    if code == 400 and 'since' in (apiag_client.detail(body) or {}):
        raise SyncFailed('since', 'apiag-cloud ya no acepta la fecha de la última consulta; la próxima será completa.', resync=True)
    if code != 200:
        raise SyncFailed(*apiag_client.verdict(code, body, headers))
    page = apiag_client.detail(body)
    if not page or not isinstance(page.get('items'), list) or not isinstance(page.get('sucursales'), list) or not page.get('server_time'):
        raise SyncFailed('unexpected', 'La respuesta de apiag-cloud no tiene el formato esperado: actualiza ese apiag-cloud.')
    return page


def references(entry):
    codes = [entry.get('codigo2')] + list(entry.get('alternos') or [])
    rows = [{'code': code.strip()} for code in codes if isinstance(code, str) and code.strip()]
    return rows[:MAX_REFERENCES]


def item_data(entry, sid, quantity, update_id):
    serializer = InventorySerializer(data={
        'update_id': update_id, 'supplier_invent_id': sid, 'codigo': entry.get('codigo') or '', 'brand': entry.get('marca') or '',
        'description': entry.get('descripcion') or '', 'references': references(entry), 'source': 'apiag', 'quantity': quantity})
    if not serializer.is_valid():
        raise ValidationError(serializer.errors)
    return serializer.validated_data


def same(item, data):
    return (item.codigo, item.brand, item.description, item.references, item.reported_quantity) == (
        data['codigo'], data['brand'], data['description'], data['references'], data['quantity'])


# --- applying it ----------------------------------------------------------------------------------------------------------------

def upsert_sucursales(account, listed, now):
    """{apiag id: ApiagSucursal} of the listed sucursales; the rest of the account's are marked unlisted."""
    found = {}
    for row in listed:
        if not isinstance(row, dict) or 'id' not in row:
            continue
        sucursal, _ = ApiagSucursal.objects.update_or_create(account=account, apiag_id=str(row['id']), defaults={
            'nombre': str(row.get('nombre') or row['id'])[:200], 'listed': True, 'seen_at': now})
        found[str(row['id'])] = sucursal
    ApiagSucursal.objects.filter(account=account).exclude(apiag_id__in=list(found)).update(listed=False)
    return found


def set_branch_rows(item, quantities, current, recorder=None):
    """Make the item's rows say {ApiagSucursal: units}: rows at 0 or for unlisted sucursales go. The recorder keeps the units each
    sucursal had before and has now, even when the item's total did not move (units shifted between sucursales)."""
    wanted = {sucursal.pk: units for sucursal, units in quantities.items() if units > 0}
    if recorder is not None:
        from .inventory_history import branch_changes
        names = {**{row.sucursal_id: row.sucursal.nombre for row in current.values()}, **{sucursal.pk: sucursal.nombre for sucursal in quantities}}
        moved = branch_changes({key: row.quantity for key, row in current.items()}, wanted, names)
        if moved:
            recorder.record(item, 'stock', branches=moved)
    SupplierItemBranchStock.objects.filter(item=item).exclude(sucursal_id__in=list(wanted)).delete()
    for sucursal_id, units in wanted.items():
        row = current.get(sucursal_id)
        if row is None:
            SupplierItemBranchStock.objects.create(item=item, sucursal_id=sucursal_id, quantity=units)
        elif row.quantity != units:
            row.quantity = units
            row.save(update_fields=['quantity', 'updated_at'])


def note(totals, samples, kind, text):
    totals[kind] += 1
    if len(samples) < MAX_SAMPLES:
        samples.append(text)


def write(account, actor, data, totals, samples, label, recorder=None):
    """ingest_inventory in its own savepoint, so one refused item does not undo the page. Returns the item or None."""
    try:
        with transaction.atomic():
            return ingest_inventory(supplier=account, actor=actor, data=data, queue_matching=False, recorder=recorder)
    except ValidationError as error:
        detail = error.detail if isinstance(error.detail, str) else json.dumps(error.detail, ensure_ascii=False, default=str)
        note(totals, samples, 'refused', f'{label}: {detail}'[:300])
        return None


def withdraw(account, actor, item, update_id, totals, samples, recorder=None, reason='unseen'):
    """Take an item off sale: its count to 0 and its sucursal rows gone. reason: retired (retired in apiag-cloud), refused (its data is
    not valid) or unseen (a full pull no longer lists it)."""
    before = item.reported_quantity
    rows = {row.sucursal_id: row for row in SupplierItemBranchStock.objects.filter(item=item).select_related('sucursal')}
    if item.reported_quantity:
        # The code the supplier sent last: a code change waiting for review (codigo_pendiente, S3) is what it sends now, so the
        # withdrawal leaves it waiting instead of reading the old code as the supplier going back to it.
        codigo = item.codigo_pendiente or item.codigo
        brand = item.brand if item.brand_pendiente is None else item.brand_pendiente
        data = {'update_id': update_id, 'supplier_invent_id': item.supplier_invent_id, 'codigo': codigo, 'brand': brand,
                'description': item.description, 'references': item.references, 'source': 'apiag', 'quantity': 0}
        if write(account, actor, data, totals, samples, item.codigo, recorder) is None:
            return
    SupplierItemBranchStock.objects.filter(item=item).delete()
    totals['withdrawn'] += 1
    if recorder is not None and (before or rows):
        from .inventory_history import branch_changes
        moved = branch_changes({key: row.quantity for key, row in rows.items()}, {}, {key: row.sucursal.nombre for key, row in rows.items()})
        recorder.record(item, 'withdrawn', fields={'reason': reason}, before=before, after=0, branches=moved)


def apply_page(account, actor, sucursales, entries, label, totals, samples, seen, recorder=None):
    with transaction.atomic():
        Account.objects.select_for_update().get(pk=account.pk)
        ids = [str(entry['id']) for entry in entries if isinstance(entry, dict) and 'id' in entry]
        # Lock order of every stock writer: the account, then the items by id (a client's request locks them the same way).
        existing = {item.supplier_invent_id: item for item in
                    SupplierItem.objects.select_for_update().filter(supplier=account, supplier_invent_id__in=ids).order_by('pk')}
        current = {}
        for row in SupplierItemBranchStock.objects.filter(item__in=list(existing.values())).select_related('sucursal'):
            current.setdefault(row.item_id, {})[row.sucursal_id] = row
        for entry in entries:
            if not isinstance(entry, dict) or 'id' not in entry:
                note(totals, samples, 'refused', 'Un artículo de la respuesta no trae su id.')
                continue
            sid = str(entry['id'])
            seen.add(sid)
            item, update_id = existing.get(sid), f'{label} · {sid}'[:120]
            totals['received'] += 1
            if item is not None and item.source != 'apiag':
                note(totals, samples, 'conflicts', f'{item.codigo} ({sid}) ya existe como carga manual: no se toca.')
                continue
            if entry.get('status') == 'retired':
                if item is not None and (item.reported_quantity or item.pk in current):
                    withdraw(account, actor, item, update_id, totals, samples, recorder, 'retired')
                continue
            quantities = {}
            for row in entry.get('stock') or []:
                sucursal = sucursales.get(str(row.get('sucursal'))) if isinstance(row, dict) else None
                units = row.get('disponible') if sucursal else None
                if isinstance(units, int) and not isinstance(units, bool) and units > 0:
                    quantities[sucursal] = units
            try:
                if sum(quantities.values()) > MAX_QUANTITY:
                    raise ValidationError({'existencias': 'Fuera de rango.'})
                data = item_data(entry, sid, sum(quantities.values()), update_id)
            except ValidationError as error:
                note(totals, samples, 'refused', f'{entry.get("codigo") or (item and item.codigo) or sid}: {json.dumps(error.detail, ensure_ascii=False, default=str)}'[:300])
                if item is not None and (item.reported_quantity or item.pk in current):
                    withdraw(account, actor, item, update_id, totals, samples, recorder, 'refused')  # its last units must not stay on sale unchecked
                continue
            if item is None or not same(item, data):
                created = item is None
                item = write(account, actor, data, totals, samples, data['codigo'], recorder)
                if item is None:
                    continue
                totals['created' if created else 'updated'] += 1
            else:
                totals['unchanged'] += 1
            set_branch_rows(item, quantities, current.get(item.pk, {}), recorder)


def withdraw_unseen(account, actor, seen, label, totals, samples, recorder=None):
    """After a complete full pull: the account's apiag items it did not list are no longer published there."""
    gone = [pk for pk, sid in SupplierItem.objects.filter(supplier=account, source='apiag').filter(
        Q(reported_quantity__gt=0) | Q(branch_stock__isnull=False)).distinct().values_list('pk', 'supplier_invent_id') if sid not in seen]
    for start in range(0, len(gone), PAGE_SIZE):
        with transaction.atomic():
            Account.objects.select_for_update().get(pk=account.pk)
            for item in SupplierItem.objects.select_for_update().filter(pk__in=gone[start:start + PAGE_SIZE]).order_by('pk'):
                withdraw(account, actor, item, f'{label} · {item.supplier_invent_id} retirado'[:120], totals, samples, recorder, 'unseen')


# --- one run ---------------------------------------------------------------------------------------------------------------------

def daily_due(sync, now):
    hour, minute = (int(part) for part in settings.APIAG_SYNC_DAILY_AT.split(':'))
    slot = timezone.make_aware(datetime.combine(timezone.localdate(now), time(hour, minute)))
    return now >= slot and (sync.last_day_at is None or sync.last_day_at < slot)


def plan(sync, now):
    """(mode, since) of the next run."""
    if not sync.since or not sync.last_success_at or now - sync.last_success_at > FULL_AFTER or sync.server_url != sync.connection.server_url:
        return 'full', None
    if daily_due(sync, now):
        since = parse_datetime(sync.since)
        return 'day', (since - timedelta(hours=24)).isoformat() if since else None
    return 'changes', sync.since


def run(sync, lease, now):
    """Pull everything the plan asks for. Returns the summary to store; raises SyncFailed. The supplier's ledger keeps what the pull
    changed, also when it stops halfway (its pages already applied stay)."""
    state = {}
    try:
        return pull(sync, lease, now, state)
    finally:
        if state.get('recorder'):
            try:
                state['recorder'].finish(mode=state['mode'])
            except Exception:  # closing the ledger's run never hides why the pull stopped
                logger.exception('Could not close the ledger run of an apiag-cloud pull.')


def pull(sync, lease, now, state):
    connection = sync.connection
    account = connection.account
    try:
        secret = token(connection)
    except TokenUnreadable:
        raise SyncFailed('token_unreadable', 'El token guardado ya no se puede leer: pide a MotionPartes que lo vuelva a ingresar.')
    actor = service_user()
    mode, since = plan(sync, now)
    started = clock.monotonic()
    while True:
        totals, samples, seen, first, after, restart = Counter(), [], set(), None, 0, False
        while True:
            checkpoint()  # a stop signal or the catalog freeze ends the pull here, between two pages
            if clock.monotonic() - started > RUN_LIMIT.total_seconds():
                raise SyncFailed('timeout', 'La sincronización tardó demasiado y se detuvo; la próxima continúa desde el mismo punto.')
            params = {'limit': PAGE_SIZE, 'after': after, **({'since': since} if since else {})}
            page = fetch(connection, secret, params)
            if first is None:
                first = page
                if mode != 'full' and sync.sucursales_version and page.get('sucursales_version') != sync.sucursales_version:
                    restart = True  # the supplier changed what they publish: every item's sucursales may differ
                    break
                sucursales = upsert_sucursales(account, page['sucursales'], now)
                stamp = parse_datetime(str(page['server_time']))
                stamp = timezone.localtime(stamp if stamp and timezone.is_aware(stamp) else now)
                label = f'apiag-cloud {stamp:%Y-%m-%d %H:%M} #{lease.hex[:4]}'
                from .inventory_history import Recorder
                recorder = state['recorder'] = Recorder(account, 'apiag', label, actor=actor, mode=mode)  # a run row only if something changes
                state['mode'] = mode
            apply_page(account, actor, sucursales, page['items'], label, totals, samples, seen, recorder)
            totals['pages'] += 1
            if recorder.touched:
                recorder.finish(done=False, mode=mode)  # a pull stopped halfway keeps the counts of the pages it applied
            ApiagSync.objects.filter(pk=sync.pk, lease=lease).update(progress={'mode': mode, **totals})
            following = page.get('next')
            if following is None:
                break
            if not isinstance(following, int) or isinstance(following, bool) or following <= after:
                raise SyncFailed('unexpected', 'apiag-cloud repitió una página: actualiza ese apiag-cloud.')
            after = following
        if not restart:
            break
        mode, since = 'full', None
    if mode == 'full':
        withdraw_unseen(account, actor, seen, label, totals, samples, recorder)
    return {'mode': mode, 'server_time': first['server_time'], 'sucursales_version': first.get('sucursales_version', ''),
            'counts': dict(totals), 'samples': samples}


# --- scheduling ------------------------------------------------------------------------------------------------------------------

def ensure_rows():
    missing = ApiagConnection.objects.filter(sync__isnull=True)
    ApiagSync.objects.bulk_create([ApiagSync(connection=connection) for connection in missing], ignore_conflicts=True)


def claim(now):
    """(sync, lease, revision) of the most pressing due pull, leased, or None. Asked runs go first."""
    due = (Q(lease_until__isnull=True) | Q(lease_until__lt=now)) & (
        Q(requested__gt=F('completed'))
        | (~Q(stage='paused') & (Q(next_run_at__isnull=True) | Q(next_run_at__lte=now)))
        | (Q(stage='paused') & Q(connection__updated_at__gt=F('started_at'))))
    with transaction.atomic():
        sync = (ApiagSync.objects.select_for_update(skip_locked=True, of=('self',)).select_related('connection__account')
                .filter(due, connection__account__active=True)
                .order_by(Case(When(requested__gt=F('completed'), then=Value(0)), default=Value(1), output_field=IntegerField()),
                          F('next_run_at').asc(nulls_first=True), 'pk').first())
        if sync is None:
            return None
        lease = uuid.uuid4()
        ApiagSync.objects.filter(pk=sync.pk).update(lease=lease, lease_until=now + LEASE, stage='running', started_at=now, progress={})
        return sync, lease, sync.requested


def process_due(*, force=False, use_ai=True):
    """One pass of the runner: at most one pull. Returns a line for the log, or None when nothing was due."""
    ensure_rows()
    now = timezone.now()
    claimed = claim(now)
    if claimed is None:
        return None
    sync, lease, revision = claimed
    fenced = ApiagSync.objects.filter(pk=sync.pk, lease=lease)
    try:
        summary = run(sync, lease, now)
    except WorkerStopping:
        # Stopped between pages (a stop signal or the catalog freeze): the pages applied stay, the cursor does not move and the row
        # looks as it did before the claim, so the same pull starts again as soon as the service is back.
        fenced.update(lease=None, lease_until=None, stage=sync.stage, started_at=sync.started_at, next_run_at=sync.next_run_at, progress={})
        raise
    except SyncFailed as failure:
        permanent = failure.status in PERMANENT
        with no_cut():  # the lease always goes back, even past the stop deadline
            fenced.update(completed=revision, lease=None, lease_until=None, stage='paused' if permanent else 'failed',
                          finished_at=timezone.now(), error=failure.message, error_code=failure.status, failures=F('failures') + 1,
                          next_run_at=None if permanent else timezone.now() + min(interval() * 2 ** min(sync.failures, 6), MAX_BACKOFF),
                          **({'since': ''} if failure.resync else {}))
        return {'apiag_sync': str(sync.connection.account_id), 'error': failure.status}
    except Exception:
        with no_cut():
            fenced.update(completed=revision, lease=None, lease_until=None, stage='failed', finished_at=timezone.now(),
                          error='La sincronización falló de forma inesperada; se intentará de nuevo.', error_code='unexpected',
                          failures=F('failures') + 1, next_run_at=timezone.now() + min(interval() * 2 ** min(sync.failures, 6), MAX_BACKOFF))
        raise
    finished = timezone.now()
    with no_cut():
        fenced.update(completed=revision, lease=None, lease_until=None, stage='completed', finished_at=finished, last_success_at=now,
                      since=str(summary['server_time'])[:40], sucursales_version=str(summary['sucursales_version'])[:40],
                      server_url=sync.connection.server_url, summary=summary, progress={}, error='', error_code='', failures=0,
                      next_run_at=finished + interval(),
                      **({'last_full_at': now, 'last_day_at': now} if summary['mode'] == 'full' else {}),
                      **({'last_day_at': now} if summary['mode'] == 'day' else {}))
    return {'apiag_sync': str(sync.connection.account_id), 'mode': summary['mode'], **summary['counts']}


def request_run(connection, user):
    """«Sincronizar ahora»: ask for one run (asking again while one is waiting changes nothing)."""
    ensure_rows()
    ApiagSync.objects.filter(connection=connection, requested=F('completed')).update(requested=F('requested') + 1, requested_by=user)
