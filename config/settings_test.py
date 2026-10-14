"""Settings for the test suite: python manage.py test --settings=config.settings_test.

Every outside service is off whatever the container's environment holds (Resend, Gemini, WhatsApp, web push; media on the local disk
instead of Spaces), and config.network_guard refuses any connection to a public address. TEST_DB_NAME names the test database
(test_partsmall_isolated by default): --noinput drops an existing database of that name, so each concurrent run needs its own. The
refactor's isolated runs import this module from a per-run copy that only sets the name (plan §1.4).

settings.py refuses a partial Spaces configuration, so blank DO_SPACES_BUCKET along with DO_SPACES_KEY and DO_SPACES_SECRET when the
environment is blanked (docker exec -e DO_SPACES_KEY= -e DO_SPACES_SECRET= -e DO_SPACES_BUCKET= ...), or blank none of them.
"""
import os

from .network_guard import install
from .settings import *  # noqa: F401,F403
from .settings import STORAGES

DATABASES['default']['TEST'] = {'NAME': os.getenv('TEST_DB_NAME', 'test_partsmall_isolated')}  # noqa: F405
RESEND_API_KEY = GEMINI_API_KEY = WHATSAPP_ACCESS_TOKEN = VAPID_PRIVATE_KEY = ''
DO_SPACES_KEY = DO_SPACES_SECRET = ''
CHAT_MEDIA_STORAGE = 'local'
STORAGES = {**STORAGES,
            'catalog_media': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'chat_media': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': CHAT_MEDIA_ROOT, 'base_url': None}}}  # noqa: F405

install()
