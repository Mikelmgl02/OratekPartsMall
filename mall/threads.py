"""Conversations reached without an order (DealThread): a client asks a supplier before ordering («Preguntar al proveedor» in the
catalog), and the conversation's chat by its own id, for one that has no order yet. Everything else about a conversation (its list, read
marks, pins) is in mall.inbox; an order's chat (mall.deal_views.DealMessages) is the same conversation.

POST accounts/<client>/threads/ {supplier_id}: the conversation of this client account with that supplier, made the first time (a client
account; an active supplier, never the account itself). GET/POST accounts/<account>/threads/<id>/messages/: its chat, as an order's
(order=<id> keeps that order's messages; a message goes about order_id when given, one of its orders, or else in general).
GET accounts/<account>/threads/<id>/orders/: its orders with their current quotation, latest first (the panel's Cotizaciones table)."""
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .deal_views import MessageSerializer, chat_page, chat_post
from .models import Account, DealThread, SupplierRequest
from .request_views import lifecycle_fields, quote_summary_fields, request_summary
from .views import account_for


def thread_participant(user, account_id, thread_id, *, lock=False):
    """(account, conversation) for a member acting for one of its sides, with that side's capability (the deal-access rule)."""
    account = account_for(user, account_id)
    rows = DealThread.objects.select_related('client', 'supplier')
    if lock:
        rows = rows.select_for_update(of=('self',))
    thread = get_object_or_404(rows.filter(Q(client=account) | Q(supplier=account)), pk=thread_id)
    capabilities = set(account.roles.values_list('capability', flat=True))
    if not ((thread.client_id == account.pk and 'client' in capabilities) or (thread.supplier_id == account.pk and 'supplier' in capabilities)):
        raise PermissionDenied('La cuenta no tiene acceso a esta conversación.')
    return account, thread


class Ask(serializers.Serializer):
    supplier_id = serializers.UUIDField()


class Threads(APIView):
    @extend_schema(request=Ask, responses=OpenApiTypes.OBJECT, description='La conversación de esta cuenta de cliente con ese proveedor '
                   '(Preguntar al proveedor): la existente, o una nueva si aún no han tratado. Nunca con la propia cuenta.')
    def post(self, request, account_id):
        client = account_for(request.user, account_id, 'client')
        data = Ask(data=request.data)
        data.is_valid(raise_exception=True)
        supplier = Account.objects.filter(pk=data.validated_data['supplier_id'], active=True, roles__capability='supplier').first()
        if supplier is None:
            raise ValidationError({'supplier_id': ['Ese proveedor no está disponible.']})
        if supplier.pk == client.pk:
            raise ValidationError({'supplier_id': ['Es tu propia cuenta: no puedes preguntarte a ti mismo.']})
        thread = DealThread.between(client.pk, supplier.pk)
        return Response({'thread_id': str(thread.pk), 'account': {'id': str(client.pk), 'name': client.name},
                         'counterpart': {'id': str(supplier.pk), 'name': supplier.name}})


class ThreadMessage(MessageSerializer):
    order_id = serializers.UUIDField(required=False, allow_null=True)  # one of the conversation's orders; absent: in general


class ThreadMessages(APIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, description='El chat de la conversación: los mensajes de todas sus órdenes y los generales '
                   '(order=<id> deja los de esa orden).')
    def get(self, request, account_id, thread_id):
        _, thread = thread_participant(request.user, account_id, thread_id)
        named = request.query_params.get('order')
        solo = get_object_or_404(SupplierRequest, pk=named, thread=thread) if named else None
        return chat_page(request, thread, solo=solo)

    @extend_schema(request=ThreadMessage, responses=OpenApiTypes.OBJECT, description='Un mensaje en la conversación: sobre order_id (una de '
                   'sus órdenes) o en general.')
    @transaction.atomic
    def post(self, request, account_id, thread_id):
        account, thread = thread_participant(request.user, account_id, thread_id, lock=True)
        tag = ThreadMessage(data=request.data)
        tag.is_valid(raise_exception=True)
        order_id = tag.validated_data.get('order_id')
        order = get_object_or_404(SupplierRequest, pk=order_id, thread=thread) if order_id else None
        return chat_post(request, account, thread, order)


class ThreadOrders(APIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, description='Las órdenes de la conversación, la más reciente primero, cada una con su '
                   'cotización vigente (revisión, total y moneda): la tabla de cotizaciones del panel de conversaciones.')
    def get(self, request, account_id, thread_id):
        _, thread = thread_participant(request.user, account_id, thread_id)
        rows = SupplierRequest.objects.filter(thread=thread).annotate(line_count=Count('lines'), unit_count=Coalesce(Sum('lines__quantity'), 0),
                                                                       **quote_summary_fields(), **lifecycle_fields()).order_by('-created_at', 'id')
        return Response({'results': [request_summary(row) for row in rows]})
