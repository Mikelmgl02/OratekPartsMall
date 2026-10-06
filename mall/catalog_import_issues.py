"""An owner-scoped queue for repairing rejected catalog import rows."""
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .management import IsSuperuser
from .models import CatalogImportIssue, CatalogImportJob, Part


FIELD_COLUMNS = {
    'sku': 'SKU', 'name': 'NOMBRE', 'description': 'DESCRIPCION',
    'code': 'CODIGO_ALTERNO', 'brand': 'MARCA_ALTERNO', 'active': 'ACTIVO',
    'category': 'CATEGORIA', 'subcategory': 'SUBCATEGORIA',
}
FIELD_LIMITS = {
    'sku': 200, 'name': 200, 'description': 10000, 'code': 120,
    'brand': 120, 'active': 10, 'category': 120, 'subcategory': 120,
}


def job_can_repair(job):
    # An expired draft can no longer commit catalog batches. Its rejected
    # rows still remain usable even when that draft was left unfinished.
    return job.status in ['completed', 'review'] or job.expires_at < timezone.now()


class CatalogImportIssueSerializer(serializers.ModelSerializer):
    job_id = serializers.UUIDField(read_only=True)
    filename = serializers.CharField(source='job.filename', read_only=True)
    job_status = serializers.CharField(source='job.status', read_only=True)
    part_id = serializers.UUIDField(read_only=True, allow_null=True)
    can_repair = serializers.SerializerMethodField()

    def get_can_repair(self, issue) -> bool:
        return job_can_repair(issue.job)

    class Meta:
        model = CatalogImportIssue
        fields = ['id', 'job_id', 'filename', 'job_status', 'can_repair', 'row', 'values', 'original', 'errors',
                  'recovery', 'status', 'created_at', 'resolved_at', 'part_id', 'applied_summary']
        read_only_fields = fields


class IssueFilters(serializers.Serializer):
    status = serializers.ChoiceField(choices=['pending', 'resolved'], default='pending')
    job = serializers.UUIDField(required=False)
    search = serializers.CharField(required=False, allow_blank=True, max_length=200)


class IssueRepairRequest(serializers.Serializer):
    values = serializers.JSONField()

    def validate_values(self, value):
        if not isinstance(value, dict) or set(value) - set(FIELD_COLUMNS):
            raise serializers.ValidationError('Indica únicamente los campos de inventario de esta fila.')
        if any(not isinstance(field_value, str) for field_value in value.values()):
            raise serializers.ValidationError('Guarda los campos de la fila como texto.')
        return value


class IssueRepairResponse(serializers.Serializer):
    valid = serializers.BooleanField()
    issue = CatalogImportIssueSerializer()
    errors = serializers.ListField(child=serializers.DictField())
    summary = serializers.DictField(required=False)
    part_id = serializers.UUIDField(required=False, allow_null=True)


class IssuePagination(PageNumberPagination):
    def get_paginated_response(self, data):
        response = super().get_paginated_response(data)
        response.data['pending_count'] = CatalogImportIssue.objects.filter(
            job__owner=self.request.user, status='pending').count()
        return response


class CatalogImportIssueList(generics.ListAPIView):
    permission_classes = [IsSuperuser]
    serializer_class = CatalogImportIssueSerializer
    pagination_class = IssuePagination

    @extend_schema(parameters=[IssueFilters], responses=CatalogImportIssueSerializer(many=True),
                   description='Lista las filas rechazadas del superusuario actual. Se conservan para corregirlas después de importar las filas válidas.')
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        filters = IssueFilters(data=self.request.query_params)
        filters.is_valid(raise_exception=True)
        data = filters.validated_data
        rows = CatalogImportIssue.objects.filter(job__owner=self.request.user, status=data['status']).select_related('job')
        if data.get('job'):
            rows = rows.filter(job_id=data['job'])
        if data.get('search'):
            term = data['search'].strip()
            rows = rows.filter(Q(values__sku__icontains=term) | Q(values__name__icontains=term) |
                               Q(values__description__icontains=term) | Q(job__filename__icontains=term))
        return rows.order_by('created_at', 'row', 'pk')


def normalized_row(issue, submitted):
    values = {field: str(issue.values.get(field, '')).strip().upper() for field in FIELD_COLUMNS}
    values.update({field: value.strip().upper() for field, value in submitted.items()})
    original_sku = issue.original.get('sku', {})
    original_name = issue.original.get('name', {})
    if (not values['name'] and values['sku'] and isinstance(original_sku, dict) and isinstance(original_name, dict)
            and original_sku.get('kind') == original_name.get('kind') == 'date'
            and original_sku.get('value') == original_name.get('value')):
        # Some exports repeat the source code in NOMBRE. Once its corrected
        # SKU is explicit, restore that paired name without an ISO-date label.
        values['name'] = values['sku']
    errors = []

    def error(field, message):
        errors.append({'row': issue.row, 'column': FIELD_COLUMNS[field], 'message': message})

    for field, value in values.items():
        if len(value) > FIELD_LIMITS[field]:
            error(field, 'El valor supera la longitud permitida.')
        source = issue.original.get(field, {})
        if isinstance(source, dict) and source.get('kind') in ['formula', 'error']:
            original = str(source.get('value', '')).strip().upper()
            if value and value == original:
                error(field, 'Reemplaza la fórmula o el error de Excel por el valor de texto correcto.')
    if not values['sku']:
        error('sku', 'Ingresa el SKU interno en esta fila.')
    if values['active'] and values['active'] not in ['SI', 'NO', 'TRUE', 'FALSE', '1', '0']:
        error('active', 'Usa SI o NO para indicar si el SKU está activo.')
    if values['brand'] and not values['code']:
        error('code', 'Ingresa el código alterno para esta marca.')
    return values, errors


def row_group(issue, values):
    fields = {field: values[field] for field in ['name', 'description', 'category', 'subcategory'] if values[field]}
    if values['active']:
        fields['active'] = values['active'] in ['SI', 'TRUE', '1']
    codes = {(values['brand'], values['code']): issue.row} if values['code'] else {}
    return {values['sku']: {'sku': values['sku'], 'row': issue.row, 'fields': fields, 'codes': codes}}


class CatalogImportIssueDetail(APIView):
    permission_classes = [IsSuperuser]

    def get_issue(self, request, pk, *, lock=False):
        rows = CatalogImportIssue.objects.filter(job__owner=request.user).select_related('job')
        if lock:
            rows = rows.select_for_update(of=('self',))
        return get_object_or_404(rows, pk=pk)

    @extend_schema(responses=CatalogImportIssueSerializer)
    def get(self, request, pk):
        return Response(CatalogImportIssueSerializer(self.get_issue(request, pk)).data)

    @extend_schema(request=IssueRepairRequest, responses=IssueRepairResponse,
                   description='Corrige una fila rechazada después de terminar su importación. Valida sus códigos contra el catálogo actual; repetir una corrección ya guardada no vuelve a aplicarla.')
    def post(self, request, pk):
        # Validate types before touching a queue record. Semantic row errors
        # remain in the queue with the administrator\'s latest attempted edits.
        serializer = IssueRepairRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        reference = self.get_issue(request, pk)
        from .catalog_import import build_plan, save_plan

        with transaction.atomic():
            # Batch commits take the same job lock, so a repair cannot race
            # against the import that owns the rejected source row.
            job = get_object_or_404(CatalogImportJob.objects.select_for_update().filter(owner=request.user), pk=reference.job_id)
            issue = self.get_issue(request, pk, lock=True)
            if issue.status == 'resolved':
                return self.resolved_response(issue)
            if not job_can_repair(job):
                return Response({'detail': 'Termina la importación de las filas válidas antes de corregir esta fila.'}, status=409)
            values, errors = normalized_row(issue, serializer.validated_data['values'])
            groups = row_group(issue, values) if not errors else {}
            if groups:
                plan, _ = build_plan(groups, [], 1, lock=True)
                errors = plan['errors']
            issue.values = values
            if not errors:
                try:
                    # Keep the outer queue/job locks after a uniqueness race;
                    # the failed catalog write must roll back as one unit.
                    with transaction.atomic():
                        save_plan(groups)
                except IntegrityError:
                    errors = [{'row': issue.row, 'column': 'SKU', 'message': 'El catálogo cambió durante la corrección. Revisa los códigos y vuelve a guardar.'}]
            if errors:
                issue.errors = errors
                issue.save(update_fields=['values', 'errors'])
                return Response({'valid': False, 'issue': CatalogImportIssueSerializer(issue).data, 'errors': errors})
            issue.status = 'resolved'
            issue.errors = []
            issue.resolved_at = timezone.now()
            issue.part = Part.objects.get(sku=values['sku'])
            issue.applied_summary = plan['summary']
            issue.save(update_fields=['values', 'status', 'errors', 'resolved_at', 'part', 'applied_summary'])
            return self.resolved_response(issue)

    @staticmethod
    def resolved_response(issue):
        return Response({'valid': True, 'issue': CatalogImportIssueSerializer(issue).data, 'errors': [],
                         'summary': issue.applied_summary, 'part_id': str(issue.part_id) if issue.part_id else None})
