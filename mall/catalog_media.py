"""Validated catalog image uploads and gallery management."""
import logging
from io import BytesIO
import warnings

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework import serializers
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.parsers import MultiPartParser, JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema

from .management import IsSuperuser, UppercaseCharField
from .media_serializers import PartImageSerializer
from .models import Part, PartImage

logger = logging.getLogger(__name__)


class MediaUnavailable(APIException):
    status_code = 503
    default_detail = 'No se pudo guardar la imagen. Inténtalo de nuevo.'


class ImageUpload(serializers.Serializer):
    file = serializers.FileField()
    alt_text = UppercaseCharField(max_length=250, required=False, allow_blank=True, default='')


class ImageCaption(serializers.Serializer):
    alt_text = UppercaseCharField(max_length=250, allow_blank=True)


class ImageOrder(serializers.Serializer):
    image_ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=True)


def prepare_image(file):
    if file.size > settings.CATALOG_IMAGE_MAX_BYTES:
        raise ValidationError({'file': 'Cada imagen puede pesar hasta 10 MB.'})
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(file) as source:
                if source.format not in ('JPEG', 'PNG', 'WEBP'):
                    raise ValidationError({'file': 'Usa una imagen JPEG, PNG o WebP.'})
                if getattr(source, 'n_frames', 1) != 1:
                    raise ValidationError({'file': 'Usa una imagen estática, sin animación.'})
                if source.width * source.height > 40_000_000:
                    raise ValidationError({'file': 'La imagen supera los 40 megapíxeles. Reduce sus dimensiones.'})
                source.load()
                oriented = ImageOps.exif_transpose(source)
                normalized = oriented.convert('RGBA' if 'A' in oriented.getbands() or 'transparency' in source.info else 'RGB')
                normalized.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
                # Re-encode decoded pixels only: no EXIF location, comments or embedded metadata.
                clean = Image.new(normalized.mode, normalized.size)
                clean.paste(normalized)
                full = BytesIO()
                clean.save(full, 'WEBP', quality=88, method=4)
                width, height = clean.size
                clean.thumbnail((480, 480), Image.Resampling.LANCZOS)
                thumb = BytesIO()
                clean.save(thumb, 'WEBP', quality=82, method=4)
                return full.getvalue(), thumb.getvalue(), width, height
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValidationError({'file': 'No se pudo leer esta imagen. Selecciona un archivo JPEG, PNG o WebP válido.'})


def delete_files(files):
    for storage, name in files:
        if name:
            try:
                storage.delete(name)
            except Exception:
                # Keep API responses free of backend credentials or provider internals.
                logger.error('Catalog media cleanup failed for object %s', name)


def images_deleted(sender, instance, **kwargs):
    files = [(field.storage, field.name) for field in (instance.image, instance.thumbnail)]
    transaction.on_commit(lambda: delete_files(files))


def gallery(part, request):
    return PartImageSerializer(part.images.all(), many=True, context={'request': request}).data


class CatalogImages(APIView):
    permission_classes = [IsSuperuser]
    parser_classes = [MultiPartParser]

    @extend_schema(responses=PartImageSerializer(many=True))
    def get(self, request, pk):
        part = get_object_or_404(Part, pk=pk, merged_into__isnull=True)
        return Response(gallery(part, request))

    @extend_schema(request=ImageUpload, responses={201: PartImageSerializer})
    def post(self, request, pk):
        # Check ownership before decoding or sending any bytes to storage.
        part = get_object_or_404(Part, pk=pk, merged_into__isnull=True)
        data = ImageUpload(data=request.data)
        data.is_valid(raise_exception=True)
        file = data.validated_data['file']
        full, thumb, width, height = prepare_image(file)
        image = PartImage(part=part, alt_text=data.validated_data['alt_text'], uploaded_by=request.user,
                          original_filename=file.name[:255], width=width, height=height, size_bytes=len(full))
        written = []
        try:
            for field, name, content in [(image.image, 'image.webp', full), (image.thumbnail, 'thumb.webp', thumb)]:
                field.save(name, ContentFile(content), save=False)
                written.append((field.storage, field.name))
            with transaction.atomic():
                part = get_object_or_404(Part.objects.select_for_update(), pk=pk, merged_into__isnull=True)
                maximum = part.images.aggregate(value=Max('position'))['value']
                image.position = maximum + 1 if maximum is not None else 0
                image.save()
        except Exception as error:
            delete_files(written)
            if isinstance(error, APIException):
                raise
            logger.error('Catalog media upload failed (%s)', type(error).__name__)
            raise MediaUnavailable() from None
        return Response(PartImageSerializer(image, context={'request': request}).data, status=201)


class CatalogImageDetail(APIView):
    permission_classes = [IsSuperuser]
    parser_classes = [JSONParser]

    @extend_schema(request=ImageCaption, responses=PartImageSerializer)
    def patch(self, request, pk, image_id):
        data = ImageCaption(data=request.data)
        data.is_valid(raise_exception=True)
        with transaction.atomic():
            part = get_object_or_404(Part.objects.select_for_update(), pk=pk, merged_into__isnull=True)
            image = get_object_or_404(part.images, pk=image_id)
            image.alt_text = data.validated_data['alt_text']
            image.save(update_fields=['alt_text'])
        return Response(PartImageSerializer(image, context={'request': request}).data)

    @extend_schema(responses={204: None})
    def delete(self, request, pk, image_id):
        with transaction.atomic():
            part = get_object_or_404(Part.objects.select_for_update(), pk=pk, merged_into__isnull=True)
            get_object_or_404(part.images, pk=image_id).delete()
        return Response(status=204)


class CatalogImageOrder(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(request=ImageOrder, responses=PartImageSerializer(many=True))
    def post(self, request, pk):
        data = ImageOrder(data=request.data)
        data.is_valid(raise_exception=True)
        ids = data.validated_data['image_ids']
        with transaction.atomic():
            part = get_object_or_404(Part.objects.select_for_update(), pk=pk, merged_into__isnull=True)
            rows = {image.id: image for image in part.images.all()}
            if len(ids) != len(rows) or set(ids) != set(rows):
                raise ValidationError('La galería cambió. Actualízala antes de ordenar las imágenes.')
            for position, image_id in enumerate(ids):
                rows[image_id].position = position
            PartImage.objects.bulk_update(rows.values(), ['position'])
            return Response(gallery(part, request))
