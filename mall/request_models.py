"""Supplier orders, immutable quotation revisions and private deal conversations."""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from .media_storage import chat_attachment_path, chat_storage

# An order's statuses by stage (mall.deal_lifecycle). They live here, not in deal_lifecycle, so the model's constraints and the request
# views can use them without an import cycle.
PRE_HANDSHAKE = ('pending', 'reviewed', 'quoted', 'adjustment')
GOODS_HELD = ('handshaked', 'processing')  # still with the supplier: a shortfall or a party's cancellation is possible
ACTIVE_AGREEMENT = ('handshaked', 'processing', 'in_transit')  # confirmed, before the supplier's Entregado
OPEN_AGREEMENT = ('handshaked', 'processing', 'in_transit', 'delivered')  # confirmed and not closed: delivered waits for the client
CLOSED_STATUSES = ('received', 'cancelled')


class ClientRequestSubmission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='request_submissions')
    submission_id = models.UUIDField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    payload_hash = models.CharField(max_length=64)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    result = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['client', 'submission_id'], name='unique_client_request_submission')]


class DealThread(models.Model):
    """The conversation between a client account and a supplier account: one per pair (who buys from whom), with every order between
    them. Its messages are one linear chat, each tagged with the order it is about when there is one (DealMessage.order); read marks, pins
    and groups are per thread (ConversationRead)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='client_threads')
    supplier = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='supplier_threads')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['client', 'supplier'], name='unique_deal_thread')]

    @classmethod
    def between(cls, client_id, supplier_id):
        """The pair's thread, made the first time they deal."""
        from django.db import IntegrityError, transaction
        try:
            with transaction.atomic():
                return cls.objects.get_or_create(client_id=client_id, supplier_id=supplier_id)[0]
        except IntegrityError:  # made at the same moment by another request
            return cls.objects.get(client_id=client_id, supplier_id=supplier_id)


class SupplierRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(ClientRequestSubmission, on_delete=models.PROTECT, related_name='requests')
    client = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='sent_supplier_requests')
    supplier = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='received_supplier_requests')
    thread = models.ForeignKey(DealThread, on_delete=models.PROTECT, related_name='orders')  # the pair's conversation (set on save)
    client_name = models.CharField(max_length=200)
    supplier_name = models.CharField(max_length=200)
    reference = models.CharField(max_length=32, unique=True)
    # In lifecycle order. After the handshake the supplier marks Procesando, En camino (skipped on a counter pickup) and Entregado; only
    # the client's Recibido closes the order.
    STATUS_CHOICES = [('pending', 'Enviada · por revisar'), ('reviewed', 'En revisión'),
                      ('quoted', 'Cotizada · por confirmar'), ('adjustment', 'Ajuste solicitado'),
                      ('handshaked', 'Acuerdo confirmado'), ('processing', 'Procesando'), ('in_transit', 'En camino'),
                      ('delivered', 'Entregado · por recibir'), ('received', 'Recibido'), ('cancelled', 'Cancelado')]
    # Who cancelled a confirmed deal and why (mall.deal_lifecycle). Only supplier cancellations count against its reliability.
    CANCEL_SIDES = [('client', 'Cliente'), ('supplier', 'Proveedor'), ('admin', 'MotionPartes')]
    CANCEL_REASONS = [('no_stock', 'Sin existencias'), ('price_error', 'Error de precio'), ('cannot_deliver', 'No puede entregar'),
                      ('not_needed', 'Ya no lo necesita'), ('found_elsewhere', 'Lo consiguió con otro proveedor'), ('delay', 'Demora en la entrega'),
                      ('shortfall', 'No acepta el faltante'), ('dispute', 'Disputa entre las partes'), ('price_too_high', 'El precio es muy alto'),
                      ('other', 'Otro motivo')]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    version = models.PositiveIntegerField(default=1)
    handshaked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name='reviewed_supplier_requests')
    processing_at = models.DateTimeField(null=True, blank=True)
    processing_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    in_transit_at = models.DateTimeField(null=True, blank=True)
    in_transit_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    delivered_at = models.DateTimeField(null=True, blank=True)  # the supplier's Entregado (kept once the client confirms)
    delivered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    # The client's Recibido. Deliveries from before it closed as received with the supplier's date and nobody (received_by NULL).
    received_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    cancelled_side = models.CharField(max_length=10, choices=CANCEL_SIDES, blank=True, default='')
    cancel_reason = models.CharField(max_length=20, choices=CANCEL_REASONS, blank=True, default='')
    cancel_note = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-created_at', 'id']
        constraints = [models.UniqueConstraint(fields=['submission', 'supplier'], name='unique_submission_supplier_request'),
                       models.CheckConstraint(condition=Q(status__in=['pending', 'reviewed', 'quoted', 'adjustment', 'handshaked', 'processing',
                                                                      'in_transit', 'delivered', 'received', 'cancelled']),
                                              name='valid_supplier_request_status'),
                       # Each step leaves its date from its status on. A received order may lack processing_at (deliveries from before
                       # Procesando existed), and a cancellation keeps the dates it reached; only MotionPartes (admin) cancels after
                       # dispatch, so only its cancellation may keep the dispatch dates (En camino, Entregado).
                       models.CheckConstraint(condition=Q(status__in=['processing', 'in_transit', 'delivered'], processing_at__isnull=False)
                                              | Q(status__in=['received', 'cancelled'])
                                              | Q(status__in=[*PRE_HANDSHAKE, 'handshaked'], processing_at__isnull=True),
                                              name='supplier_request_processing_fields'),
                       models.CheckConstraint(condition=Q(status='in_transit', in_transit_at__isnull=False) | Q(status__in=['delivered', 'received'])
                                              | Q(status='cancelled', cancelled_side='admin') | Q(status='cancelled', in_transit_at__isnull=True)
                                              | Q(status__in=[*PRE_HANDSHAKE, 'handshaked', 'processing'], in_transit_at__isnull=True),
                                              name='supplier_request_transit_fields'),
                       models.CheckConstraint(condition=Q(status__in=['delivered', 'received'], delivered_at__isnull=False)
                                              | Q(status='cancelled', cancelled_side='admin') | Q(status='cancelled', delivered_at__isnull=True)
                                              | Q(status__in=[*PRE_HANDSHAKE, 'handshaked', 'processing', 'in_transit'], delivered_at__isnull=True),
                                              name='supplier_request_delivery_fields'),
                       # The client confirms only what the supplier marked delivered, and not before it did.
                       models.CheckConstraint(condition=Q(status='received', received_at__isnull=False, delivered_at__isnull=False,
                                                          received_at__gte=F('delivered_at'))
                                              | (~Q(status='received') & Q(received_at__isnull=True)),
                                              name='supplier_request_received_fields'),
                       # A cancellation leaves its date, side and reason on exactly that status.
                       models.CheckConstraint(condition=Q(status='cancelled', cancelled_at__isnull=False, cancelled_side__in=['client', 'supplier', 'admin'])
                                              & ~Q(cancel_reason='') | (~Q(status='cancelled') & Q(cancelled_at__isnull=True, cancelled_side='', cancel_reason='')),
                                              name='supplier_request_cancel_fields')]
        # The inbox poll's Recibido reminder (every 15 s): a client's orders waiting for its confirmation.
        indexes = [models.Index(fields=['client', '-delivered_at'], condition=Q(status='delivered'), name='sreq_awaiting_receipt')]

    def save(self, *args, **kwargs):
        if self.thread_id is None:  # every order belongs to its pair's conversation
            self.thread = DealThread.between(self.client_id, self.supplier_id)
        super().save(*args, **kwargs)


STATUS_ORDER = [value for value, _ in SupplierRequest.STATUS_CHOICES]


class SupplierRequestLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(SupplierRequest, on_delete=models.CASCADE, related_name='lines')
    supplier_item = models.ForeignKey('mall.SupplierItem', on_delete=models.PROTECT, related_name='request_lines')
    part = models.ForeignKey('mall.Part', on_delete=models.PROTECT, related_name='request_lines')
    supplier_invent_id = models.CharField(max_length=120)
    sku = models.CharField(max_length=200)
    name = models.CharField(max_length=200, blank=True)
    codigo = models.CharField(max_length=120)
    brand = models.CharField(max_length=120, blank=True)
    description = models.TextField(blank=True)
    quantity = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ['sku', 'codigo', 'id']
        constraints = [models.UniqueConstraint(fields=['request', 'supplier_item'], name='unique_supplier_request_item'),
                       models.CheckConstraint(condition=Q(quantity__gte=1, quantity__lte=9999), name='valid_supplier_request_quantity')]


class RequestContribution(models.Model):
    """The exact cart addition remains attributable to its original submission."""
    submission = models.ForeignKey(ClientRequestSubmission, on_delete=models.PROTECT, related_name='contributions')
    order = models.ForeignKey(SupplierRequest, on_delete=models.PROTECT, related_name='contributions')
    lines = models.JSONField(default=list)
    appended = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['submission', 'order'], name='unique_submission_order_contribution')]


class DealQuotation(models.Model):
    """A published revision is never edited; changes create a new revision."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(SupplierRequest, on_delete=models.PROTECT, related_name='quotations')
    revision = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default='USD')
    terms = models.TextField(blank=True)
    total = models.DecimalField(max_digits=16, decimal_places=2)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='published_deal_quotes')
    supplier_confirmed_at = models.DateTimeField()
    client_confirmed_at = models.DateTimeField(null=True, blank=True)
    client_confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                           related_name='confirmed_deal_quotes')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['revision']
        constraints = [models.UniqueConstraint(fields=['order', 'revision'], name='unique_deal_quote_revision'),
                       models.CheckConstraint(condition=Q(total__gte=0), name='positive_deal_quote_total')]


class DealQuotationLine(models.Model):
    quotation = models.ForeignKey(DealQuotation, on_delete=models.PROTECT, related_name='lines')
    order_line = models.ForeignKey(SupplierRequestLine, on_delete=models.PROTECT, related_name='quotation_lines')
    quantity = models.PositiveSmallIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ['order_line__sku', 'order_line__codigo', 'id']
        constraints = [models.UniqueConstraint(fields=['quotation', 'order_line'], name='unique_deal_quote_line'),
                       models.CheckConstraint(condition=Q(quantity__gte=0, quantity__lte=9999, unit_price__gte=0),
                                              name='valid_deal_quote_line')]


class StockReservation(models.Model):
    """Units a quotation line holds on a supplier item (mall.stock_reservations): they leave the item's available stock when the supplier
    sends the quotation, so its other quotations see what is left. Until the client confirms, the hold lasts until `expires_at` (the
    supplier's quote_hold_days) or the next version, which replaces it; once confirmed it has no expiry and stays held until the supplier's
    own count includes the sale, a stock upload the supplier marks as already discounting it. Items kept by the apiag-cloud pull settle at the
    supplier's Entregado instead, never at the client's Recibido, which moves no stock. A reported shortfall takes the missing units out of
    `quantity` (the units still held); a rejection or cancellation releases what is left."""
    STATUSES = [('open', 'Reservada'), ('settled', 'Descontada por el proveedor'), ('released', 'Liberada')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey('mall.SupplierItem', on_delete=models.PROTECT, related_name='reservations')
    order = models.ForeignKey(SupplierRequest, on_delete=models.PROTECT, related_name='reservations')
    # A line holds once at a time: a hold that lapsed stays released, and the client's later confirmation reserves the line anew.
    quotation_line = models.ForeignKey(DealQuotationLine, on_delete=models.PROTECT, related_name='reservations')
    quantity = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=STATUSES, default='open', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    settled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    settled_reference = models.CharField(max_length=120, blank=True, default='')
    released_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)  # a quotation's hold, until the client confirms

    class Meta:
        ordering = ['created_at', 'id']
        constraints = [models.CheckConstraint(condition=~Q(status='open') | Q(quantity__gte=1), name='stock_reservation_open_holds_units'),
                       models.CheckConstraint(condition=Q(status__in=['open', 'settled', 'released']), name='stock_reservation_valid_status'),
                       models.UniqueConstraint(fields=['quotation_line'], condition=Q(status='open'), name='stock_reservation_one_open_per_line')]


class DealShortfall(models.Model):
    """Units the supplier could not find after the handshake (mall.deal_lifecycle), reported only while it still has the goods (Acuerdo
    confirmado or Procesando). They leave the supplier's count and the deal's reservation at once, so they never come back as available;
    the agreement itself changes only when the client accepts. Declined means the client cancelled the deal instead."""
    STATUSES = [('pending', 'Esperando al cliente'), ('accepted', 'Aceptado por el cliente'), ('declined', 'El cliente canceló el acuerdo')]
    REASONS = [('no_stock', 'Sin existencias'), ('damaged', 'Dañado'), ('other', 'Otro motivo')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(SupplierRequest, on_delete=models.PROTECT, related_name='shortfalls')
    quotation = models.ForeignKey(DealQuotation, on_delete=models.PROTECT, related_name='shortfalls')
    reason = models.CharField(max_length=10, choices=REASONS)
    note = models.TextField(blank=True, default='')
    status = models.CharField(max_length=10, choices=STATUSES, default='pending', db_index=True)
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    reported_at = models.DateTimeField(auto_now_add=True)
    responded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['reported_at', 'id']
        constraints = [models.CheckConstraint(condition=Q(status__in=['pending', 'accepted', 'declined']), name='deal_shortfall_valid_status'),
                       models.CheckConstraint(condition=Q(reason__in=['no_stock', 'damaged', 'other']), name='deal_shortfall_valid_reason'),
                       models.UniqueConstraint(fields=['order'], condition=Q(status='pending'), name='one_pending_shortfall_per_deal')]


class DealShortfallLine(models.Model):
    shortfall = models.ForeignKey(DealShortfall, on_delete=models.PROTECT, related_name='lines')
    quotation_line = models.ForeignKey(DealQuotationLine, on_delete=models.PROTECT, related_name='+')
    agreed = models.PositiveIntegerField(help_text='Unidades del acuerdo antes de este faltante.')
    missing = models.PositiveIntegerField()

    class Meta:
        ordering = ['id']
        constraints = [models.CheckConstraint(condition=Q(missing__gte=1, missing__lte=models.F('agreed')), name='deal_shortfall_line_missing'),
                       models.UniqueConstraint(fields=['shortfall', 'quotation_line'], name='unique_deal_shortfall_line')]


class DealEvent(models.Model):
    order = models.ForeignKey(SupplierRequest, on_delete=models.PROTECT, related_name='events')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    # Null for an Oratek administrator's decision (a dispute cancellation), which acts for no account.
    account = models.ForeignKey('mall.Account', on_delete=models.PROTECT, null=True, blank=True)
    kind = models.CharField(max_length=40)
    description = models.TextField(blank=True)
    quotation = models.ForeignKey(DealQuotation, on_delete=models.PROTECT, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']


class DealMessage(models.Model):
    """A message in the conversation between a client and a supplier (DealThread), tagged with the order it is about when there is one:
    written from that order, answering one of its messages, or by WhatsApp while that order was the topic. Untagged ones are about the
    relationship (a question before any order, a greeting)."""
    id = models.BigAutoField(primary_key=True)
    thread = models.ForeignKey(DealThread, on_delete=models.PROTECT, related_name='messages')
    order = models.ForeignKey(SupplierRequest, on_delete=models.PROTECT, null=True, blank=True, related_name='messages')
    account = models.ForeignKey('mall.Account', on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    message_id = models.UUIDField()
    body = models.TextField(blank=True)  # empty only when the message carries files
    # Written in MotionPartes, or a WhatsApp reply the bridge brought into the chat (mall.whatsapp_bridge).
    channel = models.CharField(max_length=10, choices=[('app', 'MotionPartes'), ('whatsapp', 'WhatsApp')], default='app')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        constraints = [models.UniqueConstraint(fields=['thread', 'account', 'message_id'], name='unique_deal_message_send')]

    def save(self, *args, **kwargs):
        if self.thread_id is None and self.order_id is not None:  # written about an order: in its pair's conversation
            self.thread_id = SupplierRequest.objects.values_list('thread_id', flat=True).get(pk=self.order_id)
        super().save(*args, **kwargs)


class DealAttachment(models.Model):
    """A file in a deal's chat (mall.deal_attachments): sent from MotionPartes, or received by WhatsApp (a voice note with its transcript).
    Private: only the deal's parties get it, through the API."""
    KINDS = [('image', 'Foto'), ('document', 'Documento'), ('audio', 'Audio'), ('video', 'Video')]
    message = models.ForeignKey(DealMessage, on_delete=models.CASCADE, related_name='attachments')
    token = models.UUIDField(default=uuid.uuid4, editable=False)  # its folder in the storage, so names never collide
    file = models.FileField(storage=chat_storage, upload_to=chat_attachment_path, max_length=500, blank=True)
    kind = models.CharField(max_length=10, choices=KINDS)
    name = models.CharField(max_length=200)
    content_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField(default=0)
    voice = models.BooleanField(default=False)  # a WhatsApp voice note, as opposed to an audio file
    # Received but not stored (Meta refused the download, or the file was too large): the chat says it could not be received.
    failed = models.BooleanField(default=False)
    # Meta's id of a file received by WhatsApp, to ask for it again (Reintentar) when it could not be received.
    whatsapp_media_id = models.CharField(max_length=64, blank=True, default='')
    transcript = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']


class DealCommand(models.Model):
    """Durable retry keys for quotation decisions, including lost responses."""
    order = models.ForeignKey(SupplierRequest, on_delete=models.PROTECT, related_name='commands')
    account = models.ForeignKey('mall.Account', on_delete=models.PROTECT)
    operation_id = models.UUIDField()
    payload_hash = models.CharField(max_length=64)
    result = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['order', 'account', 'operation_id'], name='unique_deal_command')]
