"""OEM reference library service (inventory refactor S4): the library's numbers are References (tipo oem with their manufacturer as
brand, mall.references.add_library_number), and this module reads and describes them the way the Biblioteca OEM showed the OEM table:
compact numbers, the status rule, the source kinds, the subgrupo a library part type maps to, and the catalog lines that carry a
number. From S10 it also keeps the library's helpers and choices (compact, manufacturer_name, spellings, citation_list, the old
statuses and source kinds), which lived with the OEM table's models until S10 archived those tables.

Until S8 the Biblioteca's JSON keeps the OEM table's legacy values: a number's id is its Reference's uid (the OEMReference id for the
numbers the library had before S4), its code is the compact number (compact(code): a short number's clave is its printed spelling), status is verified, declared, inferred or disputed
(Reference estado verificado, declarado, inferido, disputa) and a source's kind is one of the 8 old kinds (ReferenceSource keeps it in
detail.legacy_kind; a source written before S4 from a code's reference_source text maps back by its kind). LibraryNumber and
LibrarySource wrap the rows with those names (the read-only catalog/<id>/oem-equivalents route still shows a line's numbers that way;
the Biblioteca OEM's screen and API went in S10: library numbers are managed in Inventario's Referencia tab). Nothing here renames a
line, writes a Reference or calls an AI provider: the writer is mall.references (add_library_number, update, cite_library,
refresh_library_estado).
"""
import re
from collections import defaultdict
from datetime import datetime
from functools import reduce
from types import SimpleNamespace
import unicodedata

from django.db import connection
from django.db.models import CharField, Func, Q, Value
from django.db.models.functions import Replace, Upper

from .catalog_families import normalized_reference

REFERENCE_STATUSES = [('verified', 'Verificado'), ('declared', 'Declarado'), ('inferred', 'Inferido'), ('disputed', 'En disputa')]
SOURCE_KINDS = [('price_list', 'Lista de precios de distribuidor'), ('manufacturer_catalog', 'Catálogo del fabricante'),
                ('aftermarket_catalog', 'Catálogo de marca de repuesto'), ('ai_lookup', 'Búsqueda OEM con IA'),
                ('supplier_declaration', 'Declaración de proveedor'), ('catalog_approval', 'Aprobación en el catálogo'),
                ('oem_finder', 'Buscador OEM'), ('manual', 'Registro manual')]
# Sources a person registers (and may remove); the others follow the catalog's OEM codes and are never edited by hand.
MANUAL_KINDS = ('manual', 'price_list', 'manufacturer_catalog', 'aftermarket_catalog', 'supplier_declaration')
MAX_PRINTED = 20


def compact(code):
    """17801-30070 -> 1780130070, mr-968365 -> MR968365: the stored form of an OEM number (the catalog's normalized_reference)."""
    return normalized_reference(str(code or ''))


def manufacturer_name(value):
    return ' '.join(str(value or '').upper().split())


def spellings(forms, code):
    """Distinct uppercase spellings of this number other than the compact form itself; a spelling of another number is dropped."""
    out = []
    for form in forms or []:
        form = ' '.join(str(form or '').upper().split())[:80]
        if form and form != code and compact(form) == code and form not in out:
            out.append(form)
    return out[:MAX_PRINTED]


def citation_list(citations, *more):
    """Distinct non-blank citations in first-seen order, each cut to 500 characters."""
    out = []
    for text in [*(citations or []), *more]:
        text = str(text or '').strip()[:500]
        if text and text not in out:
            out.append(text)
    return out


VERIFIED_KINDS = {'price_list', 'manufacturer_catalog'}
DECLARED_KINDS = {'aftermarket_catalog', 'supplier_declaration', 'catalog_approval', 'manual'}
ALGO_RE = re.compile(r'^algo:(?P<finder>[^:]+):(?P<version>[^:]+):(?P<rule>.+):(?P<run>\d+)$', re.I)
# Characters the database strips where it has no REGEXP_REPLACE (SQLite): every separator the catalog's SKUs and codes use.
SEPARATORS = ' -/_.+()#,:;*&\'"[]{}|\\°!?@$%=<>~`^\t'

# The library's status and the Reference's estado (plan §3 S4: legacy values in the JSON and the filters until S8).
LIBRARY_ESTADO = {'verified': 'verificado', 'declared': 'declarado', 'inferred': 'inferido', 'disputed': 'disputa'}
LEGACY_STATUS = {estado: status for status, estado in LIBRARY_ESTADO.items()}
STATUS_LABELS = dict(REFERENCE_STATUSES)
KIND_LABELS = dict(SOURCE_KINDS)
# Plan A4: the library's source kinds and the ReferenceSource kind each becomes (catalog_approval citing "Declarado por" a supplier).
SOURCE_KIND_OF = {'price_list': 'catalogo', 'manufacturer_catalog': 'catalogo', 'aftermarket_catalog': 'catalogo', 'manual': 'biblioteca',
                  'supplier_declaration': 'proveedor', 'catalog_approval': 'admin', 'ai_lookup': 'ia', 'oem_finder': 'buscador'}
# A source with no legacy kind (one S2a wrote from a code's reference_source) reads back as the kind the library gave such a text.
LEGACY_KIND_OF = {'catalogo': 'aftermarket_catalog', 'biblioteca': 'manual', 'proveedor': 'supplier_declaration', 'admin': 'catalog_approval',
                  'ia': 'ai_lookup', 'buscador': 'oem_finder'}
LEGACY_DETAIL = ('legacy_kind', 'legacy_id', 'legacy_created_at')  # what a source's detail keeps for the legacy JSON, not shown

# A6 (OD9): the library's part types and the subgrupo (grupo, nombre) a line in referencia takes; the existing subgrupos keep their
# names (CV JOINT for the joints, DISCOS DE FRENO). Any other text (blank, about 80 short ERP labels) gives no subgrupo: it stays in
# the number's detail.part_type and the line's field_sources['subgrupo'] for Revisión IA.
LIBRARY_SUBGRUPOS = {
    'BOMBAS DE AGUA': ('REFRIGERACIÓN', 'BOMBAS DE AGUA'),
    'DISCOS DE FRENO': ('FRENOS', 'DISCOS DE FRENO'), 'DISCO FRENO': ('FRENOS', 'DISCOS DE FRENO'),
    'JUNTAS HOMOCINÉTICAS': ('TRANSMISIÓN', 'CV JOINT'),
    'SEMIEJES': ('TRANSMISIÓN', 'SEMIEJES'),
    'CUBOS DE RUEDA': ('RODAMIENTOS', 'CUBOS DE RUEDA'), 'HUB': ('RODAMIENTOS', 'CUBOS DE RUEDA'),
    'TENSORES DE CORREA': ('MOTOR', 'TENSORES DE CORREA'),
    'ACCESORIOS DE PASTILLAS': ('FRENOS', 'ACCESORIOS DE PASTILLAS'),
    'EMBRAGUES DE VENTILADOR': ('REFRIGERACIÓN', 'EMBRAGUES DE VENTILADOR'),
    'RODAMIENTOS DE RUEDA': ('RODAMIENTOS', 'RODAMIENTOS DE RUEDA'),
    'FILTRO AIRE': ('FILTROS', 'FILTROS DE AIRE'), 'FILTRO ATM': ('FILTROS', 'FILTROS DE TRANSMISIÓN'),
    'FILTRO CABINA': ('FILTROS', 'FILTROS DE CABINA'),
    'FILTRO GAS': ('FILTROS', 'FILTROS DE COMBUSTIBLE'), 'FILTRO DIESEL': ('FILTROS', 'FILTROS DE COMBUSTIBLE'),
    'COIL': ('ENCENDIDO', 'BOBINAS DE ENCENDIDO'),
}


def fold(text):
    """Upper case, single spaces, no accents: how a library part type is compared with the map."""
    text = unicodedata.normalize('NFKD', ' '.join(str(text or '').upper().split()))
    return ''.join(char for char in text if not unicodedata.combining(char))


SUBGRUPO_BY_FOLD = {fold(text): pair for text, pair in LIBRARY_SUBGRUPOS.items()}


def subgrupo_for(part_type, subgrupos=None):
    """(grupo, subgrupo) a library part type maps to: A6 (OD9), else the subgrupo with that name (the Biblioteca's Editar celdas offered the subgrupos'
    names) when a single grupo has one; None otherwise. subgrupos: {fold(name): [(grupo, name)]}
    as subgrupo_names() reads it (pass {} for A6 alone: S4's backfill maps the library by A6 only), read here when None."""
    key = fold(part_type)
    if not key:
        return None
    if key in SUBGRUPO_BY_FOLD:
        return SUBGRUPO_BY_FOLD[key]
    found = (subgrupo_names() if subgrupos is None else subgrupos).get(key) or []
    return found[0] if len(found) == 1 else None


def subgrupo_names():
    """{fold(name): [(grupo, name)]} of the subgrupos there are, the grupo by its FK (S5)."""
    from .models import PartType
    out = defaultdict(list)
    for grupo, name in PartType.objects.values_list('grupo__nombre', 'name').order_by('grupo__nombre', 'name'):
        if grupo and name:
            out[fold(name)].append((grupo, name))
    return dict(out)


def derived_status(sources):
    """verified: a distributor price list, the manufacturer's catalog, a manual entry checked against one, or an AI lookup approved
    into the catalog; declared: an aftermarket catalog, a supplier, a catalog approval or a manual entry; inferred: only the finder.
    sources: (legacy kind, detail) pairs."""
    status = 'inferred'
    for kind, detail in sources:
        detail = detail or {}
        if kind in VERIFIED_KINDS or (kind == 'manual' and detail.get('verified')) or (kind == 'ai_lookup' and detail.get('approved')):
            return 'verified'
        if kind in DECLARED_KINDS:
            status = 'declared'
    return status


def source_kind_of(legacy_kind, citation=''):
    """The ReferenceSource kind of a library source kind (A4): a catalog approval citing "Declarado por" is a supplier's."""
    if legacy_kind == 'catalog_approval' and (citation or '').startswith('Declarado por'):
        return 'proveedor'
    if legacy_kind in SOURCE_KIND_OF:
        return SOURCE_KIND_OF[legacy_kind]
    if legacy_kind in LEGACY_KIND_OF:  # already a ReferenceSource kind
        return legacy_kind
    raise ValueError(f'Tipo de fuente desconocido: {legacy_kind}.')


def legacy_kind_of(source):
    """The library's kind of a ReferenceSource (the Biblioteca's JSON until S8): the one it kept, else the one its kind stands for."""
    kept = (source.detail or {}).get('legacy_kind')
    if kept in SOURCE_KIND_OF:
        return kept
    if source.kind == 'proveedor' and (source.citation or '').startswith('Declarado por'):
        return 'catalog_approval'
    return LEGACY_KIND_OF.get(source.kind, 'manual')


def source_of(text):
    """(kind, citation, detail) of a Reference.reference_source as the library read it: 'algo:oem-finder:v2:current:7' is the OEM
    finder (run 7), an http(s) URL an AI lookup approved into the catalog, anything else (blank included) a catalog approval."""
    text = (text or '').strip()[:500]
    if text.lower().startswith('algo:'):
        match = ALGO_RE.match(text)
        return 'oem_finder', text, {'run': int(match['run']), 'rule': match['rule'], 'rules_version': match['version']} if match else {}
    if text.lower().startswith(('http://', 'https://')):
        return 'ai_lookup', text, {'approved': True}
    return 'catalog_approval', text, {}


def classify_number(manufacturer, code, forms=(), table=None):
    """(system, family) under the manufacturer's own OEM numbering systems (the OEM finder's shapes, read-only): TOYOTA 1780130070
    -> ('TOYOTA', '17801'). Blank when the manufacturer has no known system or no spelling fits one completely."""
    from . import oem_finder as of
    name = manufacturer_name(manufacturer)
    make = next((of.MAKE_TOKENS[word] for word in re.findall(r'[A-Z0-9]+', name) if word in of.MAKE_TOKENS), None)
    own = of.MAKE_SYSTEMS.get(make, ([], []))[0] if make else [system for system, brand in of.SYSTEM_BRAND.items() if brand == name]
    if not own:
        return '', ''
    if table is None:
        from .catalog_suffixes import suffix_table
        table = suffix_table()
    for form in [*forms, code]:
        hits = [hit for hit in of.system_hits(form, table) if hit['system'] in own and not hit['rest']]
        if hits:
            hit = min(hits, key=lambda h: own.index(h['system']))
            return hit['system'], (hit['lexkey'] or '')[:20]
    return '', ''


def part_defaults(description):
    """Description and part type (head noun and qualifier: FILTRO AIRE) read from a catalog description, for blank fields only."""
    from . import oem_finder as of
    from .catalog_suffixes import description_head
    text = ' '.join((description or '').upper().split())
    head, qualifier = description_head(text)
    kind = f'{head} {qualifier}'.strip() if len(head) > 2 and head not in of.MAKE_TOKENS else ''
    return {'description': text[:300], 'part_type': kind[:60]}


def merge_detail(old, new):
    """Source detail after another sighting: the catalog alternos add up, verified/approved only ever turn on, other keys stay."""
    merged = dict(old or {})
    for key, value in (new or {}).items():
        if key == 'part_codes':
            entries = {entry['id']: entry for entry in merged.get('part_codes') or []}
            entries.update({entry['id']: entry for entry in value or []})
            merged['part_codes'] = sorted(entries.values(), key=lambda entry: entry['id'])
        elif key in ('verified', 'approved'):
            merged[key] = bool(merged.get(key)) or bool(value)
        else:
            merged.setdefault(key, value)
    return merged


def compact_sql(field):
    """The compact form computed by the database: REGEXP_REPLACE on PostgreSQL, the catalog's separators stripped elsewhere."""
    if connection.vendor == 'postgresql':
        return Func(Upper(field), Value('[^A-Z0-9]+'), Value(''), Value('g'), function='REGEXP_REPLACE', output_field=CharField())
    return reduce(lambda expression, char: Replace(expression, Value(char), Value('')), SEPARATORS, Upper(field))


# --- the library's rows, as LibraryNumber shows them -------------------------------------------------------------------------------

def parse_time(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


class LibrarySource:
    """A ReferenceSource as the Biblioteca shows a library source: its old kind and label, citation, detail, who and when."""

    def __init__(self, source):
        self.source = source
        self.id, self.citation, self.created_by = source.pk, source.citation, source.created_by
        self.kind = legacy_kind_of(source)
        self.detail = {key: value for key, value in (source.detail or {}).items() if key not in LEGACY_DETAIL}
        self.created_at = parse_time((source.detail or {}).get('legacy_created_at')) or source.created_at

    def get_kind_display(self):
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def removable(self):
        return self.kind in MANUAL_KINDS


class LibraryNumber:
    """A library Reference with the OEM table's names (the Biblioteca's JSON until S8): id is its uid, manufacturer its brand, code its
    clave, status its estado's legacy value; description, part_type, applications, system and family come from its detail."""

    def __init__(self, ref, sources=None):
        detail = ref.detail or {}
        self.ref = ref
        self.pk = self.id = ref.uid
        # The compact number, as the OEM table stored it (a short number's clave is its printed spelling: OPEL 5 69 036 is 569036).
        self.manufacturer, self.code = ref.brand, compact(ref.code)
        self.printed_forms = list(dict.fromkeys(form for form in [ref.code, *(ref.printed_forms or [])] if form and form != self.code))
        self.system, self.family = detail.get('system', ''), detail.get('family', '')
        self.part_type, self.description, self.applications = detail.get('part_type', ''), detail.get('description', ''), detail.get('applications', '')
        self.status = LEGACY_STATUS.get(ref.estado, 'inferred')
        self.dispute_note, self.notes = ref.dispute_note, ref.notes
        self.created_by, self.created_at, self.updated_at, self.version = ref.created_by, ref.created_at, ref.updated_at, ref.version
        updated_by = detail.get('updated_by')
        self.updated_by = SimpleNamespace(username=updated_by) if updated_by else None
        replaced = ref.reemplazado_por if ref.reemplazado_por_id else None
        self.superseded_by = LibraryNumber(replaced) if replaced is not None and replaced.pk != ref.pk else None
        self.superseded_by_id = self.superseded_by.pk if self.superseded_by else None
        self._sources = sources

    @property
    def sources(self):
        rows = self._sources if self._sources is not None else self.ref.sources.filter(retirada=False).order_by('created_at', 'id')
        return SourceList([LibrarySource(source) for source in rows])

    def __str__(self):
        return f'{self.manufacturer} {self.code}'

    def get_status_display(self):
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def supersedes(self):
        from . import references
        return [LibraryNumber(row) for row in self.ref.reemplaza.filter(references.library_q()).order_by('brand', 'clave')]


class SourceList(list):
    """The sources of a LibraryNumber, which a serializer reads with .all()."""

    def all(self):
        return self


def wrap(refs):
    """LibraryNumbers for library References (their prefetched sources reused)."""
    out = []
    for ref in refs:
        cache = getattr(ref, '_prefetched_objects_cache', {})
        out.append(LibraryNumber(ref, sources=list(cache['sources']) if 'sources' in cache else None))
    return out


def linked_parts(refs):
    """{reference id: [{id, sku, is_OEM, active, via, code}]}: the catalog line that carries each library number (refs: References): its
    own line when it is a catalog line (via 'linea'; a line in referencia is the number's own and is left out). From S9 the SKU links
    (PartOEMLink, catalog and sku_name) are no longer read: the merge pass (mall.merge_pass) put the numbers a SKU's catalog item prints
    on that SKU's line, or opened a case for them. Merged lines are left out: their codes moved to the line they went into."""
    from .lines import catalog_q
    from .models import Part
    refs = list(refs)
    out = {ref.pk: [] for ref in refs}
    if not refs:
        return out
    lines = {line.pk: line for line in Part.objects.filter(catalog_q(), pk__in={ref.part_id for ref in refs})}
    for ref in refs:
        line = lines.get(ref.part_id)
        if line is not None:
            out[ref.pk].append({'id': line.pk, 'sku': line.sku, 'is_OEM': line.is_OEM, 'active': line.active, 'via': 'linea',
                                'code': ref.code})
    return out


def linked_q():
    """Q for library References some catalog line carries (the linked_parts rule), as an uncorrelated subquery."""
    from .lines import catalog_lines
    return Q(part__in=catalog_lines().values('pk'))
