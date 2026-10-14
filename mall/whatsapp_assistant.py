"""MotionPartes' shopping assistant on WhatsApp (decided 2026-10-11: it only fills the cesta; the client sends the solicitud on the site).

What a verified number writes that does not continue an order's chat (mall.whatsapp_bridge.inbound decides) comes here: a text, a voice
note (transcribed) or a photo of the part or its label. Gemini only reads it (interpret: what the person wants, catalog queries in the
catalog's own words, the option or quantity they chose); everything the assistant shows comes from the database: the catalog's SKUs with
suppliers in stock first, those suppliers' articles (never the account's own), and the person's cesta (mall.basket), where a chosen
article goes with source "whatsapp". No prices or stock figures, as in the catalog. Lists and buttons (WhatsApp interactive messages) let
people tap instead of writing, and a tap needs no AI. The person shops for one of their client accounts (asked once when they have several);
an account's own number shops for that account, signed by whoever confirmed it. Each reply is a service message (the person just wrote, so
their 24-hour window is open), logged as kind "assistant", which also tells the bridge the conversation is with the assistant now.

On with Gemini configured and WHATSAPP_ASSISTANT_MONTHLY_USD above 0: its spend is recorded as whatsapp_assistant, within that cap and the
platform's. Off, messages go where they went before (the order last notified, or a pointer); with its budget spent it says so.
"""
import base64
import json
import logging
import re
import unicodedata
import urllib.error
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .assistant_models import WhatsAppAssistantSession
from .inbox_models import WhatsAppMessage
from .whatsapp import accepted, graph

log = logging.getLogger(__name__)
IDLE = timedelta(hours=6)  # after a pause this long a conversation starts over
PER_HOUR = 40  # messages a number may send the assistant in an hour
LIST_ROWS = 10  # WhatsApp lists take 10 rows
SEARCH_LIMIT = 60
HISTORY = 8
QUANTITIES = range(1, 11)  # the quantity selector's rows; any other is typed

PROMPT = """Eres el asistente de compras de MotionPartes por WhatsApp, un mercado de repuestos de autos en Panamá. Ayudas a un cliente
a encontrar repuestos y a agregarlos a su cesta de solicitudes de cotización. No conoces precios, existencias ni plazos: el sistema busca
en el catálogo y muestra lo que hay. Lee el mensaje del cliente (texto, transcripción de su nota de voz o foto del repuesto o su etiqueta)
con el contexto y decide UNA acción:
- buscar: quiere un repuesto. En consultas escribe hasta 4 búsquedas para el catálogo, de la más precisa a la más amplia, en MAYÚSCULAS,
  de 2 a 4 palabras cada una: el tipo de repuesto y el modelo, sin año, sin artículos ni relleno; cada palabra debe aparecer en la
  descripción del repuesto. El catálogo abrevia: TOY Toyota, NIS Nissan, HYU Hyundai, MIT Mitsubishi, HON Honda, SUZ Suzuki, MAZ Mazda,
  ISU Isuzu, CHEV Chevrolet; DEL delantero, TRAS trasero, INF inferior, SUP superior, EXT exterior, INT interior, RH derecho, LH izquierdo;
  AMORT amortiguador, TERM terminal, ESTAB estabilizador, MANG manguera, VALV válvula, PAST pastilla, BANDA zapata o banda de freno.
  Ejemplo: «disco de freno delantero para hilux 2016» -> ["DISCO FRENO DEL HILUX", "DISCO FRENO HILUX", "DISCO HILUX"]. En codigos copia
  los números de parte, códigos OEM o de fabricante que el cliente escriba, diga o se vean en la foto. Si dice cuántas unidades, ponlas en
  cantidad.
- elegir: elige una de las opciones numeradas que se le mostraron (opcion = su número).
- cantidad: dice cuántas unidades quiere (cantidad).
- ver_cesta: quiere ver su cesta. quitar: quiere quitar una línea de su cesta (opcion = su número en la cesta). cambiar: quiere otra
  cantidad para una línea de su cesta, sin quitarla (opcion = su número en la cesta, cantidad = la nueva cantidad total), por ejemplo
  «el 1 a 8» o «solo quería 8 del primero». Con un solo artículo en la cesta, opcion puede ser 0.
- cuenta: quiere comprar para otra de sus cuentas.
- pedidos: quiere ver sus pedidos, solicitudes u órdenes enviadas, su estado o sus cotizaciones («muéstrame mis pedidos», «¿cómo va mi
  orden?»). Confirmar que recibió una orden se hace en MotionPartes, no aquí: si lo pide, también es pedidos.
- orden: quiere que su mensaje le llegue al proveedor de una orden ya enviada (por ejemplo «dile que lo necesito el lunes», «¿me lo pueden
  entregar mañana?»). Las preguntas al asistente sobre sus pedidos son pedidos, no orden.
- responder: un saludo, una pregunta sobre cómo funciona o algo que no es ninguna de las anteriores. En respuesta escribe una contestación
  breve y amable en español (máximo 3 frases): qué puede hacer el asistente y cómo pedirlo. El asistente solo busca repuestos y
  agrega, cambia o quita artículos de la cesta; si pide otra cosa, dilo y sugiere hacerlo en MotionPartes. Nunca propongas pasos que no
  existan.
Nunca inventes repuestos, precios, existencias, plazos ni datos de pedidos. Usa 0 en opcion o cantidad cuando no aplique."""

SCHEMA = {'type': 'object', 'properties': {
    'accion': {'type': 'string', 'enum': ['buscar', 'elegir', 'cantidad', 'ver_cesta', 'quitar', 'cambiar', 'cuenta', 'pedidos', 'orden', 'responder']},
    'consultas': {'type': 'array', 'items': {'type': 'string'}},
    'codigos': {'type': 'array', 'items': {'type': 'string'}},
    'opcion': {'type': 'integer'},
    'cantidad': {'type': 'integer'},
    'respuesta': {'type': 'string'}}, 'required': ['accion']}


def enabled():
    from .catalog_classification import provider_configuration
    try:
        return bool(provider_configuration()[0]) and Decimal(settings.WHATSAPP_ASSISTANT_MONTHLY_USD) > 0
    except Exception:
        return False


def site():
    return settings.SITE_URL.rstrip('/')


# WhatsApp payloads (without "messaging_product" and "to", which send() adds).
def text(body):
    return {'type': 'text', 'text': {'body': body[:4096], 'preview_url': False}}


def buttons(body, choices):
    return {'type': 'interactive', 'interactive': {'type': 'button', 'body': {'text': body[:1024]}, 'action': {'buttons': [
        {'type': 'reply', 'reply': {'id': identifier, 'title': title[:20]}} for identifier, title in choices[:3]]}}}


def choice_list(body, button, rows):
    return {'type': 'interactive', 'interactive': {'type': 'list', 'body': {'text': body[:1024]}, 'action': {'button': button[:20], 'sections': [
        {'title': 'Opciones', 'rows': [{'id': identifier[:200], 'title': title[:24], **({'description': description[:72]} if description else {})}
                                       for identifier, title, description in rows[:LIST_ROWS]]}]}}}


def send(number, user_id, payload):
    """One assistant message, logged (kind "assistant"; with the order it speaks of, when it passed a message there, so the conversation
    goes on with that order); Meta's refusal is logged, never raised."""
    payload = dict(payload)
    order_id, thread_id = payload.pop('_order', None), payload.pop('_thread', None)
    kind = payload.pop('_kind', 'assistant')  # "ask": which conversation a message is for (the bridge)
    row = WhatsAppMessage.objects.create(user_id=user_id, number=number, template='asistente', kind=kind, order_id=order_id, thread_id=thread_id)
    try:
        accepted(row.pk, graph({'messaging_product': 'whatsapp', 'to': number.lstrip('+'), **payload}))
    except Exception as error:
        detail = error.read().decode(errors='replace') if isinstance(error, urllib.error.HTTPError) else str(error)
        WhatsAppMessage.objects.filter(pk=row.pk).update(status='failed', error=detail[:300], updated_at=timezone.now())
        log.warning('WhatsApp assistant message %s refused: %s', row.pk, detail[:200])


def shopper(people, accounts):
    """(user, client accounts) the assistant shops for: a person's client accounts, or an account's own number (that account, by whoever
    confirmed it, while still a member). (None, []) when there is none."""
    from .models import Account, Membership
    if people:
        user = people[0].user
        return user, list(Account.objects.filter(memberships__user=user, active=True, roles__capability='client').distinct().order_by('name', 'id'))
    holder = accounts[0]
    user, account = holder.whatsapp_verified_by, holder.account
    if (user and user.is_active and Membership.objects.filter(user=user, account=account).exists()
            and account.roles.filter(capability='client').exists()):
        return user, [account]
    return None, []


class Turn:
    """One message being answered: the conversation (locked), who shops, and the replies to send once it is saved."""

    def __init__(self, session, user, accounts, number, people, holders, message, now):
        self.session, self.user, self.accounts, self.number, self.now = session, user, accounts, number, now
        self.people, self.holders, self.message = people, holders, message
        self.replies, self.said = [], ''

    def say(self, payload, summary=None):
        self.replies.append(payload)
        self.said = summary or (payload.get('text') or {}).get('body') or payload['interactive']['body']['text']

    @property
    def account(self):
        return self.session.account


def handle(number, people, holders, message, now):
    """One message for the assistant: in the background (it may download a file and ask Gemini), one at a time per number."""
    user, accounts = shopper(people, holders)
    if user is None:
        send(number, None, text('Tu número no está ligado a una cuenta que pueda solicitar cotizaciones en MotionPartes. '
                                f'Pídele acceso a tu empresa o escríbenos desde el sitio: {site()}/'))
        return
    if WhatsAppMessage.objects.filter(number=number, kind='assistant', status='received', created_at__gte=now - timedelta(hours=1)).count() > PER_HOUR:
        send(number, user.pk, text(f'Recibí muchos mensajes seguidos. Espera unos minutos o continúa en MotionPartes: {site()}/'))
        return
    with transaction.atomic():
        session, _ = WhatsAppAssistantSession.objects.select_for_update().get_or_create(number=number)
        if now - session.updated_at > IDLE:
            session.step, session.options, session.pending, session.history = 'idle', [], {}, []
        session.user = user
        if session.account_id not in {account.pk for account in accounts}:
            session.account = accounts[0] if len(accounts) == 1 else None
        turn = Turn(session, user, accounts, number, people, holders, message, now)
        words = ''
        try:
            words = answer(turn)
        except Exception:
            log.exception('WhatsApp assistant failed on a message from %s', number)
            turn.replies = []
            turn.say(text(f'No pude atender tu mensaje. Inténtalo de nuevo o continúa en MotionPartes: {site()}/'))
        session.history = (session.history + [{'rol': 'cliente', 'texto': (words or '[sin texto]')[:300]},
                                              {'rol': 'asistente', 'texto': turn.said[:300]}])[-HISTORY:]
        session.save()
    for payload in turn.replies:
        send(number, user.pk, payload)


def answer(turn):
    """Answers the message (filling turn.replies); what the person said, for the history."""
    message = turn.message
    kind = message.get('type', 'text')
    if kind == 'interactive':  # a tap on a list or a button the assistant sent
        reply = message.get('interactive') or {}
        chosen = (reply.get('list_reply') or reply.get('button_reply') or {})
        tap(turn, chosen.get('id', ''))
        return chosen.get('title', '')
    words, image = read(turn, message)
    if words is None and image is None:
        turn.say(text('Puedo ayudarte con mensajes de texto, notas de voz o fotos del repuesto o de su etiqueta. ¿Qué repuesto buscas?'))
        return '[archivo]'
    if turn.account is None:  # several client accounts: which one first; a written request waits for the choice
        turn.session.pending = {'texto': words} if words else {}
        ask_account(turn, 'Antes de buscar: ¿para cuál de tus cuentas es la compra?' + ('' if words else ' Luego envíame la foto otra vez.'))
        return words or '[foto]'
    understand(turn, words, image)
    return words or '[foto]'


def read(turn, message):
    """(words, image) of a message: its text, a voice note's transcript, or a photo (bytes, type) with its caption."""
    from .deal_attachments import transcribe
    from .whatsapp_bridge import fetch_media
    kind = message.get('type', 'text')
    if kind == 'text':
        return ((message.get('text') or {}).get('body') or '').strip()[:1000], None
    if kind == 'button':  # a template's quick reply
        return ((message.get('button') or {}).get('text') or '').strip()[:200], None
    media = message.get(kind) if isinstance(message.get(kind), dict) else {}
    if kind == 'image' and media.get('id'):
        try:
            content, content_type = fetch_media(media['id'])
        except Exception as error:
            log.warning('WhatsApp assistant photo not received: %s', type(error).__name__)
            return (media.get('caption') or '').strip() or None, None
        return (media.get('caption') or '').strip(), (content, (content_type or 'image/jpeg').split(';')[0].strip())
    if kind == 'audio' and media.get('id'):
        try:
            content, content_type = fetch_media(media['id'])
        except Exception as error:
            log.warning('WhatsApp assistant voice note not received: %s', type(error).__name__)
            return None, None
        return transcribe(content, content_type) or None, None
    return None, None


def understand(turn, words, image):
    """A message in the person's own words: a quantity or «cesta» without AI, anything else as Gemini reads it."""
    session = turn.session
    plain = unicodedata.normalize('NFKD', words or '').encode('ascii', 'ignore').decode().upper().strip(' .!¡?¿')
    if session.step == 'quantity' and re.fullmatch(r'\d{1,4}', plain):
        add(turn, int(plain))
        return
    if plain in ('CESTA', 'MI CESTA', 'VER CESTA', 'VER MI CESTA'):
        show_cart(turn)
        return
    decision = interpret(turn, words, image)
    if decision is None:
        return
    action = decision.get('accion')
    quantity = decision.get('cantidad') or 0
    if action == 'buscar':
        if quantity > 0:
            session.pending = {'cantidad': quantity}
        search(turn, decision.get('consultas') or [], decision.get('codigos') or [], words)
    elif action == 'elegir':
        choose(turn, decision.get('opcion') or 0)
    elif action == 'cantidad':
        add(turn, quantity) if quantity > 0 else turn.say(text('¿Cuántas unidades? Escribe el número.'))
    elif action == 'ver_cesta':
        show_cart(turn)
    elif action == 'quitar':
        remove(turn, decision.get('opcion') or 0)
    elif action == 'cambiar':
        change(turn, decision.get('opcion') or 0, quantity)
    elif action == 'cuenta' and len(turn.accounts) > 1:
        ask_account(turn, '¿Para cuál de tus cuentas es la compra?')
    elif action == 'pedidos':
        show_orders(turn)
    elif action == 'orden':
        to_order(turn, words)
    else:
        reply = re.sub(r'https?://\S+', '', (decision.get('respuesta') or '').strip())[:600]
        turn.say(text(reply or 'Dime qué repuesto buscas (por ejemplo «disco de freno Hilux») o envíame una foto de su etiqueta.'))


def interpret(turn, words, image):
    """Gemini's reading of the message, or None after telling the person the assistant is not available (off, or its budget spent)."""
    from .ai_budget import AssistantDisabled, AssistantLimit, check_monthly, estimate_cost
    from .basket_models import BasketItem
    from .catalog_classification import generate_json
    session = turn.session
    cart = [f'{row.quantity} × {row.codigo} · {row.supplier_item.supplier.name}' for row in
            BasketItem.objects.filter(user=turn.user, account=turn.account).select_related('supplier_item__supplier').order_by('created_at', 'id')[:20]]
    context = {'cuenta': turn.account.name, 'opciones_mostradas': [{'n': index, 'texto': option['label']} for index, option in enumerate(session.options, 1)],
               'mostrando': dict(WhatsAppAssistantSession.STEPS)[session.step], 'esperando_cantidad_de': session.pending.get('label'),
               'cesta': [{'n': index, 'texto': line} for index, line in enumerate(cart, 1)], 'conversacion': session.history[-HISTORY:]}
    parts = [{'text': f'Contexto: {json.dumps(context, ensure_ascii=False)}'}, {'text': f'Mensaje del cliente: {words or "(solo una foto)"}'}]
    if image:
        parts.append({'inline_data': {'mime_type': image[1], 'data': base64.b64encode(image[0]).decode()}})
    payload = {'systemInstruction': {'parts': [{'text': PROMPT}]}, 'contents': [{'role': 'user', 'parts': parts}],
               'generationConfig': {'temperature': 0.2, 'maxOutputTokens': 800, 'responseMimeType': 'application/json', 'responseJsonSchema': SCHEMA}}
    try:
        check_monthly('whatsapp_assistant', estimate_cost(len(PROMPT) + len(parts[0]['text']) + len(words or '') + (1100 if image else 0), 800), turn.now)
    except (AssistantDisabled, AssistantLimit):
        turn.say(text(f'El asistente no está disponible en este momento. Busca tus repuestos en MotionPartes: {site()}/'))
        return None
    try:
        decision = generate_json(payload, 1, feature='whatsapp_assistant')
    except Exception as error:  # never the person's words in the log
        log.warning('WhatsApp assistant could not read a message: %s', type(error).__name__)
        turn.say(text(f'No pude entender tu mensaje ahora. Inténtalo de nuevo o busca en MotionPartes: {site()}/'))
        return None
    return decision if isinstance(decision, dict) else {}


def tap(turn, identifier):
    """A tap on something the assistant listed: its id says what (part:, offer:, qty:, account:, menu:, remove:)."""
    kind, _, value = identifier.partition(':')
    if kind == 'account':
        set_account(turn, value)
    elif turn.account is None:
        ask_account(turn, '¿Para cuál de tus cuentas es la compra?')
    elif kind == 'part':
        offers(turn, value)
    elif kind == 'offer':
        pick(turn, value)
    elif kind == 'qty' and value.isdigit():
        add(turn, int(value))
    elif kind == 'remove':  # confirmed with its button
        remove_item(turn, value)
    elif kind == 'pass':  # confirmed: the message goes to the order's supplier
        pass_on(turn, value)
    elif identifier == 'menu:hold':
        turn.session.pending = {key: held for key, held in turn.session.pending.items() if key != 'pasar'}
        turn.say(text('Listo, no lo pasé. ¿En qué más te ayudo?'))
    elif kind == 'undo' and value.isdigit():
        undo(turn, int(value))
    elif identifier == 'menu:keep':
        turn.session.step = 'idle'
        turn.say(text('Listo, no quité nada. ¿Qué más necesitas?'))
    elif identifier == 'menu:cesta':
        show_cart(turn)
    else:
        turn.session.step, turn.session.options = 'idle', []
        turn.say(text('¿Qué repuesto buscas? Escribe el tipo de repuesto y el modelo (por ejemplo «filtro aceite Corolla»), el código, '
                      'o envíame una nota de voz o una foto de la etiqueta.'))


def as_uuid(value):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def find(queries, codes):
    """The catalog's SKUs for the queries (codes first, then from the most precise), those with suppliers in stock first: (parts, availability)."""
    from .catalog_availability import catalog_availability
    from .catalog_search import search_rank
    from .views import catalog_parts
    found, seen = [], set()
    for query in [*codes, *queries][:8]:
        query = re.sub(r'\s+', ' ', str(query)).strip().upper()[:80]
        if len(query) < 2:
            continue
        rows, rank = catalog_parts(query), search_rank(query)
        rows = rows.annotate(search_rank=rank).order_by('search_rank', 'sku', 'id') if rank is not None else rows.order_by('sku', 'id')
        for part in rows[:SEARCH_LIMIT]:
            if part.pk not in seen:
                seen.add(part.pk)
                found.append(part)
        if len(found) >= SEARCH_LIMIT:
            break
    availability = catalog_availability(found)
    found.sort(key=lambda part: 0 if availability.get(part.pk, {}).get('supplier_count') else 1)  # stable: keeps the queries' order
    return found[:LIST_ROWS], availability


def stocked(count):
    return 'sin existencia ahora' if not count else '1 proveedor con existencia' if count == 1 else f'{count} proveedores con existencia'


def search(turn, queries, codes, words):
    session = turn.session
    parts, availability = find(queries, codes)
    asked = (', '.join(codes) or (queries[0].lower() if queries else '') or words or 'la foto')[:60]  # what was searched, not the whole sentence
    if not parts:
        session.step, session.options = 'idle', []
        turn.say(text(f'No encontré repuestos para «{asked}». Prueba con el tipo de repuesto y el modelo (por ejemplo «disco freno Hilux»), '
                      'el código, o una foto de la etiqueta.'))
        return
    session.step = 'parts'
    session.options = [{'id': f'part:{part.pk}', 'label': f'{(part.description or part.name)[:70]} (SKU {part.sku})'} for part in parts]
    lines = [f'{index}. {(part.description or part.name)[:60]} · SKU {part.sku[:24]} · {stocked(availability.get(part.pk, {}).get("supplier_count"))}'
             for index, part in enumerate(parts, 1)]
    body = f'Encontré esto para «{asked}»:\n' + '\n'.join(lines) + '\n\nElige uno en la lista o escribe su número.'
    if len(body) > 1024:
        body = f'Encontré {len(parts)} repuestos para «{asked}». Elige uno en la lista o escribe su número.'
    turn.say(choice_list(body, 'Ver repuestos', [(f'part:{part.pk}', f'{index}. {part.sku}', (part.description or part.name))
                                                 for index, part in enumerate(parts, 1)]), body)


def offer_items(account):
    """The articles a client account may ask a quotation for, of suppliers with stock: never its own."""
    from .models import SupplierItem
    from .lines import storefront_q
    return (SupplierItem.objects.filter(storefront_q('part__'), matching_status='matched', supplier__active=True, supplier__roles__capability='supplier',
                                        reported_quantity__gt=F('reserved_quantity'))
            .exclude(supplier=account).select_related('supplier', 'part').distinct())


def offers(turn, part_id):
    from .lines import storefront_lines
    session = turn.session
    part = storefront_lines().filter(pk=as_uuid(part_id)).first() if as_uuid(part_id) else None
    if part is None:
        turn.say(text('Ese repuesto ya no está en el catálogo. ¿Qué repuesto buscas?'))
        return
    items = list(offer_items(turn.account).filter(part=part).order_by('supplier__name', 'codigo', 'brand', 'id')[:LIST_ROWS])
    name = (part.description or part.name)[:80]
    if not items:
        session.step, session.options = 'idle', []
        turn.say(text(f'Ahora no hay proveedores con existencia de {name} (SKU {part.sku}). Busca otro repuesto o vuelve a preguntar más tarde.'))
        return
    session.step = 'offers'
    session.options = [{'id': f'offer:{item.pk}', 'label': f'{item.supplier.name} · {item.codigo} · {item.brand or "sin marca"}'} for item in items]
    body = (f'{name} (SKU {part.sku})\nProveedores con existencia:\n' + '\n'.join(f'{index}. {option["label"]}' for index, option in enumerate(session.options, 1))
            + '\n\nElige uno: te enviará una cotización privada.')
    turn.say(choice_list(body, 'Ver proveedores', [(f'offer:{item.pk}', f'{index}. {item.supplier.name}', f'{item.codigo} · {item.brand or "sin marca"}')
                                                   for index, item in enumerate(items, 1)]), body)


def pick(turn, item_id):
    from .basket import requestable
    from .models import SupplierItem
    session = turn.session
    item = SupplierItem.objects.filter(pk=as_uuid(item_id)).select_related('supplier', 'part').prefetch_related('supplier__roles').first() if as_uuid(item_id) else None
    if item is None or not requestable(item) or item.supplier_id == turn.account.pk:
        turn.say(text('Ese artículo ya no se puede solicitar. ¿Qué repuesto buscas?'))
        return
    quantity = session.pending.get('cantidad')
    session.pending = {'supplier_item_id': str(item.pk), 'part_id': str(item.part_id), 'label': f'{item.codigo} de {item.supplier.name}'}
    if quantity:  # said in advance («necesito 2 discos…»)
        add(turn, quantity)
        return
    session.step, session.options = 'quantity', []
    body = (f'¿Cuántas unidades de {item.codigo} ({item.brand or "sin marca"}) de {item.supplier.name} agrego a tu cesta? '
            f'Elige en la lista o escribe la cantidad (hasta 9.999).')
    turn.say(choice_list(body, 'Elegir cantidad', [(f'qty:{units}', f'{units} {"unidad" if units == 1 else "unidades"}', '') for units in QUANTITIES]), body)


def cart_link(turn):
    from .login_hints import hint
    return f'{site()}/?vista=cesta&cuenta={turn.account.pk}&ingreso={hint(turn.user.pk)}'


def refused(error):
    """The words of a refusal (ValidationError or RequestConflict)."""
    detail = getattr(error, 'detail', error)
    while isinstance(detail, (dict, list)):
        detail = next(iter(detail.values()), '') if isinstance(detail, dict) else (detail[0] if detail else '')
    return str(detail)


def add(turn, quantity):
    from .basket import apply_changes
    from .basket_models import BasketItem
    from .request_views import RequestConflict
    from rest_framework.exceptions import ValidationError
    session = turn.session
    pending = session.pending
    if not pending.get('supplier_item_id'):
        session.pending = {'cantidad': quantity}
        turn.say(text('¿De qué repuesto? Dime cuál buscas y te muestro los proveedores.'))
        return
    quantity = max(1, min(9999, quantity))
    try:
        apply_changes(turn.user, turn.account, [{'supplier_item_id': as_uuid(pending['supplier_item_id']), 'quantity': quantity, 'mode': 'add',
                                                 'expected_part_id': as_uuid(pending['part_id'])}], source='whatsapp')
    except (ValidationError, RequestConflict) as error:
        session.step, session.pending, session.options = 'idle', {}, []
        turn.say(text(f'No pude agregarlo: {refused(error)} Busca el repuesto otra vez.'))
        return
    count = BasketItem.objects.filter(user=turn.user, account=turn.account).count()
    session.step, session.pending, session.options = 'idle', {}, []
    whose = f' de {turn.account.name}' if len(turn.accounts) > 1 else ''
    body = (f'✅ Agregué {quantity} × {pending["label"]} a tu cesta{whose}. Tienes {count} {"artículo" if count == 1 else "artículos"}.\n\n'
            f'Cuando quieras, revísala y envía la solicitud en MotionPartes:\n{cart_link(turn)}')
    turn.say(buttons(body, [('menu:buscar', 'Buscar otro'), ('menu:cesta', 'Ver mi cesta')]), body)


def choose(turn, number):
    options = turn.session.options
    if not options:
        turn.say(text('¿Qué repuesto buscas? Escribe el tipo de repuesto y el modelo, el código, o envía una foto.'))
    elif not 1 <= number <= len(options):
        turn.say(text(f'Elige un número del 1 al {len(options)}.'))
    elif turn.session.step == 'cart':  # the cesta's lines are for quitar, not to choose
        show_cart(turn)
    else:
        tap(turn, options[number - 1]['id'])


def show_cart(turn):
    from .basket_models import BasketItem
    session = turn.session
    rows = list(BasketItem.objects.filter(user=turn.user, account=turn.account).select_related('supplier_item__supplier').order_by('created_at', 'id'))
    whose = f' de {turn.account.name}' if len(turn.accounts) > 1 else ''
    if not rows:
        session.step, session.options = 'idle', []
        turn.say(text(f'Tu cesta{whose} está vacía. Dime qué repuesto buscas o envíame una foto de su etiqueta.'))
        return
    session.step = 'cart'
    session.options = [{'id': f'remove:{row.supplier_item_id}', 'label': line_label(row)} for row in rows[:20]]
    lines = '\n'.join(f'{index}. {option["label"]}' for index, option in enumerate(session.options, 1))
    more = f'\n… y {len(rows) - 20} más.' if len(rows) > 20 else ''
    example = 2 if len(rows) > 1 else 1
    turn.say(text(f'Tu cesta{whose}:\n{lines}{more}\n\nPara cambiar una cantidad escribe, por ejemplo, «el {example} a 8»; para quitar uno, «quita el {example}». '
                  f'Revísala y envía la solicitud en MotionPartes:\n{cart_link(turn)}'))


def line_label(row):
    return f'{row.quantity} × {row.codigo} · {row.supplier_item.supplier.name}'


def cart_line(turn, number):
    """The cesta's line «number» as the cesta lists it (the only one when there is one and no number was said), or None after saying why."""
    from .basket_models import BasketItem
    rows = list(BasketItem.objects.filter(user=turn.user, account=turn.account).select_related('supplier_item__supplier').order_by('created_at', 'id'))
    if not rows:
        turn.session.step = 'idle'
        turn.say(text('Tu cesta está vacía. Dime qué repuesto buscas o envíame una foto de su etiqueta.'))
        return None
    if not number and len(rows) == 1:
        return rows[0]
    if not 1 <= number <= len(rows):
        turn.say(text(f'Tu cesta tiene {len(rows)} artículos: dime cuál, del 1 al {len(rows)}.' if len(rows) > 1 else 'Tu cesta tiene un solo artículo, el 1.'))
        return None
    return rows[number - 1]


def remove(turn, number):
    """Taking a line out asks first: a button confirms it (a stray «1» never empties a cesta)."""
    row = cart_line(turn, number)
    if row is not None:
        turn.session.step = 'cart'
        body = f'¿Quito {line_label(row)} de tu cesta?'
        turn.say(buttons(body, [(f'remove:{row.supplier_item_id}', 'Sí, quitar'), ('menu:keep', 'No')]), body)


def change(turn, number, quantity):
    from .basket import apply_changes
    if quantity <= 0:
        turn.say(text('¿Cuántas unidades quieres? Escribe, por ejemplo, «el 1 a 8».'))
        return
    row = cart_line(turn, number)
    if row is None:
        return
    quantity = min(quantity, 9999)
    apply_changes(turn.user, turn.account, [{'supplier_item_id': row.supplier_item_id, 'quantity': quantity, 'mode': 'set'}], source='whatsapp')
    turn.session.step, turn.session.options = 'idle', []
    body = f'Listo: ahora pides {quantity} × {row.codigo} · {row.supplier_item.supplier.name}. Revisa tu cesta en MotionPartes:\n{cart_link(turn)}'
    turn.say(buttons(body, [(f'undo:{last_change(turn, row.supplier_item_id)}', 'Deshacer'), ('menu:cesta', 'Ver mi cesta')]), body)


def remove_item(turn, item_id):
    from .basket import apply_changes
    from .basket_models import BasketItem
    row = (BasketItem.objects.filter(user=turn.user, account=turn.account, supplier_item_id=as_uuid(item_id)).select_related('supplier_item__supplier').first()
           if as_uuid(item_id) else None)
    turn.session.step, turn.session.options = 'idle', []
    if row is None:
        turn.say(text('Ese artículo ya no está en tu cesta.'))
        return
    apply_changes(turn.user, turn.account, [{'supplier_item_id': row.supplier_item_id, 'quantity': 0, 'mode': 'set'}], source='whatsapp')
    count = BasketItem.objects.filter(user=turn.user, account=turn.account).count()
    body = f'Listo, quité {line_label(row)}. Te {"queda" if count == 1 else "quedan"} {count} {"artículo" if count == 1 else "artículos"} en la cesta.'
    turn.say(buttons(body, [(f'undo:{last_change(turn, row.supplier_item_id)}', 'Deshacer'), ('menu:buscar', 'Buscar otro')]), body)


def last_change(turn, item_id):
    from .basket_models import BasketEvent
    return BasketEvent.objects.filter(user=turn.user, account=turn.account, supplier_item_id=item_id).order_by('-id').values_list('id', flat=True).first()


def undo(turn, event_id):
    """Deshacer: a line taken out comes back, or a changed quantity returns, from the cesta's ledger, while nothing else changed it since."""
    from .basket import apply_changes
    from .basket_models import BasketEvent, BasketItem
    from .request_views import RequestConflict
    from rest_framework.exceptions import ValidationError
    entry = BasketEvent.objects.filter(pk=event_id, user=turn.user, account=turn.account, kind__in=('removed', 'changed')).first()
    current = BasketItem.objects.filter(user=turn.user, account=turn.account, supplier_item_id=entry.supplier_item_id).first() if entry else None
    turn.session.step, turn.session.options = 'idle', []
    if entry is None or entry.supplier_item_id is None or (current.quantity if current else 0) != entry.quantity_after:
        turn.say(text('Ya no se puede deshacer: la cesta cambió desde entonces. Escribe «cesta» para verla.'))
        return
    try:
        apply_changes(turn.user, turn.account, [{'supplier_item_id': entry.supplier_item_id, 'quantity': entry.quantity_before, 'mode': 'set',
                                                 'expected_part_id': entry.part_id}], source='whatsapp')
    except (ValidationError, RequestConflict) as error:
        turn.say(text(f'No pude deshacerlo: {refused(error)}'))
        return
    turn.say(text(f'Listo, volví a dejar {entry.quantity_before} × {entry.codigo} · {entry.supplier_name} en tu cesta.'))


def ask_account(turn, question):
    session = turn.session
    session.step = 'account'
    session.options = [{'id': f'account:{account.pk}', 'label': account.name} for account in turn.accounts[:LIST_ROWS]]
    body = question + '\n' + '\n'.join(f'{index}. {option["label"]}' for index, option in enumerate(session.options, 1))
    turn.say(choice_list(body, 'Ver cuentas', [(option['id'], option['label'], 'Cuenta de cliente') for option in session.options]), body)


def set_account(turn, account_id):
    session = turn.session
    account = next((account for account in turn.accounts if str(account.pk) == account_id), None)
    if account is None:
        ask_account(turn, 'Esa cuenta no está disponible. ¿Para cuál de tus cuentas es la compra?')
        return
    session.account, waiting = account, session.pending.get('texto')
    session.step, session.options, session.pending = 'idle', [], {}
    if waiting:  # the request that waited for the account
        understand(turn, waiting, None)
    else:
        turn.say(text(f'Listo: compras para {account.name}. ¿Qué repuesto buscas?'))


def to_order(turn, words):
    """A message that seems meant for a supplier (or client) the person deals with: it goes into their conversation (the one last notified
    to this number, tagged with that notice's order) only once the person confirms it with its button, never on Gemini's word alone."""
    from .whatsapp_bridge import author, deal_row, other
    row = deal_row(turn.number, None, turn.now)
    who = author(row, turn.people, turn.holders) if row else None
    if not who or not words:
        turn.say(text('Para escribirle a un proveedor, responde a uno de sus mensajes (mantén presionado el mensaje y elige Responder) o abre '
                      f'la conversación en MotionPartes: {site()}/'))
        return
    turn.session.pending = {**turn.session.pending, 'pasar': {'thread': str(row.thread_id), 'texto': words[:4000], 'wamid': turn.message.get('id', '')}}
    about = f' (sobre {row.order.reference})' if row.order_id else ''
    body = f'¿Paso tu mensaje «{words[:120]}» a tu conversación con {other(row.thread, who[1])}{about}?'
    turn.say(buttons(body, [(f'pass:{row.thread_id}', 'Sí, pasarlo'), ('menu:hold', 'No')]), body)


def pass_on(turn, thread_id):
    """The confirmed message into the conversation, as the person's (✅ on it, tagged with its latest notice's order), and the conversation
    goes on there."""
    from .whatsapp_bridge import author, bring_text, other
    held = turn.session.pending.get('pasar') or {}
    turn.session.pending = {key: value for key, value in turn.session.pending.items() if key != 'pasar'}
    row = (WhatsAppMessage.objects.filter(number=turn.number, thread_id=as_uuid(thread_id)).exclude(status__in=('received', 'failed'))
           .select_related('order', 'thread__client', 'thread__supplier', 'user').order_by('-id').first()
           if as_uuid(thread_id) and held.get('thread') == thread_id else None)
    who = author(row, turn.people, turn.holders) if row else None
    if not who:
        turn.say(text('Ese mensaje ya no está pendiente. Para escribirle a un proveedor, responde a uno de sus mensajes o abre la conversación en '
                      'MotionPartes.'))
        return
    user, account_id = who
    bring_text(row.thread, row.order, account_id, user, turn.number, held.get('wamid', ''), held['texto'], turn.now)
    WhatsAppMessage.objects.filter(number=turn.number, wamid=held.get('wamid', ''), status='received').update(order=row.order, thread=row.thread)
    turn.say({**text(f'Pasé tu mensaje a tu conversación con {other(row.thread, account_id)}. Su respuesta te llegará por aquí.'),
              '_order': row.order_id, '_thread': row.thread_id})


def show_orders(turn):
    """The account's latest solicitudes, each order with its supplier and state, and the link to Enviadas: read-only. An order the supplier
    marked delivered says where to confirm it was received (the site; the assistant never does it)."""
    from .login_hints import conversation, hint
    from .models import SupplierRequest
    orders = list(SupplierRequest.objects.filter(client=turn.account).order_by('-created_at', '-id')[:8])
    turn.session.step, turn.session.options = 'idle', []
    link = f'{site()}/?vista=solicitudes&cuenta={turn.account.pk}&ingreso={hint(turn.user.pk)}'
    whose = f' de {turn.account.name}' if len(turn.accounts) > 1 else ''
    if not orders:
        turn.say(text(f'Aún no hay solicitudes enviadas{whose}. Dime qué repuesto buscas y te ayudo a armar la cesta.'))
        return
    lines = '\n'.join(f'{index}. {order.reference} · {order.supplier_name} · {order.get_status_display()} · {timezone.localtime(order.created_at):%d/%m}'
                      + (f'\n   Confirma en MotionPartes que recibiste {order.reference}: {site()}/?conversacion={conversation(order.pk, turn.user.pk)}'
                         if order.status == 'delivered' else '')
                      for index, order in enumerate(orders, 1))
    turn.say(text(f'Tus últimas solicitudes{whose}:\n{lines}\n\nSus artículos, cotizaciones y conversaciones están en MotionPartes:\n{link}\n'
                  'Para escribirle a un proveedor, responde a un mensaje de su orden.'))
