"""Settings for the test suite: python manage.py test --settings=config.settings_test.

Every outside service is off whatever the container's environment holds: their variables are blanked before settings.py reads them
(config.network_guard.OUTSIDE_SERVICES: Resend, Gemini, WhatsApp, web push, Spaces and Redis), so media stay on the local disk, no
channel layer publishes to the live inbox groups, and no -e ... list is needed. config.network_guard refuses any connection to a public
address. TEST_DB_NAME names the test database (test_partsmall_isolated by default): --noinput drops an existing database of that name,
so each concurrent run needs its own, and config.test_runner refuses a name that does not start with test_ or is the real database's.
The refactor's isolated runs import this module from a per-run copy that only sets the name (plan §1.4).
"""
import os

from .network_guard import blank_outside_services, install

blank_outside_services()

from .settings import *  # noqa: E402,F401,F403
from .settings import STORAGES  # noqa: E402

DATABASES['default']['TEST'] = {'NAME': os.getenv('TEST_DB_NAME', 'test_partsmall_isolated')}  # noqa: F405
TEST_RUNNER = 'config.test_runner.GuardedTestRunner'
RESEND_API_KEY = GEMINI_API_KEY = WHATSAPP_ACCESS_TOKEN = VAPID_PRIVATE_KEY = ''
DO_SPACES_KEY = DO_SPACES_SECRET = DO_SPACES_BUCKET = ''
REDIS_URL, CHANNEL_LAYERS = '', {}  # test_realtime brings its own in-memory layer
CHAT_MEDIA_STORAGE = 'local'
# A test's transaction never commits, so the Search Documents (the storefront search's only source from S10) are written as each
# writer refreshes them instead of on commit (mall.search_document.refresh); the tests of the commit hook turn it off.
SEARCH_DOCUMENT_SYNC = True
STORAGES = {**STORAGES,
            'catalog_media': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'chat_media': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': CHAT_MEDIA_ROOT, 'base_url': None}}}  # noqa: F405

install()
