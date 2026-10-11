"""MotionPartes' two WhatsApp templates (mall.whatsapp) in the WhatsApp Business account: their status, or with --create the missing ones.

    docker compose exec -T app python -m mall.whatsapp_templates
    docker compose exec -T app python -m mall.whatsapp_templates --create

Both are Utility templates in WHATSAPP_TEMPLATE_LANGUAGE, with the texts mall.whatsapp fills, and a "Ver en MotionPartes" button that opens
<SITE_URL>/?conversacion=<deal>. Meta fixes the button's address when it approves a template, so each environment (the development tunnel,
production) creates its own, in its own account. Needs WHATSAPP_BUSINESS_ACCOUNT_ID and a token with whatsapp_business_management;
Meta usually approves Utility templates within minutes to a few hours."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

EXAMPLE_DEAL = '55791743-7458-49da-adf8-51a03b3819a1'


def definitions(settings):
    from .whatsapp import BODIES  # the same words the notices (and their plain-text fallback) use
    link = settings.SITE_URL.rstrip('/') + '/?conversacion='
    button = {'type': 'BUTTONS', 'buttons': [{'type': 'URL', 'text': 'Ver en MotionPartes', 'url': link + '{{1}}', 'example': [link + EXAMPLE_DEAL]}]}
    return [
        {'name': settings.WHATSAPP_TEMPLATE_UPDATE, 'language': settings.WHATSAPP_TEMPLATE_LANGUAGE, 'category': 'UTILITY', 'components': [
            {'type': 'BODY', 'text': BODIES['update'],
             'example': {'body_text': [['ORD-20261010-55791743', 'GRUPO RAZOR PARTS publicó la cotización v2']]}}, button]},
        {'name': settings.WHATSAPP_TEMPLATE_MESSAGE, 'language': settings.WHATSAPP_TEMPLATE_LANGUAGE, 'category': 'UTILITY', 'components': [
            {'type': 'BODY', 'text': BODIES['message'],
             'example': {'body_text': [['GRUPO RAZOR PARTS', 'ORD-20261010-55791743', '¿Pueden entregar el lunes?']]}}, button]},
        # A number's verification code (mall.whatsapp_verification): Meta writes the text of Authentication templates itself.
        {'name': settings.WHATSAPP_TEMPLATE_CODE, 'language': settings.WHATSAPP_TEMPLATE_LANGUAGE, 'category': 'AUTHENTICATION', 'components': [
            {'type': 'BODY', 'add_security_recommendation': True},
            {'type': 'FOOTER', 'code_expiration_minutes': 10},
            {'type': 'BUTTONS', 'buttons': [{'type': 'OTP', 'otp_type': 'COPY_CODE', 'text': 'Copiar código'}]}]},
    ]


def call(settings, method, path, payload=None, params=None):
    url = f'https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/{path}' + (f'?{urllib.parse.urlencode(params)}' if params else '')
    request = urllib.request.Request(url, method=method, data=json.dumps(payload).encode() if payload else None,
                                     headers={'Authorization': f'Bearer {settings.WHATSAPP_ACCESS_TOKEN}', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def run(settings, create, out):
    account = settings.WHATSAPP_BUSINESS_ACCOUNT_ID
    if not (account and settings.WHATSAPP_ACCESS_TOKEN):
        print('Faltan WHATSAPP_BUSINESS_ACCOUNT_ID o WHATSAPP_ACCESS_TOKEN.', file=out)
        return 1
    try:
        listed = call(settings, 'GET', f'{account}/message_templates', params={'fields': 'name,language,status,category,rejected_reason', 'limit': 200})
        existing = {(row['name'], row['language']): row for row in listed.get('data', [])}
        for template in definitions(settings):
            found = existing.get((template['name'], template['language']))
            if found:
                reason = found.get('rejected_reason')
                print(f"{template['name']} ({template['language']}): {found['status']}{f' · {reason}' if reason and reason != 'NONE' else ''}", file=out)
            elif create:
                created = call(settings, 'POST', f'{account}/message_templates', payload=template)
                print(f"{template['name']} ({template['language']}): creada, {created.get('status', 'PENDING')}", file=out)
            else:
                print(f"{template['name']} ({template['language']}): no existe; créala con --create", file=out)
    except urllib.error.HTTPError as error:
        detail = json.loads(error.read() or b'{}').get('error', {})
        print(f"Meta respondió {error.code}: {detail.get('error_user_msg') or detail.get('message', '')}", file=out)
        return 1
    return 0


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Plantillas de WhatsApp de MotionPartes: estado, o --create para crear las que faltan.')
    parser.add_argument('--create', action='store_true', help='crea en Meta las plantillas que faltan')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from django.conf import settings
    return run(settings, options.create, stdout or sys.stdout)


if __name__ == '__main__':
    sys.exit(main())
