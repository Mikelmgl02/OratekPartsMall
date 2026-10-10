"""ASGI entry point of the realtime service (daphne, docker-compose's realtime): the deal inbox's websocket at /ws/inbox/. HTTP stays
on gunicorn (config.wsgi)."""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django_application = get_asgi_application()  # sets Django up before the consumers import models

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402
from django.urls import path  # noqa: E402

from mall.realtime import InboxSocket  # noqa: E402

application = ProtocolTypeRouter({
    'http': django_application,
    # Only pages of the site (an Origin among ALLOWED_HOSTS) may open it.
    'websocket': AllowedHostsOriginValidator(URLRouter([path('ws/inbox/', InboxSocket.as_asgi())])),
})
