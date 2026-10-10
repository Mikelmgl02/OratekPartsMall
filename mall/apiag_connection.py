"""A supplier account's connection to its own apiag-cloud server, set by MotionPartes admins in Cuentas: the base URL of its API and
the access token that apiag-cloud issued for MotionPartes (encrypted by mall.apiag_tokens)."""
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.views.decorators.debug import sensitive_variables
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .apiag_models import ApiagConnection
from .apiag_tokens import ConnectionData, connection_data, encrypt, readable, rotate
from .management import IsSuperuser
from .models import Account

TOKEN = re.compile(r'[\x21-\x7e]{16,512}')
HOST = re.compile(r'[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*')


def refused(text):
    return ValidationError({'server_url': [text]})


def clean_server_url(value):
    """The base URL as saved: scheme, lowercase host (IDNA-encoded) and port, and the path without a trailing slash."""
    value = value.strip()
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise refused('La dirección del servidor no es válida.')
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise refused('Escribe la dirección completa del servidor, por ejemplo https://empresa.apiag.cloud.')
    if '@' in parts.netloc:
        raise refused('La dirección no debe incluir usuario ni contraseña: el token va en su propio campo.')
    if parts.query or parts.fragment:
        raise refused('La dirección no debe incluir parámetros (?…) ni anclas (#…).')
    if re.search(r'[\s\\]', value) or port == 0:
        raise refused('La dirección del servidor no es válida.')
    host = parts.hostname
    try:
        host = f'[{ipaddress.IPv6Address(host).compressed}]' if ':' in host else host.encode('idna').decode('ascii')
    except (ValueError, UnicodeError):
        raise refused('La dirección del servidor no es válida.')
    if not host.startswith('[') and not HOST.fullmatch(host):
        raise refused('La dirección del servidor no es válida.')
    if parts.scheme == 'http' and not settings.DEBUG:
        raise refused('Usa una dirección https:// para que el token viaje cifrado.')
    return urlunsplit((parts.scheme, host if port is None else f'{host}:{port}', parts.path.rstrip('/'), '', ''))


def origin(url):
    parts = urlsplit(url)
    return parts.scheme, parts.netloc


class ConnectionInput(serializers.Serializer):
    server_url = serializers.CharField(max_length=300)
    access_token = serializers.CharField(required=False, allow_blank=True, max_length=600,
                                         help_text='Vacío conserva el token guardado, salvo al cambiar de servidor.')

    def validate_access_token(self, value):
        if value and not TOKEN.fullmatch(value):
            raise serializers.ValidationError('Pega solo el token, sin «Token» ni «Bearer» delante: entre 16 y 512 caracteres, sin espacios.')
        return value


class ApiagConnectionView(APIView):
    """POST saves the account's connection; a blank token keeps the stored one (re-encrypted with the current key) unless the server
    changes, since a token belongs to the apiag-cloud that issued it. DELETE removes the connection."""
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_accounts_apiag_save', request=ConnectionInput, responses={200: ConnectionData, 201: ConnectionData})
    @sensitive_variables('new_token', 'plain', 'value')
    @transaction.atomic
    def post(self, request, pk):
        account = get_object_or_404(Account.objects.select_for_update(), pk=pk)
        if not account.roles.filter(capability='supplier').exists():
            raise ValidationError({'detail': 'Solo las cuentas de proveedor pueden conectarse con apiag-cloud.'})
        serializer = ConnectionInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        server_url = clean_server_url(serializer.validated_data['server_url'])
        new_token = serializer.validated_data.get('access_token', '')
        connection = ApiagConnection.objects.filter(account=account).first()
        if not new_token:
            if connection is None or not readable(connection):
                raise ValidationError({'access_token': ['Pega el token que generó el apiag-cloud del proveedor para MotionPartes.']})
            if origin(server_url) != origin(connection.server_url):
                raise ValidationError({'access_token': ['Cambiaste el servidor: pega el token que generó ese apiag-cloud.']})
        created = connection is None
        connection = connection or ApiagConnection(account=account)
        connection.server_url = server_url
        if new_token:
            connection.token_ciphertext = encrypt(new_token)
            connection.token_hint = new_token[-4:]
        else:
            rotate(connection)
        connection.updated_by = request.user
        connection.save()
        return Response(connection_data(connection), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @extend_schema(operation_id='v1_management_accounts_apiag_delete', responses={204: None})
    def delete(self, request, pk):
        account = get_object_or_404(Account, pk=pk)
        ApiagConnection.objects.filter(account=account).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
