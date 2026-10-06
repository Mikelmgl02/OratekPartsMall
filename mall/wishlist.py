"""Personal favorites; saving never creates requests or reserves stock."""
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, serializers
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import APIView

from .catalog_availability import catalog_availability
from .models import Membership, Part, WishlistItem
from .serializers import PartSerializer


class CatalogMember(permissions.BasePermission):
    message = 'Tu administrador debe configurar tu cuenta.'

    def has_permission(self, request, view):
        return request.user.is_authenticated and Membership.objects.filter(user=request.user, account__active=True).exists()


class WishlistSerializer(serializers.ModelSerializer):
    part = PartSerializer(read_only=True)
    available = serializers.SerializerMethodField()

    def get_available(self, obj) -> bool:
        return obj.part.active and not obj.part.merged_into_id

    class Meta:
        model = WishlistItem
        fields = ['part', 'saved_at', 'available']


class WishlistList(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated, CatalogMember]
    serializer_class = WishlistSerializer

    def get_queryset(self):
        query = WishlistItem.objects.filter(user=self.request.user)
        search = self.request.query_params.get('search', '').strip()
        if search:
            query = query.filter(Q(part__sku__icontains=search) | Q(part__name__icontains=search) |
                                 Q(part__description__icontains=search) | Q(part__category__icontains=search) |
                                 Q(part__subcategory__icontains=search) | Q(part__codes__code__icontains=search) |
                                 Q(part__codes__brand__icontains=search)).distinct()
        return query.select_related('part').prefetch_related('part__codes', 'part__images').order_by('-saved_at', '-id')

    def list(self, request, *args, **kwargs):
        rows = list(self.paginate_queryset(self.get_queryset()))
        context = self.get_serializer_context()
        context['availability_by_part'] = catalog_availability([row.part for row in rows if row.part.active])
        return self.get_paginated_response(self.get_serializer(rows, many=True, context=context).data)


class WishlistState(APIView):
    permission_classes = [permissions.IsAuthenticated, CatalogMember]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        ids = list(WishlistItem.objects.filter(user=request.user).values_list('part_id', flat=True))
        return Response({'part_ids': ids, 'count': len(ids)})


class CatalogChanged(APIException):
    status_code = 409
    default_detail = 'El SKU cambió durante la actualización. Actualiza el catálogo y vuelve a intentarlo.'


def locked_canonical_part(part_id):
    # Lock in the same order as catalog merges, then verify the initial link.
    initial = get_object_or_404(Part, pk=part_id)
    ids = {initial.pk, initial.merged_into_id} - {None}
    locked = {part.pk: part for part in Part.objects.select_for_update().filter(pk__in=ids).order_by('pk')}
    original = locked.get(initial.pk)
    if not original or original.merged_into_id != initial.merged_into_id:
        raise CatalogChanged()
    canonical = locked.get(original.merged_into_id or original.pk)
    if not canonical or canonical.merged_into_id:
        raise CatalogChanged()
    return canonical


class WishlistDetail(APIView):
    permission_classes = [permissions.IsAuthenticated, CatalogMember]

    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    @transaction.atomic
    def put(self, request, part_id):
        part = locked_canonical_part(part_id)
        if not part.active:
            return Response({'detail': 'Este repuesto ya no está disponible en el catálogo.'}, status=400)
        row, _ = WishlistItem.objects.get_or_create(user=request.user, part=part)
        return Response({'part_id': part.pk, 'saved': True, 'saved_at': row.saved_at})

    @extend_schema(responses={204: None})
    @transaction.atomic
    def delete(self, request, part_id):
        part = locked_canonical_part(part_id)
        WishlistItem.objects.filter(user=request.user, part=part).delete()
        return Response(status=204)


def merge_wishlists(source_ids, target):
    """Converging SKUs keep one favorite per user and its original save date."""
    rows = list(WishlistItem.objects.select_for_update().filter(part_id__in=source_ids | {target.pk}).order_by('saved_at', 'pk'))
    retained, duplicate_ids = {}, []
    for row in rows:
        if row.user_id in retained:
            duplicate_ids.append(row.pk)
        else:
            retained[row.user_id] = row
    WishlistItem.objects.filter(pk__in=duplicate_ids).delete()
    WishlistItem.objects.filter(pk__in=[row.pk for row in retained.values()]).update(part=target)
