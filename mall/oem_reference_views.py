"""Superuser API of a catalog line's OEM numbers: catalog/<id>/oem-equivalents, read only (mall.oem_links.part_equivalents): the
library numbers the line carries and the aftermarket codes equivalent to them.

The Biblioteca OEM's own API (oem-references: the list, a number's detail and edits, Editar celdas and its alternos) went with its
screen in S10 (owner, 2026-10-15): library numbers are References, managed in Inventario's Referencia tab (the line panel and the
References editor, mall.line_views)."""
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from .catalog_suffix_models import values
from .management import IsSuperuser
from .models import Part
from .oem_links import part_equivalents
from .oem_reference import REFERENCE_STATUSES

LINK_SOURCES = [('sku_name', 'Nombre del SKU'), ('catalog', 'Catálogo de marca de repuesto')]


class OEMReferenceLink(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    manufacturer = serializers.CharField(read_only=True)
    code = serializers.CharField(read_only=True)
    status = serializers.ChoiceField(choices=REFERENCE_STATUSES, read_only=True)


class PartOEMNumber(serializers.Serializer):
    reference = OEMReferenceLink()
    via = serializers.ListField(child=serializers.ChoiceField(choices=['oem', *values(LINK_SOURCES)]),
                                help_text='oem: un alterno OEM del SKU; sku_name: el código del SKU nombra el número; catalog: un catálogo lo vinculó.')
    citations = serializers.ListField(child=serializers.CharField(), help_text='Catálogo, página y artículo de los vínculos por catálogo.')


class PartCrossReference(serializers.Serializer):
    brand = serializers.CharField()
    code = serializers.CharField()
    citations = serializers.ListField(child=serializers.CharField())
    numbers = serializers.ListField(child=serializers.UUIDField(), help_text='Los números OEM del SKU que tienen esta equivalencia.')
    on_sku = serializers.BooleanField(help_text='El SKU también guarda este código como alterno propio.')


class PartOEMEquivalentsResponse(serializers.Serializer):
    numbers = PartOEMNumber(many=True)
    cross_references = PartCrossReference(many=True)


class PartOEMEquivalents(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_oem_equivalents', responses=PartOEMEquivalentsResponse,
                   description='Solo lectura: los números OEM de un SKU del catálogo (los suyos, y los que un catálogo de marca de repuesto o '
                               'su código le vinculó) y los códigos de marcas de repuesto equivalentes a esos números.')
    def get(self, request, pk):
        part = Part.objects.filter(pk=pk).first()
        if part is None:
            raise NotFound('Ese SKU no está en el catálogo.')
        return Response(PartOEMEquivalentsResponse(part_equivalents(part)).data)
