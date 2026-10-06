"""Catalog-only storage, isolated from other apps sharing the Spaces bucket."""
from django.core.files.storage import storages


def catalog_storage():
    return storages['catalog_media']


def catalog_image_path(instance, filename):
    return f'catalog/{instance.part_id}/{instance.id}/{filename}'
