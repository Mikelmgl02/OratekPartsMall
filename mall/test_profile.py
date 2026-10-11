from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .inbox_models import InboxCursor, PushSubscription
from .models import User

PASSWORD = 'SecureEmployee938!'


class ProfileTests(APITestCase):
    """Mi perfil: each person's own names, email and password; and what the admin sees of how they get notices."""

    def setUp(self):
        self.user = User.objects.create_user('empleado', 'empleado@example.com', PASSWORD, first_name='Ana')
        User.objects.create_user('otra', 'otra@example.com', 'SecureOther938!')
        self.root = User.objects.create_superuser('root', 'root@example.com', 'SecureRoot938!')
        self.client.force_authenticate(self.user)

    def patch(self, data):
        return self.client.patch('/api/v1/auth/me/', data, format='json')

    def test_people_change_their_names_freely_and_their_email_only_with_their_password(self):
        named = self.patch({'first_name': 'Ana María', 'last_name': 'Ruiz', 'username': 'otro', 'is_superuser': True})
        self.assertEqual([named.data[key] for key in ['first_name', 'last_name', 'username', 'is_superuser', 'email']],
                         ['Ana María', 'Ruiz', 'empleado', False, 'empleado@example.com'])  # the username and permissions stay
        self.assertEqual(self.patch({'email': 'nueva@example.com'}).status_code, 400)  # without the password
        self.assertEqual(self.patch({'email': 'nueva@example.com', 'current_password': 'otra cosa'}).status_code, 400)
        self.assertEqual(self.patch({'email': 'OTRA@example.com', 'current_password': PASSWORD}).status_code, 400)  # someone else's
        changed = self.patch({'email': ' Nueva@Example.com ', 'current_password': PASSWORD})
        self.assertEqual((changed.status_code, changed.data['email']), (200, 'nueva@example.com'))
        self.assertNotIn('current_password', changed.data)
        self.assertEqual(self.patch({'email': 'nueva@example.com', 'first_name': 'Ana'}).status_code, 200)  # the same email needs nothing

    def test_a_password_change_needs_the_current_one_and_signs_the_other_devices_out(self):
        old = Token.objects.create(user=self.user)
        url = '/api/v1/auth/password/'
        self.assertEqual(self.client.post(url, {'current_password': 'otra cosa', 'new_password': 'OtraClaveSegura938!'}, format='json').status_code, 400)
        weak = self.client.post(url, {'current_password': PASSWORD, 'new_password': '12345678'}, format='json')
        self.assertEqual(weak.status_code, 400)
        self.assertIn('new_password', weak.data)
        changed = self.client.post(url, {'current_password': PASSWORD, 'new_password': 'OtraClaveSegura938!'}, format='json')
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed.data['token'], old.key)
        self.assertFalse(Token.objects.filter(key=old.key).exists())  # every other session ends
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('OtraClaveSegura938!'))

    def test_the_admin_sees_how_a_person_gets_notices_without_changing_them(self):
        InboxCursor.objects.create(user=self.user, whatsapp_number='+50761234567', whatsapp_opt_in_at=timezone.now(), email_updates=False)
        PushSubscription.objects.create(user=self.user, endpoint='https://push.example.invalid/1', p256dh='clave', auth='auth')
        self.client.force_authenticate(self.root)
        url = f'/api/v1/management/users/{self.user.pk}/'
        notices = self.client.get(url).data['notices']
        self.assertEqual((notices['email']['updates'], notices['whatsapp']['number'], notices['push']['devices']), (False, '+50761234567', 1))
        self.assertIsNotNone(notices['whatsapp']['opted_in_at'])
        self.client.patch(url, {'first_name': 'Ana', 'notices': {'whatsapp': {'opted_in_at': None}}}, format='json')
        self.assertIsNotNone(InboxCursor.objects.get(user=self.user).whatsapp_opt_in_at)  # consent stays the person's
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(url).status_code, 403)
