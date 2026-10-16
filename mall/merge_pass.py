"""The catalog merge pass's rules (inventory refactor S9, plan §3 S9: the diagram's rules 3b–3g on the OEM library) as the code keeps
applying them after S9. The pass itself (python -m mall.merge_pass, --apply on 2026-10-15) went with S10: it read the aftermarket codes
the supplier catalogs filed under OEM numbers (OEMCrossReference), their tombstones and the SKUs' catalog links (PartOEMLink), which S10
archives (archive schema, 0088), and git keeps the CLI. What stays, for the writers that still meet those rules:

- join_numbers(ref_ids, rows=..., links=...): a supplier catalog import (mall.supplier_catalog_import) hands the codes it reads for its
  numbers (rows) and the SKUs its items matched (links) in memory, no longer as table rows. 3b groups the lines those numbers sit on
  (numbers a catalog prints under its own brand's item, the same compact number under two makes, twin spellings); 3c's guards and 3d's
  routes decide: a group whose lines are all in referencia without variants merges on its own (3f), any other gets a merge case. Then 3e:
  each code whose numbers sit on one line becomes its Reference (declarado for the catalog's own code; inferido for a competitor's, only
  from a PDF-checked catalog: OD17), with a catalogo source per citation (settle_line); a code on two or more lines opens a merge case
  only when a supplier sells it (OD15), else it is left out (it would have stayed a catalog row: from S10 the import's report names it).
- retire_citation (a catalog replace): the catalog's sources on the code's Reference are retired, and an
  inferido Reference left with no live source (or a declarado one this pass made) goes to disputa; a code behind an automatic merge of
  the pass opens a case on that merge's lines.
- convert_case_codes (a merge case of the pass decided): merged, the catalog rows the case waits on (evidence catalog_rows) become the
  merged line's References where their numbers now sit on it only; kept apart with a line chosen, the case's codes become References
  there. A merge case keeps the catalog rows of its lines' numbers (keep_case_rows): join_numbers adds an import's, and 0088 (S10)
  copied there the archived OEMCrossReference rows the open cases of the pass waited on.
"""
import json
import re
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from . import references

MERGE_REASON = 'merge_pass'
BATCH_KEY = 'merge-pass'
RETIRED_BATCH = 'merge-pass-retirada'
TAG = 's9'  # detail key of the References and sources this pass writes
CHUNK = 200
GMB_TITLE = 'GMB Water Pump Catalog 2016'
MARKS_FILE = Path(__file__).with_name('merge_pass_gmb_marks.json')
EXTRACTS = ('asva_hub_2017', 'asva_tensioner_2017', 'asva_cv_2017', 'ate_wear_2017', 'gmb_wp_2016_v3')
MANUAL = 'Registro manual'  # how the Biblioteca OEM (gone with its screen in S10) cited a code a person added
RETIRED_HERE = 'Retirado en la Biblioteca OEM'  # detail.retired.reason of a code a person retired from a number there: it stays retired
MERGE_DISPUTE = 'caso de unificación'  # mall.line_merge.MERGE_DISPUTE: a merge settles a dispute whose note ends so
CASE_ROWS = 'catalog_rows'  # a merge case's evidence: [pk, number Reference id, brand, code, compact code, citations] it waits on

# --- makes, families and partners (the prototype's make_groups.json and its additions) -------------------------------------------

MAKE_GROUPS = {
    'ROVER': '', 'VAUXHALL': 'VAUXHALL', 'PROTON': 'PROTON', 'TALBOT': 'TALBOT', 'CITROEN': 'CITROEN', 'SAAB': 'SAAB', 'ALFA ROMEO': 'ALFA ROMEO',
    'HOLDEN': 'HOLDEN', 'RENAULT SAMSUNG': '', 'GM': 'GM', 'GREAT WALL': 'GREAT WALL', 'VOLKSWAGEN': 'VAG', 'MAZDA': 'MAZDA', 'DAIMLER': 'DAIMLER',
    'SEAT': 'VAG', 'VOLVO': '', 'LIFAN': 'LIFAN', 'HINO': 'HINO', 'LADA': 'LADA', 'SMART': 'SMART', 'BRITISH LEYLAND': 'BRITISH LEYLAND',
    'SUZUKI': 'SUZUKI', 'KAIYI': 'KAIYI', 'JAGUAR': 'JAGUAR', 'MERCEDES-BENZ': 'MB', 'DAEWOO': 'GM', 'DODGE': 'MOPAR', 'IHC': 'IHC', 'DFSK': 'CN',
    'ASTON MARTIN': 'ASTON MARTIN', 'JEEP': 'MOPAR', 'LEXUS': 'TOYOTA', 'TOYOTA': 'TOYOTA', 'ISUZU': 'ISUZU', 'IVECO': 'IVECO', 'FORD': 'FORD',
    'HONDA': 'HONDA', 'CHERY': 'CN', 'SSANGYONG': '', 'KIA': 'HMG', 'AUSTIN': 'AUSTIN', 'FIAT': '', 'DAIHATSU': 'TOYOTA', 'AUTOBIANCHI': 'AUTOBIANCHI',
    'CHRYSLER': 'MOPAR', 'MG': 'MG', 'MINI': 'BMW', 'CHEVROLET': 'GM', 'GEELY': 'CN', 'BMW': 'BMW', 'NISSAN': 'NISSAN', 'LANCIA': 'LANCIA', 'AUDI': 'VAG',
    'BEDFORD': 'BEDFORD', 'ACURA': 'HONDA', 'SUBARU': 'SUBARU', 'LAND ROVER': '', 'DACIA': 'DACIA', 'INFINITI': 'NISSAN', 'PORSCHE': 'PORSCHE',
    'HYUNDAI': 'HMG', 'OPEL': '', 'RENAULT': '', 'SKODA': 'VAG', 'MITSUBISHI': 'MITSUBISHI', 'PEUGEOT': ''}
EXT_FAMILY = {'OPEL': 'GM', 'VAUXHALL': 'GM', 'HOLDEN': 'GM', 'SAAB': 'GM', 'CHEVROLET': 'GM', 'DAEWOO': 'GM', 'GM': 'GM',
              'PEUGEOT': 'PSA', 'CITROEN': 'PSA', 'FIAT': 'FIAT', 'ALFA ROMEO': 'FIAT', 'LANCIA': 'FIAT', 'AUTOBIANCHI': 'FIAT',
              'ABARTH': 'FIAT', 'RENAULT': 'RENAULT', 'DACIA': 'RENAULT', 'RENAULT SAMSUNG': 'RENAULT'}
PARTNERS = [{'FORD', 'MAZDA'}, {'FORD', 'VOLVO'}, {'NISSAN', 'RENAULT'}, {'HMG', 'MITSUBISHI'}, {'VAG', 'PORSCHE'}, {'MB', 'VAG'},
            {'HONDA', 'ROVER'}, {'PSA', 'FIAT'}, {'GM', 'FIAT'}, {'MITSUBISHI', 'SMART'}, {'MITSUBISHI', 'VOLVO'}, {'TOYOTA', 'PSA'},
            {'TOYOTA', 'SUBARU'}, {'MAZDA', 'HMG'}, {'MOPAR', 'FIAT'}, {'MOPAR', 'MITSUBISHI'}, {'GM', 'SUZUKI'}, {'GM', 'RENAULT'},
            {'GM', 'NISSAN'}, {'RENAULT', 'SMART'}, {'MB', 'RENAULT'}, {'MB', 'SMART'}, {'GM', 'ISUZU'}, {'FORD', 'PSA'}]


def family(maker):
    """The make family the prototype groups a maker under (TOYOTA for LEXUS and DAIHATSU, HMG for HYUNDAI and KIA, GM for OPEL...)."""
    return EXT_FAMILY.get(maker) or MAKE_GROUPS.get(maker) or maker


def make_names(fam):
    """The make names an application text may spell a family with."""
    return {m for m, f in MAKE_GROUPS.items() if f == fam} | {m for m, f in EXT_FAMILY.items() if f == fam} | {fam}


def partner_ok(fams):
    """Whether every two families of a group are an allowlisted partner pair (they build each other's parts)."""
    fl = sorted(set(fams))
    return all(any({a, b} == pair for pair in PARTNERS) for i, a in enumerate(fl) for b in fl[i + 1:])


# --- what a number or a SKU names (the prototype's classifiers) -------------------------------------------------------------------

PT_MAP = {'BOMBAS DE AGUA': 'BOMBA AGUA', 'DISCOS DE FRENO': 'DISCO FRENO', 'DISCO FRENO': 'DISCO FRENO',
          'JUNTAS HOMOCINÉTICAS': 'JUNTA HOMOCINETICA', 'CV JOINT': 'JUNTA HOMOCINETICA', 'SEMIEJES': 'SEMIEJE', 'EJE': 'SEMIEJE',
          'CUBOS DE RUEDA': 'CUBO/RODAMIENTO RUEDA', 'RODAMIENTOS DE RUEDA': 'CUBO/RODAMIENTO RUEDA', 'HUB': 'CUBO/RODAMIENTO RUEDA',
          'TENSORES DE CORREA': 'TENSOR/POLEA', 'ACCESORIOS DE PASTILLAS': 'SENSOR DESGASTE', 'EMBRAGUES DE VENTILADOR': 'EMBRAGUE VENTILADOR'}
PT_BROAD = {'BOMBA AGUA': 'pump', 'DISCO FRENO': 'disc', 'JUNTA HOMOCINETICA': 'joint', 'SEMIEJE': 'shaft', 'CUBO/RODAMIENTO RUEDA': 'hub',
            'TENSOR/POLEA': 'tensioner', 'SENSOR DESGASTE': 'sensor', 'EMBRAGUE VENTILADOR': 'fanclutch'}
SAME_MAKE_NOTE = re.compile(r'\bGW([A-Z]+)-\d+[A-Z]*\s*=\s*\(GW([A-Z]+)-')


def cc(text):
    """Letters and digits only: how the catalogs key a code and the library a number."""
    return re.sub(r'[^0-9A-Z]', '', (text or '').upper())


def ptype(num):
    """The library part type of a number, in the prototype's names ('' when neither it nor its description says)."""
    pt = (num.part_type or '').strip()
    if pt:
        return PT_MAP.get(pt, pt)
    desc = (num.description or '').upper()
    if desc.startswith('EJE INTERMEDIO') or desc.startswith('SEMIEJE'):
        return 'SEMIEJE'
    if desc.startswith('JUNTA HOMOCIN'):
        return 'JUNTA HOMOCINETICA'
    return ''


def mazda_fmt(code):
    m = re.match(r'^([A-Z0-9]{4})(\d{2})(\d{3}[A-Z0-9]{0,3})$', code)
    return (m.group(2), m.group(3)) if m else None


def basic(num):
    """(broad type, detail) the number's own prefix names, or None: Toyota 16100 is a pump assembly and 16110 a bare pump."""
    m, c = num.brand, num.number
    fam = num.family
    b = c[:5]
    if fam == 'TOYOTA' or m in ('TOYOTA', 'LEXUS', 'DAIHATSU'):
        if b == '16100':
            return ('pump', 'assy')
        if b in ('16110', '16120', '16102', '16101', '16111'):
            return ('pump', 'bare')
        if b in ('16210', '16250', '16260', '16220'):
            return ('fanclutch', '')
        if b in ('43410', '43420'):
            return ('shaft', 'R' if b == '43410' else 'L')
    if m in ('NISSAN', 'INFINITI'):
        if b == '21010':
            return ('pump', '')
        if b == '21082':
            return ('fanclutch', '')
        if b in ('39100', '39101'):
            return ('shaft', 'R' if b == '39100' else 'L')
        if b == '40202':
            return ('hub', 'F')
        if b == '43202':
            return ('hub', 'B')
    if m in ('HONDA', 'ACURA'):
        if b == '19200':
            return ('pump', '')
        if b in ('44305', '44306'):
            return ('shaft', 'R' if b == '44305' else 'L')
        if b in ('44010', '44014'):
            return ('joint', '')
        if b == '44300':
            return ('hub', 'F')
        if b == '42200':
            return ('hub', 'B')
    if m in ('HYUNDAI', 'KIA'):
        if b == '25100':
            return ('pump', '')
        if b in ('49500', '49501'):
            return ('shaft', 'L' if b == '49500' else 'R')
        if b == '51720':
            return ('hub', 'F')
        if b in ('52730', '52710'):
            return ('hub', 'B')
    if m in ('MAZDA', 'FORD', 'KIA'):
        f = mazda_fmt(c)
        if f:
            g, p = f
            if g == '15' and p.startswith('010'):
                return ('pump', '')
            if g == '15' and p.startswith('150'):
                return ('fanclutch', '')
            if g == '25' and p[:1] in '456':
                # The owner, 2026-10-14 (mall.supplier_catalog_import.JOINT_CLASSES): Mazda's 25-40X/25-50X/25-60X, under Mazda, Ford or
                # Kia, name the CV joint (the prototype read them as shafts); a SKU sold as a whole shaft still says shaft.
                return ('joint', '')
            if g == '22' and p.startswith('51'):
                return ('joint', '')
            if g == '26' and p.startswith('15'):
                return ('hub', 'B')
            if g == '33' and p[:2] in ('04', '06', '15'):
                return ('hub', 'F')
            if g in ('26', '33') and p.startswith('25'):
                return ('disc', 'B' if g == '26' else 'F')
    return None


def broad(num):
    found = basic(num)
    if found:
        return found[0]
    return PT_BROAD.get(ptype(num), '')


def sku_noun(desc):
    """The part a SKU's description names, in the classifiers' words ('' when it names none they know)."""
    t = (desc or '').upper()
    if re.search(r'\bPUNTA\b|\bJUNTA\b|HOMOCIN', t):
        return 'joint'
    if re.search(r'EJE COMPLETO|SEMIEJE|\bEJE\b', t):
        return 'shaft'
    if re.search(r'FAN CLUTCH|EMBRAGUE', t):
        return 'fanclutch'
    if re.search(r'\bBOMBA\b', t):
        return 'pump'
    if re.search(r'TENSOR|POLEA|\bLOCA\b', t):
        return 'tensioner'
    if re.search(r'\bHUB\b|MANZANA|\bCUBO\b|BALINERA|RODAMIENTO|BEARING', t):
        return 'hub'
    if re.search(r'\bDISCO\b', t):
        return 'disc'
    return ''


def sku_sides(desc):
    """(left/right, front/back) a SKU's description says, each '' when it says both or neither."""
    t = (desc or '').upper()
    lr = '' if re.search(r'LH\s*/\s*RH|RH\s*/\s*LH|L/R|R/L', t) else (
        ('L' if re.search(r'\bLH\b|\bIZQ', t) else '') + ('R' if re.search(r'\bRH\b|\bDER\b|\bDERECH', t) else ''))
    fb = '' if re.search(r'DEL\s*(Y|/)\s*TRAS', t) else (
        ('F' if re.search(r'\bDEL\b|DELANT', t) else '') + ('B' if re.search(r'\bTRAS\b|TRASER|\bPOST', t) else ''))
    return lr, fb


def consolidation_note(app):
    if 'NEW TYPE FOR' in app:
        return True
    return any(a == b for a, b in SAME_MAKE_NOTE.findall(app))


GUARD_OF = {'part_type': 'noun', 'assembly_vs_component': 'noun', 'with_housing_vs_bare': 'prefix', 'front_vs_rear': 'side',
            'left_vs_right': 'side', 'cross_make_unlisted': 'make'}
FLAG_TEXT = {'part_type': 'los números o los SKU nombran repuestos distintos',
             'number_prefix': 'los prefijos de los números nombran repuestos distintos',
             'assembly_vs_component': 'se mezclan una junta y un semieje completo',
             'with_housing_vs_bare': 'se mezclan una bomba con carcasa y una sin ella (16100 / 16110)',
             'front_vs_rear': 'se mezclan delantero y trasero', 'left_vs_right': 'se mezclan izquierdo y derecho',
             'cross_make_unlisted': 'una marca no figura en las aplicaciones del artículo'}


# --- citations -------------------------------------------------------------------------------------------------------------------

def parse_citation(text):
    """(catalog title, page or None) of a citation 'Title · pág. N'."""
    m = re.match(r'(.*) · pág\. (\d+)$', text or '')
    return (m.group(1), int(m.group(2))) if m else ((text or '').strip(), None)


def catalog_brands():
    """{catalog citation (its title and year, as its sources cite it): its own brand} for the catalogs mall.supplier_catalog_import
    loaded: the library numbers' catalogo sources keep the catalog's brand in their detail."""
    from .models import ReferenceSource
    rows = ReferenceSource.objects.filter(kind='catalogo', detail__has_key='brand').values_list('citation', 'detail__brand').distinct()
    return {citation: ' '.join(str(brand or '').upper().split()) for citation, brand in rows if citation and brand}


def catalog_info(title, brands=None):
    """(own brand, PDF-checked) of the catalog a title names: the five catalogs of OD17 (mall.reference_matching.catalog_of, all
    checked against their PDF), else the brand the import recorded for that catalog (not checked); a manual registration has neither."""
    from .reference_matching import CATALOGS, catalog_of
    if title.startswith(MANUAL):
        return '', False
    if not any(title.startswith(prefix) for prefix, _, _ in CATALOGS) and brands and title in brands:
        return brands[title], False
    _, brand, checked = catalog_of(title)
    return brand, checked


# --- the snapshot ------------------------------------------------------------------------------------------------------------------

class Num:
    __slots__ = ('id', 'uid', 'code', 'number', 'brand', 'family', 'line', 'estado', 'is_main', 'description', 'applications',
                 'part_type', 'catalogs')


class Line:
    __slots__ = ('id', 'sku', 'description', 'estado', 'part_type_id', 'subgrupo', 'applications', 'variants', 'stock', 'orders',
                 'numbers', 'sheet')


class Row:
    __slots__ = ('id', 'ref', 'brand', 'code', 'number', 'citations', 'pages', 'catalogs', 'own', 'checked', 'manual', 'marks', 'joins')


class Held:
    __slots__ = ('id', 'line', 'code', 'clave', 'brand', 'estado', 'tipo', 'is_main', 'source', 'library', 'copy', 'sources')


def chunked(values, size=2000):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def load_marks(path=MARKS_FILE):
    """(star rows {(number, item)}, W/HOUSING numbers, index pages) from the marks file; empty when it is missing."""
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
    except OSError:
        return set(), set(), range(0)
    star = {(cc(number), cc(item)) for _, number, item in data.get('star_rows', [])}
    whousing = {number for number, _ in data.get('whousing_numbers', [])}
    first, last = data.get('index_pages', [522, 547])
    return star, whousing, range(first, last + 1)


def load_positions(folder=None):
    """{compact own item code: position} from the catalog extracts in output/catalog_data (front/rear for hubs and discs)."""
    if folder is None:
        from .refactor.output import main_checkout
        folder = main_checkout() / 'output' / 'catalog_data'
    positions = {}
    for name in EXTRACTS:
        try:
            with open(Path(folder) / f'{name}.json', encoding='utf-8') as handle:
                items = json.load(handle).get('items') or []
        except (OSError, ValueError):
            continue
        for item in items:
            if item.get('brand_code'):
                positions[cc(item['brand_code'])] = (item.get('position') or '').upper()
    return positions


class Snapshot:
    """Everything the pass reads, once (no query per group)."""

    def __init__(self):
        self.numbers, self.lines, self.rows, self.tombstones = {}, {}, [], []
        self.links = defaultdict(set)     # number id -> canonical catalog lines a catalog link ties to it
        self.held = defaultdict(list)     # clave -> live public References (Held) of the codes the catalogs file
        self.company = []                 # step 0: catalog-copied company References (Held)
        self.sold = defaultdict(set)      # compact code -> canonical brands of the supplier items selling it ('' for none)
        self.pairs = {}                   # the lines kept apart (mall.line_merge.separated_pairs)
        self.cases = defaultdict(list)    # canonical line id -> line sets of the cases that cover it
        self.case_keys = set()
        self.catalog_skus = {}            # upper codigo -> catalog line id (publicada or retirada)
        self.by_number = defaultdict(set)  # compact number -> number ids
        self.star, self.whousing, self.index_pages = set(), set(), range(0)
        self.positions = {}
        self.canonical = {}               # brand spelling -> canonical nombre
        self.catalogs = {}                # catalog citation -> its own brand (catalog_brands)
        self.line_refs = {}               # line id -> its live References, loaded for the lines a merge would move

    def brand(self, spelling):
        spelling = ' '.join((spelling or '').upper().split())
        if spelling not in self.canonical:
            from .brands import resolve
            self.canonical[spelling] = resolve(spelling) if spelling else ''
        return self.canonical[spelling]


def load(*, marks_path=MARKS_FILE, positions_folder=None, scope=None, rows=(), links=()):
    """Read the snapshot (read-only). rows: the aftermarket codes a catalog import read for its numbers, in memory (pk, number Reference
    id, brand, code, compact code, citations); links: the (SKU line id, number Reference id) its items matched. From S10 nothing reads
    them from a table (S10 archived OEMCrossReference and PartOEMLink). scope {'lines': line ids, 'numbers': compact codes}: only the
    library numbers on those lines and the ones the rows of those codes are filed under (settle_line); no supplier sales
    then."""
    from .brand_models import Brand, BrandAlias
    from .line_merge import separated_pairs
    from .lines import CATALOG, canonical_ids
    from .matching_models import MatchingCase
    from .models import ItemApplication, Part, PartSpecification, Reference, ReferenceSource, SupplierItem
    from .request_models import SupplierRequestLine
    from django.db.models import Count, Sum
    snap = Snapshot()
    snap.star, snap.whousing, snap.index_pages = load_marks(marks_path)
    snap.catalogs = catalog_brands()
    snap.positions = load_positions(positions_folder)
    for nombre in Brand.objects.values_list('nombre', flat=True):
        snap.canonical[nombre] = nombre
    for alias, nombre in BrandAlias.objects.values_list('alias', 'brand__nombre'):
        snap.canonical.setdefault(' '.join(alias.split()), nombre)

    # The numbers, on live lines that are not rechazada.
    given = [tuple(row) for row in rows]
    if scope is not None:
        from django.db.models import Q
        filed = sorted({row[1] for row in given if row[4] in set(scope.get('numbers') or ())})
        numbers_q = Q(part_id__in=sorted(scope.get('lines') or ())) | Q(pk__in=filed)
    rows = (Reference.all_objects.filter(references.library_q(), supplier__isnull=True, estado__in=references.EXCLUSIVE,
                                         part__merged_into__isnull=True)
            .exclude(part__estado__in=('unificada', 'rechazada'))
            .values_list('pk', 'uid', 'code', 'brand', 'part_id', 'estado', 'is_main', 'detail'))
    if scope is not None:
        rows = rows.filter(numbers_q)
    for pk, uid, code, brand, part_id, estado, is_main, detail in rows.iterator(chunk_size=5000):
        num = Num()
        detail = detail or {}
        num.id, num.uid, num.code, num.number, num.brand = pk, str(uid), code, cc(code), brand
        num.family, num.line, num.estado, num.is_main = family(brand), str(part_id), estado, is_main
        num.description, num.applications = detail.get('description') or '', detail.get('applications') or ''
        num.part_type, num.catalogs = detail.get('part_type') or '', set()
        snap.numbers[pk] = num
        snap.by_number[num.number].add(pk)

    # Catalog links (never a join): the canonical catalog line of each SKU the import's items matched.
    raw_links = [(part_id, ref_id) for part_id, ref_id in links if scope is None or ref_id in snap.numbers]
    linked_canonical = canonical_ids({part_id for part_id, _ in raw_links})

    # The aftermarket codes and their tombstones.
    def make_row(pk, ref_id, brand, code, number, citations):
        row = Row()
        row.id, row.ref, row.brand, row.code, row.number = pk, ref_id, brand, code, number
        row.citations = list(citations or [])
        parsed = [parse_citation(text) for text in row.citations]
        row.pages = [page for title, page in parsed if title == GMB_TITLE and page is not None]
        row.catalogs = sorted({title for title, _ in parsed if title and not title.startswith(MANUAL)})
        row.manual = any(title.startswith(MANUAL) for title, _ in parsed)
        canonical = snap.brand(brand)
        infos = [catalog_info(title, snap.catalogs) for title in row.catalogs]
        row.own = any(own and snap.brand(own) == canonical for own, _ in infos)
        row.checked = any(checked for _, checked in infos)
        row.marks, row.joins = [], True
        return row
    for values in given:
        if scope is None or values[4] in set(scope.get('numbers') or ()):
            snap.rows.append(make_row(*values))
    for row in snap.rows:
        num = snap.numbers.get(row.ref)
        if num is not None:
            num.catalogs.update(row.catalogs)

    # The References that hold the codes (any live public row of their claves).
    claves = sorted({references.clave(references.code_key(row.code)) for row in snap.rows if row.code})
    held_ids = []
    for chunk in chunked(claves):
        found = (Reference.all_objects.filter(clave__in=chunk, estado__in=references.LIVE, supplier__isnull=True, part__merged_into__isnull=True)
                 .exclude(part__estado='unificada')
                 .values_list('pk', 'part_id', 'code', 'clave', 'brand', 'estado', 'tipo', 'is_main'))
        for pk, part_id, code, key, brand, estado, tipo, is_main in found:
            held = Held()
            held.id, held.line, held.code, held.clave, held.brand, held.estado, held.tipo = pk, str(part_id), code, key, brand, estado, tipo
            held.is_main, held.library, held.copy, held.sources = is_main, pk in snap.numbers, False, {}
            snap.held[key].append(held)
            held_ids.append(pk)
    by_id = {held.id: held for rows_ in snap.held.values() for held in rows_}
    for chunk in chunked(held_ids):
        for ref_id, kind, citation, retirada in ReferenceSource.objects.filter(reference_id__in=chunk).values_list('reference_id', 'kind', 'citation', 'retirada'):
            by_id[ref_id].sources[(kind, citation)] = retirada

    # The lines: the numbers', the linked SKUs' and the holders'.
    line_ids = ({num.line for num in snap.numbers.values()} | {pk for pk in linked_canonical.values()}
                | {held.line for rows_ in snap.held.values() for held in rows_})
    for chunk in chunked(sorted(line_ids)):
        for pk, sku, description, estado, part_type_id, subgrupo, applications in (
                Part._base_manager.filter(pk__in=chunk, merged_into__isnull=True)
                .values_list('pk', 'sku', 'description', 'estado', 'part_type_id', 'part_type__name', 'aplicaciones_texto')):
            line = Line()
            line.id, line.sku, line.description, line.estado = str(pk), sku, description or '', estado
            line.part_type_id, line.subgrupo, line.applications = part_type_id, subgrupo or '', applications or ''
            line.variants = line.stock = line.orders = 0
            line.numbers, line.sheet = [], None
            snap.lines[line.id] = line
        for part_id, n, stock in (SupplierItem.objects.filter(part_id__in=chunk).values('part_id')
                                  .annotate(n=Count('pk'), stock=Sum('reported_quantity')).values_list('part_id', 'n', 'stock')):
            if str(part_id) in snap.lines:
                snap.lines[str(part_id)].variants, snap.lines[str(part_id)].stock = n, int(stock or 0)
        for part_id, n in SupplierRequestLine.objects.filter(part_id__in=chunk).values('part_id').annotate(n=Count('pk')).values_list('part_id', 'n'):
            if str(part_id) in snap.lines:
                snap.lines[str(part_id)].orders = n
        sheets = defaultdict(set)
        for part_id, template_id in PartSpecification.objects.filter(part_id__in=chunk).values_list('part_id', 'field__template_id'):
            sheets[str(part_id)].add(template_id)
        for part_id in ItemApplication.objects.filter(part_id__in=chunk).values_list('part_id', flat=True):
            sheets[str(part_id)]
        for part_id, templates in sheets.items():
            if part_id in snap.lines:
                snap.lines[part_id].sheet = templates
    for num in list(snap.numbers.values()):
        line = snap.lines.get(num.line)
        if line is None:
            del snap.numbers[num.id]
            snap.by_number[num.number].discard(num.id)
            continue
        line.numbers.append(num)
        if not num.applications:
            num.applications = line.applications
        if not num.description:
            num.description = line.description
        if not num.part_type:
            num.part_type = line.subgrupo
    for part_id, ref_id in raw_links:
        target = linked_canonical.get(str(part_id))
        line = snap.lines.get(target) if target else None
        if line is not None and line.estado in CATALOG and ref_id in snap.numbers:
            snap.links[ref_id].add(line.id)

    # What suppliers sell, the lines kept apart, the cases already asking, the catalog's codigos.
    from .reference_matching import PLACEHOLDER_BRANDS
    for codigo, brand in (SupplierItem.objects.values_list('codigo', 'brand').iterator(chunk_size=5000) if scope is None else ()):
        key = cc(codigo)
        if key:
            name = snap.brand(brand)
            snap.sold[key].add('' if name in PLACEHOLDER_BRANDS else name)
    snap.pairs = separated_pairs()
    case_lines = defaultdict(set)
    snap.case_keys.update(MatchingCase.objects.filter(kind='merge').values_list('key', flat=True))
    for case_id, part_id in MatchingCase.lines.through.objects.filter(matchingcase__kind='merge').values_list('matchingcase_id', 'part_id'):
        case_lines[case_id].add(str(part_id))
    canonical = canonical_ids({pk for ids in case_lines.values() for pk in ids})
    for ids in case_lines.values():
        mapped = frozenset(canonical.get(pk, pk) for pk in ids)
        for pk in mapped:
            snap.cases[pk].append(mapped)
    from .lines import catalog_lines
    for pk, sku in catalog_lines().values_list('pk', 'sku').iterator(chunk_size=5000):
        snap.catalog_skus[references.code_key(sku)] = str(pk)
    return snap


def load_line_refs(snap, line_ids):
    """The live References of these lines (for the refusals a merge would meet), cached on the snapshot."""
    from .models import Reference
    wanted = sorted({pk for pk in line_ids if pk not in snap.line_refs})
    for pk in wanted:
        snap.line_refs[pk] = []
    for chunk in chunked(wanted):
        for pk, part_id, code, key, brand, supplier_id, estado, is_main in (
                Reference.all_objects.filter(part_id__in=chunk, estado__in=references.LIVE)
                .values_list('pk', 'part_id', 'code', 'clave', 'brand', 'supplier_id', 'estado', 'is_main')):
            snap.line_refs[str(part_id)].append((pk, code, key, brand, supplier_id, estado, is_main))


def holders_of(snap, claves):
    """{clave: [(ref id, line id, brand, supplier id, estado)]} of every live exclusive Reference of these claves (any line)."""
    from .models import Reference
    out = defaultdict(list)
    for chunk in chunked(sorted(set(claves))):
        for pk, part_id, key, brand, supplier_id, estado in (
                Reference.all_objects.filter(clave__in=chunk, estado__in=references.EXCLUSIVE, part__merged_into__isnull=True)
                .exclude(part__estado='unificada').values_list('pk', 'part_id', 'clave', 'brand', 'supplier_id', 'estado')):
            out[key].append((pk, str(part_id), brand, supplier_id, estado))
    return out


# --- union-find ----------------------------------------------------------------------------------------------------------------------

class UF:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        parent = self.parent
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra


# --- the plan ----------------------------------------------------------------------------------------------------------------------

def live_line(line):
    """A live line in rule 8's sense: a catalog line (publicada or retirada) or one holding variants (preparación with stock)."""
    from .lines import CATALOG
    return line.estado in CATALOG or line.variants > 0


def mark_rows(snap, excluded):
    """3b's exclusions on the own-code rows (GMB '*', W/HOUSING NO. under an item without H, index pages only) and the marks every
    row's sources keep."""
    for live, rows in ((True, snap.rows), (False, snap.tombstones)):
        for row in rows:
            row.marks = []
            num = snap.numbers.get(row.ref)
            number = num.number if num is not None else row.number
            if row.brand == 'GMB' or GMB_TITLE in ' '.join(row.citations):
                if (number, row.number) in snap.star or (number + '000', row.number) in snap.star:
                    row.marks.append('*')
                if number in snap.whousing and not row.number.endswith('H'):
                    row.marks.append('W/HOUSING')
                if row.pages and all(page in snap.index_pages for page in row.pages) and num is not None and not (num.applications or '').strip():
                    row.marks.append('index_only')
            row.joins = not (live and row.own and row.marks and snap.brand(row.brand) == 'GMB')
            if not row.joins:
                excluded[{'*': 'star', 'W/HOUSING': 'whousing', 'index_only': 'index_only'}[row.marks[0]]] += 1


def build_groups(snap, counts):
    """3b: the components of lines the joins connect, with how they were joined."""
    from .supplier_catalog_import import canonical_number, twin_scope
    uf = UF()
    own_lines = defaultdict(set)   # own code key -> lines its joining rows' numbers sit on
    for row in snap.rows:
        num = snap.numbers.get(row.ref)
        if num is None or not row.own or not row.joins:
            continue
        own_lines[(snap.brand(row.brand), row.number)].add(num.line)
    for key, lines in own_lines.items():
        lines = sorted(lines)
        for other in lines[1:]:
            uf.union(lines[0], other)
        if len(lines) > 1:
            counts['codigos_propios_que_unen'] += 1
    same = 0
    for number, ids in snap.by_number.items():
        lines = sorted({snap.numbers[pk].line for pk in ids})
        if len(ids) > 1:
            same += 1
        for other in lines[1:]:
            uf.union(lines[0], other)
    counts['numeros_iguales_varias_marcas'] = same
    twins = defaultdict(set)
    for num in snap.numbers.values():
        twins[(twin_scope(num.brand), canonical_number(num.brand, num.number))].add(num.id)
    twin_groups = 0
    for ids in twins.values():
        spellings = {snap.numbers[pk].number for pk in ids}
        if len(spellings) < 2:
            continue
        twin_groups += 1
        lines = sorted({snap.numbers[pk].line for pk in ids})
        for other in lines[1:]:
            uf.union(lines[0], other)
    counts['gemelos'] = twin_groups
    components = defaultdict(set)
    for pk in snap.lines:
        if snap.lines[pk].numbers:
            components[uf.find(pk)].add(pk)

    def linked_out(lines):  # a SKU a catalog link ties to one of these numbers (the catalog item is that SKU): the merge pass re-derives it
        return any(target not in lines for pk in lines for num in snap.lines[pk].numbers for target in snap.links.get(num.id, ()))
    groups = [sorted(lines) for lines in components.values() if len(lines) >= 2 or linked_out(lines)]
    counts['grupos_por_enlace'] = sum(1 for lines in groups if len(lines) == 1)
    return groups, own_lines


def number_rank(snap, num):
    """3g with no SKU: most supplier stock on its line, printed by most catalogs, the highest revision of the same base number."""
    line = snap.lines[num.line]
    base = num.number[:-2] if len(num.number) >= 8 else num.number
    return (-line.stock, -len(num.catalogs), base, tuple(-ord(ch) for ch in num.number), num.brand)


def keeper_rank(snap, line_id):
    """The line a case proposes to keep: a live line first, most stock, then most order lines (3g); then the best number."""
    line = snap.lines[line_id]
    best = min((number_rank(snap, num) for num in line.numbers), default=(0,))
    return (not live_line(line), -line.stock, -line.orders, best, line.sku, line_id)


def covered(snap, ids):
    """Whether a case (decided or not) already asks about all these lines."""
    ids = set(ids)
    first = next(iter(sorted(ids)), None)
    return any(ids <= case for case in snap.cases.get(first, ()))


def kept_apart(snap, ids):
    from .line_merge import pair
    ids = sorted(ids)
    return [(a, b) for index, a in enumerate(ids) for b in ids[index + 1:] if pair(a, b) in snap.pairs]


def evaluate(snap, lines, own_lines, own_by_line):
    """3c: the guards of a group (its flags in the prototype's names, which block it, and the details). A SKU a catalog link ties to the
    group joins it as its target (3d), so the guards read that SKU's own OEM numbers too: a joint never merges onto a SKU whose number
    names a shaft (the owner, 2026-10-14), and 16100 never onto 16110."""
    nums = [num for pk in lines for num in snap.lines[pk].numbers]
    linked = {target for num in nums for target in snap.links.get(num.id, ()) if target not in lines}
    guard_nums = nums + [num for pk in sorted(linked) for num in snap.lines[pk].numbers]
    codes_in = {}
    for pk in lines:
        for key in own_by_line.get(pk, ()):
            codes_in.setdefault(key, set()).add(pk)
    codes_in = {key: value & set(lines) for key, value in codes_in.items()}
    cover = max((len(value) for value in codes_in.values()), default=0)
    chain = cover < len(lines) and len([key for key, value in codes_in.items() if len(value) >= 2]) >= 2
    skus = {pk for pk in lines if live_line(snap.lines[pk])}
    skus |= linked
    flags = []
    types = {broad(num) for num in guard_nums} - {''}
    nouns = {sku_noun(snap.lines[pk].description) for pk in skus} - {''}
    if len(types | nouns) > 1:
        flags.append('assembly_vs_component' if (types | nouns) >= {'shaft', 'joint'} else 'part_type')
    bs = [basic(num) for num in guard_nums]
    if {('pump', 'assy'), ('pump', 'bare')} <= set(bs):
        flags.append('with_housing_vs_bare')
    fb = {b[1] for b in bs if b and b[0] in ('hub', 'disc') and b[1]}
    lr = {b[1] for b in bs if b and b[0] == 'shaft' and b[1]}
    pos = {snap.positions.get(key[1], '') for key in codes_in} - {''}
    posfb = {('F' if 'FRONT' in p else '') + ('B' if 'REAR' in p else '') for p in pos} - {'', 'FB'}
    for pk in skus:
        line = snap.lines[pk]
        s_lr, s_fb = sku_sides(line.description + ' ' + line.sku)
        if len(s_lr) == 1:
            lr.add(s_lr)
        if len(s_fb) == 1:
            fb.add(s_fb)
    if fb >= {'F', 'B'} or posfb >= {'F', 'B'}:
        flags.append('front_vs_rear')
    if lr >= {'L', 'R'}:
        flags.append('left_vs_right')
    fams = {num.family for num in guard_nums}
    partner = False
    if len(fams) > 1:
        apps = ' '.join((num.applications or '').upper() for num in guard_nums)
        missing = [f for f in fams if not any(re.search(r'\b' + re.escape(m) + r'\b', apps) for m in make_names(f))]
        same_only = all(any(len({snap.numbers[y].family for y in snap.by_number.get(num.number, {num.id})}) > 1
                            for num in guard_nums if num.family == f) for f in missing)
        if missing and not same_only:
            if partner_ok(fams):
                partner = True
            else:
                flags.append('cross_make_unlisted')
    consolidated = any(consolidation_note((num.applications or '').upper()) for num in guard_nums)
    if consolidated:
        flags.append('consolidated_item')
    blocking = [flag for flag in flags if flag != 'consolidated_item']
    guard = ''
    if blocking:
        first = blocking[0]
        guard = GUARD_OF[first]
        if first == 'part_type' and len(types) > 1:
            guard = 'prefix'
    return {'nums': nums, 'codes_in': codes_in, 'chain': chain, 'skus': skus, 'linked': linked, 'flags': flags, 'blocking': blocking,
            'guard': guard, 'partner': partner, 'families': sorted(fams), 'types': sorted(types), 'nouns': sorted(nouns)}


def predict_refusal(snap, target, sources, claves_held):
    """What merge_lines would refuse (read-only): (guard, reason) or None. A source's codigo or code another catalog line's codigo is
    (conflicto), a moving key another line holds (conflicto: the brandless reservation, I5), technical sheets of another subgrupo
    (subgrupo)."""
    participants = {target, *sources}
    target_line = snap.lines[target]
    target_keys = {(key, brand, supplier) for _, _, key, brand, supplier, _, _ in snap.line_refs.get(target, ())}
    for pk in sources:
        line = snap.lines[pk]
        for code in [line.sku, *(code for _, code, _, _, _, _, _ in snap.line_refs.get(pk, ()))]:
            holder = snap.catalog_skus.get(references.code_key(code))
            if holder is not None and holder not in participants:
                return 'conflicto', f'El código {code} es el SKU de otra línea del catálogo.'
        for _, code, key, brand, supplier, estado, _ in snap.line_refs.get(pk, ()):
            if estado not in references.EXCLUSIVE or (key, brand, supplier) in target_keys:
                continue
            for _, other_line, other_brand, other_supplier, _ in claves_held.get(key, ()):
                if other_line in participants:
                    continue
                if supplier is not None:
                    clash = other_supplier == supplier and other_brand == brand
                else:
                    clash = other_supplier is None and (not brand or other_brand in (brand, ''))
                if clash:
                    return 'conflicto', f'El código {code} pertenece a otra línea.'
    carrying = [snap.lines[pk] for pk in sources if snap.lines[pk].sheet is not None]
    if carrying:
        kinds = {line.part_type_id for line in carrying} | {t for line in carrying for t in line.sheet}
        if target_line.part_type_id is not None:
            if kinds - {target_line.part_type_id, None}:
                return 'subgrupo', 'Las líneas de origen tienen fichas técnicas de otro subgrupo.'
        elif len(kinds - {None}) > 1:
            return 'subgrupo', 'Las líneas de origen tienen fichas técnicas de subgrupos distintos.'
    return None


def code_text(snap, rows):
    """The printed form a code is written with: the commonest among its rows (the first by id on a tie)."""
    forms = Counter(' '.join((row.code or '').upper().split()) for row in rows)
    best = max(forms.values())
    return next(' '.join((row.code or '').upper().split()) for row in sorted(rows, key=lambda row: row.id)
                if forms[' '.join((row.code or '').upper().split())] == best)


def plan(snap, after_merges=False):
    """The groups the snapshot's rows make and their routes (3b–3d, 3g), read-only: {'groups', 'merged' (line -> the target an
    automatic merge would give it), 'counts'}. after_merges: no group is automatic (a group strong only now waits in a batch)."""
    counts, excluded = Counter(), Counter()
    mark_rows(snap, excluded)
    groups, own_lines = build_groups(snap, counts)
    own_by_line = defaultdict(set)
    for key, lines in own_lines.items():
        for pk in lines:
            own_by_line[pk].add(key)

    # 3c and 3d.
    evaluated = []
    for lines in groups:
        info = evaluate(snap, lines, own_lines, own_by_line)
        info['lines'] = lines
        evaluated.append(info)
    linked_to, member_of = defaultdict(set), {}
    for index, info in enumerate(evaluated):
        for target in info['linked']:
            linked_to[target].add(index)
        for pk in info['lines']:
            member_of[pk] = index
    # A SKU a catalog link ties to two groups, or to a group while it belongs to another, would join them through the link: a batch.
    shared = {target for target, ids in linked_to.items() if len(ids) > 1 or target in member_of}
    load_line_refs(snap, {pk for info in evaluated for pk in info['lines']} | {pk for info in evaluated for pk in info['linked']})
    claves_held = holders_of(snap, {key for pk in {p for info in evaluated for p in info['lines']} for _, _, key, _, _, _, _ in snap.line_refs.get(pk, ())})
    out_groups = []
    for index, info in enumerate(evaluated):
        lines, skus = info['lines'], info['skus']
        members = set(lines) | info['linked']
        group = {'id': f'g{index + 1}', 'kind': 'enlace' if len(lines) == 1 else 'numeros', 'lines': lines, 'skus': sorted(skus),
                 'linked': sorted(info['linked']), 'flags': info['flags'], 'guard': info['guard'], 'chain': info['chain'],
                 'partner': info['partner'], 'families': info['families'], 'numbers': len(info['nums']),
                 'codes': sorted(f'{brand} {number}' for brand, number in info['codes_in']), 'code_keys': sorted(info['codes_in']),
                 'target': None, 'main': None,
                 'route': '', 'motivo': '', 'reason': ''}
        apart = kept_apart(snap, members)
        if apart:
            group['route'], group['motivo'] = 'separadas', 'separated'
            group['reason'] = 'Las líneas se mantienen separadas (Separar SKU o un caso descartado): no se proponen.'
        elif covered(snap, members):
            group['route'], group['motivo'] = 'cubierto', 'covered'
            group['reason'] = 'Un caso ya pregunta por estas líneas.'
        elif info['blocking']:
            group['route'], group['motivo'] = 'caso', info['guard']
            group['reason'] = ('No se unifica sola: ' + '; '.join(FLAG_TEXT[flag] for flag in info['blocking']) + '.')
        elif info['chain'] or len(skus) >= 2 or (info['linked'] & shared):
            group['route'] = 'lote'
            group['motivo'] = 'cadena' if info['chain'] else 'skus' if len(skus) >= 2 else 'sku_compartido'
            group['reason'] = {'cadena': 'Varios artículos del catálogo encadenan estos números: confirma que son el mismo repuesto.',
                               'skus': 'Dos o más líneas vivas (SKU o con existencias) llevan estos números: elige la que se conserva.',
                               'sku_compartido': 'Un SKU que el catálogo vincula a estos números también está vinculado a otro grupo.'}[group['motivo']]
        else:
            group['route'] = 'auto'
        if group['route'] == 'auto' and after_merges:
            # The apply's second plan (on the catalog its merges left): every strong group of the first plan was merged or became a
            # case, so a group strong only now (its SKU was shared with a group that merged into it) waits in the batch; nothing merges
            # here and no code is placed as if it had.
            group['route'], group['motivo'] = 'lote', 'tras_unificar'
            group['reason'] = ('Tras las unificaciones automáticas de esta pasada estas líneas quedaron listas para unificarse: confirma que '
                               'son el mismo repuesto.')
        # 3g: the target and the main.
        ordered = sorted(members, key=lambda pk: keeper_rank(snap, pk))
        if group['route'] == 'auto':
            if skus:
                target = next(iter(skus))
                group['target'], group['main'] = target, None
            else:
                best = min(info['nums'], key=lambda num: number_rank(snap, num))
                target = best.line
                group['target'] = target
                group['main'] = None if best.is_main else best.id
                group['main_code'] = f'{best.brand} {best.code}'
            sources = [pk for pk in members if pk != target]
            refusal = predict_refusal(snap, target, sources, claves_held)
            if refusal is not None:
                group['route'], group['motivo'] = 'caso', refusal[0]
                group['reason'] = f'La unificación automática no se puede aplicar: {refusal[1]}'
                group['target'] = None
        if group['route'] != 'auto':
            group['order'] = ordered
        out_groups.append(group)

    # The lines each automatic merge would leave (the codes of a later plan read them).
    merged = {}
    for group in out_groups:
        if group['route'] == 'auto':
            for pk in set(group['lines']) | set(group['linked']):
                merged[pk] = group['target']
    for group in out_groups:
        counts[f"grupos_{group['route']}"] += 1
    counts['grupos'] = len(out_groups)
    for key, value in excluded.items():
        counts[f'excluidas_{key}'] += value
    return {'counts': counts, 'groups': out_groups, 'merged': merged}


def key_holders(snap, merged, brand, code):
    """The live exclusive public References that hold a branded code (its brand or none) and the lines they leave it on (after the
    merges), as [(Held, line)]."""
    key = references.clave(references.code_key(code))
    found = []
    for held in snap.held.get(key, ()):
        if held.estado in references.EXCLUSIVE and (held.brand == brand or held.brand == '') and held.line in snap.lines:
            found.append((held, merged.get(held.line, held.line)))
    return found


def same_part(line_text, nums):
    """Whether a line's description names the part its numbers name (3e's check before a code becomes the line's Reference): the same
    head noun as a number's library text (mall.catalog_suffixes.description_head, as rule 3 compares), or the same part in the
    classifiers' words: the line's noun (sku_noun) is a number's text's or its own prefix's or part type's (broad: Toyota 16210 is a fan
    clutch, TENSORES DE CORREA a tensioner). Unknown on the line's side: the same."""
    from .catalog_suffixes import description_head
    from .oem_finder import head_compat
    head, noun = description_head(line_text or ''), sku_noun(line_text)
    if not head[0] or not nums:
        return True
    for num in nums:
        other = description_head(num.description or '')
        if not other[0] or head_compat(head, other) or (noun and noun in (sku_noun(num.description), broad(num))):
            return True
    return False


def plan_codes(snap, merged, pending, groups, disputed=()):
    """3e: the outcome of every aftermarket code (brand, compact code) and of its rows, on the lines the automatic merges leave. The
    step 0 copies that go to disputa (`disputed`) hold their code no more."""
    by_key = defaultdict(list)
    for row in snap.rows:
        by_key[(snap.brand(row.brand), row.number)].append(row)
    counts = Counter()
    entries = {}
    for key, rows in by_key.items():
        brand, number = key
        live = [row for row in rows if row.ref in snap.numbers]
        entry = {'brand': brand, 'number': number, 'code': code_text(snap, rows), 'rows': [row.id for row in rows],
                 'own': any(row.own for row in rows), 'checked': any(row.checked for row in rows), 'manual': any(row.manual for row in rows),
                 'lines': [], 'holders': [], 'outcome': '', 'line': None, 'reference': None, 'estado': '', 'sold': False, 'group': None}
        entry['sold'] = bool(snap.sold.get(number, set()) & {brand, ''})
        if not live:
            entry['outcome'] = 'archivo'
            entry['why'] = 'sin_numero_vivo'
            counts['codigos_archivo'] += 1
            entries[key] = entry
            continue
        joining = [row for row in live if row.joins] if entry['own'] else live
        base = joining or live
        lines = {merged.get(snap.numbers[row.ref].line, snap.numbers[row.ref].line) for row in base}
        holders = [(held, line) for held, line in key_holders(snap, merged, brand, entry['code']) if held.id not in disputed]
        held_lines = {line for _, line in holders}
        entry['lines'] = sorted(lines | held_lines)
        entry['holders'] = [{'id': held.id, 'line': line, 'code': held.code, 'brand': held.brand, 'copy': held.copy,
                             'library': held.library} for held, line in holders]
        everywhere = lines | held_lines
        if len(everywhere) == 1:
            line = next(iter(everywhere))
            on_line = next((held for held, held_line in holders if held_line == line), None)
            entry['line'] = line
            nums = list({row.ref: snap.numbers[row.ref] for row in base}.values())
            if on_line is not None:
                entry['outcome'], entry['reference'] = 'existe', on_line.id
                counts['codigos_ya_referencia'] += 1
            elif snap.lines[line].estado != 'referencia' and not same_part(snap.lines[line].description, nums):
                entry['outcome'], entry['why'] = 'archivo', 'otra_pieza'  # the line names another part than the catalog's item
                counts['codigos_archivo'] += 1
                counts['codigos_otra_pieza'] += 1
            elif entry['own'] or entry['manual']:
                entry['outcome'], entry['estado'] = 'convertir', 'declarado'
                counts['refs_propias' if entry['own'] else 'refs_manuales'] += 1
            elif entry['checked']:
                entry['outcome'], entry['estado'] = 'convertir', 'inferido'
                counts['refs_competidor'] += 1
            else:
                entry['outcome'], entry['why'] = 'archivo', 'catalogo_no_revisado'
                counts['codigos_archivo'] += 1
            entries[key] = entry
            continue
        counts['codigos_varias_lineas'] += 1
        counts['codigos_varias_lineas_propios' if entry['own'] else 'codigos_varias_lineas_competidor'] += 1
        index = {pending.get(line) for line in everywhere}
        if len(index) == 1 and None not in index:
            entry['outcome'], entry['group'] = 'espera_grupo', groups[next(iter(index))]['id']
            counts['codigos_esperan_su_grupo'] += 1
        elif entry['sold']:
            entry['outcome'] = 'caso'
            counts['codigos_vendidos_varias_lineas'] += 1
        else:
            entry['outcome'], entry['why'] = 'archivo', 'no_se_vende'
            counts['codigos_archivo'] += 1
        entries[key] = entry
    for entry in entries.values():
        counts[f"filas_{entry['outcome']}"] += len(entry['rows'])
    return {'entries': entries, 'counts': counts}

















# --- writing --------------------------------------------------------------------------------------------------------------------
















def merge_case_for(snap, lines, *, evidence, reason, guard='', batch_key='', route='caso', key=None):
    """A merge case spec (mall.reference_matching.open_case) on these lines, keeper first; None when fewer than two lines remain once the
    pairs kept apart are dropped."""
    from .models import Part
    from .reference_matching import merge_case_spec
    rows = {str(part.pk): part for part in Part.objects.filter(pk__in=lines)}
    ordered = [rows[pk] for pk in lines if pk in rows]
    spec = merge_case_spec(ordered, evidence=evidence, reason=reason, guard=guard, batch_key=batch_key, route=route)
    if spec is None:
        return None
    keeper = rows.get(lines[0])  # merge_case_spec puts publicada lines first; the case proposes the keeper 3g chose
    if keeper is not None and keeper in spec['line_rows']:
        spec['line_rows'] = [keeper] + [line for line in spec['line_rows'] if line.pk != keeper.pk]
        spec['lines'] = [line.pk for line in spec['line_rows']]
    if key:
        spec['key'] = key
    return spec


def group_evidence(snap, group):
    numbers = [{'brand': num.brand, 'code': num.code, 'line': num.line, 'sku': snap.lines[num.line].sku}
               for pk in group['lines'] for num in snap.lines[pk].numbers]
    printed = {}
    for row in snap.rows:
        printed.setdefault((snap.brand(row.brand), row.number), ' '.join((row.code or '').upper().split()))
    codes = [{'brand': brand, 'code': printed.get((brand, number), number), 'own': True} for brand, number in group['code_keys'][:200]]
    return {'regla': '3d', 'grupo': group['id'], 'flags': group['flags'], 'motivo': group['motivo'], 'families': group['families'],
            'numbers': numbers[:200], 'numbers_total': len(numbers), 'codes': codes,
            'linked_skus': [snap.lines[pk].sku for pk in group['linked']]}


def group_key(group):
    from .matching_engine import digest
    return f'merge:{BATCH_KEY}:{digest(sorted(set(group["lines"]) | set(group["linked"])))[:40]}'





def tag_merge(merge, group_id, codes, flags, rule='3d'):
    """What the pass adds to a CatalogMerge's summary (the digest's evidence; retire_citation reads its codes)."""
    if merge is None:
        return
    summary = {**(merge.summary or {}), 'merge_pass': {'grupo': group_id, 'regla': rule, 'codes': list(codes)[:50], 'flags': list(flags)}}
    type(merge).objects.filter(pk=merge.pk).update(summary=summary)
    merge.summary = summary











def source_marks(snap, rows, citation):
    return sorted({mark for row in rows if citation in row.citations for mark in row.marks})


def write_code(snap, entry, rows, *, user, extra=None):
    """One code's 3e write: the Reference it becomes on its line (or the one there already), and a source per citation with its marks.
    Returns (reference, added, [source ids created])."""
    from .models import Reference, ReferenceSource
    if entry['outcome'] == 'convertir':
        key = references.clave(references.code_key(entry['code']))
        if Reference.all_objects.filter(references.brand_q(entry['brand']), part_id=entry['line'], clave=key, estado='retirada',
                                        detail__retired__reason=RETIRED_HERE).exists():
            return None, False, []  # a person retired it from the line's number in the Biblioteca: no import brings it back
        numbers = sorted({snap.numbers[row.ref].code for row in rows if row.ref in snap.numbers})[:20]
        ref, added = references.add(entry['line'], entry['code'], entry['brand'], tipo='aftermarket', estado=entry['estado'], actor=user,
                                    absorb_library=False, detail={TAG: {'regla': '3e', 'numbers': numbers, **(extra or {})}})
    else:
        ref, added = Reference.all_objects.filter(pk=entry['reference']).first(), False
        if ref is None:
            return None, False, []
    created = []
    for row in rows:
        for citation in row.citations:
            kind = 'admin' if citation.startswith(MANUAL) else 'catalogo'
            source, new = references.cite(ref, citation, user, kind=kind, detail={TAG: {'regla': '3e', 'row': row.id}})
            marks = source_marks(snap, rows, citation)
            merged = sorted(set(source.marks or []) | set(marks))
            if marks and merged != sorted(source.marks or []):
                ReferenceSource.objects.filter(pk=source.pk).update(marks=merged)
            if new:
                created.append(source.pk)
    return ref, added, created











# --- later changes (3f): imports, catalog replaces -----------------------------------------------------------------------------------




def settle_line(line_ids, *, rows=(), actor=None, extra=None):
    """3e for some lines: each aftermarket code of `rows` (the codes a catalog import read for its numbers, in memory: load) whose
    numbers sit on one of these lines only (no other line's number files it, no other line holds it) becomes its Reference, with its
    sources; a code already its Reference gains the sources. With no rows nothing changes: from S10 no table keeps the codes filed under
    numbers (S9 made each one on one line that line's Reference, and a merge carries a line's References along). Returns how many
    References were added."""
    from django.db import transaction
    line_ids = {str(getattr(pk, 'pk', pk)) for pk in ([line_ids] if not isinstance(line_ids, (list, tuple, set)) else line_ids) if pk}
    rows = [tuple(row) for row in rows]
    if not line_ids or not rows:
        return 0
    from .models import Reference
    numbers = list(Reference.objects.filter(references.library_q(), part_id__in=line_ids).values_list('pk', flat=True))
    filed = {row[4] for row in rows if row[1] in set(numbers)}
    snap = load(scope={'lines': line_ids, 'numbers': filed}, rows=rows)
    excluded = Counter()
    mark_rows(snap, excluded)
    codes = plan_codes(snap, {}, {}, [])
    rows_by_id = {row.id: row for row in snap.rows}
    added = 0
    with transaction.atomic():
        for entry in codes['entries'].values():
            if entry['outcome'] in ('convertir', 'existe') and entry['line'] in line_ids:
                ref, new, _ = write_code(snap, entry, [rows_by_id[pk] for pk in entry['rows'] if pk in rows_by_id], user=actor, extra=extra)
                added += 1 if new else 0
    return added


def quiet(snap, line_ids):
    """Whether every line is in referencia without variants (3f: only those merge on their own after S9)."""
    return all(pk in snap.lines and snap.lines[pk].estado == 'referencia' and not snap.lines[pk].variants for pk in line_ids)


def case_rows(case):
    """The catalog rows a merge case waits on (evidence catalog_rows), as load() reads rows."""
    return [tuple(row) for row in (case.evidence or {}).get(CASE_ROWS) or []]


def keep_case_rows(case, rows):
    """Add catalog rows to a merge case's evidence, each (number, brand, compact code) once with its citations merged; its fingerprint
    does not move. Returns how many rows were new."""
    from django.db import transaction
    from .matching_models import MatchingCase
    from .oem_reference import citation_list
    rows = [list(row) for row in rows]
    if not rows or case is None:
        return 0
    with transaction.atomic():
        locked = MatchingCase.objects.select_for_update().filter(pk=case.pk).first()
        if locked is None:
            return 0
        evidence = dict(locked.evidence or {})
        kept = [list(row) for row in evidence.get(CASE_ROWS) or []]
        index = {(row[1], row[2], row[4]): row for row in kept}
        new = 0
        for row in rows:
            known = index.get((row[1], row[2], row[4]))
            if known is None:
                kept.append(row)
                index[(row[1], row[2], row[4])] = row
                new += 1
            else:
                known[5] = citation_list(known[5] or [], *(row[5] or []))
        evidence[CASE_ROWS] = kept
        MatchingCase.objects.filter(pk=locked.pk).update(evidence=evidence)
        case.evidence = evidence
    return new


def join_numbers(ref_ids, *, rows=(), links=(), actor=None, reason=''):
    """3f for a later catalog import: the groups the numbers `ref_ids` now belong to, by the import's rows and links (in memory: load).
    A group whose lines are all in referencia without variants (and that passes the guards) merges on its own (merge_lines, library); any
    other gets a merge case, unless a case already asks about those lines; either way the case keeps the rows of its lines' numbers
    (keep_case_rows), which its decision settles. Then the codes now on one line become References (3e, settle_line). Returns
    {'merged', 'cases', 'references'}."""
    from django.db import transaction
    from .line_merge import merge_lines, service_actor
    from .models import Part
    from .reference_matching import open_case
    ref_ids = {int(pk) for pk in ref_ids if pk}
    out = Counter()
    if not ref_ids:
        return dict(out)
    rows, links = [tuple(row) for row in rows], [tuple(link) for link in links]
    snap = load(rows=rows, links=links)
    lines = {snap.numbers[pk].line for pk in ref_ids if pk in snap.numbers}
    if not lines:
        return dict(out)
    planned = plan(snap)
    actor = actor or service_actor()
    touched = set(lines)
    with transaction.atomic():
        def waiting(members):
            return [row for row in rows if row[1] in snap.numbers and snap.numbers[row[1]].line in members]
        for group in planned['groups']:
            members = set(group['lines']) | set(group['linked'])
            if not members & lines or group['route'] == 'separadas':
                continue
            if group['route'] == 'cubierto':  # a case already asks about these lines: it waits on these rows too
                covering = references.covering_case(members)
                if covering is not None and covering.status == 'review':
                    keep_case_rows(covering, waiting(members))
                continue
            if group['route'] == 'auto' and quiet(snap, members):
                target = group['target']
                if group['main']:
                    references.set_main(Part.objects.get(pk=target), group['main'], actor=actor, reason=reason or 'S9 (3f)')
                merge = merge_lines(uuid.UUID(target), [uuid.UUID(pk) for pk in sorted(members - {target})], actor=actor,
                                    reason=MERGE_REASON, automatic=True, library=True)
                tag_merge(merge, group['id'], group['codes'], group['flags'], rule='3f')
                touched.add(target)
                out['merged'] += 1
                continue
            order = group.get('order') or sorted(members, key=lambda pk: keeper_rank(snap, pk))
            spec = merge_case_for(snap, order, evidence={**group_evidence(snap, group), 'regla': '3f'},
                                  reason=group['reason'] or 'Una equivalencia nueva de un catálogo une estas líneas: si son el mismo repuesto, '
                                                            'unifícalas; si no, mantenlas separadas.',
                                  guard=group['motivo'] if group['route'] == 'caso' else 'lineas', route='caso')
            if spec is not None:
                keep_case_rows(open_case(spec), waiting(members))
                out['cases'] += 1
        from .lines import canonical_ids
        targets = set(canonical_ids(touched).values())
        out['references'] += settle_line(targets, rows=rows, actor=actor, extra={'regla_3f': True})
    return dict(out)


def retire_citation(line, brand, code, citations, *, actor=None):
    """3f, when a code is retired (a catalog replace): the sources these citations
    gave the code's Reference on `line` (a line, its id, or a Reference on it: the number) become retired; an inferido Reference left with
    no live source goes to disputa, and so does a declarado one this pass made (3e from a catalog's own code, or a code added by hand in
    the Biblioteca); and when the code was one of the catalog's own codes behind an automatic merge of this pass, a merge case lists that
    merge's lines (Unificar keeps it, Mantener separadas takes it apart). Returns {'sources', 'disputed', 'case'}."""
    from django.db import transaction
    from django.utils import timezone
    from .brands import resolve
    from .models import CatalogMerge, Reference, ReferenceSource
    from .reference_matching import open_case
    out = {'sources': 0, 'disputed': 0, 'case': None}
    name = resolve(brand) if brand else ''
    key = references.clave(references.code_key(code))
    line_id = getattr(line, 'part_id', None) or getattr(line, 'pk', line)
    with transaction.atomic():
        held = (Reference.all_objects.select_for_update().filter(references.brand_q(name), part_id=line_id, clave=key, supplier__isnull=True,
                                                                 estado__in=references.LIVE).order_by('pk').first())
        if held is not None:
            now = timezone.now()
            for citation in citations or []:
                kind = 'admin' if citation.startswith(MANUAL) else 'catalogo'
                updated = ReferenceSource.objects.filter(reference=held, kind=kind, citation=citation[:500], retirada=False).update(
                    retirada=True, retired_by=actor if getattr(actor, 'pk', None) else None, retired_at=now)
                out['sources'] += updated
            # An inferido Reference, or a declarado one this pass made from catalog or Biblioteca rows (detail s9 with its sources),
            # left with no live source stops showing: disputa, the code is no longer on any number of this line. A person's own
            # declarado code (no s9 detail) and a main stay.
            made_here = held.estado == 'declarado' and TAG in (held.detail or {})
            if ((held.estado == 'inferido' or made_here) and not held.is_main and held.sources.exists()
                    and not held.sources.filter(retirada=False).exists()):
                manual = all(citation.startswith(MANUAL) for citation in citations or [])
                note = ('Se retiró de la Biblioteca la equivalencia en la que se basaba.' if manual and made_here
                        else 'Su catálogo retiró la equivalencia en la que se basaba.')
                references.update(held, estado='disputa', dispute_note=note, actor=actor)
                out['disputed'] = 1
        brands = catalog_brands()
        own = any(catalog_info(parse_citation(citation)[0], brands)[0] == name for citation in citations or [] if not citation.startswith(MANUAL))
        if own:
            merge = next((merge for merge in CatalogMerge.objects.filter(target_id=line_id, summary__reason=MERGE_REASON).order_by('-created_at')
                          if f'{name} {cc(code)}' in ((merge.summary or {}).get('merge_pass') or {}).get('codes', [])), None)
            if merge is not None and any(entry.get('id') for entry in merge.sources or []):
                lines = [str(merge.target_id), *(str(entry['id']) for entry in merge.sources)]
                from .models import Part
                rows = {str(part.pk): part for part in Part._base_manager.filter(pk__in=lines)}
                ordered = [rows[pk] for pk in lines if pk in rows]
                spec = {'kind': 'merge', 'lines': [line.pk for line in ordered], 'line_rows': ordered, 'guard': 'retirada',
                        'batch_key': RETIRED_BATCH, 'route': 'caso', 'reference': None, 'key': f'merge:{RETIRED_BATCH}:{merge.pk}',
                        'evidence': {'regla': '3f', 'merge_id': str(merge.pk), 'codes': [{'brand': name, 'code': code}],
                                     'citations': list(citations or [])[:20]},
                        'reason': (f'Se retiró del catálogo {name} {code}, uno de los códigos que unificaron estas líneas: «Unificar» conserva '
                                   'la unificación; «Mantener separadas» la deshace con Separar SKU.')}
                if len(ordered) >= 2:
                    out['case'] = open_case(spec)
    return out


S9_RULES = ('3d', '3e', 'paso0', '3f')


def convert_case_codes(case, action, *, line=None, target=None, actor=None):
    """A merge case of this pass decided (mall.reference_matching.decide_merge). Merged: nothing more (the merge carries the lines'
    References along, and the step 0 copies there were settled by it: their note ends "caso de unificación").
    Merged: the catalog rows the case waits on whose numbers now sit on the merged line only become its References (3e, settle_line).
    Kept apart with a line chosen: the case's codes become References of that line only (a copy in disputa there is declarado again; a
    Reference this pass placed on another of the case's lines and the copies elsewhere leave them: retired, unless variants hang from
    them). Kept apart with no line (a step 0 case, from a screen that sends none): each copy leaves disputa, retired when another line
    holds its code (the code stays with its OEM numbers), declarado again on its SKU otherwise; no copy waits hidden with no way out.
    Returns how many References were added."""
    evidence = case.evidence or {}
    if evidence.get('regla') not in S9_RULES:
        return 0
    if action in ('approve', 'merge'):
        return settle_line([target], rows=case_rows(case), actor=actor, extra={'caso': str(case.pk)}) if target else 0
    if not line:
        if evidence.get('regla') == 'paso0':
            release_copies(case, actor=actor)
        return 0
    line_id = str(getattr(line, 'pk', line))
    return place_case_codes(case, line_id, actor=actor) + settle_line([line_id], rows=case_rows(case), actor=actor, extra={'caso': str(case.pk)})


def exclusive_holders(code, brand, exclude_line=None):
    """The live exclusive public References that hold a branded code (its brand, or none), on lines that are not unificada."""
    from .models import Reference
    rows = Reference.all_objects.filter(references.brand_q(brand) | references.blank_brand_q(), clave=references.clave(references.code_key(code)),
                                        supplier__isnull=True, estado__in=references.EXCLUSIVE,
                                        part__merged_into__isnull=True).exclude(part__estado='unificada')
    return rows.exclude(part_id=exclude_line) if exclude_line else rows


def release_copies(case, *, actor=None):
    """Step 0 kept apart with no line chosen: see convert_case_codes. Returns {'retired', 'declared'}."""
    from django.db import transaction
    from .models import Reference
    out = Counter()
    for code in (case.evidence or {}).get('codes') or []:
        copy = Reference.all_objects.filter(pk=code.get('copy'), estado='disputa', is_main=False, supplier__isnull=True).first()
        if copy is None:
            continue
        try:
            with transaction.atomic():
                if exclusive_holders(copy.code, copy.brand, exclude_line=copy.part_id).exists():
                    references.retire(copy, actor=actor, reason='Caso de unificación: líneas separadas; el código queda con sus números OEM')
                    out['retired'] += 1
                else:
                    references.update(copy, estado='declarado', dispute_note='', actor=actor)
                    out['declared'] += 1
        except references.ReferenceConflict:
            continue  # variants hang from it, or another line took the code meanwhile: it stays in disputa for a person
    return dict(out)


def place_case_codes(case, line_id, *, actor=None):
    """Kept apart with a line chosen: see convert_case_codes. Returns how many References were added."""
    from django.db import transaction
    from .models import Reference
    case_lines = {str(pk) for pk in case.lines.values_list('pk', flat=True)}
    from .lines import canonical_ids
    case_lines = set(canonical_ids(case_lines).values()) | case_lines
    added = 0
    for code in (case.evidence or {}).get('codes') or []:
        brand, text = code.get('brand') or '', code.get('code') or ''
        if not brand or not text:
            continue
        try:
            with transaction.atomic():
                for held in exclusive_holders(text, brand, exclude_line=line_id).filter(part_id__in=sorted(case_lines), is_main=False):
                    made = (held.detail or {}).get(TAG) or {}
                    if made and not made.get('caso') and not made.get('manual'):  # placed by this pass, not by a person
                        references.retire(held, actor=actor, reason='Caso de unificación: el código queda en la línea elegida')
                key = references.clave(references.code_key(text))
                for copy in Reference.all_objects.filter(clave=key, brand=brand, supplier__isnull=True, estado='disputa').exclude(is_main=True):
                    if str(copy.part_id) == line_id:
                        references.update(copy, estado='declarado', dispute_note='', actor=actor)
                    elif code.get('copy') == copy.pk:
                        references.retire(copy, actor=actor, reason='Caso de unificación: el código queda en la línea elegida')
                ref, created = references.add(line_id, text, brand, tipo='aftermarket', estado='declarado' if code.get('own') else 'inferido',
                                              actor=actor, absorb_library=False, detail={TAG: {'regla': '3e', 'caso': str(case.pk)}})
        except references.ReferenceConflict:
            continue  # another line holds it in a way this case does not decide (a person's code, variants): the code stays where it is
        added += 1 if created else 0
    return added


# --- the report and the CLI ---------------------------------------------------------------------------------------------------------


















