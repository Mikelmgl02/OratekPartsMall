from django.db import transaction
from django.db.models import F, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema
from .models import Account, Invitation, Membership, Part, StockEntry, SupplierItem, User
from .serializers import AccountSerializer, InventorySerializer, OfferSerializer, PartSerializer, SignupResponseSerializer, SignupSerializer, StockEntrySerializer, SupplierItemSerializer
from .services import ingest_inventory
from .catalog_availability import catalog_availability
from .catalog_filters import apply_filters, catalog_facets, filter_values

def account_for(user, account_id, capability=None):
    account = get_object_or_404(Account, pk=account_id, active=True, memberships__user=user)
    if capability and not account.roles.filter(capability=capability).exists():
        raise PermissionDenied('La cuenta no tiene el rol requerido.')
    return account

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
        return Account.objects.filter(memberships__user=self.request.user, active=True).prefetch_related('roles').order_by('name', 'id')

def catalog_parts(search=''):
    query = Part.objects.filter(active=True, merged_into__isnull=True)
    if search:
        query = query.filter(Q(sku__icontains=search) | Q(name__icontains=search) | Q(description__icontains=search)
                             | Q(category__icontains=search) | Q(subcategory__icontains=search)
                             | Q(codes__code__icontains=search) | Q(codes__brand__icontains=search)
                             | Q(supplier_items__codigo__icontains=search, supplier_items__matching_status='matched', supplier_items__supplier__active=True)).distinct()
    return query


class CatalogList(generics.ListAPIView):
    serializer_class = PartSerializer
    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        parts = list(page if page is not None else queryset)
        context = {**self.get_serializer_context(), 'availability_by_part': catalog_availability(parts)}
        data = self.get_serializer(parts, many=True, context=context).data
        response = self.get_paginated_response(data) if page is not None else Response(data)
        if request.query_params.get('include_facets') == '1' and page is not None:
            response.data['facets'] = catalog_facets(self.base_query, self.catalog_filters)
        return response

    def get_queryset(self):
        if not Membership.objects.filter(user=self.request.user, account__active=True).exists():
            raise PermissionDenied('Tu administrador debe configurar tu cuenta.')
        self.catalog_filters = filter_values(self.request.query_params)
        self.base_query = catalog_parts(self.request.query_params.get('search', '').strip())
        return apply_filters(self.base_query, self.catalog_filters).prefetch_related('codes', 'images').order_by('sku', 'id')

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

class InventoryList(generics.ListAPIView):
    serializer_class = SupplierItemSerializer
    def get_queryset(self):
        supplier = account_for(self.request.user, self.kwargs['account_id'], 'supplier')
        return SupplierItem.objects.filter(supplier=supplier).order_by('supplier_invent_id')

class InventoryIngest(APIView):
    @extend_schema(request=InventorySerializer, responses=SupplierItemSerializer)
    def post(self, request, account_id):
        supplier = account_for(request.user, account_id, 'supplier')
        serializer = InventorySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = ingest_inventory(supplier=supplier, actor=request.user, data=serializer.validated_data)
        return Response(SupplierItemSerializer(item).data)

class LedgerList(generics.ListAPIView):
    serializer_class = StockEntrySerializer
    def get_queryset(self):
        supplier = account_for(self.request.user, self.kwargs['account_id'], 'supplier')
        item = get_object_or_404(SupplierItem, pk=self.kwargs['item_id'], supplier=supplier)
        return StockEntry.objects.filter(item=item).order_by('-created_at', '-id')
