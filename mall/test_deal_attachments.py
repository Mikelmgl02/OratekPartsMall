import uuid
from unittest.mock import patch

from django.core.files.storage import InMemoryStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase

from .request_models import DealAttachment, DealMessage
from . import test_deals as deal_tests

PNG = b'\x89PNG\r\n\x1a\n' + bytes(range(64))
PDF = b'%PDF-1.4\n% prueba\n'
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class ChatStorage:
    """Chat files go to memory in tests, never to the configured storage (Spaces on a development machine)."""

    def use_memory_storage(self):
        self.storage = InMemoryStorage()
        patcher = patch.object(DealAttachment._meta.get_field('file'), 'storage', self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)


@patch('mall.push.dispatch', lambda job: job())
class DealAttachmentTests(ChatStorage, APITestCase):
    """Files in a deal's chat: sent with or without text, private to the deal's parties, and described where only text fits."""
    account = deal_tests.DealWorkflowTests.account
    item = deal_tests.DealWorkflowTests.item
    url = deal_tests.DealWorkflowTests.url
    payload = deal_tests.DealWorkflowTests.payload
    submit = deal_tests.DealWorkflowTests.submit

    def setUp(self):
        deal_tests.DealWorkflowTests.setUp(self)
        self.use_memory_storage()
        self.client.force_authenticate(self.buyer)
        self.order = deal_tests.DealWorkflowTests.order(self)

    def messages(self, account):
        return f'/api/v1/accounts/{account.pk}/deals/{self.order.pk}/messages/'

    def file(self, account, attachment_id):
        return self.client.get(f'/api/v1/accounts/{account.pk}/deals/{self.order.pk}/attachments/{attachment_id}/')

    def send(self, files=(), body='', message_id=None, user=None, account=None):
        self.client.force_authenticate(user or self.buyer)
        data = {'message_id': str(message_id or uuid.uuid4()), 'body': body, **({'files': list(files)} if files else {})}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.messages(account or self.client_account), data, format='multipart')

    def test_a_photo_and_a_pdf_go_with_the_message_and_only_the_deals_parties_open_them(self):
        response = self.send([SimpleUploadedFile('tambor trasero.png', PNG, 'image/png'), SimpleUploadedFile('factura.pdf', PDF, 'application/pdf')],
                             'Así se ve el que tengo')
        self.assertEqual(response.status_code, 200)
        photo, pdf = response.data['attachments']
        self.assertEqual({key: photo[key] for key in ('kind', 'name', 'content_type', 'size', 'voice', 'failed', 'transcript')},
                         {'kind': 'image', 'name': 'tambor trasero.png', 'content_type': 'image/png', 'size': len(PNG), 'voice': False,
                          'failed': False, 'transcript': ''})
        self.assertEqual((pdf['kind'], pdf['content_type']), ('document', 'application/pdf'))
        stored = DealAttachment.objects.get(pk=photo['id']).file.name
        self.assertTrue(stored.startswith(f'deals/{self.order.pk}/') and stored.endswith('/tambor_trasero.png'))
        self.assertTrue(self.storage.exists(stored))

        self.client.force_authenticate(self.seller_a)
        [message] = self.client.get(self.messages(self.supplier_a)).data['results']
        self.assertEqual((message['body'], [item['name'] for item in message['attachments']]), ('Así se ve el que tengo', ['tambor trasero.png', 'factura.pdf']))
        opened = self.file(self.supplier_a, photo['id'])
        self.assertEqual((opened.status_code, opened['Content-Type'], b''.join(opened.streaming_content)), (200, 'image/png', PNG))
        self.assertTrue(opened['Content-Disposition'].startswith('inline'))
        self.assertEqual((opened['X-Content-Type-Options'], opened['Cache-Control']), ('nosniff', 'private, max-age=86400'))
        self.assertIn('sandbox', opened['Content-Security-Policy'])
        self.assertNotIn('Content-Security-Policy', self.file(self.supplier_a, pdf['id']))  # a PDF shows in the browser's viewer
        url = f'/api/v1/accounts/{self.supplier_a.pk}/deals/{self.order.pk}/attachments/{photo["id"]}/'
        part = self.client.get(url, HTTP_RANGE='bytes=2-9')  # how Safari plays audio and video
        self.assertEqual((part.status_code, part.content, part['Content-Range'], part['Accept-Ranges']), (206, PNG[2:10], f'bytes 2-9/{len(PNG)}', 'bytes'))
        self.assertEqual(self.client.get(url, HTTP_RANGE='bytes=-4').content, PNG[-4:])
        self.assertEqual(self.client.get(url, HTTP_RANGE=f'bytes={len(PNG)}-').status_code, 416)

        self.client.force_authenticate(self.seller_b)  # another supplier: not through its own account, nor through theirs
        self.assertEqual(self.file(self.supplier_b, photo['id']).status_code, 404)
        self.assertIn(self.file(self.supplier_a, photo['id']).status_code, (403, 404))
        self.client.force_authenticate(None)
        self.assertEqual(self.file(self.supplier_a, photo['id']).status_code, 401)

    def test_a_message_may_be_only_files_and_says_what_they_are_where_only_text_fits(self):
        for user in (self.buyer, self.seller_a):  # the inbox counts what arrives after a person's first visit
            self.client.force_authenticate(user)
            self.client.get('/api/v1/inbox/')
        self.assertEqual(self.send().status_code, 400)  # neither text nor files
        response = self.send([SimpleUploadedFile('foto.png', PNG, 'image/png'), SimpleUploadedFile('lista.pdf', PDF, 'application/pdf')])
        self.assertEqual((response.status_code, response.data['body']), (200, ''))
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.client.get('/api/v1/inbox/').data['latest_message']['body'], '📷 Foto y 1 archivo más')
        self.send([SimpleUploadedFile('lista.pdf', PDF, 'application/pdf')], user=self.seller_a, account=self.supplier_a)
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.client.get('/api/v1/inbox/').data['latest_message']['body'], '📄 lista.pdf')

    def test_files_the_chat_does_not_take_never_reach_the_storage(self):
        refused = [
            ([SimpleUploadedFile('pagina.html', b'<script>alert(1)</script>', 'text/html')], 'no se puede enviar'),
            ([SimpleUploadedFile('imagen.svg', b'<svg/>', 'image/svg+xml')], 'no se puede enviar'),
            ([SimpleUploadedFile(f'foto{index}.png', PNG, 'image/png') for index in range(6)], 'hasta 5 archivos'),
        ]
        for files, reason in refused:
            response = self.send(files, 'mira')
            self.assertEqual(response.status_code, 400)
            self.assertIn(reason, str(response.data['files']))
        with override_settings(CHAT_MEDIA_MAX_BYTES=16):
            response = self.send([SimpleUploadedFile('foto.png', PNG, 'image/png')], 'mira')
        self.assertEqual(response.status_code, 400)
        self.assertIn('pesa más de', str(response.data['files']))
        self.assertFalse(DealMessage.objects.exists())
        self.assertEqual(self.storage.listdir('')[1], [])

    def test_a_spreadsheet_named_right_is_taken_and_downloads_and_a_retry_saves_it_once(self):
        key = uuid.uuid4()
        sheet = lambda: SimpleUploadedFile('Cotización marzo.xlsx', b'PK\x03\x04hoja', 'application/octet-stream')
        first = self.send([sheet()], 'la lista', message_id=key)
        again = self.send([sheet()], 'la lista', message_id=key)  # the same send, retried
        self.assertEqual((first.status_code, again.status_code, first.data['id']), (200, 200, again.data['id']))
        self.assertEqual(DealAttachment.objects.count(), 1)
        [sheet_data] = first.data['attachments']
        self.assertEqual((sheet_data['kind'], sheet_data['content_type'], sheet_data['name']), ('document', XLSX, 'Cotización marzo.xlsx'))
        downloaded = self.file(self.client_account, sheet_data['id'])
        self.assertTrue(downloaded['Content-Disposition'].startswith('attachment'))
        self.assertEqual(downloaded['Content-Type'], XLSX)
