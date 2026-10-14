"""Read-only snapshot of what the catalog's screens return, taken before and after each stage of the inventory refactor (plan §1.5).

Run: python -m mall.refactor.snapshot [--out DIR] [--name NOMBRE] [--user USUARIO] [--sample INSTANTÁNEA]
     python -m mall.refactor.snapshot --diff [BASE] [--against INSTANTÁNEA] [--allow PATRÓN ...] [--limit N]
     python -m mall.refactor.snapshot --checks

It reads through its own connection with default_transaction_read_only=on (it refuses to run otherwise) and calls the views in-process
with APIClient(HTTP_HOST='localhost') and force_authenticate, so nothing is written: no session, no last_login, no staging job. The
supplier import preview runs stock_plan(lock=False) directly, because its endpoint stages a job. Run it with the outside services'
keys blank (-e DO_SPACES_KEY= -e DO_SPACES_SECRET= -e DO_SPACES_BUCKET= -e RESEND_API_KEY= ...).

What it captures: the storefront catalog and suggest for 30 searches, the offers of 50 lines and the availability of every live line;
the admin grid (pages 1-3 in 4 orderings); the Biblioteca (pages 1-3 and 50 numbers); Productos nuevos (list and summary); each
supplier's first inventory page; 20 rows of Revisión OEM and of Pendientes; the equivalents of every line that has them; the matching
overview with its review and unmatched cases; the merged list, 10 split plans and the alternates grid; 50 oem-equivalents, a mark-oem plan
of 20 lines, 20 technical sheets and a 200-row supplier import preview.

Each snapshot keeps its sample (the users, searches, lines, numbers and import rows it read) and is saved as gzipped JSON in --out.
--diff captures again with the base's sample, so both read the same inputs (BASE defaults to the newest snapshot in --out; --against
compares two saved snapshots without reading the database), and prints every changed key as a path (section/key/...; rows of a list
by id, with their order as [order]). What the matching worker changes on its own (the run's state) is ignored, and --allow takes
fnmatch patterns of the changes the stage expects. Exit codes: a capture with a server error 2, a diff with an unexpected change 1, a
failed check 1. --checks runs plan Appendix C and skips the queries whose columns no stage has added yet.
"""
import argparse
import fnmatch
import gzip
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

DEFAULT_OUT = Path('/app/output/refactor/snapshots')
WORDS = ['FILTRO AIRE', 'BOMBA AGUA', 'BASE AMORT', 'TERM ESTAB', 'PASTILLA FRENO', 'TOY HILUX', 'NIS FRONTIER', 'HYU ACCENT',
         'SENSOR OXIGENO']
ORDERINGS = ['sku', '-stock_record_count', 'description', '-is_OEM']
PAGES = (1, 2, 3)
SIZES = {'searches': 30, 'offers': 50, 'biblioteca': 50, 'oem_equivalents': 50, 'mark_oem': 20, 'technical': 20, 'split': 10,
         'review_rows': 20, 'import_rows': 200, 'import_new': 20}
MATCHING_STATUSES = ('review', 'unmatched')
# What the matching worker changes between two reads of an unchanged catalog.
VOLATILE = [f'matching/*/{key}' for key in ('running', 'queued', 'state', 'stage', 'started_at', 'last_run', 'summary', 'summary/*', 'error')]
MISSING = object()
WRITES = re.compile(r'^\s*(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE)\b', re.I)


def compact(code):
    return re.sub(r'[^A-Z0-9]', '', str(code or '').upper())


def ordered_by_hash(queryset):
    """A fixed pseudo-random order (md5 of the id), so the same data always gives the same sample."""
    from django.db.models import CharField
    from django.db.models.functions import MD5, Cast
    return queryset.annotate(sample_hash=MD5(Cast('pk', CharField()))).order_by('sample_hash')


def ids(queryset, limit):
    return [str(pk) for pk in ordered_by_hash(queryset).values_list('pk', flat=True)[:limit]]


def choose_sample(username=None):
    """The inputs a snapshot reads. Every --diff reuses its base's sample, so a later stage reads exactly these again."""
    from django.db.models import F, Q
    from ..models import Account, Membership, Part, PartCode, SupplierItem, User
    from ..oem_reference_models import OEMReference, PartOEMLink
    admins = User.objects.filter(is_superuser=True, is_active=True)
    admin = (admins.filter(username=username) if username else admins).order_by('pk').first()
    if admin is None:
        raise SystemExit(f'No hay un superusuario activo{f" {username}" if username else ""} para leer las pantallas de administración.')
    client = (User.objects.filter(is_active=True, membership__account__active=True, membership__account__roles__capability='client')
              .order_by('pk').values_list('pk', flat=True).first())
    suppliers = {}
    for account in Account.objects.filter(active=True, roles__capability='supplier').distinct().order_by('name', 'pk'):
        member = Membership.objects.filter(account=account, user__is_active=True).order_by('pk').values_list('user_id', flat=True).first()
        if member is not None:
            suppliers[str(account.pk)] = member
    live = Part.objects.filter(active=True, merged_into__isnull=True)
    skus = list(ordered_by_hash(live).values_list('sku', flat=True)[:SIZES['import_new']])
    codes = list(ordered_by_hash(PartCode.objects.filter(part__in=live)).values_list('code', flat=True)[:5])
    searches = list(dict.fromkeys(['', *WORDS, *skus[:10], *[compact(sku) for sku in skus[:5]], *codes]))[:SIZES['searches']]
    stocked = SupplierItem.objects.filter(matching_status='matched', reported_quantity__gt=F('reserved_quantity'), supplier__active=True)
    numbered = Q(pk__in=PartCode.objects.filter(ref_type='oem', oem_reference__isnull=False).values('part_id')) | Q(
        pk__in=PartOEMLink.objects.values('part_id'))
    # mark-oem: lines whose code is a library number and are not OEM yet, then those already OEM, then any line.
    library = set(OEMReference.objects.values_list('code', flat=True))
    mark = [pk for _, _, pk in sorted((0 if compact(sku) in library and not is_oem else 1 if compact(sku) in library else 2,
                                        hashlib.md5(str(pk).encode()).hexdigest(), str(pk))
                                       for pk, sku, is_oem in live.values_list('pk', 'sku', 'is_OEM').iterator())[:SIZES['mark_oem']]]
    return {
        'users': {'admin': admin.pk, 'client': client, 'suppliers': suppliers},
        'searches': searches,
        'offers': ids(live.filter(pk__in=stocked.values('part_id')), SIZES['offers']),
        'biblioteca': ids(OEMReference.objects.all(), SIZES['biblioteca']),
        'oem_equivalents': ids(live.filter(numbered), SIZES['oem_equivalents']),
        'mark_oem': mark,
        'technical': ids(live.filter(part_type__isnull=False), SIZES['technical']),
        'split': ids(live.filter(pk__in=Part.objects.filter(merged_into__isnull=False).values('merged_into_id')), SIZES['split']),
        'import': import_rows(skus),
    }


def import_rows(new_codes):
    """200 rows of an Excel load: the upload supplier with the most items gets its first items back with -1, 0 or +1 units, plus new
    IDs that carry catalog codes, so the preview creates, updates, leaves alone and matches."""
    from django.db.models import Count
    from ..models import SupplierItem
    uploads = SupplierItem.objects.filter(source='upload', supplier__active=True, supplier__roles__capability='supplier')
    top = uploads.values('supplier_id').annotate(n=Count('pk', distinct=True)).order_by('-n', 'supplier_id').first()
    if top is None:
        return {'supplier': None, 'rows': []}
    kept = SIZES['import_rows'] - min(SIZES['import_new'], len(new_codes))
    rows = []
    for item in SupplierItem.objects.filter(supplier_id=top['supplier_id'], source='upload').order_by('supplier_invent_id', 'pk')[:kept]:
        rows.append({'row': len(rows) + 2, 'data': {'supplier_invent_id': item.supplier_invent_id, 'codigo': item.codigo, 'brand': item.brand,
                                                    'description': item.description,
                                                    'quantity': max(0, item.reported_quantity + len(rows) % 3 - 1)}})
    for number, code in enumerate(new_codes[:SIZES['import_rows'] - len(rows)], 1):
        rows.append({'row': len(rows) + 2, 'data': {'supplier_invent_id': f'INSTANTANEA-{number:03d}', 'codigo': code.upper()[:120], 'brand': '',
                                                    'description': 'ARTICULO DE PRUEBA', 'quantity': 5}})
    return {'supplier': str(top['supplier_id']), 'rows': rows}


def plain(value):
    """value as JSON would carry it (dates as ISO text, UUIDs and decimals as strings)."""
    from django.core.serializers.json import DjangoJSONEncoder
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


class Reader:
    def __init__(self, sample):
        from rest_framework.test import APIClient
        from ..models import User
        users = sample['users']
        wanted = {users['admin'], users['client'], *users['suppliers'].values()} - {None}
        self.users = {user.pk: user for user in User.objects.filter(pk__in=wanted)}
        self.client = APIClient(HTTP_HOST='localhost')
        self.errors = []

    def get(self, user_id, path, params=None):
        """The JSON body of a 200; otherwise {status, body}. Server errors and exceptions also go to self.errors."""
        user = self.users.get(user_id)
        if user is None:
            return {'error': 'Sin usuario para leer esta pantalla.'}
        self.client.force_authenticate(user)
        try:
            response = self.client.get(path, params or {})
        except Exception as exc:  # the view raised: with a read-only connection, usually a write it attempted
            self.errors.append(f'GET {path} {params or ""}: {type(exc).__name__}: {exc}'.strip())
            return {'error': f'{type(exc).__name__}: {exc}'}
        if response.get('Content-Type', '').startswith('application/json'):
            body = json.loads(response.content or b'null')
        else:
            body = {'content_type': response.get('Content-Type', ''), 'bytes': len(response.content)}
        if response.status_code >= 500:
            self.errors.append(f'GET {path} {params or ""}: {response.status_code}')
        return body if response.status_code == 200 else {'status': response.status_code, 'body': body}

    def run(self, label, read):
        """An in-process read; an exception is recorded like a server error."""
        try:
            return plain(read())
        except Exception as exc:
            self.errors.append(f'{label}: {type(exc).__name__}: {exc}')
            return {'error': f'{type(exc).__name__}: {exc}'}


def first_rows(page, count):
    if isinstance(page, dict) and isinstance(page.get('results'), list):
        return {**page, 'results': page['results'][:count]}
    return page


def availability(line_ids):
    from ..catalog_availability import catalog_availability
    return {str(pk): value for pk, value in catalog_availability([SimpleNamespace(pk=pk) for pk in line_ids]).items()}


def equivalents(line_ids):
    from ..oem_links import equivalents_for
    return {str(pk): value for pk, value in equivalents_for(line_ids).items() if value}


def import_preview(spec):
    from ..models import Account
    from ..supplier_import import stock_plan
    if not spec['supplier']:
        return {}
    plan, _ = stock_plan(Account.objects.get(pk=spec['supplier']), [{'row': row['row'], 'data': dict(row['data'])} for row in spec['rows']],
                         lock=False)
    return plan


def sections(sample, reader):
    """(name, read) in capture order; each read returns the section's {key: value}."""
    from ..models import Part
    users, admin, client = sample['users'], sample['users']['admin'], sample['users']['client']
    get = reader.get
    management = '/api/v1/management'

    def live_ids():
        return list(Part.objects.filter(active=True, merged_into__isnull=True).values_list('pk', flat=True))

    return [
        ('catalog', lambda: {q: get(client, '/api/v1/catalog/', {'search': q, 'include_facets': '1'}) for q in sample['searches']}),
        ('suggest', lambda: {q: get(client, '/api/v1/catalog/suggest/', {'search': q}) for q in sample['searches'] if q}),
        ('offers', lambda: {pk: get(client, f'/api/v1/catalog/{pk}/suppliers/') for pk in sample['offers']}),
        ('availability', lambda: reader.run('availability', lambda: availability(live_ids()))),
        ('equivalents', lambda: reader.run('equivalents', lambda: equivalents(live_ids()))),
        ('admin_grid', lambda: {f'{ordering} p{page}': get(admin, f'{management}/catalog/', {'ordering': ordering, 'page': page})
                                for ordering in ORDERINGS for page in PAGES}),
        ('biblioteca', lambda: {**{f'p{page}': get(admin, f'{management}/oem-references/', {'page': page}) for page in PAGES},
                                **{pk: get(admin, f'{management}/oem-references/{pk}/') for pk in sample['biblioteca']}}),
        ('productos_nuevos', lambda: {'list': get(admin, f'{management}/catalog/proposals/'),
                                      'summary': get(admin, f'{management}/catalog/proposals/summary/')}),
        ('supplier_inventory', lambda: {pk: get(member, f'/api/v1/accounts/{pk}/inventory/') for pk, member in users['suppliers'].items()}),
        ('oem_review', lambda: {'review': first_rows(get(admin, f'{management}/oem-review/'), SIZES['review_rows']),
                                'pending': first_rows(get(admin, f'{management}/oem-finder/pending/'), SIZES['review_rows'])}),
        ('matching', lambda: {status: get(admin, f'{management}/matching/', {'status': status}) for status in MATCHING_STATUSES}),
        ('merged', lambda: {'p1': get(admin, f'{management}/catalog/merged/')}),
        ('split', lambda: {pk: get(admin, f'{management}/catalog/{pk}/split/') for pk in sample['split']}),
        ('alternates', lambda: {f'p{page}': get(admin, f'{management}/alternates/', {'page': page}) for page in PAGES}),
        ('oem_equivalents', lambda: {pk: get(admin, f'{management}/catalog/{pk}/oem-equivalents/') for pk in sample['oem_equivalents']}),
        ('mark_oem', lambda: {'plan': get(admin, f'{management}/catalog/mark-oem/', {'ids': ','.join(sample['mark_oem'])})} if sample['mark_oem'] else {}),
        ('technical', lambda: {pk: get(admin, f'{management}/catalog/{pk}/technical/') for pk in sample['technical']}),
        ('import_preview', lambda: reader.run('import_preview', lambda: import_preview(sample['import']))),
    ]


def capture(sample=None, *, username=None, log=None):
    """{meta, sample, sections, errors}. Reads only; the caller provides the read-only connection (main does)."""
    from django.db import connection
    from django.utils import timezone
    started = time.monotonic()
    sample = sample or choose_sample(username)
    reader, captured, timings = Reader(sample), {}, {}
    for name, read in sections(sample, reader):
        began = time.monotonic()
        captured[name] = read()
        timings[name] = round(time.monotonic() - began, 2)
        if log:
            log(f'  {name}: {len(captured[name])} claves en {timings[name]} s')
    with connection.cursor() as cursor:
        cursor.execute("SELECT max(name) FROM django_migrations WHERE app = 'mall'")
        migration = cursor.fetchone()[0]
    meta = {'created_at': timezone.now().isoformat(), 'database': connection.settings_dict['NAME'], 'migration': migration,
            'seconds': round(time.monotonic() - started, 1), 'timings': timings}
    return {'meta': meta, 'sample': sample, 'sections': captured, 'errors': reader.errors}


def flatten(value, path='', out=None):
    """{path: leaf}: dict keys join with '/', rows of a list that all carry a distinct id go by [id=...] plus their order as [order],
    other lists by index."""
    out = {} if out is None else out
    if isinstance(value, dict):
        if not value:
            out[path] = {}
        for key, item in value.items():
            flatten(item, f'{path}/{key}' if path else str(key), out)
    elif isinstance(value, list):
        if not value:
            out[path] = []
        keys = [str(item['id']) for item in value if isinstance(item, dict) and 'id' in item]
        if value and len(keys) == len(value) == len(set(keys)):
            out[f'{path}[order]'] = keys
            for key, item in zip(keys, value):
                flatten({field: leaf for field, leaf in item.items() if field != 'id'}, f'{path}[id={key}]', out)
        else:
            for index, item in enumerate(value):
                flatten(item, f'{path}[{index}]', out)
    else:
        out[path] = value
    return out


def compare(base, current, allow=(), ignore=VOLATILE):
    """{'unexpected': [...], 'expected': [...], 'ignored': [...]} of (path, before, after); MISSING marks an added or removed key."""
    before, after = flatten(base['sections']), flatten(current['sections'])
    result = {'unexpected': [], 'expected': [], 'ignored': []}
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path, MISSING), after.get(path, MISSING)
        if old == new:
            continue
        kind = ('ignored' if any(fnmatch.fnmatchcase(path, pattern) for pattern in ignore) else
                'expected' if any(fnmatch.fnmatchcase(path, pattern) for pattern in allow) else 'unexpected')
        result[kind].append((path, old, new))
    return result


def short(value, width=160):
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= width else text[:width - 1] + '…'


def render_diff(result, limit=200):
    lines = []
    for kind, title in (('unexpected', 'Cambios no previstos'), ('expected', 'Cambios previstos (--allow)')):
        changes = result[kind]
        by_section = Counter(re.split(r'[/\[]', path, maxsplit=1)[0] for path, _, _ in changes)
        lines.append(f'{title}: {len(changes)}' + (f" ({', '.join(f'{s} {n}' for s, n in sorted(by_section.items()))})" if changes else ''))
        if kind == 'unexpected':
            for path, old, new in changes[:limit]:
                if old is MISSING:
                    lines.append(f'  + {path}: {short(new)}')
                elif new is MISSING:
                    lines.append(f'  - {path}: {short(old)}')
                else:
                    lines.append(f'  ~ {path}: {short(old)} → {short(new)}')
            if len(changes) > limit:
                lines.append(f'  … y {len(changes) - limit} más (--limit)')
    lines.append(f"Ignorados (estado del servicio de coincidencias): {len(result['ignored'])}")
    return '\n'.join(lines)


def save(snapshot, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, 'wt', encoding='utf-8') as out:
        json.dump(snapshot, out, ensure_ascii=False, sort_keys=True, default=str)
    return path


def load(path):
    with gzip.open(path, 'rt', encoding='utf-8') as source:
        return json.load(source)


def newest(folder):
    found = sorted(Path(folder).glob('*.json.gz'), key=lambda path: path.stat().st_mtime)
    if not found:
        raise SystemExit(f'No hay instantáneas en {folder}: indica la base de --diff.')
    return found[-1]


# Plan Appendix C: (id, stage, expectation, query, columns it needs). Expectation 0: the first value must be 0; 'none': no rows;
# None: informational.
CHECKS = [
    ('C0', 'hoy', None, 'SELECT active, merged_into_id IS NOT NULL, count(*) FROM mall_part GROUP BY 1, 2 ORDER BY 1, 2', []),
    ('C0', 'hoy', None, 'SELECT matching_status, count(*) FROM mall_supplieritem GROUP BY 1 ORDER BY 1', []),
    ('C0', 'hoy', None, 'SELECT ref_type, count(*) FROM mall_partcode GROUP BY 1 ORDER BY 1', []),
    ('C1', 'S1', None, 'SELECT estado, active, merged_into_id IS NOT NULL, count(*) FROM mall_part GROUP BY 1, 2, 3 ORDER BY 1, 2, 3',
     ['mall_part.estado']),
    ('I1', 'S2b', 0, "SELECT count(*) FROM mall_part p WHERE p.estado <> 'unificada' AND (SELECT count(*) FROM mall_partcode r "
                     "WHERE r.part_id = p.id AND r.is_main AND r.estado <> 'retirada') <> 1",
     ['mall_part.estado', 'mall_partcode.is_main', 'mall_partcode.estado']),
    ('I2', 'S2b', 0, "SELECT count(*) FROM mall_part p JOIN mall_partcode r ON r.part_id = p.id AND r.is_main AND r.estado <> 'retirada' "
                     "WHERE p.estado <> 'unificada' AND p.sku <> r.code",
     ['mall_part.estado', 'mall_partcode.is_main', 'mall_partcode.estado']),
    ('I3', 'S2b', 0, "SELECT count(*) FROM mall_partcode r JOIN mall_part p ON p.id = r.part_id WHERE p.estado = 'unificada' AND r.estado <> 'retirada'",
     ['mall_part.estado', 'mall_partcode.estado']),
    ('I5', 'S2a', 'none', "SELECT a.clave FROM mall_partcode a JOIN mall_partcode b ON a.clave = b.clave AND a.part_id <> b.part_id "
                          "WHERE a.brand = '' AND b.brand <> '' AND a.supplier_id IS NULL AND b.supplier_id IS NULL "
                          "AND a.estado IN ('verificado', 'declarado', 'inferido') AND b.estado IN ('verificado', 'declarado', 'inferido') LIMIT 20",
     ['mall_partcode.clave', 'mall_partcode.supplier_id', 'mall_partcode.estado']),
    ('I11', 'S2b', 0, "SELECT count(*) FROM mall_part p JOIN mall_partcode r ON r.part_id = p.id AND r.is_main AND r.estado <> 'retirada' "
                      "WHERE p.estado <> 'unificada' AND p.tipo <> CASE r.tipo WHEN 'oem' THEN 'OEM' WHEN 'aftermarket' THEN 'AFTERMARKET' ELSE 'UNKNOWN' END",
     ['mall_part.estado', 'mall_part.tipo', 'mall_partcode.is_main', 'mall_partcode.estado', 'mall_partcode.tipo']),
    ('I12', 'S1', 0, "SELECT count(*) FROM mall_partcode r WHERE r.brand <> '' AND NOT EXISTS (SELECT 1 FROM mall_brand b WHERE b.nombre = r.brand) "
                     "AND NOT EXISTS (SELECT 1 FROM mall_brandalias a WHERE a.alias = r.brand)",
     ['mall_brand.nombre', 'mall_brandalias.alias']),
    ('C3', 'S3', None, 'SELECT matching_status, reference_id IS NULL, count(*) FROM mall_supplieritem GROUP BY 1, 2 ORDER BY 1, 2',
     ['mall_supplieritem.reference_id']),
    ('I4', 'S3', 0, 'SELECT count(*) FROM mall_supplieritem s JOIN mall_partcode r ON r.id = s.reference_id WHERE s.part_id <> r.part_id',
     ['mall_supplieritem.reference_id']),
    ('I7', 'S3', 0, "SELECT count(*) FROM mall_supplieritem s JOIN mall_partcode r ON r.id = s.reference_id "
                    "WHERE r.tipo = 'interno' AND r.supplier_id <> s.supplier_id",
     ['mall_supplieritem.reference_id', 'mall_partcode.tipo', 'mall_partcode.supplier_id']),
    ('C4', 'S4', None, 'SELECT estado, origen, count(*) FROM mall_part GROUP BY 1, 2 ORDER BY 1, 2', ['mall_part.estado', 'mall_part.origen']),
    ('C4', 'S4', 0, 'SELECT count(*) FROM mall_oemreference o LEFT JOIN mall_partcode r ON r.uid = o.id WHERE r.id IS NULL',
     ['mall_partcode.uid', 'mall_oemreference.id']),
    ('C4', 'S4', 0, 'SELECT count(*) FROM mall_oemcrossreference WHERE ref_id IS NULL', ['mall_oemcrossreference.ref_id']),
    ('C4', 'S4', None, "SELECT count(*) FROM mall_partcode WHERE tipo = 'oem' AND brand <> '' AND estado <> 'retirada'",
     ['mall_partcode.tipo', 'mall_partcode.estado']),
    ('I9', 'S6', 0, "SELECT count(*) FROM mall_supplieritem s JOIN mall_part p ON p.id = s.part_id "
                    "WHERE s.matching_status = 'matched' AND p.estado NOT IN ('publicada', 'retirada')", ['mall_part.estado']),
    ('I10', 'S6', 0, "SELECT count(*) FROM mall_supplieritem s WHERE s.reference_id IS NULL AND s.updated_at < now() - interval '15 minutes' "
                     "AND NOT EXISTS (SELECT 1 FROM mall_matchingcase c WHERE c.item_id = s.id AND c.kind = 'line' AND c.status = 'review')",
     ['mall_supplieritem.reference_id', 'mall_matchingcase.kind']),
    ('C6', 'S6', None, "SELECT count(*) FROM mall_part p WHERE p.estado = 'preparacion' AND EXISTS (SELECT 1 FROM mall_supplieritem s WHERE s.part_id = p.id)",
     ['mall_part.estado']),
    ('C6', 'S6', None, 'SELECT kind, status, count(*) FROM mall_matchingcase GROUP BY 1, 2 ORDER BY 1, 2', ['mall_matchingcase.kind']),
    ('I8', 'S7', 0, "SELECT count(*) FROM mall_part p LEFT JOIN mall_searchdocument d ON d.line_id = p.id "
                    "WHERE (p.estado = 'publicada' AND p.merged_into_id IS NULL) <> (d.line_id IS NOT NULL)",
     ['mall_part.estado', 'mall_searchdocument.line_id']),
    ('I6', 'siempre', 0, 'SELECT count(*) FROM mall_part a JOIN mall_part b ON a.merged_into_id = b.id WHERE b.merged_into_id IS NOT NULL', []),
    ('C-', 'siempre', None, "SELECT count(*) FROM mall_part WHERE estado = 'publicada'", ['mall_part.estado']),
]


def columns():
    """{'table.column'} of the public schema."""
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'")
        return {f'{table}.{column}' for table, column in cursor.fetchall()}


def run_checks():
    """[(id, stage, outcome, detail)], outcome OK, FALLA, INFO or OMITIDA."""
    from django.db import connection
    present, results = columns(), []
    for check_id, stage, expected, query, needs in CHECKS:
        missing = [column for column in needs if column not in present]
        if missing:
            results.append((check_id, stage, 'OMITIDA', f"falta {', '.join(missing)}"))
            continue
        with connection.cursor() as cursor:
            cursor.execute(query)
            rows = cursor.fetchall()
        if expected is None:
            results.append((check_id, stage, 'INFO', short([list(row) for row in rows], 400)))
        elif expected == 'none':
            results.append((check_id, stage, 'OK' if not rows else 'FALLA', f'{len(rows)} filas' + (f': {short(rows, 300)}' if rows else '')))
        else:
            value = rows[0][0] if rows else None
            results.append((check_id, stage, 'OK' if value == expected else 'FALLA', f'{value} (se espera {expected})'))
    return results


def setup_read_only():
    """Django on a connection that cannot write, named refactor:snapshot; refuses to go on unless PostgreSQL confirms it."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    from django.conf import settings
    database = settings.DATABASES['default']
    if 'postgresql' not in database['ENGINE']:
        raise SystemExit('La instantánea lee PostgreSQL: configura POSTGRES_DB.')
    options = database.setdefault('OPTIONS', {})
    options['options'] = ' '.join(filter(None, [options.get('options', ''), '-c default_transaction_read_only=on']))
    options['application_name'] = 'refactor:snapshot'
    if not {'localhost', '*'} & set(settings.ALLOWED_HOSTS):
        settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, 'localhost']
    import django
    django.setup()
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute('SHOW transaction_read_only')
        if cursor.fetchone()[0] != 'on':
            raise SystemExit('La conexión no quedó en solo lectura: no se toma la instantánea.')
    from django.conf import settings as configured
    keys = [name for name in ('RESEND_API_KEY', 'GEMINI_API_KEY', 'WHATSAPP_ACCESS_TOKEN', 'VAPID_PRIVATE_KEY', 'DO_SPACES_KEY') if getattr(configured, name, '')]
    if keys:
        print(f"Aviso: corre con las claves en blanco ({', '.join(keys)} tienen valor).", file=sys.stderr)


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(prog='python -m mall.refactor.snapshot',
                                     description='Instantánea de solo lectura de las pantallas del catálogo, para comparar antes y después de cada etapa.')
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT, help=f'Carpeta de las instantáneas ({DEFAULT_OUT} por omisión).')
    parser.add_argument('--name', help='Nombre del archivo, sin extensión (por omisión, snapshot-AAAAMMDD-HHMMSS).')
    parser.add_argument('--user', help='Superusuario que lee las pantallas de administración (por omisión, el primero activo).')
    parser.add_argument('--sample', type=Path, help='Lee con la muestra de otra instantánea en vez de elegir una.')
    parser.add_argument('--diff', nargs='?', const='newest', metavar='BASE', help='Compara con BASE (por omisión, la más reciente de --out).')
    parser.add_argument('--against', type=Path, metavar='INSTANTÁNEA', help='Con --diff: compara con esta instantánea guardada, sin leer la base de datos.')
    parser.add_argument('--allow', action='append', default=[], metavar='PATRÓN', help='Ruta de un cambio previsto (fnmatch); se repite.')
    parser.add_argument('--limit', type=int, default=200, help='Cambios no previstos que se listan (200 por omisión).')
    parser.add_argument('--checks', action='store_true', help='Corre las consultas del apéndice C y termina.')
    args = parser.parse_args(argv)
    out = stdout or sys.stdout
    base = None
    if args.diff:
        base_path = newest(args.out) if args.diff == 'newest' else Path(args.diff)
        base = load(base_path)
        print(f'Base: {base_path} ({base["meta"]["created_at"]}, migración {base["meta"]["migration"]})', file=out)
        if args.against:
            current = load(args.against)
            print(f'Comparada con: {args.against} ({current["meta"]["created_at"]})', file=out)
            result = compare(base, current, args.allow)
            print(render_diff(result, args.limit), file=out)
            return 1 if result['unexpected'] else 0
    setup_read_only()
    if args.checks:
        results = run_checks()
        for check_id, stage, outcome, detail in results:
            print(f'{outcome:8} {check_id:4} {stage:8} {detail}', file=out)
        return 1 if any(outcome == 'FALLA' for _, _, outcome, _ in results) else 0
    sample = base['sample'] if base else load(args.sample)['sample'] if args.sample else None
    print('Leyendo (solo lectura)…', file=out)
    snapshot = capture(sample, username=args.user, log=lambda text: print(text, file=out))
    name = args.name or f"snapshot-{time.strftime('%Y%m%d-%H%M%S')}"
    path = save(snapshot, args.out / f'{name}.json.gz')
    print(f"Instantánea: {path} ({path.stat().st_size / 1e6:.1f} MB, {snapshot['meta']['seconds']} s, migración {snapshot['meta']['migration']})", file=out)
    for error in snapshot['errors']:
        print(f'  ERROR {error}', file=out)
    print(f"Errores del servidor: {len(snapshot['errors'])}", file=out)
    status = 2 if snapshot['errors'] else 0
    if base:
        result = compare(base, snapshot, args.allow)
        print(render_diff(result, args.limit), file=out)
        status = status or (1 if result['unexpected'] else 0)
    return status


if __name__ == '__main__':
    sys.exit(main())
