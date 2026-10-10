"""Encryption of the apiag-cloud access tokens that admins save in Cuentas (mall.apiag_connection).

The keys are APIAG_TOKEN_KEYS, comma-separated Fernet keys: the first one encrypts and all of them decrypt. To rotate, put the new
key first (APIAG_TOKEN_KEYS=new,old), run `python -m mall.apiag_keys rotate`, then drop the old key. `python -m mall.apiag_keys`
prints a key. Local DEBUG runs without keys fall back to one derived from DJANGO_SECRET_KEY. Responses carry the last four
characters of a token, never the token."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from rest_framework import serializers, status
from rest_framework.exceptions import APIException


class TokenKeyMissing(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = 'El servidor no tiene configurada la clave para guardar tokens de apiag-cloud (APIAG_TOKEN_KEYS).'


class TokenUnreadable(Exception):
    """The stored token was encrypted with a key that APIAG_TOKEN_KEYS no longer holds: an admin has to enter it again."""


def cipher():
    keys = list(settings.APIAG_TOKEN_KEYS)
    if not keys and settings.DEBUG:
        keys = [base64.urlsafe_b64encode(hashlib.sha256(b'motionpartes.apiag-token.' + settings.SECRET_KEY.encode()).digest())]
    if not keys:
        raise TokenKeyMissing()
    try:
        return MultiFernet([Fernet(key) for key in keys])
    except (TypeError, ValueError):
        raise TokenKeyMissing('La clave APIAG_TOKEN_KEYS del servidor no es válida. Genera una con python -m mall.apiag_keys.')


def encrypt(plain):
    return cipher().encrypt(plain.encode()).decode()


def token(connection):
    """The plain token, for the calls MotionPartes makes to the supplier's apiag-cloud."""
    try:
        return cipher().decrypt(connection.token_ciphertext.encode()).decode()
    except (InvalidToken, TokenKeyMissing):
        raise TokenUnreadable()


def readable(connection):
    try:
        token(connection)
    except TokenUnreadable:
        return False
    return True


def rotate(connection):
    """Re-encrypts the stored token with the first key, so the older keys can be dropped. Raises TokenUnreadable."""
    try:
        connection.token_ciphertext = cipher().rotate(connection.token_ciphertext.encode()).decode()
    except (InvalidToken, TokenKeyMissing):
        raise TokenUnreadable()


def rotate_all():
    """Re-encrypts every stored token with the first key. Returns how many moved and the accounts whose token no key reads."""
    from .apiag_models import ApiagConnection
    moved, unreadable = 0, []
    for connection in ApiagConnection.objects.select_related('account').order_by('account__name', 'pk'):
        try:
            rotate(connection)
        except TokenUnreadable:
            unreadable.append(connection.account.name)
            continue
        ApiagConnection.objects.filter(pk=connection.pk).update(token_ciphertext=connection.token_ciphertext)
        moved += 1
    return moved, unreadable


class ConnectionData(serializers.Serializer):
    server_url = serializers.CharField()
    token_hint = serializers.CharField(help_text='Los últimos cuatro caracteres del token; el token nunca sale del servidor.')
    token_readable = serializers.BooleanField(help_text='Falso cuando ninguna clave de APIAG_TOKEN_KEYS lee el token guardado.')
    updated_at = serializers.DateTimeField()
    updated_by = serializers.CharField(allow_null=True)


def connection_data(connection):
    if connection is None:
        return None
    return ConnectionData({'server_url': connection.server_url, 'token_hint': connection.token_hint, 'token_readable': readable(connection),
                           'updated_at': connection.updated_at, 'updated_by': connection.updated_by.username if connection.updated_by else None}).data
