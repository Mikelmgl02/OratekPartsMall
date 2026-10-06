from rest_framework import serializers
from .models import PartImage


class PartImageSerializer(serializers.ModelSerializer):
    url = serializers.ImageField(source='image', read_only=True)
    thumbnail_url = serializers.ImageField(source='thumbnail', read_only=True)

    class Meta:
        model = PartImage
        fields = ['id', 'url', 'thumbnail_url', 'alt_text', 'position', 'width', 'height', 'size_bytes', 'created_at']
        read_only_fields = fields
