"""The person's deal inbox, across every account they belong to: the conversations of their deals with unread counts, and notifications
of what the other party did on them. Read by polling, nudged by the websocket (realtime.py); nothing here changes a deal.

A person takes part in a deal through a membership of its client account (with the client capability) or of its supplier account
(with the supplier capability), the rule of deal_views.participant. Unread messages are the ones someone else wrote after the person's
read mark; notifications are the deal events of accounts the person does not belong to. The person organizes their conversations list
by pinning conversations (listed first, under Fijadas) and keeping others in groups they make; the rest list as Sin grupo."""
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import BigIntegerField, Case, Count, F, IntegerField, Max, OuterRef, Q, Subquery, UUIDField, Value, When
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .inbox_models import ConversationRead, InboxCursor, InboxGroup
from .models import Account, DealEvent, DealMessage, SupplierRequest

NOTIFICATION_PAGE = 30
GROUP_LIMIT = 30
ACTIVITY_WRITE = timedelta(minutes=1)
SNIPPET = 140
TITLES = {
    'order_sent': '{who} te envió una orden', 'order_appended': '{who} agregó artículos a la orden',
    'review_started': '{who} empezó a revisar tu orden', 'quote': '{who} publicó la cotización v{revision}',
    'request_adjustment': '{who} solicitó un ajuste', 'return_quote': '{who} devolvió la cotización v{revision} para confirmar',
    'accept': '{who} confirmó el acuerdo', 'deliver': '{who} marcó la orden como entregada', 'report_shortfall': '{who} reportó un faltante',
    'accept_shortfall': '{who} aceptó el faltante', 'cancel': '{who} canceló el acuerdo', 'reject': '{who} rechazó la orden',
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


def placed(deals, user):
    """Each deal with where the person keeps it in their list: pinned, in one of their groups (with its position), or neither."""
    place = ConversationRead.objects.filter(user=user, order=OuterRef('pk'))
    return deals.annotate(list_pinned=Subquery(place.values('pinned_at')[:1]), list_group=Subquery(place.values('group_id')[:1]),
                          list_position=Subquery(place.values('group__position')[:1]))


def list_order(rows):
    """Fijadas first, then each group in the person's order, then Sin grupo; the most recent activity first within each."""
    loose = Q(list_pinned__isnull=True)
    section = Case(When(list_pinned__isnull=False, then=Value(0)), When(list_group__isnull=False, then=Value(1)), default=Value(2))
    return rows.order_by(section, Case(When(loose, then=F('list_position')), default=Value(None), output_field=IntegerField()),
                         Case(When(loose, then=F('list_group')), default=Value(None), output_field=UUIDField()), '-activity', '-pk')


def groups_of(user):
    """The person's groups in order, each with how many of their conversations it lists (pinned ones list under Fijadas instead)."""
    counts = (ConversationRead.objects.filter(group=OuterRef('pk'), pinned_at__isnull=True, order__in=my_deals(user).values('pk'))
              .order_by().values('group').annotate(total=Count('pk')).values('total'))
    groups = InboxGroup.objects.filter(user=user).annotate(total=Coalesce(Subquery(counts, output_field=IntegerField()), Value(0)))
    return [group_data(group, group.total) for group in groups]


def group_data(group, count=0):
    return {'id': str(group.pk), 'name': group.name, 'count': count}


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
    # An administrator's decision (a dispute cancellation) acts for no account.
    who = event.account.name if event.account else 'MotionPartes'
    return TITLES.get(event.kind, '{who} actualizó la orden').format(who=who, revision=revision)


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
        now = timezone.now()
        if not cursor.active_at or now - cursor.active_at > ACTIVITY_WRITE:  # someone using the app gets no email about a message
            InboxCursor.objects.filter(pk=cursor.pk).update(active_at=now)
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
                   'cambio de estado), con su lado, la otra parte, el último mensaje y cuántos mensajes no ha leído; primero los fijados, '
                   'luego los de cada grupo de la persona y al final los demás (groups lista sus grupos). search filtra por referencia o '
                   'nombre de cuenta; unread=1 deja solo los que tienen mensajes sin leer; order=<id> deja ese acuerdo.')
    def get(self, request):
        user, cursor = request.user, cursor_for(request.user)
        rows = placed(with_activity(my_deals(user), user, cursor), user)
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
        page = self.paginate_queryset(list_order(rows))
        messages = DealMessage.objects.select_related('account', 'actor').in_bulk([row.last_message_id for row in page if row.last_message_id])
        results = []
        for row in page:
            mine, counterpart = own_side(row)
            results.append({'id': str(row.pk), 'reference': row.reference, 'status': row.status, 'side': row.side, 'account': mine,
                            'counterpart': counterpart, 'unread': row.unread, 'activity': iso(row.activity),
                            'last_message': message_preview(messages.get(row.last_message_id), user),
                            'pinned': row.list_pinned is not None, 'group': str(row.list_group) if row.list_group else None})
        response = self.get_paginated_response(results)
        response.data['groups'] = groups_of(user)
        return response


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
        from .realtime import publish_later  # the person's other tabs and devices update their badges
        publish_later({request.user.pk}, 'read', row.pk)
        return Response({'order_id': str(row.pk), 'last_message_id': mark.last_message_id})


class Placement(serializers.Serializer):
    pinned = serializers.BooleanField(required=False)
    group = serializers.UUIDField(required=False, allow_null=True)


class InboxConversationPlace(APIView):
    @extend_schema(request=Placement, responses=OpenApiTypes.OBJECT, description='Dónde guarda la persona la conversación en su lista: '
                   'pinned la fija arriba (o la devuelve a su grupo); group la pone en uno de sus grupos (null la deja sin grupo).')
    def post(self, request, pk):
        row = generics.get_object_or_404(my_deals(request.user), pk=pk)
        data = Placement(data=request.data)
        data.is_valid(raise_exception=True)
        changes = {}
        if 'pinned' in data.validated_data:
            changes['pinned_at'] = timezone.now() if data.validated_data['pinned'] else None
        if 'group' in data.validated_data:
            group = data.validated_data['group']
            changes['group'] = generics.get_object_or_404(InboxGroup, user=request.user, pk=group) if group else None
        if not changes:
            raise ValidationError({'detail': 'Indica si la conversación va fijada (pinned) o en qué grupo (group).'})
        mark, _ = ConversationRead.objects.get_or_create(user=request.user, order=row)
        for field, value in changes.items():
            setattr(mark, field, value)
        mark.save(update_fields=[*changes, 'updated_at'])  # never the read mark, which another request may be moving
        from .realtime import publish_later  # the person's other tabs and devices list it there too
        publish_later({request.user.pk}, 'read', row.pk)
        return Response({'order_id': str(row.pk), 'pinned': mark.pinned_at is not None, 'group': str(mark.group_id) if mark.group_id else None})


class GroupName(serializers.Serializer):
    name = serializers.CharField(max_length=60)


def group_name(request, group=None):
    data = GroupName(data=request.data)
    data.is_valid(raise_exception=True)
    name = data.validated_data['name']
    taken = InboxGroup.objects.filter(user=request.user, name__iexact=name)
    if (taken.exclude(pk=group.pk) if group else taken).exists():
        raise ValidationError({'name': ['Ya tienes un grupo con ese nombre.']})
    return name


def saved_group(request, group):
    try:
        with transaction.atomic():
            group.save()
    except IntegrityError:  # the same name, saved by a parallel request
        raise ValidationError({'name': ['Ya tienes un grupo con ese nombre.']})
    from .realtime import publish_later
    publish_later({request.user.pk}, 'read')


class InboxGroups(APIView):
    @extend_schema(request=GroupName, responses=OpenApiTypes.OBJECT, description='Crea un grupo en la lista de conversaciones de la '
                   f'persona, después de los que ya tiene (hasta {GROUP_LIMIT}; el nombre no se repite).')
    def post(self, request):
        groups = InboxGroup.objects.filter(user=request.user)
        if groups.count() >= GROUP_LIMIT:
            raise ValidationError({'name': [f'Puedes tener hasta {GROUP_LIMIT} grupos.']})
        group = InboxGroup(user=request.user, name=group_name(request), position=(groups.aggregate(top=Max('position'))['top'] or 0) + 1)
        saved_group(request, group)
        return Response(group_data(group), status=201)


class InboxGroupDetail(APIView):
    @extend_schema(operation_id='v1_inbox_groups_rename', request=GroupName, responses=OpenApiTypes.OBJECT, description='Cambia el nombre del grupo.')
    def post(self, request, pk):
        group = generics.get_object_or_404(InboxGroup, user=request.user, pk=pk)
        group.name = group_name(request, group)
        saved_group(request, group)
        return Response(group_data(group, group.conversations.filter(pinned_at__isnull=True, order__in=my_deals(request.user).values('pk')).count()))

    @extend_schema(responses={204: None}, description='Elimina el grupo; sus conversaciones quedan sin grupo.')
    def delete(self, request, pk):
        generics.get_object_or_404(InboxGroup, user=request.user, pk=pk).delete()
        from .realtime import publish_later
        publish_later({request.user.pk}, 'read')
        return Response(status=204)


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
        from .realtime import publish_later
        publish_later({request.user.pk}, 'read')
        return Response({'seen_event_id': cursor.seen_event_id})
