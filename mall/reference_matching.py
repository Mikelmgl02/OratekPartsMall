"""Matching on References (inventory refactor S6.1, plan §3 S6): which Reference a supplier's item (a variant) hangs from, decided when
it arrives. The only writer of the S6 matching cases (MatchingCase kinds reference, code_change, variant, line and merge).

    resolve(subject)          the rules in order, read-only: a Resolution (attach, new_reference_on_line, case or no_line, and why)
    plan_load(item, data)     what a load does to an item, read-only (services.ingest_inventory and the Excel preview share it)
    carry_out(item, resolution)  write a Resolution: the Reference found or added, the item attached, or a line case, or place()
    sync_cases(item)          the cases a variant's own data asks for: its declared codes (rule 5b), a waiting code change
    placement(variant)        the main a new line gets for a variant no line takes (rule 4: OD6, OD20, Revisión IA's internal_code)
    place(variant)            mall.new_products.place (S6.2): a line in preparación for the variant, its main as placement says
    backlog(), recheck_merge_cases(), open_reference_merge_cases()   the matching worker's stages
    decide(case, action, ...) a person's decision on a case (matching_views: approve, dismiss, separar_referencia, merge, keep_apart)

The rules, in order (resolve), for an item without a Reference (new, or waiting in a line case):
1. Rules 1 and 2. The item's key is (clave of its code, its brand through mall.brands.resolve; a placeholder brand or the supplier's
   own name counts as none). Looked up in this order: the supplier's own interno References; the exclusive public References
   (clave, brand), then (clave, ''), then the brands of its familia with the same clave (D3: KAIYI and CHERY are one part only when the
   code itself matches, never by brand alone). A brandless item joins the one line its clave sits on. Its clave on two or more lines, or
   only a Reference in disputa, gives a line case and no Reference.
2. (3e-lazy, the aftermarket codes a supplier catalog filed under OEM numbers, left with S10: S9 made those codes References of the
   line their numbers sit on, and their table is archived. Such a code is found by rules 1 and 2 now.)
3. Rule 3 (D2). Trailing TAGs are stripped while each is auto-eligible (mall.catalog_suffixes.auto_eligible: -L, -RH and the other D4
   tokens never are). The base's clave on exactly one live line, the description heads agreeing, no spec_conflict and every stripped
   TAG a known brand suffix (brand_tag): a new Reference for the full code on that line (58411-1R000-GMB on 58411-1R000), its tipo from
   its brand's (a brandless item takes the brand an identified TAG names). Otherwise a line case on that line (an origin or genuine
   mark such as -J or -G: guard sufijo, a click links it).
4. Rule 5. A new attach whose description contradicts its line (spec_conflict) still hangs from the Reference, but in review (out of
   the line's stock) with a variant case.
5. Rule 4 and D1 (S0.9, kept). No code evidence: no_line, and place() makes a line in preparación (Productos nuevos) whose main is
   interno (scoped to the supplier) for a brandless or placeholder-brand short code (OD6), a Revisión IA internal_code, or a brand that
   is the supplier's own name (OD20); a shared Reference otherwise. Description words never link an item: they stay hints
   (mall.matching_engine.MatchIndex.hints) for Revisión IA. An item no line can be made for (a code with no letter or digit, a key
   place() refuses) waits in a line case that says why (refused_case), so no item is ever without a Reference and without a case (I10).
For an attached item (plan_load): a code change whose new key stays on its line applies on its own (S3, OD5); any other waits in
codigo_pendiente with a code_change case (rule 5b, out of stock: OD7). Each declared code not already on the item's line is one reference
case (rule 5b); nothing is stored on the item for it. A description change is checked against the line again (rule 5).

Lock order (plan §1.6.5): the supplier's Account FOR UPDATE, the item read unlocked (every writer of a supplier's items takes that lock
first), resolve, the catalog writes (a clave's advisory lock 724631114, then Part and Reference rows), the item locked, written.
"""
import logging
from collections import namedtuple
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import references

logger = logging.getLogger(__name__)

ACTIONS = ('attach', 'new_reference_on_line', 'case', 'no_line')
TAG = 's6'                 # detail key of what the S6 code makes (References, lines): the rollback finds it outside a manifest
PASS_SECONDS = 120         # the worker's backlog runs this long at most, then asks for another pass
BATCH = 500
# Brands that name no brand (a blank one says the same): the item's code is keyed as brandless.
PLACEHOLDER_BRANDS = {'GENERICO', 'GENERICA', 'GENERIC', 'S/M', 'SM', 'S/MARCA', 'SIN MARCA', 'N/A', 'NA', 'VARIOS', 'VARIAS', 'OTRO',
                      'OTROS', 'OTRA', 'OTRAS', 'NINGUNA', 'ALTERNO', 'ALTERNA', 'IMPORTADO', 'CHINA', 'TAIWAN'}
# OD17: the supplier catalogs (title as the citation starts), the brand that is their own, and whether their competitor codes were
# checked against the PDF (only those make inferido References).
CATALOGS = (('GMB Water Pump Catalog', 'GMB', True),
            ('ASVA Sport Catalogue Constant Velocity Joints', 'ASVA', True),
            ('ASVA Sport Catalogue Wheel Hub', 'ASVA', True),
            ('ASVA Tensioner', 'ASVA', True),
            ('ATE K24', 'ATE', True))
TAG_ROLES = ('brand', 'house_brand')
# D2 (owner, 2026-10-13, widened 2026-10-15): rule 3 links on its own through any auto-eligible TAG when the part names agree: a brand
# suffix (-GMB) and also an origin (-J, -JP), a genuine mark (-G) or a supplier or commercial marker (-NP, -K), the diagram's rule 3 and
# "keep merging suffix variants". A lifecycle TAG (ANULADO) is never auto-eligible, so the strip stops before it.
RULE3_ROLES = ('brand', 'house_brand', 'genuine', 'origin', 'neutral')
# What a case's fingerprint rests on, besides its evidence: bumped only when the fingerprints themselves change shape (never with the
# matching engine's VERSION, which would put every dismissed case back in review).
FINGERPRINT_VERSION = 1
KINDS = ('reference', 'code_change', 'variant', 'line', 'merge')
STATUSES = ('review', 'dismissed', 'applied')
# S10's 0086 turned the cases of the code before S6 into these kinds and statuses, keeping what each was in evidence['s10'] ({'kind',
# 'status'}): a supplier case became a line case, a catalog, catalog_reference or seed case a merge case, unmatched review and resolved
# applied (mall.migrations.0086's convert_cases).
ITEM_KINDS = ('reference', 'code_change', 'variant', 'line')
HOLDS_STOCK = ('variant', 'code_change')  # open cases that keep their item in review, out of the line's stock
METHODS = {'line': 'linea', 'variant': 'spec_conflict', 'code_change': 'code_change', 'reference': 'referencia_declarada',
           'merge': 'unificacion'}
REASONS = {
    'disputa': 'El código está en una referencia en disputa entre líneas: elige la línea o sepáralo.',
    'lineas': 'El código está en varias líneas: elige la línea correcta o separa la referencia.',
    'catalogo': 'Un catálogo de repuesto lo vincula a una línea, pero no es un catálogo revisado contra el PDF: confírmalo.',
    'noun': 'El código base está en una línea, pero el nombre del repuesto no coincide: confírmalo.',
    'spec_conflict': 'El código base está en una línea, pero hay una contradicción de posición, medida o aplicación.',
    'sufijo': 'El código base está en una línea, pero su sufijo no es una marca conocida (origen, genuino, comercial): confírmalo.',
}

Resolution = namedtuple('Resolution', 'action rule reference line new status cases evidence reason')
Resolution.__new__.__defaults__ = (None, None, None, 'pending', (), None, '')
LoadPlan = namedtuple('LoadPlan', 'plan resolution cases refresh')
LoadPlan.__new__.__defaults__ = (None,)  # refresh: the line in preparación whose description follows the item's new text


def _models():
    from .models import Part, Reference, SupplierItem
    return Part, Reference, SupplierItem


def converted_q(kind='supplier', prefix=''):
    """The cases 0086 converted from a legacy kind (supplier: the items' cases before S6, now line cases)."""
    return Q(**{f'{prefix}evidence__s10__kind': kind})


def _cases():
    from .matching_models import MatchingCase, MatchingDecision
    return MatchingCase, MatchingDecision


def now_iso():
    return timezone.now().isoformat()


def stamp(rule, item=None, **extra):
    """What detail[TAG] says about a row the S6 code made: the rule, the item, when."""
    return {'regla': rule, 'articulo': str(item.pk) if getattr(item, 'pk', None) else None, 'at': now_iso(), **extra}


# --- what is being matched -------------------------------------------------------------------------------------------------------------

def subject_of(item=None, data=None, supplier=None):
    """The item as resolve reads it: its code, brand and description (the load's data over the row's), its supplier and the row."""
    data = data or {}
    supplier = supplier or (getattr(item, 'supplier', None) if item is not None else None)
    return SimpleNamespace(
        code=references.code_key(data.get('codigo', getattr(item, 'codigo', ''))),
        brand=(data.get('brand', getattr(item, 'brand', '')) or '').strip().upper(),
        description=data.get('description', getattr(item, 'description', '')) or '',
        references=data.get('references', getattr(item, 'references', None)) or [],
        supplier_id=getattr(supplier, 'pk', supplier) if supplier is not None else getattr(item, 'supplier_id', None),
        supplier_name=getattr(supplier, 'name', '') or '',
        item=item)


class Lookups:
    """The reads resolve makes, cached for one call (or for a whole Excel preview, prefetched): live References by clave, brand
    spellings and familias, the suffix table. It never caches across writes."""

    def __init__(self):
        self.claves, self.spellings, self.roots, self.tipos, self.names = {}, {}, {}, {}, {}
        self.table = None

    def prefetch(self, codes):
        """Read every clave these codes (and their bases once TAGs are stripped) may need at once (the Excel preview's 500 rows)."""
        keys = set()
        for code in codes:
            code = references.code_key(code)
            if not code:
                continue
            keys.add(references.clave(code))
            base = strip_eligible(code, self)[0]
            if base:
                keys.add(references.clave(base))
        self._load_claves(keys - set(self.claves))
        return self

    def _load_claves(self, keys):
        _, Reference, _ = _models()
        keys = sorted(keys)
        for key in keys:
            self.claves.setdefault(key, [])
        for start in range(0, len(keys), 2000):
            rows = (Reference.all_objects.filter(clave__in=keys[start:start + 2000], estado__in=references.LIVE)
                    .select_related('part').order_by('pk'))
            for row in rows:
                self.claves[row.clave].append(row)


    def by_clave(self, key):
        """The live References of a clave (any supplier and estado but retirada), on live lines."""
        if key not in self.claves:
            self._load_claves({key})
        return [row for row in self.claves[key] if references.is_live(row.part)]


    def canonical(self, spelling):
        """mall.brands.resolve, cached."""
        spelling = (spelling or '').strip().upper()
        if spelling not in self.spellings:
            from .brands import resolve
            self.spellings[spelling] = resolve(spelling) if spelling else ''
        return self.spellings[spelling]

    def root(self, spelling):
        """The nombre heading the spelling's familia (mall.brands.familia_root), cached."""
        name = self.canonical(spelling)
        if name not in self.roots:
            from .brands import familia_root
            self.roots[name] = familia_root(name) if name else ''
        return self.roots[name]

    def same_familia(self, a, b):
        return bool(self.canonical(a)) and bool(self.canonical(b)) and self.root(a) == self.root(b)

    def brand_tipo(self, spelling):
        """Brand.tipo (vehiculo, repuesto or '') of the brand a spelling names. A spelling no brand has yet gets the tipo the brand will
        be created with when a Reference carries it (mall.references.ensure_brand): vehiculo for a vehicle make
        (mall.sku_proposals.vehicle_makes), repuesto otherwise."""
        name = self.canonical(spelling)
        if name not in self.tipos:
            from .brands import lookup
            found = lookup(name) if name else None
            if found is not None:
                self.tipos[name] = found.tipo
            elif name:
                from .sku_proposals import vehicle_makes
                makes = vehicle_makes()
                self.tipos[name] = 'vehiculo' if name in makes or name in set(makes.values()) else 'repuesto'
            else:
                self.tipos[name] = ''
        return self.tipos[name]

    def supplier_name(self, subject):
        if subject.supplier_name or subject.supplier_id is None:
            return subject.supplier_name
        if subject.supplier_id not in self.names:
            from .models import Account
            self.names[subject.supplier_id] = Account.objects.filter(pk=subject.supplier_id).values_list('name', flat=True).first() or ''
        return self.names[subject.supplier_id]

    def suffixes(self):
        if self.table is None:
            from .catalog_suffixes import suffix_table
            self.table = suffix_table()
        return self.table


def compact(code):
    """A code's letters and digits only (an aftermarket code as the catalogs file it)."""
    import re
    return re.sub(r'[^A-Z0-9]', '', (code or '').upper())


def effective_brand(subject, lookups):
    """The brand the item's code is keyed under: its canonical brand, or '' for a blank or placeholder brand, or the supplier's own name
    (its own code, OD20)."""
    name = lookups.canonical(subject.brand)
    if not name or name in PLACEHOLDER_BRANDS:
        return ''
    own = lookups.supplier_name(subject)
    if own and name in (lookups.canonical(own), ' '.join(own.upper().split())):
        return ''
    return name


def own_name_brand(subject, lookups):
    """Whether the item's brand is its supplier's own name (OD20)."""
    name, own = lookups.canonical(subject.brand), lookups.supplier_name(subject)
    return bool(name) and bool(own) and name in (lookups.canonical(own), ' '.join(own.upper().split()))


def brand_tag(entry):
    """Whether a suffix-table entry links on its own under rule 3 (D2): a TAG whose role is in RULE3_ROLES, every part of a slash compound
    too: a brand, house brand or company code (58411-1R000-GMB), and since 2026-10-15 an origin, genuine, supplier or commercial marker
    (J, G, NP, K). Any other TAG finds the base's line and waits for a click there (a line case, guard sufijo)."""
    from .catalog_suffixes import ROLE_BY_KIND
    components = getattr(entry, 'components', None)
    if components:
        return all(brand_tag(component) for component in components)
    kind = getattr(entry, 'tag_kind', None)  # a suffix table row's, or an Entry's kind (as auto_eligible reads them)
    if kind is None:
        kind = getattr(entry, 'kind', '')
    return getattr(entry, 'cls', None) == 'TAG' and ROLE_BY_KIND.get(kind or '', 'other') in RULE3_ROLES


def strip_eligible(code, lookups=None):
    """(base, stripped TAG entries) once a code's trailing TAGs are stripped while each is auto-eligible (rule 3: the shared
    mall.catalog_suffixes.auto_eligible), or (None, []) when none is: 58411-1R000-GMB → 58411-1R000. A position or size token (-L, -RH:
    D4) is no TAG and stops the strip. Whether the strip links on its own is rule 3's (brand_tag: D2)."""
    from . import catalog_suffixes
    table = lookups.suffixes() if lookups is not None else None
    try:
        base, chain, _ = catalog_suffixes.strip_tags(code, table=table)
    except Exception:  # a code the suffix parser cannot read has no TAG to strip
        return None, []
    stripped = []
    for link in reversed(chain):
        if not catalog_suffixes.auto_eligible(link['entry']):
            break
        stripped.append(link)
    if not stripped:
        return None, []
    kept = chain[:len(chain) - len(stripped)]
    return base + ''.join(f"{link['sep']}{link['tok']}" for link in kept), [link['entry'] for link in reversed(stripped)]


def tag_brand(entries, lookups):
    """The brand the outermost stripped brand or house_brand TAG names (an identified brand: no compound name), or ''."""
    from .brands import lookup
    tags = [entry for entry in entries if entry.cls == 'TAG' and entry.role in TAG_ROLES]
    if not tags:
        return ''
    name = tags[-1].brand_name()
    if not name or '/' in name:
        return ''
    found = lookup(name)
    return found.nombre if found is not None else ''


def tipo_for(brand, lookups):
    """The tipo of a public Reference the matching adds under this brand: oem for a vehicle brand, aftermarket for a brand of parts,
    otro otherwise (or brandless)."""
    tipo = lookups.brand_tipo(brand) if brand else ''
    return {'vehiculo': 'oem', 'repuesto': 'aftermarket'}.get(tipo, 'otro')


def catalog_of(citation):
    """(title, own brand, PDF-checked) of the supplier catalog a citation names (OD17); an unknown catalog's own brand is the first
    word of its title, and it is not checked."""
    title = (citation or '').split(' · pág.')[0].strip()
    for prefix, brand, checked in CATALOGS:
        if title.startswith(prefix):
            return title, brand, checked
    return title, (title.split() or [''])[0].upper(), False


# --- the rules -------------------------------------------------------------------------------------------------------------------------

def ref_row(row):
    return {'id': row.pk, 'code': row.code, 'brand': row.brand, 'tipo': row.tipo, 'estado': row.estado, 'line': str(row.part_id),
            'sku': row.part.sku, 'line_estado': row.part.estado}


def rank(row):
    """The best Reference of a line for an item: its supplier's own first, the main, the surest estado, the oldest."""
    return (row.supplier_id is None, not row.is_main, references.ESTADO_RANK.get(row.estado, 9), row.pk)


def supplier_text(line):
    """Whether a line's description is its items' own text: a line in preparación whose description no administrator wrote (the
    supplier's, or Revisión IA's fix of it). Rule 5 has nothing to compare there: the line follows its items (mall.new_products.refresh),
    so a supplier correcting its text never puts its own item in review."""
    if line is None or line.estado != 'preparacion':
        return False
    return (((line.field_sources or {}).get('description') or {}).get('source')) != 'admin'


def item_status(line, description, wanted=None):
    """(status, conflict) of an item hanging from `line`: review with a contradiction (rule 5), else matched on a catalog line,
    pending elsewhere. A line in preparación whose text is its items' (supplier_text) contradicts nothing."""
    from .matching_engine import spec_conflict
    from .variants import status_for
    conflict = (bool(description) and bool(line.description) and not supplier_text(line)
                and spec_conflict(description, line.description))
    return ('review' if conflict else status_for(line, wanted)), conflict


def written_brand(spelling, lookups=None):
    """The brand a Reference the S6 code writes carries (I12 from S6): the canonical Brand.nombre of a spelling or alias (TOY →
    TOYOTA), '' for a blank or placeholder brand (GENERICO, S/M: no brand), else the normalized spelling (mall.references.ensure_brand
    makes it a brand)."""
    name = (lookups or Lookups()).canonical(spelling)
    return '' if not name or name in PLACEHOLDER_BRANDS else name


def attach_to(subject, row, rule, evidence, lookups):
    """An attach Resolution to Reference `row`, with rule 5's check against its line."""
    line = row.part
    status, conflict = item_status(line, subject.description)
    cases = (variant_case(subject, line, row),) if conflict else ()
    return Resolution('attach', rule, row, line, None, status, cases, evidence)


def line_case_for(subject, rows, guard, rule, evidence, lines=None):
    """A line case Resolution: the item waits with no Reference while a person chooses among the lines."""
    lines = lines if lines is not None else sorted({row.part for row in rows}, key=lambda line: (line.sku, str(line.pk)))
    spec = {'kind': 'line', 'lines': [line.pk for line in lines], 'line_rows': lines, 'guard': guard, 'reference': None,
            'evidence': {**evidence, 'rule': rule, 'code': subject.code, 'brand': subject.brand, 'clave': references.clave(subject.code),
                         'refs': [ref_row(row) for row in rows]},
            'reason': REASONS.get(guard, REASONS['lineas'])}
    return Resolution('case', rule, None, None, None, 'pending', (spec,), spec['evidence'], spec['reason'])


def refused_case(subject, guard, message):
    """The line case of an item no line can take and place() cannot give one (a code with no letter or digit, one over 120 characters,
    a key the brandless reservation gives to other lines): it waits there with no Reference (I10) until the supplier fixes its code or
    a person chooses its line. Its fingerprint (code, brand, guard) keeps it from being rewritten on every pass."""
    key = references.clave(subject.code) if subject.code else ''
    spec = {'kind': 'line', 'lines': [], 'line_rows': [], 'guard': guard, 'reference': None,
            'evidence': {'rule': '4', 'code': subject.code, 'brand': subject.brand, 'clave': key, 'refs': [], 'motivo': message},
            'reason': f'{message} Corrige el código con el proveedor o elige la línea OEM del artículo.'.strip()}
    return Resolution('case', '4', None, None, None, 'pending', (spec,), spec['evidence'], spec['reason'])


def has_code(code):
    """Whether a code has a letter or a digit (a supplier's '.' or '-' is no code)."""
    import re
    return bool(re.search(r'[0-9A-Z]', (code or '').upper()))


def variant_case(subject, line, row):
    """The variant case of an item whose description contradicts the line it hangs from (row: its Reference, None while rule 3e has
    still to add it)."""
    return {'kind': 'variant', 'lines': [line.pk], 'line_rows': [line], 'guard': 'spec_conflict', 'reference': row,
            'evidence': {'item_description': subject.description, 'line_description': line.description, 'code': subject.code,
                         'brand': subject.brand, 'reference': ref_row(row) if row is not None else None},
            'reason': 'La descripción del artículo contradice la de la línea (posición, medida, aplicación o tipo de repuesto): queda '
                      'fuera de existencias hasta que lo revises.'}


def rule_1_2(subject, brand, lookups):
    """Rules 1 and 2 (and D3), or None when no live Reference has the item's key."""
    key = references.clave(subject.code)
    rows = lookups.by_clave(key)
    if not rows:
        return None
    evidence = {'clave': key}
    own = [row for row in rows if row.supplier_id is not None and row.supplier_id == subject.supplier_id and row.estado in references.EXCLUSIVE]
    if own:
        exact = [row for row in own if lookups.canonical(row.brand) == brand]
        blank = [row for row in own if not row.brand]
        found = sorted(exact or blank or (own if not brand else []), key=rank)
        if found:
            return attach_to(subject, found[0], 'interna', evidence, lookups)
    public = [row for row in rows if row.supplier_id is None]

    def tier(found, rule):
        """The Resolution of the rows one step of the lookup reaches, or None when it reaches none. A Reference in disputa among them
        takes no variant: a line case, unless it shares the one line of an exclusive one."""
        if not found:
            return None
        exclusive = [row for row in found if row.estado in references.EXCLUSIVE]
        lines = {row.part_id for row in found}
        if len(lines) == 1 and exclusive:
            pick = sorted(exclusive, key=lambda row: (bool(row.brand) if not brand else False, *rank(row)))[0]
            return attach_to(subject, pick, rule, evidence, lookups)
        guard = 'disputa' if any(row.estado == 'disputa' for row in found) else 'lineas'
        return line_case_for(subject, found, guard, rule, evidence)
    if brand:
        for rule, wanted in (('exacta', lambda row: lookups.canonical(row.brand) == brand),
                             ('sin_marca', lambda row: not row.brand),
                             ('familia', lambda row: bool(row.brand) and lookups.same_familia(row.brand, brand))):
            found = tier([row for row in public if wanted(row)], rule)
            if found is not None:
                return found
        return None
    found = tier(public, 'sin_marca')
    if found is not None and found.action == 'attach' and found.reference.brand:
        found = found._replace(rule='marca_de_la_linea')
    return found





def rule_3(subject, brand, lookups):
    """Rule 3 (D2): the code once its trailing auto-eligible TAGs are stripped, or None when no line has that base."""
    from .catalog_suffixes import description_head
    from .matching_engine import spec_conflict
    from .oem_finder import head_compat
    base, entries = strip_eligible(subject.code, lookups)
    if not base:
        return None
    key = references.clave(base)
    rows = [row for row in lookups.by_clave(key) if row.supplier_id is None]
    if not rows:
        return None
    evidence = {'base': base, 'clave_base': key, 'tags': [entry.token for entry in entries]}
    lines = sorted({row.part for row in rows}, key=lambda line: (line.sku, str(line.pk)))
    if any(row.estado == 'disputa' for row in rows):
        return line_case_for(subject, rows, 'disputa', '3', evidence, lines=lines)
    if len(lines) > 1:
        return line_case_for(subject, rows, 'lineas', '3', evidence, lines=lines)
    line = lines[0]
    if not head_compat(description_head(subject.description), description_head(line.description)):
        return line_case_for(subject, rows, 'noun', '3', evidence, lines=lines)
    if spec_conflict(subject.description, line.description):
        return line_case_for(subject, rows, 'spec_conflict', '3', evidence, lines=lines)
    if not all(brand_tag(entry) for entry in entries):  # D2: only a known brand suffix links on its own; any other waits for a click
        return line_case_for(subject, rows, 'sufijo', '3', evidence, lines=lines)
    named = brand or tag_brand(entries, lookups)
    new = {'line': line, 'code': subject.code, 'brand': named, 'tipo': tipo_for(named, lookups), 'estado': 'declarado',
           'source': '', 'rule': '3'}
    return Resolution('new_reference_on_line', '3', None, line, new, references_status(line), (), evidence)


def references_status(line):
    from .variants import status_for
    return status_for(line)


def resolve(subject, lookups=None):
    """Which Reference the item hangs from (the module's docstring): a Resolution, read-only. subject: subject_of(item, data)."""
    lookups = lookups or Lookups()
    brand = effective_brand(subject, lookups)
    if not has_code(subject.code):  # nothing to look up, and no line can be made of it: it waits in a line case (I10)
        return refused_case(subject, 'sin_codigo', 'El artículo no tiene código.')
    for rule in (rule_1_2, rule_3):
        found = rule(subject, brand, lookups)
        if found is not None:
            return found
    return Resolution('no_line', '4', evidence={'brand': brand, 'placement': placement_of(subject, lookups)})


# --- rule 4: a line of its own -------------------------------------------------------------------------------------------------------

def internal_code(item):
    """Whether Revisión IA (or a person) said the item's code is the supplier's internal code: its line outside the catalog (a new
    product, in preparación or rechazada) has the supplier's interno code as its main (from S6: new_products.make_interno, ai_fill).
    (What a proposal said before S6, SkuProposal.internal_code, is archived with it in S10.)"""
    if item is None or not getattr(item, 'pk', None):
        return False
    _, Reference, _ = _models()
    return bool(getattr(item, 'part_id', None)) and Reference.all_objects.filter(
        part_id=item.part_id, is_main=True, tipo='interno', supplier_id=item.supplier_id, part__estado__in=('preparacion', 'rechazada'),
        estado__in=references.LIVE).exists()


def placement_of(subject, lookups, *, interno=False):
    """The main of the line rule 4 makes: {code, brand, tipo, supplier}. interno (supplier set) for a brandless or placeholder-brand
    short code (OD6), Revisión IA's internal_code, a brand that is the supplier's own name (OD20), or when asked (Separar referencia);
    an interno main keeps the item's canonical brand (MOTION MOTORS for Motion Motors' own code), '' for a placeholder one. Otherwise a
    shared Reference: oem under a vehicle brand, aftermarket under a brand of parts (or one no brand has yet), otro for a brand of no
    tipo or none."""
    brand = effective_brand(subject, lookups)
    short = not references.oem_like(compact(subject.code))
    own = own_name_brand(subject, lookups)
    if interno or (not brand and short) or own or internal_code(subject.item):
        printed = lookups.canonical(subject.brand)
        kept = brand or (printed if own else '')
        return {'code': subject.code, 'brand': '' if kept in PLACEHOLDER_BRANDS else kept, 'tipo': 'interno',
                'supplier': subject.supplier_id}
    return {'code': subject.code, 'brand': brand, 'tipo': tipo_for(brand, lookups), 'supplier': None}


def placement(variant, *, interno=None):
    """What new_products.place gives a variant no line takes (rule 4, the interface 6.2 reads): the main's code, brand, tipo and
    supplier. interno=True (or the variant's _placement_interno, Separar referencia) asks for an interno main whatever the rules say."""
    if interno is None:
        interno = bool(getattr(variant, '_placement_interno', False))
    return placement_of(subject_of(variant), Lookups(), interno=interno)


def place(variant, *, actor=None, interno=False):
    """Rule 4, the shared interface with S6.2: mall.new_products.place gives the variant a line in preparación whose main is what
    placement() says (interno=True: Separar referencia), the variant hanging from it as pending (or joins the line in preparación that
    already has its key). Returns the line; raises (new_products.PlaceRefused) when it cannot, and the variant stays pending."""
    from .new_products import place as products_place
    variant._placement_interno = bool(interno)
    line, _ = products_place(variant, actor=actor, internal=bool(interno))
    return line


def enter_preparacion(line, *, item=None, actor=None):
    """Rule 7: a line in referencia (a library number no product carried) that a variant joins becomes a line in preparación
    (mall.lines.prepare: the matching's own move, nobody decided it; field_sources['estado'] says so, for Productos nuevos and the
    rollback)."""
    from .lines import prepare
    from .models import Part
    if line.estado != 'referencia':
        return line
    locked = Part.objects.select_for_update().get(pk=line.pk)
    if locked.estado == 'referencia':
        prepare(locked, item=item)
    return locked


def joined(item, line, *, prepared=False):
    """After a variant was hung on `line`, a line outside the catalog: mall.new_products.joined counts it there (a line in preparación
    takes version + 1, changed_at and its description from the items, unless rule 7 just moved it there; a rechazada one takes it with no
    notice, OD13)."""
    from .lines import CATALOG
    if line is None or line.estado in CATALOG:
        return
    from .new_products import joined as products_joined
    products_joined(line, item, prepared=prepared)


# --- writing a resolution ------------------------------------------------------------------------------------------------------------

def share_line(line):
    """Lock a line FOR SHARE (plan §1.6.5: the line before its References): loads may hang items from it at once, a merge or an edit
    waits for them, and they for it. A line outside the catalog is locked FOR UPDATE instead, since the item joining it writes it (rule
    7 moves a line in referencia to preparación; a line in preparación counts the change: mall.new_products.joined): two loads never
    both hold it shared and then wait for each other. False when it is no longer live (merged meanwhile)."""
    from django.db import connection
    from .lines import CATALOG
    if connection.vendor == 'postgresql':
        mode = 'SHARE' if line.estado in CATALOG else 'UPDATE'
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT estado, merged_into_id FROM mall_part WHERE id = %s FOR {mode}', [line.pk])
            row = cursor.fetchone()
        return row is not None and row[0] != 'unificada' and row[1] is None
    return references.is_live(line)


def share_reference(ref):
    """The Reference re-read under FOR SHARE locks on its line, then itself: None when a merge moved it to another line or retired it
    since it was read unlocked (resolve)."""
    _, Reference, _ = _models()
    if not share_line(ref.part):
        return None
    from django.db import connection
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1 FROM mall_partcode WHERE id = %s FOR SHARE', [ref.pk])
    fresh = Reference.all_objects.select_related('part').filter(pk=ref.pk).first()
    if fresh is None or fresh.estado == 'retirada' or fresh.part_id != ref.part_id or not references.is_live(fresh.part):
        return None
    return fresh


def carry_out(item, resolution, *, actor=None, save=False, queue=True, _again=False):
    """Write a Resolution on the item (a saved row): the Reference found, or the one added (rules 3e and 3), attached; or the item left
    without one in a line case; or place() (rule 4: when it fails, the item stays pending and a pass is queued). The cases the
    resolution names are opened. save=False leaves the item's own row unsaved when it only changes in memory (the caller saves it
    whole); place() always writes it. Returns the resolution actually carried out (a key another line took meanwhile turns an add into
    a line case)."""
    from .variants import attach
    if resolution.action == 'no_line':
        from .new_products import PlaceRefused
        line = None
        try:
            with transaction.atomic():
                line = place(item, actor=actor)
        except PlaceRefused as error:  # a reason a pass does not change: a line case says it (I10), and the item waits there
            if error.guard == 'sin_articulo':  # deleted meanwhile: nothing waits
                return resolution._replace(action='failed')
            refused = refused_case(subject_of(item), error.guard or 'colocacion', str(error))
            return carry_out(item, refused, actor=actor, save=save, queue=queue, _again=True)
        except Exception:  # its line is made by the next pass
            logger.exception('No se pudo colocar el artículo %s en Productos nuevos.', item.pk)
            if queue:
                from .matching_queue import enqueue_matching
                enqueue_matching()
            moved = item.reference_id is not None or item.part_id is not None or item.matching_status != 'pending'
            item.reference, item.part, item.matching_status = None, None, 'pending'
            item._attached = True
            if save and moved:  # an item already waiting is not written again (C6's I10 query reads updated_at)
                item.save(update_fields=['reference', 'part', 'matching_status', 'updated_at'])
            return resolution._replace(action='failed')
        item.refresh_from_db(fields=['reference', 'part', 'matching_status'])
        close_line_case(item, line, actor=None, how='colocado')
        return resolution._replace(line=line)
    if resolution.action == 'case':
        moved = item.reference_id is not None or item.part_id is not None or item.matching_status != 'pending'
        item.reference, item.part, item.matching_status = None, None, 'pending'
        item._attached = True
        if save and moved:  # an item already waiting is not written again on every pass
            item.save(update_fields=['reference', 'part', 'matching_status', 'updated_at'])
        for spec in resolution.cases:
            open_case(spec, item=item)
        return resolution
    ref = resolution.reference
    if resolution.action == 'new_reference_on_line':
        new = resolution.new
        if not share_line(new['line']):  # merged meanwhile: resolved again, once
            again = Resolution('no_line', '4') if _again else resolve(subject_of(item))
            return carry_out(item, again, actor=actor, save=save, queue=queue, _again=True)
        Part, _, _ = _models()
        new = {**new, 'line': Part._base_manager.get(pk=new['line'].pk)}  # read again under its lock: published meanwhile, say
        try:
            with transaction.atomic():
                ref = add_reference(new, item, actor=actor)
        except references.ReferenceConflict as error:
            rows = [error.reference] if error.reference is not None else []
            fallback = line_case_for(subject_of(item), rows, 'lineas', new['rule'], {'conflicto': error.message},
                                     lines=sorted({new['line'], *(row.part for row in rows)}, key=lambda line: str(line.pk)))
            return carry_out(item, fallback, actor=actor, save=save, queue=queue, _again=True)
    else:
        held = share_reference(ref)
        if held is None:  # a merge moved or retired it since resolve read it: resolved again, once
            if _again:
                return carry_out(item, Resolution('no_line', '4'), actor=actor, save=save, queue=queue, _again=True)
            return carry_out(item, resolve(subject_of(item)), actor=actor, save=save, queue=queue, _again=True)
        ref = held
    prepared = ref.part.estado == 'referencia'
    line = enter_preparacion(ref.part, item=item, actor=actor) if prepared else ref.part
    # The status from the line as it is under its lock (resolve read it unlocked: a line in preparación published since, say).
    status = item_status(line, item.description)[0]
    attach(item, ref, status=status, save=save)
    joined(item, line, prepared=prepared)
    for spec in resolution.cases:
        open_case({**spec, 'reference': spec.get('reference') or ref}, item=item)
    close_line_case(item, line, actor=None, how='vinculado')
    return resolution._replace(reference=ref, line=line)


def add_reference(new, item, *, actor=None):
    """The Reference rule 3e or 3 adds to a line (references.add: found when the line already has the key), its sources cited."""
    from .variants import declared_by
    source = new.get('source') or declared_by(item)
    ref, added = references.add(new['line'], new['code'], new['brand'], tipo=new['tipo'], estado=new['estado'], source=source,
                                actor=actor, detail={TAG: stamp(new['rule'], item)})
    for citation in new.get('citations', ())[:20]:
        if citation != source:
            references.cite(ref, citation, actor, kind='catalogo')
    return ref


# --- loads -----------------------------------------------------------------------------------------------------------------------------

def open_review_case(item, kinds=HOLDS_STOCK, ignore=None):
    """The open case that keeps the item in review: its code change, or a contradiction with the line it is on now (a variant case
    of a line it left holds nothing). ignore: a case being decided right now."""
    MatchingCase, _ = _cases()
    if item is None or not item.pk:
        return None
    held = Q(kind__in=[kind for kind in kinds if kind != 'variant'])
    if 'variant' in kinds:
        held |= Q(kind='variant', key=f'variant:{item.pk}:{item.part_id}')
    rows = MatchingCase.objects.filter(held, item_id=item.pk, status='review')
    if ignore is not None:
        rows = rows.exclude(pk=ignore.pk)
    return rows.order_by('created_at', 'pk').first()


def rejected_change(item, codigo, brand):
    """Whether a person rejected exactly this code change for the item (a dismissed code_change case with the same new code): a load
    that sends it again changes nothing."""
    MatchingCase, _ = _cases()
    if item is None or not item.pk:
        return False
    return MatchingCase.objects.filter(key=f'code_change:{item.pk}', status='dismissed', new_code=codigo, new_brand=brand).exists()


def attached_status(item, line, text, *, waiting=False):
    """(status, variant case spec or None) of an attached item whose code stays on its line: a description change is checked against
    the line again (rule 5: review, with a variant case); an open variant case keeps it in review while the description stays; a code
    change still waiting too (waiting); otherwise its line's status."""
    from .matching_engine import description
    from .variants import status_for
    if description(item.description) != description(text):
        if item_status(line, text)[1]:
            return 'review', variant_case(subject_of(item, {'description': text}), line, item.reference)
    elif item.matching_status == 'review' and open_review_case(item, ('variant',)) is not None:  # only a review item has one
        return 'review', None
    if waiting:
        return 'review', None
    return status_for(line), None


def plan_load(item, data, *, lookups=None, supplier=None):
    """What a load does to an item, read-only (LoadPlan: the mall.variants.Plan carry_out_load writes, the Resolution of an item without
    a Reference, the cases to open). item None: a new item. An item without a Reference goes through resolve; an attached one keeps
    its line: the same code keeps it (rule 5 on a description change), a code change applies on its own when its new key stays on the
    line, and otherwise waits in codigo_pendiente with a code_change case (the module's docstring)."""
    from .variants import Plan, reference_for
    codigo, brand = references.code_key(data['codigo']), (data.get('brand') or '').strip().upper()
    text = data.get('description', getattr(item, 'description', '') or '')
    if item is None or item.reference_id is None:
        resolution = resolve(subject_of(item, data, supplier), lookups)
        part_id = resolution.line.pk if resolution.line is not None else None
        return LoadPlan(Plan(part_id, resolution.status, codigo, brand, (None, None), resolution.reference, 'directo'), resolution, ())
    line = item.part
    from .matching_engine import description
    # A line in preparación follows its items' text (mall.new_products.refresh, after the row is saved; its lock is taken first).
    refresh = line.pk if line.estado == 'preparacion' and description(item.description) != description(text) else None
    if (codigo, brand) == (item.codigo, item.brand):  # the same code (a change that waited and is sent back is dropped)
        status, spec = attached_status(item, line, text)
        return LoadPlan(Plan(item.part_id, status, codigo, brand, (None, None), item.reference, 'igual'), None, (spec,) if spec else (),
                        refresh)
    waiting = (item.codigo_pendiente or item.codigo, item.brand if item.brand_pendiente is None else item.brand_pendiente)
    if (codigo, brand) == waiting:
        return LoadPlan(Plan(item.part_id, 'review', item.codigo, item.brand, (item.codigo_pendiente, item.brand_pendiente),
                             item.reference, 'pendiente'), None, (code_change_case(item, codigo, brand),), refresh)
    if rejected_change(item, codigo, brand):  # a person turned this change down: the item keeps its code and stays as it is
        status, spec = attached_status(item, line, text)
        return LoadPlan(Plan(item.part_id, status, item.codigo, item.brand, (None, None), item.reference, 'rechazado'), None,
                        (spec,) if spec else (), refresh)
    if references.clave(codigo) == references.clave(item.codigo) and (brand == item.brand or not item.brand):
        found = item.reference if brand == item.brand else reference_for(line, codigo, brand, item.supplier_id, exact=True)[0]
        free = found is not None or not references.conflicts(codigo, brand, line=line.pk, estado='declarado').exclude(part_id=line.pk).exists()
        if free:
            status, spec = attached_status(item, line, text)
            return LoadPlan(Plan(item.part_id, status, codigo, brand, (None, None), found, 'misma_clave'), None, (spec,) if spec else (),
                            refresh)
    found = references.find(codigo, brand, supplier=item.supplier_id)
    if found is not None and found.part_id == item.part_id:
        status, spec = attached_status(item, line, text)
        return LoadPlan(Plan(item.part_id, status, codigo, brand, (None, None), found, 'misma_linea'), None, (spec,) if spec else (),
                        refresh)
    return LoadPlan(Plan(item.part_id, 'review', item.codigo, item.brand, (codigo, brand), item.reference, 'pendiente'), None,
                    (code_change_case(item, codigo, brand),), refresh)


def carry_out_load(item, load, *, actor=None):
    """Write a LoadPlan on the item (a saved row; its own columns are left for the caller to save): an item without a Reference
    through carry_out (its Resolution), an attached one through mall.variants.carry_out (its code, a waiting code, its Reference).
    Returns the Resolution carried out, or None."""
    from .variants import carry_out as write_plan
    if load.resolution is not None:
        item.codigo, item.brand = load.plan.codigo, load.plan.brand
        item.codigo_pendiente = item.brand_pendiente = None
        return carry_out(item, load.resolution, actor=actor)
    if load.refresh is not None and item.part is not None:
        share_line(item.part)  # FOR UPDATE: its description follows the item (finish_load), the line locked before the item (§1.6.5)
    write_plan(item, load.plan, actor=actor)
    return None


def finish_load(item, load, *, actor=None, changed=True):
    """After the load's row is saved: its cases (a code change waiting, a contradiction, its declared codes), and the cases it no longer
    needs closed."""
    for spec in load.cases:
        if spec is not None:
            open_case(spec, item=item)
    if load.plan.rule != 'pendiente' and item.codigo_pendiente is None and item.brand_pendiente is None:
        drop_open(item, 'code_change')
    if item.reference_id is not None and not any(spec and spec['kind'] == 'variant' for spec in load.cases):
        from .matching_engine import description
        held = open_review_case(item, ('variant',))
        if held is not None and held.evidence.get('item_description') is not None and \
                description(held.evidence['item_description']) != description(item.description):
            close_case(held, 'applied', actor=None, action='automatic', note='La descripción ya no contradice la línea.')
            settle_item(item)
    if load.refresh is not None and item.part_id == load.refresh:
        from .models import Part
        from .new_products import refresh
        line = Part._base_manager.filter(pk=load.refresh).first()
        if line is not None:
            refresh(line, save=True)  # the new text, unless a person wrote the line's (version + 1 when it changes)
    if changed or load.resolution is not None:
        sync_declared(item)


def settle_item(item, *, ignore=None):
    """An attached item that no case holds any more (ignore: the case being decided) takes its line's status (matched on a catalog
    line)."""
    from .variants import status_for
    SupplierItem = _models()[2]
    item.refresh_from_db(fields=['reference', 'part', 'matching_status', 'codigo_pendiente', 'brand_pendiente'])
    if (item.reference_id is None or open_review_case(item, ignore=ignore) is not None or item.codigo_pendiente is not None
            or item.brand_pendiente is not None):
        return item
    status = status_for(item.part)
    if status != item.matching_status:
        SupplierItem.objects.filter(pk=item.pk).update(matching_status=status, updated_at=timezone.now())
        item.matching_status = status
    return item


# --- the cases ---------------------------------------------------------------------------------------------------------------------------

def line_row(line):
    from .matching_engine import part_row
    return {**part_row(line), 'estado': line.estado}


def code_change_case(item, codigo, brand):
    found = references.find(codigo, brand, supplier=item.supplier_id)
    lines = [item.part] + ([found.part] if found is not None and found.part_id != item.part_id else [])
    return {'kind': 'code_change', 'lines': [line.pk for line in lines], 'line_rows': lines, 'reference': item.reference,
            'new_code': codigo, 'new_brand': brand, 'guard': '',
            'evidence': {'old': {'code': item.codigo, 'brand': item.brand}, 'new': {'code': codigo, 'brand': brand},
                         'holder': ref_row(found) if found is not None else None},
            'reason': f'El proveedor cambió el código de {item.codigo} a {codigo}: el artículo queda fuera de existencias hasta que lo '
                      'apruebes o lo rechaces.'}


def reference_case(item, declared, holders):
    """The reference case of one declared code: the lines that hold its key are listed with the item's."""
    lines = [item.part] + sorted({row.part for row in holders if row.part_id != item.part_id}, key=lambda line: (line.sku, str(line.pk)))
    return {'kind': 'reference', 'lines': [line.pk for line in lines], 'line_rows': lines,
            'reference': next((row for row in holders), None), 'new_code': declared['code'], 'new_brand': declared.get('brand', ''),
            'guard': 'lineas' if holders else '',
            'evidence': {'declared': declared, 'holders': [ref_row(row) for row in holders]},
            'reason': (f'El proveedor declara {declared.get("brand", "")} {declared["code"]} para este artículo; ese código está en otra '
                       'línea.' if holders else f'El proveedor declara {declared.get("brand", "")} {declared["code"]} para este artículo: '
                       'apruébalo para agregarlo a la línea.').replace('  ', ' ')}


def merge_case_spec(lines, *, evidence, reason, guard='', batch_key='', route='caso'):
    """A merge case among live lines not kept apart (a pair kept apart is never proposed: CatalogSeparation, on canonical lines), or
    None when fewer than two remain."""
    from .line_merge import apart, separated_pairs
    pairs = separated_pairs([line.pk for line in lines])
    kept = []
    for line in sorted(lines, key=lambda line: (line.estado != 'publicada', line.sku, str(line.pk))):
        if references.is_live(line) and not any(apart(line.pk, other.pk, pairs) for other in kept):
            kept.append(line)
    if len(kept) < 2:
        return None
    return {'kind': 'merge', 'lines': [line.pk for line in kept], 'line_rows': kept, 'guard': guard, 'batch_key': batch_key,
            'route': route, 'evidence': evidence, 'reason': reason, 'reference': None}


def case_key(spec, item=None):
    kind = spec['kind']
    if kind == 'line':
        return f'line:{item.pk}'
    if kind == 'code_change':
        return f'code_change:{item.pk}'
    if kind == 'variant':
        return f'variant:{item.pk}:{spec["lines"][0]}'
    if kind == 'reference':
        return f'reference:{item.pk}:{references.clave(spec["new_code"])}|{spec.get("new_brand", "")}'[:240]
    if spec.get('batch_key') and spec.get('key'):
        return spec['key']
    key = 'merge:' + ':'.join(sorted(str(pk) for pk in spec['lines']))
    if len(key) > 240:  # more than six lines: their digest, so two sets of lines never share a truncated key
        from .matching_engine import digest
        key = 'merge:' + digest(sorted(str(pk) for pk in spec['lines']))
    return key


def fingerprint_of(spec, item=None):
    """What a case rests on: a change to it reopens the case (and clears its AI answer); anything else is refreshed in place. A merge
    case rests on its lines (and its batch): the codes that brought them together are evidence, gathered as they come (open_case), so
    two codes pointing at the same lines never rewrite it in turn."""
    from .matching_engine import description, digest
    kind, evidence = spec['kind'], spec.get('evidence') or {}
    lines = sorted(str(pk) for pk in spec['lines'])
    if kind == 'line':
        basis = [evidence.get('code'), evidence.get('brand'), lines, spec.get('guard', '')]
    elif kind == 'variant':
        basis = [description(evidence.get('item_description', '')), description(evidence.get('line_description', '')), lines]
    elif kind == 'code_change':
        basis = [evidence.get('old'), evidence.get('new')]
    elif kind == 'reference':
        basis = [spec['new_code'], spec.get('new_brand', ''), (evidence.get('declared') or {}).get('ref_type', ''), lines]
    else:
        basis = [lines, spec.get('batch_key', '')]
    return digest([FINGERPRINT_VERSION, kind, basis])


def open_case(spec, *, item=None, status='review'):
    """Create or refresh the case `spec` describes (its key: case_key). A changed fingerprint rewrites it and puts it back in review
    (its AI answer and decision cleared); an unchanged one keeps its status (a dismissed case stays dismissed) and only its rows are
    refreshed. Returns the case."""
    from .matching_engine import item_row
    MatchingCase, _ = _cases()
    key = spec.get('key') or case_key(spec, item)
    fingerprint = fingerprint_of(spec, item)
    rows = spec.get('line_rows') or []
    ref = spec.get('reference')
    fields = {'kind': spec['kind'], 'item': item if spec['kind'] in ITEM_KINDS else None,
              'sources': [item_row(item)] if item is not None and spec['kind'] in ITEM_KINDS else [line_row(line) for line in rows[:1]],
              'candidates': [line_row(line) for line in rows] if spec['kind'] != 'merge' else [line_row(line) for line in rows[1:]],
              'target_sku': rows[0].sku if spec['kind'] == 'merge' and rows else '',
              'method': spec.get('method') or METHODS[spec['kind']], 'reason': spec.get('reason') or '',
              'reference': ref if getattr(ref, 'pk', None) else None, 'new_code': spec.get('new_code', '') or '',
              'new_brand': spec.get('new_brand', '') or '', 'evidence': spec.get('evidence') or {}, 'guard': spec.get('guard', '') or '',
              'route': spec.get('route', 'caso'), 'batch_key': spec.get('batch_key', '') or ''}
    case, created = MatchingCase.objects.get_or_create(key=key, defaults={**fields, 'fingerprint': fingerprint, 'status': status})
    if created:
        case.lines.set(spec['lines'])
        return case
    if spec['kind'] == 'merge' and case.fingerprint == fingerprint and 'codes' in fields['evidence']:
        known = list((case.evidence or {}).get('codes') or [])  # every code that brought these lines together, once each
        fields['evidence'] = {**fields['evidence'], 'codes': known + [row for row in fields['evidence']['codes'] if row not in known]}
    # The catalog rows a merge case waits on (mall.merge_pass.keep_case_rows) and which of them 0088 added (s10_rows), what 0086 converted.
    for kept in ('catalog_rows', 's10_rows', 's10'):
        if kept in (case.evidence or {}) and kept not in fields['evidence']:
            fields['evidence'] = {**fields['evidence'], kept: case.evidence[kept]}
    if case.fingerprint != fingerprint:
        for name, value in fields.items():
            setattr(case, name, value)
        case.fingerprint, case.status, case.ai, case.decided_by, case.decided_at = fingerprint, status, {}, None, None
        case.save()
        case.lines.set(spec['lines'])
        return case
    stale = {name: value for name, value in fields.items() if name in ('sources', 'candidates', 'evidence', 'reason', 'target_sku')
             and getattr(case, name) != value}
    if stale:  # the rows a person reads, refreshed without moving the case in the list
        MatchingCase.objects.filter(pk=case.pk, fingerprint=fingerprint).update(**stale)
        for name, value in stale.items():
            setattr(case, name, value)
    return case


def close_case(case, status, *, actor=None, action='automatic', note='', evidence=None):
    """Decide a case: applied or dismissed, by whom (None: the matching itself), with a MatchingDecision."""
    MatchingCase, MatchingDecision = _cases()
    case.status = status
    case.decided_by = actor if getattr(actor, 'pk', None) else None
    case.decided_at = timezone.now()
    if note:
        case.evidence = {**(case.evidence or {}), 'nota': note}
    case.save(update_fields=['status', 'decided_by', 'decided_at', 'evidence', 'updated_at'])
    MatchingDecision.objects.create(case=case, actor=case.decided_by, action=action,
                                    evidence={'kind': case.kind, 'fingerprint': case.fingerprint, **(evidence or {}),
                                              **({'note': note} if note else {})})
    return case


def drop_open(item, kind, keep=()):
    """Undecided cases of an item that no longer apply (a code change undone, a code no longer declared): deleted, since nobody decided
    anything on them. Decided ones stay as history."""
    MatchingCase, _ = _cases()
    rows = MatchingCase.objects.filter(item_id=item.pk, kind=kind, status='review', decided_at__isnull=True).exclude(key__in=list(keep))
    return rows.delete()[0]


def close_line_case(item, line, *, actor=None, how='vinculado'):
    """The line case of an item that got a Reference (attached by a later load, a merge that left one line, place()): applied."""
    MatchingCase, _ = _cases()
    case = MatchingCase.objects.filter(key=f'line:{item.pk}', status='review').first()
    if case is not None:
        close_case(case, 'applied', actor=actor, note=f'{"Colocado en Productos nuevos" if how == "colocado" else "Vinculado"}: '
                                                     f'{getattr(line, "sku", "")}.', evidence={'line': str(getattr(line, "pk", ""))})
    return case


def declared_holders(declared, item, lookups=None):
    """The live public References of a declared code's key (its brand, none, or its familia), on any line."""
    lookups = lookups or Lookups()
    key = references.clave(references.code_key(declared['code']))
    brand = lookups.canonical(declared.get('brand', ''))
    rows = [row for row in lookups.by_clave(key) if row.supplier_id is None or row.supplier_id == item.supplier_id]
    if brand:
        rows = [row for row in rows if not row.brand or lookups.canonical(row.brand) == brand or lookups.same_familia(row.brand, brand)]
    return sorted(rows, key=lambda row: (row.part_id != item.part_id, *rank(row)))


def sync_declared(item, lookups=None):
    """Rule 5b: one reference case per code the item declares (its references) that is not already on its line; the open ones it no
    longer declares are dropped. An item without a line has none (its line case lists its declared codes)."""
    from .sku_proposals import merge_references
    if item.reference_id is None or item.part_id is None:
        return 0
    lookups = lookups or Lookups()
    declared, seen = [], set()
    # One case per key: two spellings of one code (04465-02220 and 0446502220) or of one brand (TOY and TOYOTA) are the same code; the
    # first in merge_references' stable order is kept, so a sync never flips the case between them.
    for row in merge_references(item.references or []):  # one at a time: a malformed code is skipped, not the rest with it
        key = (references.clave(references.code_key(row['code'])), lookups.canonical(row.get('brand', '')))
        if key not in seen:
            seen.add(key)
            declared.append(row)
    keys = []
    for row in declared:
        holders = declared_holders(row, item, lookups)
        spec = reference_case(item, row, holders)
        key = case_key(spec, item)
        if any(holder.part_id == item.part_id for holder in holders):  # already a code of its line: nothing to decide
            continue
        keys.append(key)
        open_case(spec, item=item)
    return drop_open(item, 'reference', keep=keys)


def sync_cases(item, lookups=None):
    """The cases an attached item's own data asks for, whatever wrote it (the worker's sweep): a waiting code change, its declared
    codes."""
    if item.reference_id is None:
        return
    if item.codigo_pendiente is not None or item.brand_pendiente is not None:
        codigo = item.codigo_pendiente or item.codigo
        brand = item.brand if item.brand_pendiente is None else item.brand_pendiente
        open_case(code_change_case(item, codigo, brand), item=item)
    sync_declared(item, lookups)


# --- the worker ------------------------------------------------------------------------------------------------------------------------

def busy_suppliers():
    from .supplier_import_models import SupplierInventoryImportJob
    return set(SupplierInventoryImportJob.objects.filter(status='importing', expires_at__gt=timezone.now()).values_list('supplier_id', flat=True))


def backlog(*, seconds=PASS_SECONDS, actor=None, stats=None):
    """The worker's supplier stage: every item without a Reference (a line case waiting, a load whose place() failed, an item code
    wrote without one) is resolved again and carried out (rules 1-4: a line case whose lines became one attaches; place() for no_line);
    then every attached item's own cases are kept (sync_cases: a waiting code change, its declared codes). Suppliers importing are left
    for the next pass. Stops after `seconds` and asks for another pass. Returns the counts."""
    import time
    from .matching_queue import request_another_pass
    from .models import Account
    _, _, SupplierItem = _models()
    stats = stats if stats is not None else {}
    counts = {'examined': 0, 'attached': 0, 'placed': 0, 'line_cases': 0, 'failed': 0}
    busy = busy_suppliers()
    deadline = time.monotonic() + seconds
    rows = list(SupplierItem.objects.filter(reference__isnull=True).exclude(supplier_id__in=busy).order_by('supplier_id', 'pk')
                .values_list('pk', 'supplier_id'))
    for pk, supplier_id in rows:
        if time.monotonic() > deadline:
            request_another_pass()
            counts['stopped'] = True
            break
        try:  # one item's failure is logged and left for the next pass: it never stops the pass for the others
            with transaction.atomic():
                Account.objects.select_for_update().get(pk=supplier_id)  # the order of every writer of this supplier's items
                item = SupplierItem.objects.select_related('supplier').filter(pk=pk, reference__isnull=True).first()
                if item is None:
                    continue
                counts['examined'] += 1
                done = carry_out(item, resolve(subject_of(item)), actor=actor, save=True, queue=False)
                if done.action == 'failed':
                    counts['failed'] += 1
                elif done.action == 'no_line':
                    counts['placed'] += 1
                elif done.action == 'case':
                    counts['line_cases'] += 1
                else:
                    counts['attached'] += 1
                if item.reference_id is not None:
                    sync_declared(item)
        except Exception:
            logger.exception('No se pudo emparejar el artículo %s; se reintenta en la próxima pasada.', pk)
            counts['failed'] += 1
    lookups = Lookups()
    held = SupplierItem.objects.filter(reference__isnull=False).exclude(supplier_id__in=busy).filter(
        Q(codigo_pendiente__isnull=False) | Q(brand_pendiente__isnull=False) | ~Q(references=[]))
    for item in held.select_related('part', 'reference', 'supplier').order_by('pk').iterator(chunk_size=500):
        try:
            with transaction.atomic():
                sync_cases(item, lookups)
        except Exception:
            logger.exception('No se pudieron revisar los casos del artículo %s; se reintenta en la próxima pasada.', item.pk)
            counts['failed'] += 1
    stats['supplier_examined'] = stats.get('supplier_examined', 0) + counts['examined']
    return counts


def recheck_cases(*, actor=None):
    """Open cases the catalog or another path settled meanwhile: a line case whose item got a Reference, a variant case whose item left
    that line, a code_change case with nothing waiting any more: applied by the matching itself; a merge case whose lines are one line
    now: applied. Returns how many."""
    from .lines import canonical_ids
    MatchingCase, _ = _cases()
    closed = 0
    for case in MatchingCase.objects.filter(status='review', kind__in=ITEM_KINDS).select_related('item').order_by('pk'):
        item = case.item
        if item is None:
            continue
        note = ''
        if case.kind == 'line' and item.reference_id is not None:
            note = 'El artículo ya cuelga de una referencia.'
        elif case.kind == 'variant' and str(item.part_id) not in [str(pk) for pk in case.lines.values_list('pk', flat=True)]:
            note = 'El artículo ya no está en esa línea.'
        elif case.kind == 'code_change' and item.codigo_pendiente is None and item.brand_pendiente is None:
            note = 'El cambio de código ya no espera revisión.'
        if note:
            close_case(case, 'applied', actor=None, note=note)
            closed += 1
    for case in MatchingCase.objects.filter(status='review', kind='merge').order_by('pk'):
        ids = [str(pk) for pk in case.lines.values_list('pk', flat=True)]
        canonical = canonical_ids(ids)
        if len(ids) > 1 and len({canonical.get(pk, pk) for pk in ids}) == 1 and not case.batch_key:
            close_case(case, 'applied', actor=None, note='Las líneas ya están unificadas.')
            closed += 1
    return closed


def merge_candidates():
    """[(lines, evidence)] of catalog lines (publicada or retirada) that one clave puts together (rule 8; what the worker's catalog
    references merged on their own before S6 now asks a person): live public References of one clave on two or more such lines, unless
    every two of them carry different brands of no common familia (two makers' numbers). Pairs kept apart, and lines a case already
    covers, are left to merge_case_spec and open_reference_merge_cases."""
    from django.db.models import Count
    _, Reference, _ = _models()
    from .lines import CATALOG
    rows = Reference.objects.filter(supplier__isnull=True, estado__in=references.LIVE, part__estado__in=CATALOG, part__merged_into__isnull=True)
    shared = list(rows.values('clave').annotate(n=Count('part', distinct=True)).filter(n__gt=1).values_list('clave', flat=True))
    lookups = Lookups()
    found = []
    for start in range(0, len(shared), 1000):
        chunk = rows.filter(clave__in=shared[start:start + 1000]).select_related('part').order_by('clave', 'pk')
        groups = {}
        for row in chunk:
            groups.setdefault(row.clave, []).append(row)
        for key, group in groups.items():
            brands = {}
            for row in group:
                brands.setdefault(row.part_id, set()).add(lookups.canonical(row.brand))
            lines = sorted({row.part for row in group}, key=lambda line: (line.sku, str(line.pk)))

            def related(a, b):
                return any(not x or not y or x == y or lookups.same_familia(x, y) for x in brands[a] for y in brands[b])
            if not any(related(a.pk, b.pk) for index, a in enumerate(lines) for b in lines[index + 1:]):
                continue
            found.append((lines, {'clave': key, 'refs': [ref_row(row) for row in group]}))
    found.extend(tag_base_candidates(lookups))
    return found


def tag_base_candidates(lookups=None):
    """[(lines, evidence)] of a catalog line whose codigo, its trailing auto-eligible TAGs stripped (rule 3), is another catalog line's
    alterno (a non-main Reference): the base path of the catalog references before S6, with D2's strip instead of supplier_base. The
    guard says when the part names disagree or contradict (noun, spec_conflict)."""
    from .catalog_suffixes import description_head
    from .lines import CATALOG, catalog_lines
    from .matching_engine import spec_conflict
    from .oem_finder import head_compat
    _, Reference, _ = _models()
    lookups = lookups or Lookups()
    bases = {}
    for line in catalog_lines().only('pk', 'sku', 'description', 'estado', 'merged_into_id'):
        base, entries = strip_eligible(line.sku, lookups)
        if base:
            bases.setdefault(references.clave(base), []).append((line, base, entries))
    holders = {}
    keys = sorted(bases)
    for start in range(0, len(keys), 2000):
        rows = (Reference.objects.filter(clave__in=keys[start:start + 2000], supplier__isnull=True, is_main=False, estado__in=references.LIVE,
                                         part__estado__in=CATALOG, part__merged_into__isnull=True).select_related('part').order_by('pk'))
        for row in rows:
            holders.setdefault(row.clave, []).append(row)
    found = []
    for key, children in bases.items():
        for child, base, entries in children:
            for row in holders.get(key, ()):
                other = row.part
                if other.pk == child.pk:
                    continue
                guard = ('noun' if not head_compat(description_head(child.description), description_head(other.description))
                         else 'spec_conflict' if spec_conflict(child.description, other.description) else '')
                found.append(([child, other], {'clave': key, 'base': base, 'tags': [entry.token for entry in entries], 'via': 'tag',
                                               'guard': guard, 'refs': [ref_row(row)]}))
    return found


def covered(line_ids):
    """Whether a merge case already lists all these lines, decided or not (from S10 the legacy catalog cases are merge cases: 0086)."""
    from .references import covering_case
    return covering_case(sorted({str(pk) for pk in line_ids})) is not None


def open_reference_merge_cases(*, dry_run=False):
    """The worker's catalog stage (it replaced the automatic merges of reconcile_catalog_references): one merge case per group
    merge_candidates finds, unless a merge case already covers those lines (decided or not) or they
    are kept apart. Never merges anything. Returns the specs (dry_run) or how many cases were opened."""
    MatchingCase, _ = _cases()
    specs, seen = [], set()
    for lines, evidence in merge_candidates():
        if evidence.get('via') == 'tag':
            reason = (f'El código {lines[0].sku} sin su sufijo es {evidence["base"]}, un alterno de {lines[1].sku}: si son el mismo '
                      'repuesto, unifícalas; si no, mantenlas separadas.')
        else:
            reason = (f'El código {evidence["refs"][0]["code"]} está en varias líneas del catálogo: si son el mismo repuesto, unifícalas; '
                      'si no, mantenlas separadas.')
        spec = merge_case_spec(lines, evidence=evidence, reason=reason, guard=evidence.get('guard', ''))
        if spec is None:
            continue
        key = case_key(spec)
        if key in seen or MatchingCase.objects.filter(key=key).exists() or covered(spec['lines']):
            continue
        seen.add(key)
        specs.append(spec)
    if dry_run:
        return specs
    for spec in specs:
        open_case(spec)
    return len(specs)


# --- a person's decision -----------------------------------------------------------------------------------------------------------

class CaseChanged(ValidationError):
    """The case, its item or its lines changed since the person opened it (409 in matching_views)."""


def lock_for(case, line_ids=()):
    """The locks of a decision on an item case, in the plan's order (§1.6.5): the Accounts of the item's supplier and of every supplier
    with items on the lines involved (a decision may merge the item's own line into the chosen one: mall.line_merge locks them too), by
    id, then the lines by id; the item is read under its Account's lock (every writer of a supplier's items takes it first) and written
    last by the decision, and the case is locked when it is decided (confirm). Refuses (CaseChanged) a case decided or changed
    meanwhile, or a supplier importing. Returns (item, {line id: Part})."""
    from .models import Account, Part
    _, _, SupplierItem = _models()
    item = SupplierItem.objects.filter(pk=case.item_id).first()
    if item is None:
        raise CaseChanged('El artículo de este caso ya no existe.')
    ids = sorted({str(pk) for pk in [*line_ids, item.part_id] if pk})
    suppliers = sorted({item.supplier_id, *SupplierItem.objects.filter(part_id__in=ids).values_list('supplier_id', flat=True)}, key=str)
    list(Account.objects.select_for_update().filter(pk__in=suppliers).order_by('pk').values_list('pk', flat=True))
    from .line_merge import importing
    if importing(suppliers):
        raise CaseChanged('Espera a que termine la importación de este proveedor.')
    lines = {str(line.pk): line for line in Part.objects.select_for_update().filter(pk__in=ids).order_by('pk')}
    item = SupplierItem.objects.select_related('supplier', 'part', 'reference').get(pk=item.pk)
    current(case)
    return item, lines


def current(case):
    """The case as stored, refused (CaseChanged) when it was decided or its evidence changed since the person opened it."""
    MatchingCase, _ = _cases()
    stored = MatchingCase.objects.filter(pk=case.pk).first()
    if stored is None or stored.fingerprint != case.fingerprint or stored.status != 'review':
        raise CaseChanged('El caso cambió. Actualiza las coincidencias antes de confirmar.')
    return stored


def confirm(case):
    """The case locked, last (plan §1.6.5: cases after the catalog rows and the items), and checked again before it is decided: a
    change meanwhile refuses the decision (CaseChanged) and rolls back what it wrote."""
    MatchingCase, _ = _cases()
    locked = MatchingCase.objects.select_for_update().filter(pk=case.pk).first()
    if locked is None or locked.fingerprint != case.fingerprint or locked.status != 'review':
        raise CaseChanged('El caso cambió. Actualiza las coincidencias antes de confirmar.')
    return locked


def line_reference(line, item):
    """The live Reference of `line` the item's code (and brand) belongs to, or None: its supplier's own code first, then the same key,
    the brandless one, a branded one of its clave for a brandless item, a brand of its familia (D3)."""
    _, Reference, _ = _models()
    lookups = Lookups()
    key = references.clave(item.codigo)
    brand = effective_brand(subject_of(item), lookups)
    rows = [row for row in Reference.all_objects.filter(part_id=line.pk, clave=key, estado__in=references.EXCLUSIVE)
            if row.supplier_id in (None, item.supplier_id)]
    for wanted in (lambda row: row.supplier_id is not None and (not row.brand or lookups.canonical(row.brand) == brand),
                   lambda row: lookups.canonical(row.brand) == brand,
                   lambda row: not row.brand,
                   lambda row: not brand,
                   lambda row: bool(brand) and lookups.same_familia(row.brand, brand)):
        found = sorted((row for row in rows if wanted(row)), key=rank)
        if found:
            return found[0]
    return None


def absorb_new_product(item, line, *, actor=None):
    """The item's own line in preparación, a new product holding only this item (Productos nuevos, rule 4), merged into `line` before
    the item moves there (mall.line_merge.merge_lines: its codes go along, Separar SKU undoes it): no empty line is left holding the
    item's code for the next supplier's stock to join. Returns whether it merged. A merge the catalog refuses (MergeGuard, a code
    another line holds) refuses the decision with its reason."""
    Part, _, SupplierItem = _models()
    if not item.part_id or str(item.part_id) == str(line.pk):
        return False
    own = Part._base_manager.filter(pk=item.part_id).first()
    if own is None or own.estado != 'preparacion' or own.merged_into_id:
        return False
    if SupplierItem.objects.filter(part_id=own.pk).exclude(pk=item.pk).exists():
        return False
    from .line_merge import merge_lines, service_actor
    merge_lines(line, [own], actor=actor if getattr(actor, 'pk', None) else service_actor(), reason='Vincular un producto nuevo')
    item.refresh_from_db(fields=['reference', 'part', 'matching_status'])
    return True


def hang_on_line(item, line, *, actor=None, status=None, approve=True):
    """A person's Vincular from a case (S6): the item hangs from `line`'s Reference for its code, or from a new one: shared (declarado,
    a proveedor source) when placement says so and the key is free, else interno for its supplier (mall.variants.reference_for: a
    code reviewed onto a line is the supplier's own name for it), under its brand's canonical nombre (I12). approve applies a waiting
    code first (rule 5b: the code_change case approved); without it a waiting code keeps waiting and the item stays in review, out of
    stock (OD7). The item's own line in preparación, when it holds nothing else, joins `line` first (absorb_new_product). Refuses a
    unificada line (VariantConflict). Returns the item, saved."""
    from .variants import VariantConflict, apply_pending, attach, declared_by, reference_for, status_for, waiting
    Part, _, _ = _models()
    if not references.is_live(line):
        raise VariantConflict(f'El SKU {line.sku} se agrupó en otro: vincula el artículo al SKU en el que se agrupó.')
    if approve:
        apply_pending(item)
    if absorb_new_product(item, line, actor=actor):
        line = Part._base_manager.get(pk=line.pk)  # the merge may have moved it (a line in referencia takes preparación)
    ref = line_reference(line, item)
    if ref is None:
        spec = placement(item)
        if spec['tipo'] != 'interno':
            try:
                with transaction.atomic():
                    ref, _ = references.add(line, item.codigo, spec['brand'], tipo=spec['tipo'], estado='declarado', source=declared_by(item),
                                            actor=actor, detail={TAG: stamp('caso', item)})
            except references.ReferenceConflict:
                ref = None
        if ref is None:
            own = placement(item, interno=True)  # the brand an interno code carries: canonical, '' for a placeholder
            ref, _ = reference_for(line, item.codigo, own['brand'], item.supplier_id, create=True, actor=actor, source=declared_by(item),
                                   item=item)
    if ref is None:
        raise VariantConflict(f'El código {item.codigo} es un código interno de este proveedor en otro SKU: corrígelo allí antes de vincularlo aquí.')
    prepared = line.estado == 'referencia'
    line = enter_preparacion(line, item=item, actor=actor) if prepared else line
    if status is None:
        status = 'review' if waiting(item) else status_for(line)
    attach(item, ref, status=status, save=True)
    joined(item, line, prepared=prepared)
    return item


def separate(item, *, actor=None):
    """Separar referencia: the item leaves its line (or its line case) for a line of its own in preparación whose main is an interno
    code of its supplier (its code is another line's, so it cannot be a shared one). A new product holding only this item keeps its
    line: its main becomes the supplier's own code (mall.new_products.make_interno). Refused while a code change waits (rule 5b: the
    code_change case decides it first). Returns the line."""
    from .variants import detach, waiting
    Part, _, SupplierItem = _models()
    if waiting(item):
        raise ValidationError('Este artículo tiene un cambio de código esperando revisión: apruébalo o recházalo antes de separar su '
                              'referencia.')
    own = item.part if item.reference_id is not None else None
    if (own is not None and own.estado == 'preparacion' and not own.merged_into_id
            and not SupplierItem.objects.filter(part_id=own.pk).exclude(pk=item.pk).exists()):
        main = references.main_of(own)
        if main is not None and main.pk == item.reference_id:
            if main.tipo != 'interno':
                from django.db.models import F
                from .new_products import make_interno
                make_interno(own, main, actor if getattr(actor, 'pk', None) else None)
                Part.objects.filter(pk=own.pk).update(version=F('version') + 1, changed_at=timezone.now())
            return Part._base_manager.get(pk=own.pk)
    if item.reference_id is not None:
        detach(item)
    return place(item, actor=actor, interno=True)


def close_item_cases(item, *, keep=None, actor=None, note=''):
    """The other open cases that held an item a decision settled (its variant case once it moved, its line case once it hangs)."""
    MatchingCase, _ = _cases()
    for case in MatchingCase.objects.filter(item_id=item.pk, status='review', kind__in=('variant', 'line')).exclude(pk=getattr(keep, 'pk', None)):
        close_case(case, 'applied', actor=actor, note=note or 'Resuelto por otra decisión sobre el artículo.')


def decide(case, action, *, actor, line=None):
    """A person's decision on an S6 case (matching_views), with its fingerprint already checked by the caller:
    approve      line or variant: the item hangs from `line` (a line id; required for a line case, the case's line by default for a
                 variant case); code_change: the waiting code is applied, on `line`, or on the line its new key is on, or on the item's
                 own line; reference: the declared code becomes a Reference of the item's line, or with `line` (Elegir línea) the item
                 moves to that line; merge: the case's lines are merged into `line` (or the case's first line)
    dismiss      variant: the contradiction is accepted and the item counts again; code_change: the change is rejected (the item keeps
                 its code, and a load that sends it again changes nothing); reference: the code is not the item's; merge: kept apart;
                 line: Separar referencia (an item never waits without a Reference and without a case)
    separar_referencia  line or variant: the item gets a line of its own (interno main)
    merge, keep_apart   merge cases (keep_apart writes CatalogSeparation pairs; on lines a legacy merge joined it takes them apart)
    One transaction, in the plan's lock order: the supplier's Account and the lines (lock_for), the catalog rows, the item, then the
    cases (confirm: a case changed meanwhile rolls everything back). Returns the case."""
    from .new_products import PlaceRefused
    from .variants import VariantConflict
    kind = case.kind
    if kind == 'merge':
        return decide_merge(case, action, actor=actor, line=line)
    if action in ('merge', 'keep_apart'):
        raise ValidationError('Esta acción es solo para casos de unificación.')
    extra = [line] if line else []
    holder = None
    if kind == 'code_change':
        holder = references.find(case.new_code, case.new_brand, supplier=case.item.supplier_id if case.item_id else None)
        if holder is not None:
            extra.append(holder.part_id)
    try:
        with transaction.atomic():
            item, lines = lock_for(case, extra)
            if line and str(line) not in lines:
                raise CaseChanged('La línea elegida no existe.')
            chosen = lines.get(str(line)) if line else None
            outcome = decide_item(case, action, item, chosen, lines, holder, actor)
            if outcome is None:
                raise ValidationError('Acción no válida para este caso.')
            status, decision, evidence, others = outcome
            close_case(confirm(case), status, actor=actor, action=decision, evidence=evidence)
            if others:
                close_item_cases(item, keep=case, actor=actor)
            return case
    except VariantConflict as error:
        raise ValidationError(error.message)
    except PlaceRefused as error:  # Separar referencia on a code no line of its own can take
        raise ValidationError(str(error))


def decide_item(case, action, item, chosen, lines, holder, actor):
    """decide's writes on the catalog and the item, before the case is locked: (status, decision, evidence, close the item's other
    cases) for the case, or None for an action the case does not take."""
    kind = case.kind
    if action == 'separar_referencia' or (action == 'dismiss' and kind == 'line'):
        if kind not in ('line', 'variant'):
            raise ValidationError('Separar referencia es para casos de línea o de variante.')
        if kind == 'variant' and str(item.part_id) not in {str(pk) for pk in case.lines.values_list('pk', flat=True)}:
            raise CaseChanged('El artículo ya no está en la línea de este caso.')
        new = separate(item, actor=actor)
        return 'applied', 'separated', {'line': str(new.pk)}, True
    if kind == 'line':
        if item.reference_id is not None:
            raise CaseChanged('El artículo ya cuelga de una referencia.')
        if chosen is None:
            raise ValidationError('Elige la línea del artículo.')
        hang_on_line(item, chosen, actor=actor)
        remember_choice(item, chosen, actor)
        return 'applied', 'approved', {'line': str(chosen.pk)}, False
    if kind == 'variant':
        # The case is about the item on the line it hangs from, one of the case's lines (a merge adds the line it went into: follow_cases).
        if item.part_id is None or str(item.part_id) not in {str(pk) for pk in case.lines.values_list('pk', flat=True)}:
            raise CaseChanged('El artículo ya no está en la línea de este caso.')
        case_line = item.part
        if action == 'dismiss' or chosen is None or chosen.pk == case_line.pk:
            settle_item(item, ignore=case)
            if action == 'dismiss':
                return 'dismissed', 'dismissed', {'line': str(case_line.pk)}, False
            return 'applied', 'approved', {'line': str(case_line.pk)}, False
        hang_on_line(item, chosen, actor=actor, approve=False)  # a move approves no waiting code (rule 5b): its code_change case does
        remember_choice(item, chosen, actor)
        settle_item(item, ignore=case)
        return 'applied', 'approved', {'line': str(chosen.pk), 'from': str(case_line.pk)}, True
    if kind == 'code_change':
        waiting = (item.codigo_pendiente or item.codigo, item.brand if item.brand_pendiente is None else item.brand_pendiente)
        if waiting != (case.new_code, case.new_brand) or (item.codigo_pendiente is None and item.brand_pendiente is None):
            raise CaseChanged('El cambio de código ya no es el de este caso.')
        if action == 'dismiss':
            item.codigo_pendiente = item.brand_pendiente = None
            item._attached = True
            item.save(update_fields=['codigo_pendiente', 'brand_pendiente', 'updated_at'])
            settle_item(item, ignore=case)
            return 'dismissed', 'dismissed', {}, False
        destination = chosen or (lines.get(str(holder.part_id)) if holder is not None else None) or item.part
        moved = destination.pk != item.part_id
        hang_on_line(item, destination, actor=actor, approve=True)
        remember_choice(item, destination, actor)
        settle_item(item, ignore=case)
        return 'applied', 'approved', {'line': str(destination.pk)}, moved
    if kind == 'reference':
        declared = [row for row in (item.references or []) if references.clave(row.get('code', '')) == references.clave(case.new_code)
                    and (row.get('brand', '') or '') == case.new_brand]
        if not declared or item.part_id is None:
            raise CaseChanged('El proveedor ya no declara este código para el artículo.')
        if action == 'dismiss':
            return 'dismissed', 'dismissed', {}, False
        if chosen is not None and chosen.pk != item.part_id:
            hang_on_line(item, chosen, actor=actor, approve=False)
            remember_choice(item, chosen, actor)
            settle_item(item, ignore=case)
            return 'applied', 'approved', {'line': str(chosen.pk), 'moved': True}, True
        from .variants import declared_by
        try:
            with transaction.atomic():
                # The brand as its canonical nombre (I12 from S6: an alias spelling such as TOY is written TOYOTA; a placeholder, none).
                ref, _ = references.add(item.part, case.new_code, written_brand(case.new_brand),
                                        tipo=references.tipo_of(declared[0].get('ref_type', 'unknown')),
                                        estado='declarado', source=declared_by(item), actor=actor,
                                        detail={TAG: stamp('referencia_declarada', item)})
        except references.ReferenceConflict as error:
            raise ValidationError(f'{error.message} Elige esa línea para el artículo o unifica las dos líneas.')
        return 'applied', 'approved', {'reference': ref.pk}, False
    return None


def remember_choice(item, line, actor):
    """A person's choice is remembered: a proveedor source on the Reference the item hangs from (mall.matching_engine.remember)."""
    from .matching_engine import remember
    item.refresh_from_db(fields=['reference', 'part'])
    remember(item, line, actor)


def decide_merge(case, action, *, actor, line=None):
    """merge (or approve): the case's lines merged into `line` (one of them; the case's first by default) through
    mall.line_merge.merge_lines; a merge that waits (guard subgrupo) refuses with the reason and keeps the case. keep_apart (or dismiss):
    CatalogSeparation pairs; lines a legacy merge joined (the legacy-auto-merge batch) are taken apart first (Separar SKU, as the
    merge recorded). A batch case whose lines are one line already: approve keeps them merged. A case of the catalog merge pass (S9,
    mall.merge_pass.convert_case_codes): merged, the codes now on the merged line only become its References; kept apart with `line`
    (one of the case's lines), its codes become References of that line only. The case is locked and checked last, in the same
    transaction as the merge or the split (plan §1.6.5)."""
    from .line_merge import MergeGuard, keep_apart, merge_lines
    from .lines import canonical_ids
    from .models import Part
    MatchingCase, _ = _cases()
    current(case)
    ids = [str(pk) for pk in case.lines.values_list('pk', flat=True)]
    canonical = canonical_ids(ids)
    merged = len({canonical.get(pk, pk) for pk in ids}) == 1
    if action in ('approve', 'merge'):
        if merged:
            with transaction.atomic():
                return close_case(confirm(case), 'applied', actor=actor, action='approved', note='Se conserva la unificación.')
        # By default the line the case proposes to keep: its target_sku (merge_case_spec puts the publicada line first; a legacy case
        # converted by mall.refactor.s6_cases names its parent there), else its first row.
        skus = dict(Part._base_manager.filter(pk__in=ids).values_list('pk', 'sku'))
        proposed = next((str(pk) for pk, sku in skus.items() if case.target_sku and sku == case.target_sku), None)
        first = proposed or (case.sources or [{}])[0].get('id') or ids[0]
        target_id = str(line) if line else str(first)
        if target_id not in ids:
            raise ValidationError('Elige una de las líneas del caso como línea principal.')
        target = Part.objects.get(pk=canonical.get(target_id, target_id))
        sources = list(Part.objects.filter(pk__in=sorted({canonical.get(pk, pk) for pk in ids} - {str(target.pk)})).order_by('pk'))
        try:
            with transaction.atomic():
                merge = merge_lines(target, sources, actor=actor, reason='Caso de unificación')
                from .merge_pass import convert_case_codes
                added = convert_case_codes(case, action, target=target.pk, actor=actor)  # S9: the codes now on one line (3e)
                return close_case(confirm(case), 'applied', actor=actor, action='approved',
                                  evidence={'target': str(target.pk), 'merge_id': str(merge.pk) if merge is not None else None,
                                            **({'references': added} if added else {})})
        except MergeGuard as guard:
            MatchingCase.objects.filter(pk=case.pk).update(guard=guard.guard)
            raise
    if action in ('keep_apart', 'dismiss'):
        from .merge_pass import S9_RULES
        if line and str(line) not in ids and (case.evidence or {}).get('regla') in S9_RULES:
            raise ValidationError('Elige una de las líneas del caso para su código.')
        with transaction.atomic():
            if merged and len(ids) > 1:
                split_legacy(ids, canonical, actor=actor)
            pairs = keep_apart(ids, actor=actor, case=case, reason='caso')
            from .merge_pass import convert_case_codes
            added = convert_case_codes(case, action, line=line, actor=actor)  # S9: the code goes to the line the reviewer picks (3e)
            return close_case(confirm(case), 'dismissed', actor=actor, action='dismissed',
                              evidence={'separated': pairs, **({'references': added, 'line': str(line)} if added else {})})
    raise ValidationError('Acción no válida para un caso de unificación.')


def split_legacy(ids, canonical, *, actor=None):
    """The lines of a merge case that a merge joined: each one merged into another comes back out of it (Separar SKU, with the rows the
    merge recorded as its own: mall.catalog_split.split_plan's suggestions)."""
    from .catalog_split import split_catalog_part, split_plan
    from .models import Part
    by_target = {}
    for pk in ids:
        target = canonical.get(pk, pk)
        if target != pk:
            by_target.setdefault(target, []).append(pk)
    for target_id, children in by_target.items():
        target = Part.objects.get(pk=target_id)
        plan = split_plan(target)
        restore = [{'id': child, 'codes': [code.pk for code in plan['codes'] if str(code.suggested) == child],
                    'items': [str(item.pk) for item in plan['items'] if str(item.suggested) == child],
                    'images': [str(image.pk) for image in plan['images'] if str(image.suggested) == child]}
                   for child in children if any(str(row.pk) == child for row in plan['children'])]
        if restore:
            split_catalog_part(actor=actor, target_id=target.pk, restore=restore, reason='Caso de unificación: mantener separadas')
