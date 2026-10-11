"""Catalog-only storage, isolated from other apps sharing the Spaces bucket."""
from django.core.files.storage import storages


def catalog_storage():
    return storages['catalog_media']


def catalog_image_path(instance, filename):
    return f'catalog/{instance.part_id}/{instance.id}/{filename}'


def chat_storage():
    """The deals' chat files (mall.deal_attachments): private, never served by a public URL."""
    return storages['chat_media']


def chat_attachment_path(instance, filename):
    return f'deals/{instance.message.order_id}/{instance.token}/{filename}'
