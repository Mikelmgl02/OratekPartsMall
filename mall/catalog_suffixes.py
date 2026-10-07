"""Code suffix table service (OEM finder phase 1), ported from the validated v2 prototype.

suffix_table() is cached per process and keyed by the table's (max version, rows); the key is re-checked at most every
CHECK_SECONDS, and edits made in this process invalidate it at once. While the table is not migrated (or empty) the packaged
fixture is used and company_suffixes() keeps the legacy FEB/FEBEST registry, so the live app works in any deploy order.
"""
import json
import re
import time
import unicodedata
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'catalog_code_suffixes.json'
LEGACY_COMPANY_SUFFIXES = {'FEB': 'FEBEST', 'FEBEST': 'FEBEST'}
CHECK_SECONDS = 30
CONFIDENCE_ORDER = ['low', 'medium', 'high']
SIZE_RE = re.compile(r'^(STD|0\.\d{2}|\d\.\d{2}|0\d{2}|0?[1-9]0|100|075|125|150)$')
ALPHA_RE = re.compile(r'^[A-Z]{1,16}$')
SEP_RE = re.compile(r'((?:[-/_+() ]|(?<!\d)\.|\.(?!\d))+)')
LIFECYCLE_RE = re.compile(r'(?:[-_ /]+(?:ANULAD[OA]S?\d*|AULADO\d*))+[-_ ]*$|_\d+_DELETED$')
ROLE_BY_KIND = {'genuine': 'genuine', 'origin': 'origin', 'house_brand': 'house_brand', 'brand': 'brand', 'company_code': 'brand',
                'supplier_marker': 'neutral', 'commercial': 'neutral', 'lifecycle': 'lifecycle'}
COMPANY_KINDS = ('brand', 'company_code', 'house_brand')
SHAPE_PREFIXES = ('ALT_TAIL:', 'CODE:')
DELETED_TOKEN = '_<ID>_DELETED'

# Description head nouns (curated ERP abbreviations, exact match only: TERM is not TERMOSTATO) used by scope_overrides.
HEAD_SYN = {
    'SOPORTE': 'BASE', 'RODAMIENTO': 'BALINERA', 'BAL': 'BALINERA', 'BALIN': 'BALINERA', 'BUCHING': 'BUJE',
    'AMORTIGUADOR': 'AMORT', 'CILINDRO': 'CIL', 'MANGUERA': 'MANG', 'RET': 'RETENEDORA', 'RETEN': 'RETENEDORA',
    'CUBRE': 'CUBREPOLVO', 'VALVULA': 'VALV', 'LAMPARA': 'LAMP', 'RETROVISOR': 'RETROV', 'CIG': 'CIGUE',
    'CIGUENAL': 'CIGUE', 'EMPAQUE': 'EMP', 'CASQUETE': 'CASQ', 'ESCLAVO': 'ESCL', 'MASTER': 'MAST',
    'BOMBILLOS': 'BOMBILLO', 'BOBINA': 'COIL', 'CREM': 'CREMALLERA', 'BRACKET': 'BASE',
}
QUAL = {h: set(v.split()) for h, v in {
    'FILTRO': 'AIRE ATM ACEITE CABINA DIESEL GAS COMBUSTIBLE GASOLINA D/COMBUSTIBLE COOLANT',
    'BANDA': 'FRENO PARKING', 'MAST': 'FRENO CLUTCH',
    'SENSOR': 'OXIG OXIGENO O2 ABS CIG LEVA MAF VELOC VEL VVTI VALVULA IAC TEMP TPS MAP KNOCK FILTRO PRESION PEDAL ACEL RIEL SELECTOR',
    'BOMBA': 'AGUA GAS GASOLINA DIESEL ACEITE P/W PW CEBADORA WIPER VACIO CLUTCH',
    'TERM': 'EXT ESTAB CREM INT', 'BASE': 'AMORT MOTOR TRANS CAJA MOFLE PALANCA COMPRESOR DIF',
    'EMP': 'TAPA CABEZ OVER CUELLO ESC ADM MULTIPLE MANIFOR',
    'TAPA': 'VALV DIST ACEITE GAS RADIADOR MOTOR SOL COOLANT CRANKCASE FILTRO DIESEL AGUA TIEMPO',
    'MANG': 'FRENO BYPASS TOMA TOME P/S PS RADIADOR RAD GASOLINA AGUA VACIO AIRE ACEITE CLUTCH',
    'DISCO': 'FRENO CLUTCH',
    'KIT': 'EMBRAGUE CLUTCH TAMBOR CALIPER CLIP CADENA MAST CORREA ESCLAVO BOMBA CAJA CREM PALANCA CABLE CARBURADOR',
    'BALINERA': 'CLUTCH TENSOR CENTRO', 'MOTOR': 'ARRANQUE ABAN ABANICO BLOWER WIPER',
    'CABLE': 'FRENO BUJIA CAMBIO ACEL CLUTCH TAPA VEL BATERIA MALETERO',
    'VALV': 'ADM ESC REPARTIDORA INYECTOR', 'POLEA': 'CIG TENSOR TENSORA ALT A/C AC',
    'TENSOR': 'CORREA CADENA HIDRA ALT', 'CASQ': 'BIELA BANCADA LEVA EJE', 'BOTA': 'EXT INT CREM CREMALLERA AMORT FLECHA',
    'BUJE': 'MUELLE MACHETE CREM CREMALLERA BRAZO TOPE PUENTE CROSSMEMBER BARRA PALANCA BIELA AMORT AMORTIGUADOR DIF TENSOR WIPER',
    'SWITCH': 'TEMP FRENO ARRANQUE REVERSA IGNICION PITO LUZ VENTANA', 'TOPE': 'AMORT AMORTIGUADOR MUELLE PUERTA',
    'CORREA': 'TIEMPO MOTOR ALT AC A/C PS P/S V', 'EJE': 'COMPLETO LEVA MANDO',
    'PORTA': 'BROCHA FILTRO FUSIBLE PLACA LAMPARA', 'REGULADOR': 'VIDRIO PRESION',
    'BARRA': 'CENTRO ESTABILIZADOR ESTAB TORSION TORCION',
}.items()}
QUAL_SYN = {'GASOLINA': 'GAS', 'COMBUSTIBLE': 'GAS', 'D/COMBUSTIBLE': 'GAS', 'OXIGENO': 'OXIG', 'O2': 'OXIG', 'VEL': 'VELOC',
            'EMBRAGUE': 'CLUTCH', 'PW': 'P/W', 'PS': 'P/S', 'RAD': 'RADIADOR', 'TOME': 'TOMA', 'DIFER': 'DIF', 'DIFF': 'DIF',
            'TRANSFER': 'TRANSF', 'TRANF': 'TRANSF', 'TRANSM': 'TRANS', 'TENSORA': 'TENSOR', 'A/C': 'AC', 'ABANICO': 'ABAN',
            'CREMALLERA': 'CREM', 'AMORTIGUADOR': 'AMORT', 'ESTABILIZADOR': 'ESTAB', 'TORCION': 'TORSION', 'ESCLAVO': 'ESCL'}
QUAL_GROUP = {('CORREA', q): 'ACCESORIO' for q in ('MOTOR', 'ALT', 'AC', 'P/S', 'V')}
FILLER = {'DE', 'PARA', 'P', 'EL', 'LA'}


def up(s):
    s = unicodedata.normalize('NFKD', (s or '').upper())
    return ''.join(c for c in s if not unicodedata.combining(c))


def shape(s):
    return re.sub(r'[A-Z]', 'A', re.sub(r'\d', '9', s))


def has_digit(s):
    return any(c.isdigit() for c in s)


def clean_code(raw):
    return re.sub(r'\s+', ' ', up(raw).strip().replace('–', '-').replace('—', '-'))


def strip_lifecycle(s):
    m = LIFECYCLE_RE.search(s)
    return (s[:m.start()].rstrip('-_ /'), m.group(0).strip('-_ /')) if m else (s, '')


def description_head(desc):
    """'BUJE BIELA HYU ...' -> ('BUJE', 'BIELA'): head noun after the abbreviation map plus a part-type qualifier."""
    toks = re.findall(r'[A-Z][A-Z0-9/]*', up(desc))
    if not toks:
        return ('', '')
    h0 = re.match(r'[A-Z]+', toks[0]).group(0)
    h, q = HEAD_SYN.get(h0, h0), ''
    rest = [w for w in toks[1:4] if w not in FILLER]
    if h in QUAL and rest:
        w = QUAL_SYN.get(rest[0], rest[0])
        if w in QUAL[h] or rest[0] in QUAL[h]:
            q = QUAL_GROUP.get((h, w), w)
    return (h, q)


def _head(head):
    if not head:
        return None
    return (head, '') if isinstance(head, str) else tuple(head)


class Entry:
    __slots__ = ('token', 'cls', 'kind', 'confidence', 'status', 'owner_confirmed', 'attribution', 'alias_of', 'company',
                 'derived', 'meaning', 'components', 'scenario')

    def __init__(self, token, cls, kind='', confidence='low', status='needs_owner_label', owner_confirmed=False, attribution='',
                 alias_of=None, company=False, derived=False, meaning='', components=None, scenario=frozenset()):
        self.token, self.cls, self.kind, self.confidence = token, cls, kind or '', confidence or 'low'
        self.status, self.owner_confirmed, self.attribution = status, owner_confirmed, attribution or ''
        self.alias_of, self.company, self.derived, self.meaning = alias_of, company, derived, meaning or ''
        self.components, self.scenario = components, scenario

    @property
    def role(self):
        return ROLE_BY_KIND.get(self.kind, 'other') if self.cls == 'TAG' else self.cls.lower()

    @property
    def auto_eligible(self):
        """A TAG an AUTO-applied rename may strip: owner-confirmed, or a confident seed row (or confirmed in the scenario)."""
        if self.cls != 'TAG' or self.role == 'lifecycle':
            return False
        if self.components is not None:
            return all(c.auto_eligible for c in self.components)
        return (self.owner_confirmed or (self.status == 'seed_confirmed' and self.confidence in ('high', 'medium'))
                or self.token in self.scenario
                or (self.alias_of is not None and self.alias_of in self.scenario and self.status != 'needs_owner_label'))

    confirmed = auto_eligible

    @property
    def canon(self):
        return self.alias_of or self.token

    def brand_name(self):
        a = re.sub(r'^\s*(brands?|supplier)\s*:\s*', '', self.attribution or '', flags=re.I)
        a = re.sub(r'\(.*?\)', '', a).strip()
        if (not a or 'unidentified' in a.lower() or ' or ' in a or '?' in a
                or a.lower().startswith(('origin', 'lifecycle', 'commercial'))):
            return self.canon
        return a.upper()

    def label(self):
        if self.role == 'genuine':
            return 'GENUINE' if self.canon in ('G', 'GEN') else 'GENUINE:' + self.brand_name()
        if self.role == 'origin':
            return 'ORIGIN:' + (self.attribution.split(':', 1)[-1].strip() or self.canon)
        return self.brand_name()

    def brief(self):
        return {'token': self.token, 'class': self.cls, 'kind': self.kind, 'confidence': self.confidence,
                'status': self.status, 'auto_eligible': self.auto_eligible, 'derived': self.derived}


class SuffixTable:
    """rows: dicts with the CatalogCodeSuffix fields (alias_of as a token). confirm/as_tag build the owner-confirms and
    unlock-simulation scenarios of the OEM finder without touching the stored rows."""

    def __init__(self, rows, *, source='rows', version=None, confirm=frozenset(), as_tag=frozenset()):
        self.rows, self.source, self.version = list(rows), source, version
        self.confirm, self.as_tag = frozenset(confirm), frozenset(as_tag)
        self.exact, self.alt_tail, self.code_shape, self.scopes = {}, {}, {}, {}
        for r in self.rows:
            tok, e = r['token'], self._entry(r)
            if tok.startswith('ALT_TAIL:'):
                self.alt_tail[tok[len('ALT_TAIL:'):]] = e
            elif tok.startswith('CODE:'):
                self.code_shape[tok[len('CODE:'):]] = e
            elif tok == DELETED_TOKEN:
                self.exact['_DELETED'] = e
            else:
                self.exact[tok] = e
            for o in r.get('scope_overrides') or []:
                kind = o.get('kind') or ''
                scoped = dict(r, cls=o['cls'], tag_kind=kind if o['cls'] == 'TAG' else '',
                              variant_kind=kind if o['cls'] == 'VARIANT' else '', attribution=o.get('attribution') or '',
                              confidence=o.get('confidence') or r.get('confidence'),
                              creates_company_reference=o['cls'] == 'TAG' and kind in COMPANY_KINDS)
                self.scopes.setdefault(tok, []).append((set(o['heads']), self._entry(scoped, scoped=True)))
        self.multiword = {t for t in self.exact if ' ' in t}
        self.company = {r['token']: e.brand_name() for r in self.rows for e in [self.exact.get(r['token'])]
                        if e and r['cls'] == 'TAG' and r.get('tag_kind') == 'company_code' and r.get('creates_company_reference')}

    @classmethod
    def from_fixture(cls, **kwargs):
        return cls(fixture_rows(), source='fixture', **kwargs)

    def scenario(self, *, confirm=frozenset(), as_tag=frozenset()):
        return SuffixTable(self.rows, source=self.source, version=self.version, confirm=confirm, as_tag=as_tag)

    def _entry(self, r, scoped=False):
        cls = r['cls']
        kind = (r.get('tag_kind') if cls == 'TAG' else r.get('variant_kind')) or ''
        status = r.get('status') or 'needs_owner_label'
        if scoped:
            status = 'seed_confirmed' if r.get('confidence') in ('high', 'medium') and cls != 'UNKNOWN' else 'needs_owner_label'
        return Entry(r['token'], cls, kind, r.get('confidence') or 'low', status, bool(r.get('owner_confirmed')),
                     r.get('attribution') or '', r.get('alias_of'), bool(r.get('creates_company_reference')), False,
                     r.get('notes') or '', scenario=self.confirm)

    def classify(self, sep, tok, head=None):
        e = self._classify(sep, tok, _head(head))
        if e.cls == 'UNKNOWN' and e.token in self.as_tag:
            return Entry(e.token, 'TAG', 'brand', 'high', 'seed_confirmed', True, 'brand: ' + e.token, company=True, derived=True,
                         meaning='simulated owner label TAG', scenario=self.confirm)
        return e

    def _classify(self, sep, tok, head):
        if tok in self.exact:
            if head and tok in self.scopes:
                for heads, scoped in self.scopes[tok]:
                    if head[0] in heads or (head[0] + ' ' + head[1]) in heads:
                        return scoped
            return self.exact[tok]
        if '/' in tok:
            parts = [p for p in tok.split('/') if p]
            if parts and all(ALPHA_RE.match(p) for p in parts):
                sub = [self._classify('-', p, head) for p in parts]
                conf = min((e.confidence for e in sub), key=CONFIDENCE_ORDER.index)
                if all(e.cls == 'TAG' for e in sub):
                    return Entry(tok, 'TAG', 'brand', conf, 'derived', False, 'brands: ' + tok, company=True, derived=True,
                                 components=sub, scenario=self.confirm)
                if any(e.cls == 'VARIANT' for e in sub):
                    return Entry(tok, 'VARIANT', 'compound', conf, derived=True)
                return Entry(tok, 'UNKNOWN', 'compound', 'low', derived=True)
            return Entry(tok, 'UNKNOWN', 'code_compound', 'low', derived=True)
        if SIZE_RE.match(tok):
            return Entry(tok, 'VARIANT', 'size', 'high', 'seed_confirmed', derived=True)
        if has_digit(tok):
            e = self.code_shape.get((sep or '(NONE)') + shape(tok)) or self.code_shape.get('-' + shape(tok))
            return e or Entry('CODE:' + shape(tok), 'UNKNOWN', 'code_tail', 'low', derived=True)
        return Entry(tok, 'UNKNOWN', 'unseen', 'low', derived=True)

    def alt_tail_entry(self, tok):
        return self.alt_tail.get('/' + shape(tok)) or Entry('ALT_TAIL:/' + shape(tok), 'VARIANT', 'alt_oem_tail', 'high',
                                                            'seed_confirmed', derived=True)

    def lifecycle_entry(self, marker):
        e = self.exact.get('_DELETED' if marker.endswith('DELETED') else 'ANULADO')
        return e or Entry(marker, 'TAG', 'lifecycle', 'high', 'seed_confirmed', derived=True)

    def company_suffixes(self):
        return dict(LEGACY_COMPANY_SUFFIXES) if self.source == 'fixture' else dict(self.company)


def tokenize_chain(rest, table=None):
    """'-STD-TAIH' -> [('-','STD'),('-','TAIH')]; joins alpha compounds 'SPK/WAP' and known multi-word tokens
    ('NEW_ERA' -> 'NEW ERA'); reads '-.25' as size 0.25."""
    rest = re.sub(r'(^|[-/ ])\.(\d{2})(?=$|[-/ ])', r'\g<1>0.\2', rest)
    out, sep = [], ''
    for i, x in enumerate(SEP_RE.split(rest)):
        if i % 2:
            sep = x
        elif x:
            out.append([sep, x])
            sep = ''
    merged = []
    for sep, t in out:
        if (sep == '/' and merged and ALPHA_RE.match(t) and not SIZE_RE.match(t)
                and all(ALPHA_RE.match(p) for p in merged[-1][1].split('/'))):
            merged[-1][1] += '/' + t
        elif (table is not None and merged and ALPHA_RE.match(t) and ALPHA_RE.match(merged[-1][1])
              and (merged[-1][1] + ' ' + t) in table.multiword):
            merged[-1][1] += ' ' + t
        else:
            merged.append([sep, t])
    return [(s, t) for s, t in merged]


def classify_chain(rest, table=None, base_written=None, head=None, full_alternate=None):
    """Classify every token after a base. full_alternate(rest) -> written code finds a complete second OEM number after '/'
    (needs the OEM format systems of the finder); short slash tails expand into alternate codes of the same base."""
    table = table or suffix_table()
    items = []
    if base_written and rest.startswith('/') and full_alternate:
        written = full_alternate(rest[1:])
        if written:
            items.append({'sep': '/', 'tok': written, 'entry': table.alt_tail_entry(written), 'alt_code': written})
            rest = rest[1 + len(written):]
    for sep, tok in tokenize_chain(rest, table):
        if '/' in sep and base_written and re.fullmatch(r'[0-9A-Z]{1,6}', tok) and has_digit(tok):
            last = re.split(r'[-.]', base_written)[-1]
            if len(tok) <= len(last):
                items.append({'sep': sep, 'tok': tok, 'entry': table.alt_tail_entry(tok),
                              'alt_code': base_written[:len(base_written) - len(tok)] + tok})
                continue
        items.append({'sep': sep, 'tok': tok, 'entry': table.classify(sep, tok, head)})
    return items


def chain_summary(chain):
    tags = [c for c in chain if c['entry'].cls == 'TAG' and c['entry'].role != 'lifecycle']
    s = {
        'tokens': [c['tok'] for c in chain],
        'tags': [c['tok'] for c in tags],
        'tag_entries': [c['entry'] for c in tags],
        'variants': [c['tok'] for c in chain if c['entry'].cls == 'VARIANT' and c['entry'].kind != 'alt_oem_tail'],
        'variant_kinds': sorted({c['entry'].kind for c in chain if c['entry'].cls == 'VARIANT'}),
        'alt_codes': [c['alt_code'] for c in chain if c.get('alt_code')],
        'unknowns': [c['entry'].token for c in chain if c['entry'].cls == 'UNKNOWN'],
        'lifecycle': [c['tok'] for c in chain if c['entry'].role == 'lifecycle'],
        'genuine_tokens': [c['entry'].token for c in tags if c['entry'].role == 'genuine'],
    }
    labels = sorted({c['entry'].label() for c in tags if c['entry'].role in ('genuine', 'origin', 'house_brand', 'brand')})
    s['attribution'] = tuple(labels) if labels else ('BARE',)
    if s['unknowns'] or s['lifecycle']:
        s['chain_class'] = 'unknown' if s['unknowns'] else 'lifecycle'
    elif s['variants'] or s['alt_codes']:
        s['chain_class'] = 'variant'
    elif tags:
        s['chain_class'] = 'tag'
    else:
        s['chain_class'] = 'bare'
    return s


def strip_tags(code, head=None, table=None):
    """'54650-D4000-MANDO' -> ('54650-D4000', chain, 'tag'). The chain is the trailing run of digit-free tokens after the first
    token (plus a lifecycle marker); a digit-bearing token ends it, so the base is never cut inside a number. chain_class is
    bare/tag/variant/unknown/lifecycle: only a bare or tag chain makes the base a stripped equivalent."""
    table = table or suffix_table()
    body, marker = strip_lifecycle(clean_code(code))
    pieces = SEP_RE.split(body)
    tokens = [i for i in range(0, len(pieces), 2) if pieces[i]]
    k = len(tokens)
    while k > 1 and not has_digit(pieces[tokens[k - 1]]):
        k -= 1
    cut = sum(len(p) for p in pieces[:tokens[k - 1] + 1]) if tokens else len(body)
    chain = classify_chain(body[cut:], table, head=head)
    if marker:
        chain.append({'sep': '', 'tok': marker, 'entry': table.lifecycle_entry(marker)})
    return body[:cut], chain, chain_summary(chain)['chain_class']


def generic_base(seg, head=None, table=None):
    """Aftermarket base for a company reference: strip trailing neutral TAGs (-NP, -REMATE) and then at most ONE
    brand/house/genuine tag (the outermost), like company_reference() does with the last token."""
    table = table or suffix_table()
    toks = tokenize_chain(seg, table)
    i, stripped = len(toks), []
    while i > 1 and not has_digit(toks[i - 1][1]):
        e = table.classify(toks[i - 1][0], toks[i - 1][1], head)
        if e.cls != 'TAG':
            break
        stripped.append(e)
        i -= 1
        if e.role in ('brand', 'house_brand', 'genuine'):
            break
    return ''.join(s + t for s, t in toks[:i]).lstrip('-/ '), stripped


def classify(sep, tok, head=None, table=None):
    return (table or suffix_table()).classify(sep, tok, head)


def company_suffixes(table=None):
    """{token: brand} for TAG rows of kind company_code that create company references (FEB/FEBEST today)."""
    return (table or suffix_table()).company_suffixes()


def fixture_rows():
    with open(FIXTURE, encoding='utf-8') as f:
        return json.load(f)['rows']


FIELDS = ('token', 'match', 'cls', 'tag_kind', 'variant_kind', 'attribution', 'creates_company_reference', 'scope_overrides',
          'confidence', 'status', 'owner_confirmed', 'notes')
_cache = {'key': None, 'table': None, 'checked': float('-inf')}


def _guarded(read):
    """Run a read in a savepoint; None while the table does not exist (a deploy that has not migrated yet)."""
    from django.db import OperationalError, ProgrammingError, transaction
    try:
        with transaction.atomic():
            return read()
    except (ProgrammingError, OperationalError):
        return None


def table_state():
    from django.db.models import Count, Max
    from .catalog_suffix_models import CatalogCodeSuffix
    state = _guarded(lambda: CatalogCodeSuffix.objects.aggregate(version=Max('version'), rows=Count('pk')))
    return (state['version'], state['rows']) if state and state['rows'] else None


def _db_rows():
    from .catalog_suffix_models import CatalogCodeSuffix
    return _guarded(lambda: [dict(r, alias_of=r.pop('alias_of__token'))
                             for r in CatalogCodeSuffix.objects.values(*FIELDS, 'alias_of__token')])


def suffix_table():
    # Locals only: a concurrent invalidate() (threaded server) must never make this return None.
    now, table = time.monotonic(), _cache['table']
    if table is not None and now - _cache['checked'] < CHECK_SECONDS:
        return table
    state = table_state()
    if table is None or state != _cache['key']:
        rows = _db_rows() if state else None
        table = SuffixTable(rows, source='db', version=state[0]) if rows else SuffixTable.from_fixture()
        _cache.update(table=table, key=state if rows else None)
    _cache['checked'] = now
    return table


def current_suffix_table():
    """suffix_table() re-checked now rather than up to CHECK_SECONDS late (another worker may have just relabelled a token)."""
    _cache['checked'] = float('-inf')
    return suffix_table()


def invalidate():
    _cache.update(key=None, table=None, checked=float('-inf'))
