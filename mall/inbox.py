"""The person's deal inbox, across every account they belong to: the conversations of their deals with unread counts, and notifications
of what the other party did on them. Read by polling (no websocket); nothing here changes a deal.

A person takes part in a deal through a membership of its client account (with the client capability) or of its supplier account
(with the supplier capability), the rule of deal_views.participant. Unread messages are the ones someone else wrote after the person's
read mark; notifications are the deal events of accounts the person does not belong to."""
import uuid

from django.db import IntegrityError, transaction
from django.db.models import BigIntegerField, Case, Count, F, IntegerField, Max, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Coalesce, Greatest
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from .inbox_models import ConversationRead, InboxCursor
from .models import Account, DealEvent, DealMessage, SupplierRequest

NOTIFICATION_PAGE = 30
SNIPPET = 140
TITLES = {
    'order_sent': '{who} te envió una orden', 'order_appended': '{who} agregó artículos a la orden',
    'review_started': '{who} empezó a revisar tu orden', 'quote': '{who} publicó la cotización v{revision}',
    'request_adjustment': '{who} solicitó un ajuste', 'return_quote': '{who} devolvió la cotización v{revision} para confirmar',
    'accept': '{who} confirmó el acuerdo',
}


def iso(value):
    return value.isoformat() if value else None


def member_accounts(user, capability=None):
    accounts = Account.objects.filter(active=True, memberships__user=user)
    return (accounts.filter(roles__capability=capability) if capability else accounts).values('pk')


def my_deals(user):
    """The deals the person takes part in, with side: 'supplier' when they act for its supplier, 'client' otherwise."""
    suppliers = member_accounts(user, 'supplier')
    return (SupplierRequest.objects.filter(Q(client__in=member_accounts(user, 'client')) | Q(supplier__in=suppliers))
            .annotate(side=Case(When(supplier__in=suppliers, then=Value('supplier')), default=Value('client'))))


def cursor_for(user):
    cursor = InboxCursor.objects.filter(user=user).first()
    if cursor is None:
        try:
            with transaction.atomic():
                cursor = InboxCursor.objects.create(user=user, seen_event_id=DealEvent.objects.aggregate(top=Max('id'))['top'] or 0,
                                                    message_floor=DealMessage.objects.aggregate(top=Max('id'))['top'] or 0)
        except IntegrityError:  # a parallel poll created it first
            cursor = InboxCursor.objects.get(user=user)
    return cursor


def unread(messages, user, cursor):
    """The messages someone else wrote after the person's read mark of their deal (or after the inbox floor, whichever is later)."""
    read = ConversationRead.objects.filter(user=user, order=OuterRef('order_id')).values('last_message_id')
    upto = Greatest(Coalesce(Subquery(read), Value(0), output_field=BigIntegerField()), Value(cursor.message_floor),
                    output_field=BigIntegerField())
    return messages.exclude(actor=user).annotate(read_upto=upto).filter(id__gt=F('read_upto'))


def unread_messages(user, cursor):
    return unread(DealMessage.objects.filter(order__in=my_deals(user).values('pk')), user, cursor)


def with_activity(deals, user, cursor):
    unread_count = (unread(DealMessage.objects.filter(order=OuterRef('pk')), user, cursor).order_by().values('order')
                    .annotate(total=Count('pk')).values('total'))
    latest = DealMessage.objects.filter(order=OuterRef('pk')).order_by('-id')
    return deals.annotate(unread=Coalesce(Subquery(unread_count, output_field=IntegerField()), Value(0)),
                          last_message_id=Subquery(latest.values('id')[:1]),
                          activity=Greatest('updated_at', Coalesce(Subquery(latest.values('created_at')[:1]), 'updated_at')))


def notifications(user):
    return DealEvent.objects.filter(order__in=my_deals(user).values('pk')).exclude(account__in=member_accounts(user))


def own_side(row):
    supplier = row.side == 'supplier'
    return ({'id': str(row.supplier_id), 'name': row.supplier_name} if supplier else {'id': str(row.client_id), 'name': row.client_name},
            {'id': str(row.client_id), 'name': row.client_name} if supplier else {'id': str(row.supplier_id), 'name': row.supplier_name})


def message_preview(message, user):
    if message is None:
        return None
    return {'id': message.pk, 'body': snippet(message.body), 'account_id': str(message.account_id), 'account_name': message.account.name,
            'actor_name': message.actor.get_full_name() or message.actor.username, 'mine': message.actor_id == user.pk,
            'created_at': iso(message.created_at)}


def event_title(event):
    revision = event.quotation.revision if event.quotation_id else ''
    return TITLES.get(event.kind, '{who} actualizó la orden').format(who=event.account.name, revision=revision)


def snippet(body):
    return body if len(body) <= SNIPPET else f'{body[:SNIPPET - 1]}…'


def notification_data(event, deal, cursor):
    mine, counterpart = own_side(deal)
    return {'id': event.pk, 'kind': event.kind, 'title': event_title(event), 'description': event.description, 'order_id': str(deal.pk),
            'reference': deal.reference, 'status': deal.status, 'side': deal.side, 'account': mine,
            'counterpart': counterpart, 'created_at': iso(event.created_at), 'unseen': event.pk > cursor.seen_event_id}


def event_rows(user, events, limit):
    """The first events with their deal, annotated with the person's side (one query for the deals)."""
    events = list(events.select_related('account', 'quotation')[:limit])
    sides = {row.pk: row for row in my_deals(user).filter(pk__in={event.order_id for event in events})}
    return [(event, sides[event.order_id]) for event in events if event.order_id in sides]


class InboxSummary(APIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, description='Lo que la persona tiene pendiente en su bandeja: mensajes sin leer (y en '
                   'cuántas conversaciones), notificaciones sin ver, y el mensaje y la notificación más recientes para avisar de ellos.')
    def get(self, request):
        user, cursor = request.user, cursor_for(request.user)
        unread = unread_messages(user, cursor)
        totals = unread.aggregate(messages=Count('pk'), conversations=Count('order', distinct=True))
        latest_message = unread.select_related('account', 'actor', 'order').order_by('-id').first()
        unseen = notifications(user).filter(id__gt=cursor.seen_event_id)
        latest_event = event_rows(user, unseen.order_by('-id'), 1)
        message = None
        if latest_message:
            row = my_deals(user).get(pk=latest_message.order_id)
            mine, counterpart = own_side(row)
            message = {**message_preview(latest_message, user), 'order_id': str(row.pk), 'reference': row.reference,
                       'account': mine, 'counterpart': counterpart, 'side': row.side}
        return Response({'unread_messages': totals['messages'], 'unread_conversations': totals['conversations'],
                         'unseen_notifications': unseen.count(), 'latest_message': message,
                         'latest_notification': notification_data(*latest_event[0], cursor) if latest_event else None})


class InboxConversations(generics.GenericAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, description='Los acuerdos de la persona, del más reciente al más antiguo (mensaje o '
                   'cambio de estado), con su lado, la otra parte, el último mensaje y cuántos mensajes no ha leído. search filtra por '
                   'referencia o nombre de cuenta; unread=1 deja solo los que tienen mensajes sin leer; order=<id> deja ese acuerdo.')
    def get(self, request):
        user, cursor = request.user, cursor_for(request.user)
        rows = with_activity(my_deals(user), user, cursor)
        search = request.query_params.get('search', '').strip()
        if search:
            rows = rows.filter(Q(reference__icontains=search) | Q(client_name__icontains=search) | Q(supplier_name__icontains=search))
        if request.query_params.get('unread') == '1':
            rows = rows.filter(unread__gt=0)
        order = request.query_params.get('order', '')
        if order:  # a push notification's link names its deal
            try:
                rows = rows.filter(pk=uuid.UUID(order))
            except ValueError:
                rows = rows.none()
        page = self.paginate_queryset(rows.order_by('-activity', '-pk'))
        messages = DealMessage.objects.select_related('account', 'actor').in_bulk([row.last_message_id for row in page if row.last_message_id])
        results = []
        for row in page:
            mine, counterpart = own_side(row)
            results.append({'id': str(row.pk), 'reference': row.reference, 'status': row.status, 'side': row.side, 'account': mine,
                            'counterpart': counterpart, 'unread': row.unread, 'activity': iso(row.activity),
                            'last_message': message_preview(messages.get(row.last_message_id), user)})
        return self.get_paginated_response(results)


class ReadMark(serializers.Serializer):
    last_message_id = serializers.IntegerField(min_value=0)


class InboxConversationRead(APIView):
    @extend_schema(request=ReadMark, responses=OpenApiTypes.OBJECT, description='Marca la conversación como leída hasta ese mensaje. '
                   'La marca solo avanza; un número mayor que el último mensaje del acuerdo se limita a él.')
    def post(self, request, pk):
        row = generics.get_object_or_404(my_deals(request.user), pk=pk)
        data = ReadMark(data=request.data)
        data.is_valid(raise_exception=True)
        upto = min(data.validated_data['last_message_id'], row.messages.aggregate(top=Max('id'))['top'] or 0)
        mark, _ = ConversationRead.objects.get_or_create(user=request.user, order=row)
        ConversationRead.objects.filter(pk=mark.pk).update(last_message_id=Greatest('last_message_id', Value(upto)))
        mark.refresh_from_db()
        return Response({'order_id': str(row.pk), 'last_message_id': mark.last_message_id})


class InboxNotifications(APIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, description='Lo que la otra parte hizo en los acuerdos de la persona, del más reciente '
                   f'al más antiguo, de {NOTIFICATION_PAGE} en {NOTIFICATION_PAGE} (before=<id> para seguir). unseen marca las que aún no vio.')
    def get(self, request):
        cursor = cursor_for(request.user)
        events = notifications(request.user).order_by('-id')
        before = request.query_params.get('before', '')
        if before.isdigit():
            events = events.filter(id__lt=int(before))
        page = event_rows(request.user, events, NOTIFICATION_PAGE + 1)
        return Response({'results': [notification_data(event, deal, cursor) for event, deal in page[:NOTIFICATION_PAGE]],
                         'has_more': len(page) > NOTIFICATION_PAGE})


class SeenMark(serializers.Serializer):
    last_event_id = serializers.IntegerField(min_value=0)


class InboxNotificationsSeen(APIView):
    @extend_schema(request=SeenMark, responses=OpenApiTypes.OBJECT, description='Marca como vistas las notificaciones hasta ese evento. '
                   'La marca solo avanza.')
    def post(self, request):
        data = SeenMark(data=request.data)
        data.is_valid(raise_exception=True)
        cursor = cursor_for(request.user)
        upto = min(data.validated_data['last_event_id'], DealEvent.objects.aggregate(top=Max('id'))['top'] or 0)
        InboxCursor.objects.filter(pk=cursor.pk).update(seen_event_id=Greatest('seen_event_id', Value(upto)))
        cursor.refresh_from_db()
        return Response({'seen_event_id': cursor.seen_event_id})
