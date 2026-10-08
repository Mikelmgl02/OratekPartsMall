from io import BytesIO
from unittest.mock import patch

from django.core.files.storage import InMemoryStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient
from storages.backends.s3 import S3Storage

from .catalog_grouping import merge_catalog_parts
from .models import Part, PartImage, User


def upload(name='photo.png', size=(80, 60), format='PNG'):
    data = BytesIO()
    Image.new('RGB', size, '#228855').save(data, format)
    return SimpleUploadedFile(name, data.getvalue(), content_type='image/png')


class CatalogMediaTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('media-admin', email='media@example.test', is_superuser=True)
        self.user = User.objects.create_user('media-user', email='user@example.test')
        self.part = Part.objects.create(sku='TEST-MEDIA')
        self.other = Part.objects.create(sku='TEST-OTHER')
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.path = f'/api/v1/management/catalog/{self.part.pk}/images/'
        self.storage = InMemoryStorage()
        for name in ['image', 'thumbnail']:
            patcher = patch.object(PartImage._meta.get_field(name), 'storage', self.storage)
            patcher.start()
            self.addCleanup(patcher.stop)

    def add_image(self, **kwargs):
        result = self.client.post(self.path, {'file': upload(**kwargs)}, format='multipart')
        self.assertEqual(result.status_code, 201, result.data)
        return PartImage.objects.get(pk=result.data['id'])

    def test_upload_gallery_optimization_and_caption(self):
        first = self.add_image(size=(2400, 1200))
        second = self.add_image()
        self.assertEqual((first.width, first.height), (2000, 1000))
        self.assertEqual(first.uploaded_by, self.admin)
        self.assertTrue(first.image.name.startswith(f'catalog/{self.part.pk}/{first.pk}/'))
        with first.image.open() as stream:
            image = Image.open(stream)
            self.assertEqual(image.format, 'WEBP')
            self.assertFalse(image.getexif())
        with first.thumbnail.open() as stream:
            self.assertEqual(Image.open(stream).size, (480, 240))
        response = self.client.get(self.path)
        self.assertEqual([row['id'] for row in response.data], [str(first.pk), str(second.pk)])
        self.assertIn('/thumb.webp', response.data[0]['thumbnail_url'])
        response = self.client.patch(f'{self.path}{first.pk}/', {'alt_text': 'vista frontal'}, format='json')
        self.assertEqual(response.data['alt_text'], 'VISTA FRONTAL')
        managed = self.client.get(f'/api/v1/management/catalog/{self.part.pk}/')
        self.assertEqual(len(managed.data['images']), 2)

    def test_order_cover_delete_and_storage_cleanup(self):
        first, second = self.add_image(), self.add_image()
        response = self.client.post(f'{self.path}order/', {'image_ids': [str(second.pk), str(first.pk)]}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]['id'], str(second.pk))
        names = [second.image.name, second.thumbnail.name]
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(f'{self.path}{second.pk}/')
        self.assertEqual(response.status_code, 204)
        self.assertTrue(all(not self.storage.exists(name) for name in names))
        self.assertEqual(self.client.get(self.path).data[0]['id'], str(first.pk))
        self.assertTrue(self.storage.exists(first.image.name))

    def test_rejects_invalid_stale_or_cross_part_order(self):
        first, second = self.add_image(), self.add_image()
        for ids in [[str(first.pk)], [str(first.pk), str(first.pk)], [str(first.pk), str(self.other.pk)]]:
            response = self.client.post(f'{self.path}order/', {'image_ids': ids}, format='json')
            self.assertEqual(response.status_code, 400)
        path = f'/api/v1/management/catalog/{self.other.pk}/images/{second.pk}/'
        self.assertEqual(self.client.patch(path, {'alt_text': 'OTHER'}, format='json').status_code, 404)
        self.assertEqual(self.client.delete(path).status_code, 404)
        self.assertEqual(list(self.part.images.values_list('pk', flat=True)), [first.pk, second.pk])

    def test_only_superusers_can_manage_images(self):
        first = self.add_image()
        for user in [self.user, None]:
            self.client.force_authenticate(user)
            for response in [self.client.get(self.path),
                             self.client.post(self.path, {'file': upload()}, format='multipart'),
                             self.client.patch(f'{self.path}{first.pk}/', {'alt_text': 'NO'}, format='json'),
                             self.client.delete(f'{self.path}{first.pk}/'),
                             self.client.post(f'{self.path}order/', {'image_ids': []}, format='json')]:
                self.assertIn(response.status_code, (401, 403))
        self.assertEqual(PartImage.objects.count(), 1)

    def test_invalid_formats_and_size_never_reach_storage(self):
        for file in [SimpleUploadedFile('fake.png', b'<svg><script>bad</script></svg>', content_type='image/png'), upload('image.gif', format='GIF')]:
            response = self.client.post(self.path, {'file': file}, format='multipart')
            self.assertEqual(response.status_code, 400)
        with override_settings(CATALOG_IMAGE_MAX_BYTES=10):
            self.assertEqual(self.client.post(self.path, {'file': upload()}, format='multipart').status_code, 400)
        self.assertEqual(PartImage.objects.count(), 0)
        self.assertEqual(self.storage.listdir(''), ([], []))

    def test_failed_thumbnail_upload_cleans_written_image(self):
        original = self.storage.save
        saved = []
        def fail_second(name, *args, **kwargs):
            if 'thumb.webp' in name:
                raise OSError('provider credentials must not reach the response')
            value = original(name, *args, **kwargs)
            saved.append(value)
            return value
        with patch.object(self.storage, 'save', side_effect=fail_second):
            response = self.client.post(self.path, {'file': upload()}, format='multipart')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('credentials', str(response.data))
        self.assertFalse(PartImage.objects.exists())
        self.assertTrue(all(not self.storage.exists(name) for name in saved))

    def test_merge_preserves_gallery_keys_and_target_cover(self):
        original = self.add_image()
        source = Part.objects.create(sku='TEST-MEDIA-ALT')
        self.path = f'/api/v1/management/catalog/{source.pk}/images/'
        alternate = self.add_image()
        name = alternate.image.name
        result = merge_catalog_parts(actor=self.admin, target_sku=self.part.sku, source_skus=[source.sku])
        self.assertEqual(result['summary']['images_transferred'], 1)
        self.assertEqual(list(self.part.images.values_list('pk', flat=True)), [original.pk, alternate.pk])
        alternate.refresh_from_db()
        self.assertEqual(alternate.image.name, name)
        self.assertTrue(self.storage.exists(name))
        self.assertEqual(self.client.post(self.path, {'file': upload()}, format='multipart').status_code, 404)

    def test_catalog_queries_prefetch_gallery(self):
        from .serializers import PartSerializer
        self.add_image(); self.add_image()
        with self.assertNumQueries(4):  # parts, codes, images and the page's OEM numbers (no SKU reaches one: no codes query)
            data = PartSerializer(Part.objects.all().prefetch_related('codes', 'images'), many=True).data
        self.assertEqual(len(next(row for row in data if row['sku'] == self.part.sku)['images']), 2)

    def test_rollback_does_not_remove_files(self):
        first = self.add_image()
        name = first.image.name
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.part.delete()
                    raise ValueError('rollback')
            except ValueError:
                pass
        self.assertTrue(PartImage.objects.filter(pk=first.pk).exists())
        self.assertTrue(self.storage.exists(name))

    def test_spaces_cdn_urls_are_unsigned_and_namespaced(self):
        storage = S3Storage(access_key='test', secret_key='test', bucket_name='media-test',
            region_name='nyc3', endpoint_url='https://nyc3.digitaloceanspaces.com',
            custom_domain='media-test.nyc3.cdn.digitaloceanspaces.com', location='motionpartes/media',
            querystring_auth=False, default_acl='public-read')
        self.assertEqual(storage.url('catalog/sku/image.webp'),
            'https://media-test.nyc3.cdn.digitaloceanspaces.com/motionpartes/media/catalog/sku/image.webp')
