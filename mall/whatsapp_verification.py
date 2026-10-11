"""Proving a WhatsApp number is the person's before any notice goes there (mall.whatsapp). Saving a number (with consent) sends a 6-digit
code to it by WhatsApp; the number becomes the person's notices number only once they type the code back. Until then it waits as the
pending number, and an earlier verified number keeps working.

The code goes with the Authentication template WHATSAPP_TEMPLATE_CODE (Meta requires that category for codes; python -m
mall.whatsapp_templates creates it), or as plain text while that template cannot be used. Only its hash is kept; it is valid for CODE_TTL
and ATTEMPTS tries, and a new one can be asked for every RESEND_AFTER, PER_HOUR an hour at most. Every send is logged (WhatsAppMessage,
kind "verify")."""
import hashlib
import hmac
import logging
import secrets
import urllib.error
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import APIException, Throttled, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .inbox_models import InboxCursor, WhatsAppMessage

log = logging.getLogger(__name__)
KIND = 'verify'
CODE_TTL = timedelta(minutes=10)
ATTEMPTS = 5
RESEND_AFTER = timedelta(seconds=60)
PER_HOUR = 5
CLEARED = {'whatsapp_pending_number': '', 'whatsapp_code_hash': '', 'whatsapp_code_sent_at': None, 'whatsapp_code_attempts': 0}


class CodeNotSent(APIException):
    status_code = 502
    default_detail = 'No pudimos enviar el código por WhatsApp a ese número. Revisa que sea tu número de WhatsApp e inténtalo de nuevo.'


def code_hash(user_id, number, code):
    return hmac.new(settings.SECRET_KEY.encode(), f'{user_id}:{number}:{code}'.encode(), hashlib.sha256).hexdigest()


def code_template(number, code):
    return {'messaging_product': 'whatsapp', 'to': number.lstrip('+'), 'type': 'template', 'template': {
        'name': settings.WHATSAPP_TEMPLATE_CODE, 'language': {'code': settings.WHATSAPP_TEMPLATE_LANGUAGE}, 'components': [
            {'type': 'body', 'parameters': [{'type': 'text', 'text': code}]},
            {'type': 'button', 'sub_type': 'url', 'index': '0', 'parameters': [{'type': 'text', 'text': code}]}]}}


def code_text(number, code):
    return {'messaging_product': 'whatsapp', 'to': number.lstrip('+'), 'type': 'text', 'text': {
        'body': f'Tu código de verificación de MotionPartes es {code}. Vence en {int(CODE_TTL.total_seconds() // 60)} minutos. No lo compartas con nadie.'}}


def send(user, number, code):
    """The code to the number: the Authentication template, or plain text when Meta refuses the template as unusable. True if Meta took it."""
    from .whatsapp import TEMPLATE_UNUSABLE, graph, meta_code
    row = WhatsAppMessage.objects.create(user=user, number=number, template=settings.WHATSAPP_TEMPLATE_CODE, kind=KIND)
    note, template = '', settings.WHATSAPP_TEMPLATE_CODE
    try:
        try:
            result = graph(code_template(number, code))
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors='replace')
            reason = meta_code(detail)
            if reason not in TEMPLATE_UNUSABLE:
                raise RuntimeError(f'{error.code} {detail}')
            note, template = f'Plantilla no disponible ({reason}); enviado como texto', f'{settings.WHATSAPP_TEMPLATE_CODE} (texto)'[:80]
            result = graph(code_text(number, code))
    except urllib.error.HTTPError as error:
        refused = f'{error.code} {error.read().decode(errors="replace")}'
        WhatsAppMessage.objects.filter(pk=row.pk).update(status='failed', template=template, error=(f'{note}; texto: {refused}' if note else refused)[:300],
                                                         updated_at=timezone.now())
        log.warning('WhatsApp code to %s refused: %s', number, refused[:200])
        return False
    except Exception as error:  # Meta refused the template for another reason, or the network failed
        WhatsAppMessage.objects.filter(pk=row.pk).update(status='failed', error=str(error)[:300], updated_at=timezone.now())
        log.warning('WhatsApp code to %s failed: %s', number, str(error)[:200])
        return False
    WhatsAppMessage.objects.filter(pk=row.pk).update(status='accepted', wamid=(result.get('messages') or [{}])[0].get('id', ''), template=template,
                                                     error=note, updated_at=timezone.now())
    return True


def start(user, cursor, number):
    """A new code to this number (within the limits), which waits as the person's pending number."""
    now = timezone.now()
    recent = WhatsAppMessage.objects.filter(user=user, kind=KIND, created_at__gte=now - timedelta(hours=1))
    last = recent.order_by('-created_at').values_list('created_at', flat=True).first()
    if last and now - last < RESEND_AFTER:
        wait = int((RESEND_AFTER - (now - last)).total_seconds()) + 1
        raise Throttled(wait=wait, detail=f'Espera {wait} segundos para pedir otro código.')
    if recent.count() >= PER_HOUR:
        raise Throttled(detail='Ya pediste varios códigos. Inténtalo de nuevo en una hora.')
    code = f'{secrets.randbelow(10 ** 6):06d}'
    if not send(user, number, code):
        raise CodeNotSent()
    InboxCursor.objects.filter(pk=cursor.pk).update(whatsapp_pending_number=number, whatsapp_code_hash=code_hash(user.pk, number, code),
                                                    whatsapp_code_sent_at=now, whatsapp_code_attempts=0)


class CodeSerializer(serializers.Serializer):
    code = serializers.RegexField(r'^\s*\d{6}\s*$', error_messages={'invalid': 'Escribe los 6 dígitos del código.'})


class InboxWhatsAppVerify(APIView):
    @extend_schema(request=CodeSerializer, responses=OpenApiTypes.OBJECT, description='Confirma el número pendiente con el código que se le '
                   f'envió por WhatsApp: vence a los {int(CODE_TTL.total_seconds() // 60)} minutos y admite {ATTEMPTS} intentos. Con el código '
                   'correcto el número queda verificado y los avisos activos.')
    def post(self, request):
        from .inbox import cursor_for
        from .whatsapp import preferences
        data = CodeSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        code, now = data.validated_data['code'].strip(), timezone.now()
        refusal = None  # raised after the transaction, so a wrong attempt stays counted
        with transaction.atomic():
            cursor = InboxCursor.objects.select_for_update().get(pk=cursor_for(request.user).pk)
            pending = cursor.whatsapp_pending_number
            if not pending or not cursor.whatsapp_code_hash:
                refusal = 'Pide un código primero.'
            elif now - cursor.whatsapp_code_sent_at > CODE_TTL:
                InboxCursor.objects.filter(pk=cursor.pk).update(whatsapp_code_hash='')
                refusal = 'El código venció. Pide uno nuevo.'
            elif not hmac.compare_digest(code_hash(request.user.pk, pending, code), cursor.whatsapp_code_hash):
                attempts = cursor.whatsapp_code_attempts + 1
                left = ATTEMPTS - attempts
                if left <= 0:
                    InboxCursor.objects.filter(pk=cursor.pk).update(whatsapp_code_hash='', whatsapp_code_attempts=attempts)
                    refusal = 'Demasiados intentos. Pide un código nuevo.'
                else:
                    InboxCursor.objects.filter(pk=cursor.pk).update(whatsapp_code_attempts=attempts)
                    refusal = f'El código no es correcto. Te {"queda 1 intento" if left == 1 else f"quedan {left} intentos"}.'
            else:
                InboxCursor.objects.filter(pk=cursor.pk).update(whatsapp_number=pending, whatsapp_verified_at=now, whatsapp_opt_in_at=now, **CLEARED)
            cursor.refresh_from_db()
        if refusal:
            raise ValidationError({'code': [refusal]})
        return Response(preferences(cursor))
