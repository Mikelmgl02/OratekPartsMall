"""Acuerdos, for Oratek superusers: confirmed deals with their lifecycle (Procesando, En camino, Entregado, Recibido, shortfalls,
cancellations) to settle disputes, and the administrator's cancellation (side 'admin', which never counts against the supplier) of any
agreement the client has not received yet, also after dispatch. Never a price, total, currency or term (decision D5):
the lines show units only, and the timeline leaves out the parties' own words (adjustment requests and returns) and the conversation.
"""
from django.db import transaction
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .deal_lifecycle import CANCEL_REASONS, SHORTFALL_REASONS, agreement, cancel
from .management import IsSuperuser
from .request_models import DealEvent, DealShortfall, SupplierRequest, SupplierRequestLine
from .request_views import RequestFilters, lifecycle_fields, status_filter

# Event kinds whose description is written by the system; the others carry the parties' own words and show only their label.
SYSTEM_EVENTS = {'review_started', 'quote', 'accept', 'process', 'ship', 'deliver', 'receive', 'report_shortfall', 'accept_shortfall', 'cancel'}
EVENT_LABELS = {'request_adjustment': 'EL CLIENTE PIDIÓ UN AJUSTE.', 'return_quote': 'EL PROVEEDOR DEVOLVIÓ LA COTIZACIÓN.'}


def deal_queryset():
    # Correlated subqueries: joining lines and shortfalls in one aggregate would repeat each line once per shortfall.
    lines = SupplierRequestLine.objects.filter(request_id=OuterRef('pk')).order_by().values('request_id')
    shortfalls = DealShortfall.objects.filter(order_id=OuterRef('pk')).order_by().values('order_id')
    return SupplierRequest.objects.select_related('client', 'supplier').annotate(
        line_count=Coalesce(Subquery(lines.annotate(n=Count('pk')).values('n')[:1], output_field=IntegerField()), 0),
        unit_count=Coalesce(Subquery(lines.annotate(n=Sum('quantity')).values('n')[:1], output_field=IntegerField()), 0),
        shortfall_count=Coalesce(Subquery(shortfalls.annotate(n=Count('pk')).values('n')[:1], output_field=IntegerField()), 0),
        **lifecycle_fields()).order_by('-created_at', 'id')


def iso(value):
    return value.isoformat() if value else None


def summary(row):
    return {'id': str(row.pk), 'reference': row.reference, 'status': row.status, 'version': row.version,
            'client': {'id': str(row.client_id), 'name': row.client_name}, 'supplier': {'id': str(row.supplier_id), 'name': row.supplier_name},
            'created_at': iso(row.created_at), 'handshaked_at': iso(row.handshaked_at), 'processing_at': iso(row.processing_at),
            'in_transit_at': iso(row.in_transit_at), 'delivered_at': iso(row.delivered_at), 'received_at': iso(row.received_at),
            'cancelled_at': iso(row.cancelled_at), 'cancelled_side': row.cancelled_side or None,
            'cancel_reason_label': CANCEL_REASONS.get(row.cancel_reason) if row.cancel_reason else None,
            'line_count': row.line_count, 'unit_count': row.unit_count, 'shortfall_count': row.shortfall_count,
            'shortfall_pending': bool(row.shortfall_pending)}


def detail(row):
    current = agreement(row) if row.handshaked_at else None
    final = {entry['line'].order_line_id: entry for entry in current['lines']} if current else {}
    events = row.events.select_related('account').order_by('id')
    return {**summary(row), 'cancel_note': row.cancel_note,
            'lines': [{'id': str(line.pk), 'codigo': line.codigo, 'sku': line.sku, 'description': line.description, 'requested': line.quantity,
                       **({key: final[line.pk][key] for key in ('agreed', 'missing', 'final')} | {'pending_missing': final[line.pk]['pending']}
                          if line.pk in final else {'agreed': None, 'missing': 0, 'final': None, 'pending_missing': 0})}
                      for line in row.lines.order_by('codigo', 'id')],
            'shortfalls': [{'id': str(item.pk), 'status': item.status, 'reason_label': SHORTFALL_REASONS[item.reason], 'note': item.note,
                            'reported_at': iso(item.reported_at), 'responded_at': iso(item.responded_at),
                            'lines': [{'codigo': line.quotation_line.order_line.codigo, 'agreed': line.agreed, 'missing': line.missing}
                                      for line in item.lines.select_related('quotation_line__order_line')]}
                           for item in row.shortfalls.all()],
            'events': [{'id': event.pk, 'kind': event.kind, 'created_at': iso(event.created_at),
                        'account_name': event.account.name if event.account else 'MOTIONPARTES',
                        'description': event.description if event.kind in SYSTEM_EVENTS else EVENT_LABELS.get(event.kind, '')}
                       for event in events]}


class AdminCancelSerializer(serializers.Serializer):
    expected_version = serializers.IntegerField(min_value=1)
    reason_code = serializers.ChoiceField(choices=list(CANCEL_REASONS))
    note = serializers.CharField(required=False, default='', allow_blank=True, max_length=2000)

    def validate_note(self, value):
        return value.strip().upper()


class DealAdminList(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_deals_list', responses=OpenApiTypes.OBJECT,
                   parameters=[OpenApiParameter('status', OpenApiTypes.STR, description='Un estado, o un grupo: agreements (todos los acuerdos '
                                                'confirmados), active_agreements (confirmados, sin el Entregado del proveedor) u '
                                                'open_agreements (confirmados y sin cerrar, también los que esperan el recibido del cliente).'),
                               OpenApiParameter('search', OpenApiTypes.STR, description='Referencia, cliente o proveedor.'),
                               OpenApiParameter('page', OpenApiTypes.INT)],
                   description='Acuerdos de todas las cuentas para resolver disputas: estados, fechas y unidades, sin precios.')
    def get(self, request):
        filters = RequestFilters(data=request.query_params)
        filters.is_valid(raise_exception=True)
        rows = status_filter(deal_queryset(), filters.validated_data.get('status'))
        term = filters.validated_data.get('search', '').strip()
        if term:
            rows = rows.filter(Q(reference__icontains=term) | Q(client_name__icontains=term) | Q(supplier_name__icontains=term))
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response([summary(row) for row in page])


class DealAdminDetail(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_deals_retrieve', responses=OpenApiTypes.OBJECT,
                   description='Un acuerdo con sus líneas en unidades, faltantes y actividad, sin precios ni la conversación.')
    def get(self, request, pk):
        return Response(detail(get_object_or_404(deal_queryset(), pk=pk)))


class DealAdminCancel(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_deals_cancel', request=AdminCancelSerializer, responses=OpenApiTypes.OBJECT,
                   description='MotionPartes cancela un acuerdo abierto que el cliente aún no recibió (disputa), también después del envío: '
                               'libera las unidades que aún reserva (lo descontado por apiag-cloud al Entregado no cambia); no cuenta contra el '
                               'proveedor. 409 si cambió (expected_version) o ya está cerrado (recibido o cancelado).')
    @transaction.atomic
    def post(self, request, pk):
        from .request_views import RequestConflict
        data = AdminCancelSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        row = get_object_or_404(SupplierRequest.objects.select_for_update(), pk=pk)
        if row.version != data.validated_data['expected_version']:
            raise RequestConflict('El acuerdo cambió. Actualízalo antes de cancelarlo.')
        description, fields = cancel(row, 'admin', request.user, data.validated_data['reason_code'], data.validated_data['note'])
        row.version += 1
        row.save(update_fields=['status', 'version', 'updated_at', *fields])
        DealEvent.objects.create(order=row, account=None, actor=request.user, kind='cancel', description=description,
                                 quotation=row.quotations.order_by('-revision').first())
        return Response(detail(get_object_or_404(deal_queryset(), pk=pk)))
