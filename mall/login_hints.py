"""Who a notice link was sent to, so a signed-out visitor lands on the sign-in with their username filled in. WhatsApp and email links
carry ?conversacion=<deal>~<hint>: the hint is signed and opaque (it names nobody by itself, and cannot be forged or altered), and expires
after HINT_DAYS. The storefront asks GET /api/v1/auth/login-hint/?hint=… for the username; nothing else is revealed, and only an active
person's."""
from django.core import signing
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

SALT = 'motionpartes.notice-login-hint'
HINT_DAYS = 30


def hint(user_id):
    return signing.dumps(user_id, salt=SALT, compress=True)


def conversation(order_id, user_id):
    """The ?conversacion= value of a notice link for this person: the deal, then their hint."""
    return f'{order_id}~{hint(user_id)}'


def username(value):
    from .models import User
    try:
        user_id = signing.loads(value, salt=SALT, max_age=HINT_DAYS * 86400)
    except signing.BadSignature:  # also an expired one
        return None
    return User.objects.filter(pk=user_id, is_active=True).values_list('username', flat=True).first()


class LoginHint(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [AnonRateThrottle]

    @extend_schema(parameters=[OpenApiParameter('hint', OpenApiTypes.STR)], responses=OpenApiTypes.OBJECT,
                   description='El usuario al que se envió un aviso (la pista firmada de su enlace), para prellenar el inicio de sesión.')
    def get(self, request):
        name = username(request.query_params.get('hint', '')[:400])
        return Response({'username': name}) if name else Response({'detail': 'El enlace ya no es válido.'}, status=404)
