from django.db import transaction
from django.db.models import Exists, F, OuterRef, Prefetch, Q, Value
from django.db.models.functions import Greatest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import filters, generics, permissions, status
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from .list_ordering import StableOrdering
from .models import Account, Invitation, Membership, Part, StockEntry, SupplierItem, SupplierItemBranchStock, User
from .serializers import AccountSerializer, InventorySerializer, OfferSerializer, PartSerializer, SignupResponseSerializer, SignupSerializer, StockEntrySerializer, SupplierItemSerializer, SupplierStockSerializer
from .services import ingest_inventory
from .catalog_availability import catalog_availability
from .catalog_filters import apply_filters, catalog_facets, filter_values
from .catalog_search import search_parts, search_rank
from .oem_links import equivalents_for

def account_for(user, account_id, capability=None):
    account = get_object_or_404(Account, pk=account_id, active=True, memberships__user=user)
    if capability and not account.roles.filter(capability=capability).exists():
        raise PermissionDenied('La cuenta no tiene el rol requerido.')
    return account

PERMISSION_RANK = {'staff': 0, 'manager': 1, 'owner': 2}

def membership_for(user, account_id, capability, minimum=None):
    """Like account_for, plus the caller's own membership; non-members (superusers included) get 404."""
    membership = get_object_or_404(Membership.objects.select_related('account'), account_id=account_id, account__active=True, user=user)
    account = membership.account
    if capability and not account.roles.filter(capability=capability).exists():
        raise PermissionDenied('La cuenta no tiene el rol requerido.')
    if minimum and PERMISSION_RANK.get(membership.permission, -1) < PERMISSION_RANK[minimum]:
        raise PermissionDenied('Tu permiso en la cuenta no permite esta acción.')
    return account, membership

class SignupView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    @extend_schema(request=SignupSerializer, responses={201: SignupResponseSerializer})
    @transaction.atomic
    def post(self, request):
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        invitation = Invitation.objects.select_for_update().filter(token=data['token'], email__iexact=data['email'], accepted_at__isnull=True, expires_at__gt=timezone.now()).first()
        if not invitation:
            raise ValidationError('La invitación no es válida o ha vencido.')
        if User.objects.filter(Q(username=data['username']) | Q(email__iexact=data['email'])).exists():
            raise ValidationError('El usuario o el correo electrónico ya está registrado.')
        user = User.objects.create_user(username=data['username'], email=invitation.email.lower(), password=data['password'])
        invitation.accepted_at = timezone.now()
        invitation.save(update_fields=['accepted_at'])
        return Response({'user_id': user.pk, 'detail': 'Registro completado. Un administrador debe configurar tu cuenta.'}, status=status.HTTP_201_CREATED)

class LogoutView(APIView):
    @extend_schema(request=None, responses={204: None})
    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        from django.contrib.auth import logout
        logout(request)
        return Response(status=204)

class AccountList(generics.ListAPIView):
    serializer_class = AccountSerializer
    def get_queryset(self):
        from .apiag_models import ApiagConnection
        own = Prefetch('memberships', queryset=Membership.objects.filter(user=self.request.user), to_attr='own_memberships')
        return (Account.objects.filter(memberships__user=self.request.user, active=True).prefetch_related('roles', own)
                .annotate(apiag_linked=Exists(ApiagConnection.objects.filter(account=OuterRef('pk')))).order_by('name', 'id'))

def catalog_parts(search=''):
    """The canonical catalog SKUs the search finds (mall.catalog_search: every word, any order, accents and separators ignored)."""
    return search_parts(Part.objects.filter(active=True, merged_into__isnull=True), search)


REUSED_MATCHES = 2000

class CatalogList(generics.ListAPIView):
    serializer_class = PartSerializer
    reuse_matches = True
    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        data = self.serialize(page if page is not None else queryset)
        response = self.get_paginated_response(data) if page is not None else Response(data)
        if request.query_params.get('include_facets') == '1' and page is not None:
            response.data['facets'] = catalog_facets(self.base_query, self.catalog_filters)
        return response

    def serialize(self, parts):
        parts = list(parts)
        context = {**self.get_serializer_context(), 'availability_by_part': catalog_availability(parts),
                   'equivalents_by_part': equivalents_for([part.pk for part in parts])}
        return self.get_serializer(parts, many=True, context=context).data

    def get_queryset(self):
        if not Membership.objects.filter(user=self.request.user, account__active=True).exists():
            raise PermissionDenied('Tu administrador debe configurar tu cuenta.')
        self.catalog_filters = filter_values(self.request.query_params)
        search = self.request.query_params.get('search', '').strip()
        self.base_query = catalog_parts(search)
        if search and self.reuse_matches:
            # The count, the page and the four facet queries read the matches by id instead of each running the search again.
            ids = list(self.base_query.order_by().values_list('pk', flat=True)[:REUSED_MATCHES + 1])
            if len(ids) <= REUSED_MATCHES:
                self.base_query = Part.objects.filter(pk__in=ids)
        query = apply_filters(self.base_query, self.catalog_filters).prefetch_related('codes', 'images')
        # A code search lists the exact code first, then the codes containing it; everything else stays in SKU order.
        rank = search_rank(search)
        return query.annotate(search_rank=rank).order_by('search_rank', 'sku', 'id') if rank is not None else query.order_by('sku', 'id')


SUGGESTIONS = 6

class CatalogSuggest(CatalogList):
    """The search box's preview while typing: the first results the search would list, without the count or the facets."""
    pagination_class = None
    reuse_matches = False
    def list(self, request, *args, **kwargs):
        if not request.query_params.get('search', '').strip():
            return Response([])
        return Response(self.serialize(self.get_queryset()[:SUGGESTIONS]))

class OffersView(APIView):
    @extend_schema(responses=OfferSerializer(many=True))
    def get(self, request, part_id):
        if not Membership.objects.filter(user=request.user, account__active=True, account__roles__capability='client').exists():
            raise PermissionDenied('Se requiere un rol de cliente.')
        get_object_or_404(Part, pk=part_id, active=True, merged_into__isnull=True)
        items = SupplierItem.objects.filter(part_id=part_id, matching_status='matched', supplier__active=True, supplier__roles__capability='supplier', reported_quantity__gt=F('reserved_quantity')).select_related('supplier').distinct()
        suppliers = {}
        for item in items.order_by('supplier__name', 'codigo', 'brand', 'id'):
            offer = suppliers.setdefault(str(item.supplier_id), {'supplier_id': item.supplier_id, 'supplier_name': item.supplier.name, 'in_stock': True, 'items': []})
            offer['items'].append(item)
        return Response(OfferSerializer(list(suppliers.values()), many=True).data)

MATCHING_FILTERS = {'matched': Q(matching_status='matched'), 'pending': Q(matching_status='pending'), 'review': Q(matching_status='review'),
                    'unmatched': ~Q(matching_status='matched')}


class InventoryList(generics.ListAPIView):
    serializer_class = SupplierStockSerializer
    filter_backends = [filters.SearchFilter, StableOrdering]
    search_fields = ['codigo', 'brand', 'supplier_invent_id', 'description', 'part__sku']
    ordering_fields = ['codigo', 'brand', 'supplier_invent_id', 'source', 'available', 'reserved_quantity', 'matching_status', 'updated_at']

    @extend_schema(parameters=[OpenApiParameter('matching_status', OpenApiTypes.STR, enum=list(MATCHING_FILTERS),
                                                description='unmatched: pendientes y por revisar (lo que aún no ven los clientes).')])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        supplier = account_for(self.request.user, self.kwargs['account_id'], 'supplier')
        # available mirrors SupplierItem.available_quantity (never below zero) so the grid can sort by it.
        branches = SupplierItemBranchStock.objects.select_related('sucursal').order_by('sucursal__nombre', 'sucursal_id')
        rows = (SupplierItem.objects.filter(supplier=supplier).select_related('part').prefetch_related(Prefetch('branch_stock', queryset=branches))
                .annotate(available=Greatest(F('reported_quantity') - F('reserved_quantity'), Value(0))).order_by('supplier_invent_id', 'id'))
        matching = self.request.query_params.get('matching_status', '')
        if matching:
            if matching not in MATCHING_FILTERS:
                raise ValidationError({'matching_status': 'Usa matched, pending, review o unmatched.'})
            rows = rows.filter(MATCHING_FILTERS[matching])
        return rows

class InventoryIngest(APIView):
    @extend_schema(request=InventorySerializer, responses=SupplierItemSerializer)
    def post(self, request, account_id):
        supplier = account_for(request.user, account_id, 'supplier')
        from .supplier_import import SYNCED_MANUAL, refuse_synced
        refuse_synced(supplier, SYNCED_MANUAL)  # a synced account's stock comes only from its pull
        serializer = InventorySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data['source'] == 'apiag':  # only the pull (mall.apiag_sync) writes them, with their sucursales
            raise ValidationError({'source': 'Los artículos de apiag-cloud solo se actualizan desde su sincronización.'})
        from .inventory_history import RESERVED_PREFIXES, Recorder
        if serializer.validated_data['update_id'].startswith(RESERVED_PREFIXES):
            raise ValidationError({'update_id': 'Ese prefijo está reservado para las cargas Excel y la sincronización con apiag-cloud.'})
        recorder = Recorder(supplier, 'manual', serializer.validated_data['update_id'], actor=request.user)
        item = ingest_inventory(supplier=supplier, actor=request.user, data=serializer.validated_data, recorder=recorder)
        if recorder.touched:  # a replay records nothing: its run stays as it was
            recorder.finish()
        return Response(SupplierItemSerializer(item).data)

class LedgerList(generics.ListAPIView):
    serializer_class = StockEntrySerializer
    def get_queryset(self):
        supplier = account_for(self.request.user, self.kwargs['account_id'], 'supplier')
        item = get_object_or_404(SupplierItem, pk=self.kwargs['item_id'], supplier=supplier)
        return StockEntry.objects.filter(item=item).order_by('-created_at', '-id')
