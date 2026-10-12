"""Files in a deal's chat: photos, documents, audio and video, sent from MotionPartes or received by WhatsApp (mall.whatsapp_bridge).

They are private to the deal: stored in the chat_media storage (never a public URL) and served only to the deal's parties, through
GET accounts/<account>/deals/<deal>/attachments/<id>/. A message takes up to CHAT_MEDIA_MAX_FILES files of CHAT_MEDIA_MAX_BYTES each
(CHAT_MEDIA_MAX_TOTAL together), of the TYPES below; it may carry files and no text. A voice note received by WhatsApp is transcribed by
Gemini (CHAT_TRANSCRIPTION; the platform AI budget counts it as chat_transcription), and its transcript shows under it and in the notices.
"""
import base64
import logging
import mimetypes
import os
import re
import unicodedata

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.base import ContentFile
from django.http import FileResponse, HttpResponse
from django.utils.http import content_disposition_header
from django.utils.text import get_valid_filename
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

log = logging.getLogger(__name__)

# What a chat takes, by MIME type: its kind and the extension a received file gets.
TYPES = {
    'image/jpeg': ('image', '.jpg'), 'image/png': ('image', '.png'), 'image/webp': ('image', '.webp'), 'image/gif': ('image', '.gif'),
    'application/pdf': ('document', '.pdf'),
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ('document', '.xlsx'), 'application/vnd.ms-excel': ('document', '.xls'),
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': ('document', '.docx'), 'application/msword': ('document', '.doc'),
    'text/csv': ('document', '.csv'), 'text/plain': ('document', '.txt'),
    'audio/ogg': ('audio', '.ogg'), 'audio/mpeg': ('audio', '.mp3'), 'audio/mp4': ('audio', '.m4a'), 'audio/x-m4a': ('audio', '.m4a'),
    'audio/aac': ('audio', '.aac'), 'audio/amr': ('audio', '.amr'), 'audio/webm': ('audio', '.webm'), 'audio/wav': ('audio', '.wav'),
    'video/mp4': ('video', '.mp4'), 'video/3gpp': ('video', '.3gp'), 'video/quicktime': ('video', '.mov'), 'video/webm': ('video', '.webm'),
}
# A file's type by its extension, when the browser declares none or a generic one (the server's own list may not know .xlsx).
EXTENSIONS = {**{extension: content_type for content_type, (_, extension) in TYPES.items()}, '.jpeg': 'image/jpeg', '.opus': 'audio/ogg',
              '.oga': 'audio/ogg', '.m4v': 'video/mp4'}
CHAT_MEDIA_MAX_TOTAL = 32 * 1024 * 1024
# Opened in the browser; anything else downloads.
INLINE = ('image/', 'audio/', 'video/', 'application/pdf')
NAMES = {'image': 'foto', 'audio': 'audio', 'video': 'video', 'document': 'documento'}
TRANSCRIBE_MAX = 8 * 1024 * 1024
TRANSCRIBE_PROMPT = ('Transcribe esta nota de voz de WhatsApp tal como se dice, en su idioma original (normalmente español de Panamá). '
                     'Devuelve solo la transcripción, sin comillas, títulos ni comentarios. Si no se entiende ninguna palabra, no devuelvas nada.')


def plain_type(content_type):
    return (content_type or '').split(';')[0].strip().lower()


def display_name(name, fallback):
    """The name people see: the file's own (no folders, no control characters), or a plain one."""
    name = ''.join(char for char in unicodedata.normalize('NFC', os.path.basename(name or '')) if unicodedata.category(char)[0] != 'C').strip()
    return (name or fallback)[-120:]


def classify(name, content_type):
    """(kind, content type) of a file a person sends, or None when the chat does not take it: the declared type, or the one of its
    extension when the browser declared none or a generic one."""
    declared = plain_type(content_type)
    if declared in TYPES:
        return TYPES[declared][0], declared
    guessed = EXTENSIONS.get(os.path.splitext(name or '')[1].lower())
    return (TYPES[guessed][0], guessed) if guessed else None


def validate_uploads(files):
    """The files of one message, checked: [(upload, kind, content type, name)], or a refusal people can act on."""
    if len(files) > settings.CHAT_MEDIA_MAX_FILES:
        raise ValidationError({'files': [f'Envía hasta {settings.CHAT_MEDIA_MAX_FILES} archivos por mensaje.']})
    checked, total = [], 0
    for upload in files:
        name = display_name(upload.name, 'archivo')
        found = classify(name, upload.content_type)
        if not found:
            raise ValidationError({'files': [f'«{name}» no se puede enviar. Envía fotos, PDF, Excel, Word, audio o video.']})
        if upload.size > settings.CHAT_MEDIA_MAX_BYTES:
            raise ValidationError({'files': [f'«{name}» pesa más de {settings.CHAT_MEDIA_MAX_BYTES // (1024 * 1024)} MB.']})
        total += upload.size
        checked.append((upload, *found, name))
    if total > CHAT_MEDIA_MAX_TOTAL:
        raise ValidationError({'files': [f'Los archivos de un mensaje pueden pesar hasta {CHAT_MEDIA_MAX_TOTAL // (1024 * 1024)} MB en total.']})
    return checked


def store(message, kind, content_type, name, content, **fields):
    """One file of the message, saved in the private storage under a folder of its own."""
    from .models import DealAttachment
    attachment = DealAttachment(message=message, kind=kind, content_type=content_type, name=name, size=len(content) if content else 0, **fields)
    if content:
        keep(attachment, content)
    attachment.save()
    return attachment


def keep(attachment, content):
    """The file's content into the private storage (the attachment is saved by the caller)."""
    stem, extension = os.path.splitext(attachment.name)
    try:
        stored = get_valid_filename(f'{stem[:80]}{extension[:10]}')
    except SuspiciousFileOperation:  # nothing usable in the name: the storage keeps a plain one (people still see theirs)
        stored = f'{NAMES[attachment.kind]}{get_valid_filename(extension[:10]) if extension[1:].isalnum() else ""}'
    attachment.file.save(stored, ContentFile(content), save=False)
    attachment.size = len(content)


def received(kind, content_type, filename, day):
    """(kind, content type, name) of a file that came by WhatsApp: types the chat does not know are kept as plain downloads."""
    content_type = plain_type(content_type)
    known = TYPES.get(content_type)
    if known:
        kind, extension = known
    else:
        kind = kind if kind in NAMES else 'document'
        extension = mimetypes.guess_extension(content_type) or ''
        content_type = content_type if kind != 'document' and content_type.startswith(f'{kind}/') else 'application/octet-stream'
    return kind, content_type, display_name(filename, f'{NAMES[kind]}-{day:%Y%m%d-%H%M%S}{extension}')


def describe(message):
    """The message's text, or for one with only files what they are, for previews and notices: «📷 Foto», a voice note's words…"""
    if message.body:
        return message.body
    files = list(message.attachments.all())
    if not files:
        return ''
    first = files[0]
    if first.kind == 'audio' and first.voice:
        text = f'🎤 Nota de voz: «{first.transcript}»' if first.transcript else '🎤 Nota de voz'
    else:
        text = {'image': '📷 Foto', 'video': '🎬 Video', 'audio': '🎵 Audio'}.get(first.kind) or f'📄 {first.name}'
    more = len(files) - 1
    return f'{text} y {more} archivo{"s" if more > 1 else ""} más' if more else text


def attachment_data(attachment):
    return {'id': attachment.pk, 'kind': attachment.kind, 'name': attachment.name, 'content_type': attachment.content_type,
            'size': attachment.size, 'voice': attachment.voice, 'failed': attachment.failed, 'transcript': attachment.transcript,
            'retryable': attachment.failed and bool(attachment.whatsapp_media_id)}


def transcribe(content, content_type):
    """A voice note's words, by Gemini: '' when transcription is off, the platform AI budget is spent, the note is too long, or the
    provider fails (the note stays playable)."""
    from .ai_budget import estimate_cost, micro_usd, month_spend
    from .catalog_classification import generate_json, provider_configuration
    if not settings.CHAT_TRANSCRIPTION or not content or len(content) > TRANSCRIBE_MAX or not provider_configuration()[0]:
        return ''
    # About 32 audio tokens a second, and a voice note (Opus) takes about 2 KB a second.
    if month_spend() + estimate_cost(len(content) // 16, 1500) > micro_usd(settings.AI_MONTHLY_BUDGET_USD):
        return ''
    payload = {'contents': [{'role': 'user', 'parts': [
        {'inline_data': {'mime_type': plain_type(content_type), 'data': base64.b64encode(content).decode()}}, {'text': TRANSCRIBE_PROMPT}]}],
        'generationConfig': {'temperature': 0, 'maxOutputTokens': 1500}}
    try:
        text = generate_json(payload, 1, feature='chat_transcription', raw_text=True)['text']
    except Exception as error:  # never the note's words in the log
        log.warning('Voice note transcription failed: %s', type(error).__name__)
        return ''
    return re.sub(r'\s+', ' ', text).strip()[:4000]


def byte_range(header, size):
    """(start, end) of a single 'bytes=' range within size, 'unsatisfiable' past its end, or None for the whole file."""
    found = re.fullmatch(r'bytes=(\d*)-(\d*)', header.strip())
    if not found or not size or found.groups() == ('', ''):
        return None
    first, last = found.groups()
    if first == '':  # the last N bytes
        return max(0, size - int(last)), size - 1
    start, end = int(first), min(int(last) if last else size - 1, size - 1)
    return 'unsatisfiable' if start >= size or start > end else (start, end)


class DealAttachmentFile(APIView):
    """One file of a deal's chat, to its parties only."""

    def perform_content_negotiation(self, request, force=False):  # an <img> or <audio> asks for its own type, not JSON
        return super().perform_content_negotiation(request, force=True)

    @extend_schema(responses={(200, '*/*'): OpenApiTypes.BINARY}, description='Un archivo del chat de la orden, solo para sus partes: fotos, '
                   'audio, video y PDF se abren en el navegador; lo demás se descarga.')
    def get(self, request, account_id, pk, attachment_id):
        from .deal_views import participant
        from .models import DealAttachment
        _, row = participant(request.user, account_id, pk)
        attachment = DealAttachment.objects.filter(pk=attachment_id, message__order=row, failed=False).exclude(file='').first()
        if attachment is None:
            raise NotFound('Este archivo no está disponible.')
        inline = attachment.content_type.startswith(INLINE)
        part = byte_range(request.headers.get('Range', ''), attachment.size)
        if part == 'unsatisfiable':
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{attachment.size}'
            return response
        if part:  # Safari plays audio and video only by ranges
            start, end = part
            with attachment.file.open('rb') as handle:
                handle.seek(start)
                response = HttpResponse(handle.read(end - start + 1), status=206, content_type=attachment.content_type)
            response['Content-Range'] = f'bytes {start}-{end}/{attachment.size}'
            response['Content-Disposition'] = content_disposition_header(not inline, attachment.name)
        else:
            response = FileResponse(attachment.file.open('rb'), content_type=attachment.content_type, as_attachment=not inline, filename=attachment.name)
        response['Accept-Ranges'] = 'bytes'
        response['X-Content-Type-Options'] = 'nosniff'
        if attachment.content_type != 'application/pdf':  # Chrome refuses to show a PDF in a sandbox
            response['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox"
        response['Cache-Control'] = 'private, max-age=86400'
        return response


class NotReceivedAgain(APIException):
    status_code = 502
    default_detail = 'WhatsApp no entregó el archivo. Inténtalo más tarde o pide que lo envíen de nuevo.'


class DealAttachmentRetry(APIView):
    """Reintentar: a file received by WhatsApp that could not be stored, asked of Meta again (an expired token, a network failure)."""

    @extend_schema(request=None, responses=OpenApiTypes.OBJECT, description='Pide de nuevo a WhatsApp un archivo que no se pudo recibir; '
                   'cualquiera de las partes puede hacerlo. Responde el mensaje con el archivo ya disponible (y una nota de voz transcrita).')
    def post(self, request, account_id, pk, attachment_id):
        from django.db import transaction
        from .deal_views import message_data, participant
        from .models import DealAttachment
        from .whatsapp_bridge import TooLarge, fetch_media
        _, row = participant(request.user, account_id, pk)
        with transaction.atomic():  # one retry at a time
            attachment = (DealAttachment.objects.select_for_update().filter(pk=attachment_id, message__order=row, failed=True)
                          .exclude(whatsapp_media_id='').first())
            if attachment is None:
                raise NotFound('Este archivo no necesita reintentarse.')
            try:
                content, _ = fetch_media(attachment.whatsapp_media_id)
            except TooLarge:
                raise NotReceivedAgain(f'El archivo pesa más de {settings.CHAT_MEDIA_MAX_BYTES // (1024 * 1024)} MB; pide que lo envíen por otro medio.')
            except Exception as error:
                log.warning('WhatsApp file %s not received again: %s', attachment.pk, type(error).__name__)
                raise NotReceivedAgain()
            keep(attachment, content)
            attachment.failed = False
            if attachment.voice:
                attachment.transcript = transcribe(content, attachment.content_type)
            attachment.save()
        return Response(message_data(attachment.message))
