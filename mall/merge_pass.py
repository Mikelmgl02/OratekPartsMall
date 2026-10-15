"""The catalog merge pass (inventory refactor S9, plan §3 S9): the diagram's rules 3b–3g on the OEM library, as the owner approved them
(2026-10-13/14/15: only identical parts merge; partner makes and items that replace others merge on their own; merges into a SKU's
line are automatic, with a digest and Separar; competitor codes open merge cases only when a supplier sells them; rule 3 links -J,
-G, -NP and -K on its own). It ports the scratchpad prototype (interchange/proc/P2.py with build.py) from OEM numbers to lines: since
S4 every number of the library is a Reference on a line (its referencia line, or the SKU that carries it), so a group of numbers is a
group of lines, and merging them is mall.line_merge.merge_lines.

    python -m mall.merge_pass --dry-run|--apply --user USERNAME|--rollback --report DIR [--chunk 200] [--json]

What it reads (a dry run reads only, inside a READ ONLY transaction):
- the library's numbers: live References of tipo oem with a maker as brand (mall.references.library_q) on live lines that are not
  rechazada, in an exclusive estado (a number in disputa joins nothing);
- the aftermarket codes the supplier catalogs file under them (OEMCrossReference), their tombstones (OEMCrossReferenceRetired) and the
  SKUs' catalog links (PartOEMLink, source catalog), which never join numbers: they only tell which SKU a group's catalog item is;
- the GMB rows marked '*' and the numbers printed under W/HOUSING NO. (mall/merge_pass_gmb_marks.json, read from the PDF's text) and
  each catalog item's position (output/catalog_data/*.json, when the folder is there);
- the References already holding the codes, the codes suppliers sell, the lines kept apart and the cases already open.

Step 0, catalog-copied company References (the 276 company codes citing a catalog; Alternos › Por revisar's 55 SKUs): a copy on the
line its numbers end up on is kept and gains its catalogo sources (3e); a copy on another line gets a merge case with those lines,
and goes to disputa (unless it is its line's main), its note ending "caso de unificación" so a merge settles it.

3b, groups. Lines join when a catalog prints their numbers under its own brand's item (GMB, ASVA, ATE in their own catalogs), when the
same compact number sits under two makes, and when a number is the twin spelling of another (16100-87795-000 = 16100-87795, the GM
family's leading zero: S0.10's leftovers, which became S9's once S4 froze the OEM table). Numbers on one SKU line are joined already (a
SKU named after a number, an OEM alterno: S4 put them there). Competitor codes and catalog links never join, nor do GMB rows marked *,
W/HOUSING NO. numbers under an item without H, or rows citing only the GMB index pages (522–547) for a number with no applications.

3c, guards (the failing guard is stored on the case, its flags in the evidence): the numbers' own prefixes or the SKU nouns name
different parts (pump / fan clutch, joint / shaft: prefix or noun; with housing / without, Toyota 16100 / 16110: prefix), front/rear or
LH/RH differ (side), or a make is missing from the applications and the makes are not partners (make). The guards read the numbers of
the group's lines and of the SKU a catalog link ties to it (it becomes the target): a joint never merges onto a SKU whose number names
a shaft. Items the catalog says consolidate others (=(GW..), NEW TYPE FOR, two rival brands mapping two codes) and partner makes
(Ford/Mazda, Nissan/Renault...) are flagged only: the owner approved them merging on their own.

3d, routes. Strong (every guard passes, at most one live line: publicada, retirada, or holding variants; a SKU tied to the group by a
catalog link counts): merged on its own (merge_lines, automatic, reason merge_pass; the digest is the CatalogMerge list, Unificados
with origen automático, and this pass's report; Separar undoes each), into that SKU's line when there is one. Two or more live lines,
a chain of items, or a SKU linked to two groups: one merge case, route lote, batch merge-pass. A failed guard: one merge case, route
caso. Lines kept apart (CatalogSeparation, on canonical lines) are never proposed, and lines a case already covers (decided or not)
are left to it. A merge the database refuses when it is applied becomes a case with the reason (as the dry run predicts:
conflicto, subgrupo).

3g, the main. Into a SKU's line its main stays (the SKU's number). With no SKU: the number with most supplier stock, then printed by
most catalogs, then the highest revision of the same base number; its line is the target and the number its main (set_main). The
other numbers stay OEM References (tipo oem, their maker as brand) on the merged line, with their estado.

3e, codes. After the merges, an aftermarket code whose numbers all sit on one line (and that no other line holds) becomes a Reference
there: declarado for the catalog's own code, inferido for a competitor's and only from a PDF-checked catalog (OD17: GMB v3, ASVA CV,
ASVA Wheel Hub, ASVA Tensioner, ATE K24); a code a person registered (Registro manual) is declarado with an admin source. Every
citation becomes a catalogo source, its marks kept ('*', 'W/HOUSING', 'index_only'). A code already a Reference of that line only
gains the sources. A code on two or more lines stays a catalog row: within one group's case it waits there; otherwise the lines its
codes join form one merge case per cluster, only for codes a supplier sells (OD15); the rest stay in the archive and this report.
Every OEMCrossReference row gets an outcome (converted, case, archive), which --apply writes to output/refactor/s9/xref_outcomes.jsonl
for S10.

3f. A tombstone (a code retired from a number) is a retired catalogo source (ReferenceSource.retirada) on the Reference the code
became, and an inferido Reference left with no live source goes to disputa. From S9 on, retire_citation does the same when the
Biblioteca or a catalog replace retires a code, and opens a merge case on the lines of an automatic merge the code backed. A later
import that would join lines (join_numbers): merged on its own only when every line is in referencia without variants, otherwise a
merge case; a code added by hand in the Biblioteca on two or more lines always opens a merge case (code_added: it never merges). A
declarado Reference this pass made that loses its last source goes to disputa as an inferido one does.

The report (--report DIR): summary.json (counts, the prototype's for comparison), groups.json, digest.json (the automatic merges),
cases.json, codes.json (the outcome of every code), hidden.json (every storefront code that would stop showing, and where it goes),
step0.json, tombstones.json. --apply runs under the catalog freeze (mall.refactor.guard) and the matching worker's lock, and refuses
while an Excel or catalog import is half applied: the automatic merges in chunks of --chunk groups, each atomic and verified (I1–I3 on
its lines) before the next (a supplier import met on the way stops the run, that chunk rolled back); then, on the catalog as the merges
left it, a second plan where nothing is automatic any more (a group strong only now waits in the batch, and no code is placed as if it
had merged): the cases, the References and sources of 3e and step 0, and 3f, in chunks of lines. Every row it creates or changes goes
to output/refactor/s9/manifest.jsonl with its values before; --rollback undoes them newest first (sources deleted, References retired
or given their estado back, mains given back, each merge split with Separar, cases still in review deleted).

Deploy (plan §1.2.3, review 2026-10-15): under the freeze, --apply from the worktree before the code lands (the code still live reads
the SKUs' catalog links, so storefront equivalents, search and the Biblioteca keep every code while the pass writes); then land the code
(its readers no longer read the links) and run --apply once more (it finds nothing left); then unfreeze. Roll back in the same freeze:
revert the code first, then --rollback from the worktree.
"""
import argparse
import json
import os
import re
import shutil
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from . import references

STAGE = 's9'
MERGE_REASON = 'merge_pass'
BATCH_KEY = 'merge-pass'
RETIRED_BATCH = 'merge-pass-retirada'
TAG = 's9'  # detail key of the References and sources this pass writes
CHUNK = 200
GMB_TITLE = 'GMB Water Pump Catalog 2016'
MARKS_FILE = Path(__file__).with_name('merge_pass_gmb_marks.json')
EXTRACTS = ('asva_hub_2017', 'asva_tensioner_2017', 'asva_cv_2017', 'ate_wear_2017', 'gmb_wp_2016_v3')
MANUAL = 'Registro manual'  # how the Biblioteca cites a code a person added (oem_reference_views)
MERGE_DISPUTE = 'caso de unificación'  # mall.line_merge.MERGE_DISPUTE: a merge settles a dispute whose note ends so
# The prototype on corrected GMB data (P2_summary.json, 2026-10-13), for the report's comparison.
EXPECTED = {'grupos_auto': 2298, 'numeros_auto': 9129, 'auto_sin_sku': 2103, 'auto_un_sku': 195, 'grupos_lote': 65, 'numeros_lote': 406,
            'grupos_caso': 54, 'numeros_caso': 392, 'refs_propias': 3951, 'refs_competidor': 5886, 'codigos_varias_lineas': 933,
            'clusters': 208}

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


def load(*, marks_path=MARKS_FILE, positions_folder=None, scope=None):
    """Read the snapshot (read-only). scope {'lines': line ids, 'numbers': compact code numbers}: only the library numbers on those
    lines and the ones those aftermarket codes are filed under, with their rows (the incremental paths: settle_line, code_added); no
    step 0 copies and no supplier sales then."""
    from .brand_models import Brand, BrandAlias
    from .line_merge import separated_pairs
    from .lines import CATALOG, canonical_ids
    from .matching_models import MatchingCase
    from .models import ItemApplication, Part, PartSpecification, Reference, ReferenceSource, SupplierItem
    from .oem_reference_models import OEMCrossReference, OEMCrossReferenceRetired, PartOEMLink
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
    rows = (Reference.all_objects.filter(references.library_q(), supplier__isnull=True, estado__in=references.EXCLUSIVE,
                                         part__merged_into__isnull=True)
            .exclude(part__estado__in=('unificada', 'rechazada'))
            .values_list('pk', 'uid', 'code', 'brand', 'part_id', 'estado', 'is_main', 'detail'))
    if scope is not None:
        from django.db.models import Q
        filed = OEMCrossReference.objects.filter(number__in=sorted(scope.get('numbers') or ())).values('reference_id')
        rows = rows.filter(Q(part_id__in=sorted(scope.get('lines') or ())) | Q(pk__in=filed))
    for pk, uid, code, brand, part_id, estado, is_main, detail in rows.iterator(chunk_size=5000):
        num = Num()
        detail = detail or {}
        num.id, num.uid, num.code, num.number, num.brand = pk, str(uid), code, cc(code), brand
        num.family, num.line, num.estado, num.is_main = family(brand), str(part_id), estado, is_main
        num.description, num.applications = detail.get('description') or '', detail.get('applications') or ''
        num.part_type, num.catalogs = detail.get('part_type') or '', set()
        snap.numbers[pk] = num
        snap.by_number[num.number].add(pk)

    # Catalog links (never a join): the canonical catalog line of each linked SKU.
    raw_links = PartOEMLink.objects.filter(source='catalog')
    if scope is not None:
        raw_links = raw_links.filter(reference_id__in=list(snap.numbers))
    raw_links = list(raw_links.values_list('part_id', 'reference_id'))
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
    crossed, retired = OEMCrossReference.objects.all(), OEMCrossReferenceRetired.objects.all()
    if scope is not None:
        crossed = crossed.filter(number__in=sorted(scope.get('numbers') or ()))
        retired = retired.filter(number__in=sorted(scope.get('numbers') or ()))
    for values in crossed.values_list('pk', 'reference_id', 'brand', 'code', 'number', 'citations').iterator(chunk_size=5000):
        snap.rows.append(make_row(*values))
    for values in retired.values_list('pk', 'reference_id', 'brand', 'code', 'number', 'citations').iterator(chunk_size=5000):
        snap.tombstones.append(make_row(*values))
    for row in snap.rows:
        num = snap.numbers.get(row.ref)
        if num is not None:
            num.catalogs.update(row.catalogs)

    # The References that hold the codes (any live public row of their claves), and step 0's catalog-copied company codes.
    claves = sorted({references.clave(references.code_key(row.code)) for row in snap.rows + snap.tombstones if row.code})
    held_ids = []
    for chunk in chunked(claves):
        found = (Reference.all_objects.filter(clave__in=chunk, estado__in=references.LIVE, supplier__isnull=True, part__merged_into__isnull=True)
                 .exclude(part__estado='unificada')
                 .values_list('pk', 'part_id', 'code', 'clave', 'brand', 'estado', 'tipo', 'is_main', 'reference_source'))
        for pk, part_id, code, key, brand, estado, tipo, is_main, source in found:
            held = Held()
            held.id, held.line, held.code, held.clave, held.brand, held.estado, held.tipo = pk, str(part_id), code, key, brand, estado, tipo
            held.is_main, held.source, held.library, held.copy, held.sources = is_main, source or '', pk in snap.numbers, False, {}
            snap.held[key].append(held)
            held_ids.append(pk)
    by_id = {held.id: held for rows_ in snap.held.values() for held in rows_}
    for chunk in chunked(held_ids):
        for ref_id, kind, citation, retirada in ReferenceSource.objects.filter(reference_id__in=chunk).values_list('reference_id', 'kind', 'citation', 'retirada'):
            by_id[ref_id].sources[(kind, citation)] = retirada
    copies = set(ReferenceSource.objects.filter(kind='catalogo', reference__tipo='aftermarket', reference__estado__in=references.LIVE,
                                                reference__supplier__isnull=True).values_list('reference_id', flat=True)) if scope is None else set()
    missing = copies - set(by_id)
    if missing:  # a copy whose clave no catalog row has (its code is not an aftermarket code of any number)
        for pk, part_id, code, key, brand, estado, tipo, is_main, source in (
                Reference.all_objects.filter(pk__in=missing, part__merged_into__isnull=True).exclude(part__estado='unificada')
                .values_list('pk', 'part_id', 'code', 'clave', 'brand', 'estado', 'tipo', 'is_main', 'reference_source')):
            held = Held()
            held.id, held.line, held.code, held.clave, held.brand, held.estado, held.tipo = pk, str(part_id), code, key, brand, estado, tipo
            held.is_main, held.source, held.library, held.copy, held.sources = is_main, source or '', False, False, {}
            by_id[pk] = held
    for pk in sorted(copies):
        if pk in by_id:
            by_id[pk].copy = True
            snap.company.append(by_id[pk])

    # The lines: the numbers', the linked SKUs' and the holders'.
    line_ids = ({num.line for num in snap.numbers.values()} | {pk for pk in linked_canonical.values()}
                | {held.line for rows_ in snap.held.values() for held in rows_} | {held.line for held in snap.company})
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
    for case_id, key, kind in MatchingCase.objects.filter(kind__in=('merge', 'catalog', 'catalog_reference')).values_list('pk', 'key', 'kind'):
        snap.case_keys.add(key)
    for case_id, part_id in MatchingCase.lines.through.objects.filter(matchingcase__kind='merge').values_list('matchingcase_id', 'part_id'):
        case_lines[case_id].add(str(part_id))
    for case_id, sources, candidates in MatchingCase.objects.filter(kind__in=('catalog', 'catalog_reference')).values_list('pk', 'sources', 'candidates'):
        case_lines[case_id] |= {str((row or {}).get('id')) for row in [*(sources or []), *(candidates or [])] if (row or {}).get('id')}
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
    """The whole pass, read-only: groups and their routes (3b–3d, 3g), cases, the codes' outcomes (3e), step 0 and the tombstones (3f),
    and the storefront codes that would stop showing. after_merges: the apply's second plan, on the catalog its merges left, where no
    group is automatic any more (a group strong only now goes to the batch), so the codes, step 0 and 3f map no merge."""
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

    # The lines each automatic merge leaves (L), and the projections with the batches and cases approved.
    def mapping(routes):
        found = {}
        for group in out_groups:
            if group['route'] in routes:
                target = group['target'] or group['order'][0]
                for pk in set(group['lines']) | set(group['linked']):
                    found[pk] = target
        return found
    merged = mapping({'auto'})
    pending = {}
    for index, group in enumerate(out_groups):
        if group['route'] in ('lote', 'caso', 'cubierto', 'separadas'):
            for pk in set(group['lines']) | set(group['linked']):
                pending[merged.get(pk, pk)] = index

    step0 = plan_step0(snap, merged)
    disputed = {item['reference'] for item in step0['rows'] if item.get('dispute')}
    codes = plan_codes(snap, merged, pending, out_groups, disputed)
    projections = {label: reach_counts(snap, mapping(routes)) for label, routes in
                   (('A', {'auto'}), ('A+B', {'auto', 'lote'}), ('A+B+C', {'auto', 'lote', 'caso'}))}
    clusters = plan_clusters(snap, merged, codes, step0, out_groups)
    tombstones = plan_tombstones(snap, merged, codes)
    hidden = plan_hidden(snap, merged, codes, clusters, step0)

    # Counts.
    for group in out_groups:
        route = group['route']
        counts[f'grupos_{route}'] += 1
        if group['kind'] == 'enlace':
            counts[f'enlace_{route}'] += 1
        counts[f'numeros_{route}'] += group['numbers']
        counts[f'lineas_{route}'] += len(set(group['lines']) | set(group['linked']))
        if route == 'auto':
            counts['auto_un_sku' if group['skus'] else 'auto_sin_sku'] += 1
            counts['auto_con_principal_nuevo'] += 1 if group['main'] else 0
        counts[f"{route}/{'sku2+' if len(group['skus']) >= 2 else 'sku1' if group['skus'] else 'sku0'}"] += 1
        for flag in group['flags']:
            counts[f'bandera_{flag}'] += 1
        if group['route'] in ('caso',):
            counts[f"control_{group['motivo']}"] += 1
        if group['route'] == 'lote':
            counts[f"lote_{group['motivo']}"] += 1
        counts['cadenas'] += 1 if group['chain'] else 0
        counts['socios_revisados'] += 1 if group['partner'] and not group['flags'] else 0
    counts['grupos'] = len(out_groups)
    counts['numeros_en_grupos'] = sum(group['numbers'] for group in out_groups)
    for key, value in excluded.items():
        counts[f'excluidas_{key}'] += value
    counts['filas_equivalencias'] = len(snap.rows)
    counts['filas_propias'] = sum(1 for row in snap.rows if row.own)
    counts['filas_competidor'] = len(snap.rows) - counts['filas_propias']
    counts['numeros'] = len(snap.numbers)
    for name, value in codes['counts'].items():
        counts[name] += value
    for name, value in step0['counts'].items():
        counts[name] += value
    counts['clusters'] = len(clusters['cases'])
    counts['clusters_lineas'] = sum(len(case['lines']) for case in clusters['cases'])
    for name, value in clusters['counts'].items():
        counts[name] += value
    for name, value in tombstones['counts'].items():
        counts[name] += value
    for name, value in hidden['counts'].items():
        counts[name] += value
    return {'counts': counts, 'groups': out_groups, 'merged': merged, 'codes': codes, 'step0': step0, 'clusters': clusters,
            'tombstones': tombstones, 'hidden': hidden, 'projections': projections}


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


def reach_counts(snap, mapping):
    """The prototype's reach figures on one projection: codes on one line or on two or more, own and competitor."""
    out = Counter()
    by_key = defaultdict(set)
    own = set()
    for row in snap.rows:
        num = snap.numbers.get(row.ref)
        if num is None:
            continue
        key = (snap.brand(row.brand), row.number)
        if row.own:
            own.add(key)
        if not row.own or row.joins:
            by_key[key].add(mapping.get(num.line, num.line))
    for key, lines in by_key.items():
        kind = 'propios' if key in own else 'competidor'
        out[f"{kind}_{'una_linea' if len(lines) == 1 else 'varias_lineas'}"] += 1
    return dict(out)


def plan_step0(snap, merged):
    """Step 0: each catalog-copied company Reference kept (its numbers end up on its line), or, on another line, in disputa (unless it
    is its line's main) with one merge case per SKU: the SKU and the line of its catalog item (the line its own-brand copies' numbers
    sit on, else the line most of its copies' numbers sit on)."""
    by_key = defaultdict(list)
    for row in snap.rows:
        if row.ref in snap.numbers:
            by_key[(snap.brand(row.brand), row.number)].append(row)
    counts, rows = Counter(), []
    for held in snap.company:
        brand = snap.brand(held.brand)
        rows_k = by_key.get((brand, cc(held.code)), [])
        line = merged.get(held.line, held.line)
        item = {'reference': held.id, 'line': held.line, 'sku': snap.lines[held.line].sku if held.line in snap.lines else '',
                'code': held.code, 'brand': brand, 'main': held.is_main, 'outcome': '', 'lines': [],
                'own': any(row.own for row in rows_k)}
        number_lines = {merged.get(snap.numbers[row.ref].line, snap.numbers[row.ref].line) for row in rows_k}
        item['lines'] = sorted(number_lines)
        if not rows_k:
            item['outcome'] = 'sin_equivalencia'
        elif line in number_lines:
            item['outcome'] = 'misma_linea'
        else:
            item['outcome'] = 'otra_linea'
            item['dispute'] = not held.is_main and held.estado in references.EXCLUSIVE
        counts[f"paso0_{item['outcome']}"] += 1
        counts['paso0_a_disputa'] += 1 if item.get('dispute') else 0
        rows.append(item)
    cases = []
    by_sku = defaultdict(list)
    for item in rows:
        if item['outcome'] == 'otra_linea':
            by_sku[merged.get(item['line'], item['line'])].append(item)
    for line, items in sorted(by_sku.items()):
        own = [item for item in items if item['own']]
        if own:
            focus = {pk for item in own for pk in item['lines']}
        else:
            seen = Counter(pk for item in items for pk in item['lines'])
            best = max(seen.values())
            focus = {pk for pk, n in seen.items() if n == best}
        focus.discard(line)
        if focus:
            cases.append({'sku_line': line, 'lines': [line, *sorted(focus, key=lambda pk: keeper_rank(snap, pk))],
                          'codes': [{'brand': item['brand'], 'code': item['code'], 'copy': item['reference'], 'own': item['own']}
                                    for item in items]})
    counts['paso0_casos'] = len(cases)
    counts['paso0_copias'] = len(snap.company)
    counts['paso0_skus'] = len({item['line'] for item in rows})
    return {'rows': rows, 'cases': cases, 'counts': counts}


def plan_clusters(snap, merged, codes, step0, groups):
    """3e's merge cases: one per cluster of lines that sold codes on two or more lines join (OD15), plus step 0's case per SKU. Lines
    kept apart are dropped from a case (merge_case_spec); a cluster a case already covers, or one inside a group's case, is left to
    it."""
    uf, by_root = UF(), defaultdict(list)
    edges = [(entry['lines'], {'brand': entry['brand'], 'code': entry['code'], 'sold': True, 'own': entry['own']})
             for entry in codes['entries'].values() if entry['outcome'] == 'caso']
    for lines, _ in edges:
        for other in lines[1:]:
            uf.union(lines[0], other)
    for lines, code in edges:
        by_root[uf.find(lines[0])].append((lines, code))
    candidates = [(sorted({pk for line_set, _ in items for pk in line_set if pk in snap.lines}, key=lambda pk: keeper_rank(snap, pk)),
                   [code for _, code in items], 'codigos') for _, items in sorted(by_root.items())]
    candidates += [(case['lines'], case['codes'], 'copias') for case in step0['cases']]
    counts, cases = Counter(), []
    in_group = [frozenset(merged.get(pk, pk) for pk in set(group['lines']) | set(group['linked']))
                for group in groups if group['route'] in ('lote', 'caso')]
    from .line_merge import pair
    for lines, codes_, kind in candidates:
        apart_pairs = set(kept_apart(snap, lines))
        kept = []
        for pk in lines:
            if not any(pair(pk, other) in apart_pairs for other in kept):
                kept.append(pk)
        if len(kept) < 2:
            counts[f'{kind}_separados'] += 1
            continue
        if covered(snap, kept):
            counts[f'{kind}_cubiertos'] += 1
            continue
        if kind == 'codigos' and any(set(kept) <= line_set for line_set in in_group):
            counts[f'{kind}_en_grupo'] += 1
            continue
        cases.append({'kind': kind, 'lines': kept, 'codes': codes_[:200], 'codes_total': len(codes_),
                      'copies': [code['copy'] for code in codes_ if code.get('copy')]})
    return {'cases': cases, 'counts': counts}


def plan_tombstones(snap, merged, codes):
    """3f on the catalog the pass leaves: each tombstone becomes a retired catalogo source on the Reference its code is (or becomes) on
    its number's line; an inferido Reference left with no live source goes to disputa."""
    counts, rows = Counter(), []
    for row in snap.tombstones:
        num = snap.numbers.get(row.ref)
        if num is None:
            counts['lapidas_sin_numero'] += 1
            continue
        line = merged.get(num.line, num.line)
        brand = snap.brand(row.brand)
        entry = codes['entries'].get((brand, row.number))
        target = None
        if entry is not None and entry['line'] == line and entry['outcome'] in ('existe', 'convertir'):
            target = entry
        held = next((held for held, held_line in key_holders(snap, merged, brand, row.code) if held_line == line), None)
        if target is None and held is None:
            counts['lapidas_sin_referencia'] += 1
            continue
        reference = held.id if held is not None else None
        live_sources = {citation for (kind, citation), retirada in (held.sources.items() if held is not None else ()) if not retirada}
        if target is not None and target['outcome'] == 'convertir':
            live_sources |= {citation for r in snap.rows if r.id in set(target['rows']) for citation in r.citations}
        retired = [citation for citation in row.citations if citation not in live_sources]
        if not retired:
            counts['lapidas_cubiertas'] += 1
            continue
        dispute = held is not None and held.estado == 'inferido' and not live_sources
        rows.append({'tombstone': row.id, 'reference': reference, 'code_key': [brand, row.number], 'code': row.code, 'line': line,
                     'citations': retired, 'dispute': dispute})
        counts['lapidas_fuente_retirada'] += 1
        counts['lapidas_a_disputa'] += 1 if dispute else 0
    return {'rows': rows, 'counts': counts}


def plan_hidden(snap, merged, codes, clusters, step0=None):
    """The storefront codes that would stop showing: what each publicada line reaches today (its numbers' codes and its catalog and
    name links' numbers' codes) against what it shows after the pass (mall.oem_links.equivalents_for: its own codes in an exclusive
    estado, and the codes filed under the numbers it holds that no other line holds), with where each hidden code goes (a Reference, a
    case, the archive); and the step 0 copies that go to disputa (a code in disputa is not shown), which wait in their case."""
    step0 = step0 or {'rows': []}
    disputed = {item['reference'] for item in step0['rows'] if item.get('dispute')}
    from .lines import canonical_ids, storefront_lines
    from .oem_reference_models import PartOEMLink
    publicada = {str(pk) for pk in storefront_lines().values_list('pk', flat=True)}
    by_ref = defaultdict(list)
    for row in snap.rows:
        by_ref[row.ref].append(row)
    reached_today, filed_after = defaultdict(set), defaultdict(set)
    for num in snap.numbers.values():
        for row in by_ref.get(num.id, ()):
            key = (snap.brand(row.brand), row.number)
            if num.line in publicada:
                reached_today[num.line].add(key)
            filed_after[merged.get(num.line, num.line)].add(key)
    links = list(PartOEMLink.objects.values_list('part_id', 'reference_id'))
    canonical = canonical_ids({part_id for part_id, _ in links})
    for part_id, ref_id in links:
        line = canonical.get(str(part_id))
        if line in publicada:
            for row in by_ref.get(ref_id, ()):
                reached_today[line].add((snap.brand(row.brand), row.number))
    own_codes = defaultdict(set)  # the codes a line holds itself and shows (exclusive estado; a copy step 0 disputes is not shown)
    for rows_ in snap.held.values():
        for held in rows_:
            if held.estado in references.EXCLUSIVE and held.id not in disputed:
                own_codes[merged.get(held.line, held.line)].add((snap.brand(held.brand), cc(held.code)))
    held_by = defaultdict(set)    # code key -> lines holding it exclusively after the pass
    for key, entry in codes['entries'].items():
        if entry['outcome'] in ('existe', 'convertir'):
            held_by[key].add(entry['line'])
        else:
            held_by[key] |= {holder['line'] for holder in entry['holders']}
    in_case = {(code['brand'], cc(code['code'])) for case in clusters['cases'] for code in case['codes']}
    counts, rows = Counter(), []
    for line, today in reached_today.items():
        after = merged.get(line, line)
        for key in sorted(today):
            entry = codes['entries'].get(key)
            if entry is None or key in own_codes[after]:
                continue
            if after in held_by[key] or (key in filed_after[after] and not held_by[key] - {after}):
                continue
            where = ('referencia' if entry['outcome'] in ('existe', 'convertir') else 'caso' if key in in_case or entry['outcome'] in
                     ('espera_grupo', 'caso') else 'archivo')
            reason = 'en_otra_linea' if held_by[key] - {after} else 'solo_por_enlace'
            counts['ocultos'] += 1
            counts[f'ocultos_{where}'] += 1
            counts[f'ocultos_{reason}'] += 1
            rows.append({'line': line, 'sku': snap.lines[line].sku if line in snap.lines else '', 'brand': key[0], 'code': entry['code'],
                         'goes': where, 'why': reason, 'to_line': entry['line'],
                         'to_sku': snap.lines[entry['line']].sku if entry['line'] in snap.lines else ''})
    counted = {(row['line'], (row['brand'], cc(row['code']))) for row in rows}
    for item in step0['rows']:
        key = (item['brand'], cc(item['code']))
        if not item.get('dispute') or item['line'] not in publicada or (item['line'], key) in counted:
            continue
        entry = codes['entries'].get(key) or {}
        counts['ocultos'] += 1
        counts['ocultos_caso'] += 1
        counts['ocultos_copia_en_disputa'] += 1
        rows.append({'line': item['line'], 'sku': item['sku'], 'brand': item['brand'], 'code': item['code'], 'goes': 'caso',
                     'why': 'copia_en_disputa', 'to_line': entry.get('line'),
                     'to_sku': snap.lines[entry['line']].sku if entry.get('line') in snap.lines else ''})
    counts['ocultos_lineas'] = len({row['line'] for row in rows})
    return {'rows': rows, 'counts': counts}


# --- writing --------------------------------------------------------------------------------------------------------------------

def manifest_path(name='manifest.jsonl'):
    from .refactor.output import stage_dir
    return stage_dir(STAGE) / name


def manifest_lines():
    path = manifest_path()
    if not path.exists():
        return []
    with open(path, encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def current_database():
    from django.db import connection
    return str(connection.settings_dict['NAME'])


def manifest_problem():
    from .refactor.output import main_checkout
    if not os.environ.get('REFACTOR_OUTPUT', '').strip() and not (main_checkout() / 'output').is_dir():
        return f'Falta la carpeta {main_checkout() / "output"} para el manifiesto; ejecútalo dentro del contenedor de la app.'
    others = sorted({str(line.get('database')) for line in manifest_lines()} - {current_database()})
    if others:
        return (f"El manifiesto {manifest_path()} tiene líneas de otra base ({', '.join(others)}): muévelo antes de aplicar o deshacer "
                'en esta.')
    return None


def write_manifest(lines, path=None):
    """Append the lines through a flushed temporary copy renamed over the file: all or none, inside the chunk's transaction."""
    path = path or manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    try:
        with open(temporary, 'w', encoding='utf-8') as handle:
            if path.exists():
                with open(path, encoding='utf-8') as old:
                    shutil.copyfileobj(old, handle)
            for line in lines:
                handle.write(json.dumps(line, ensure_ascii=False, default=str) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


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


def apply_merges(snap, planned, *, actor, user, chunk, log, run, stdout):
    """The automatic merges, chunk by chunk (each atomic, verified, its manifest lines written with it). A merge the database refuses
    becomes a merge case with the reason, except a supplier's import in progress (line_merge.SupplierImporting, temporary): that chunk
    rolls back and the run stops. Returns the counts."""
    from django.db import IntegrityError, transaction
    from rest_framework.exceptions import APIException
    from .line_merge import SupplierImporting, detail_text, merge_lines
    from .models import Part
    from .reference_matching import open_case
    written = Counter()
    autos = [group for group in planned['groups'] if group['route'] == 'auto']
    for start in range(0, len(autos), chunk):
        lines_log = []
        with transaction.atomic():
            for group in autos[start:start + chunk]:
                target = group['target']
                sources = sorted((set(group['lines']) | set(group['linked'])) - {target})
                library = all(snap.lines[pk].estado == 'referencia' and not snap.lines[pk].variants for pk in [target, *sources])
                try:
                    with transaction.atomic():
                        if group['main']:
                            line = Part.objects.get(pk=target)
                            before = references.main_of(line)
                            references.set_main(line, group['main'], actor=user, reason='S9: principal del grupo (3g)')
                            lines_log.append({**run, 'model': 'mall.Reference', 'action': 'set_main', 'line': target,
                                              'before': before.pk if before else None, 'after': group['main']})
                        merge = merge_lines(uuid.UUID(target), [uuid.UUID(pk) for pk in sources], actor=actor, reason=MERGE_REASON,
                                            automatic=True, library=library)
                        if merge is not None:
                            tag_merge(merge, group['id'], group['codes'], group['flags'])
                            verify_merge(target, sources)
                            lines_log.append({**run, 'hook': True, 'model': 'mall.CatalogMerge', 'id': str(merge.pk), 'action': 'create',
                                              'after': {'grupo': group['id'], 'target': target, 'sources': sources, 'reason': MERGE_REASON}})
                            written['unificaciones'] += 1
                            written['lineas_unificadas'] += len(sources)
                except SupplierImporting:
                    raise  # temporary: the whole chunk rolls back and the run stops (run() says so); a re-run picks it up
                except (APIException, IntegrityError, references.ReferenceConflict) as error:
                    lines_log = [line for line in lines_log if not (line.get('action') == 'set_main' and line.get('line') == target)]
                    spec = merge_case_for(snap, sorted({target, *sources}, key=lambda pk: keeper_rank(snap, pk)),
                                          evidence={**group_evidence(snap, group), 'refused': detail_text(error)},
                                          reason=f'La unificación automática no se pudo aplicar: {detail_text(error)}',
                                          guard=getattr(error, 'guard', '') or 'conflicto')
                    if spec is not None:
                        case = open_case(spec)
                        lines_log.append({**run, 'hook': True, 'model': 'matching.MatchingCase', 'id': str(case.pk), 'action': 'create',
                                          'after': {'key': case.key, 'grupo': group['id'], 'refused': True}})
                        written['rechazadas_a_caso'] += 1
            if lines_log:
                write_manifest(lines_log)
        log.extend(lines_log)
        stdout.write(f"  unificaciones {min(start + chunk, len(autos)):,}/{len(autos):,}\n")
    return written


def tag_merge(merge, group_id, codes, flags, rule='3d'):
    """What the pass adds to a CatalogMerge's summary (the digest's evidence; retire_citation reads its codes)."""
    if merge is None:
        return
    summary = {**(merge.summary or {}), 'merge_pass': {'grupo': group_id, 'regla': rule, 'codes': list(codes)[:50], 'flags': list(flags)}}
    type(merge).objects.filter(pk=merge.pk).update(summary=summary)
    merge.summary = summary


def verify_merge(target, sources):
    """I1–I3 on a merge's lines, inside its transaction: the target live with one main whose code is its codigo; each source unificada
    into it, holding no live Reference and no variant."""
    from .models import Part, Reference, SupplierItem
    line = Part._base_manager.get(pk=target)
    mains = list(Reference.all_objects.filter(part_id=target, is_main=True).exclude(estado='retirada'))
    if line.merged_into_id or line.estado == 'unificada' or len(mains) != 1 or references.code_key(mains[0].code) != references.code_key(line.sku):
        raise RuntimeError(f'S9: la línea {line.sku} no quedó con un principal (I1/I2).')
    bad = Part._base_manager.filter(pk__in=sources).exclude(merged_into_id=target, estado='unificada').count()
    live = Reference.all_objects.filter(part_id__in=sources).exclude(estado='retirada').count()
    items = SupplierItem.objects.filter(part_id__in=sources).count()
    if bad or live or items:
        raise RuntimeError(f'S9: la unificación en {line.sku} dejó líneas sin unificar o con referencias (I3).')


def apply_cases(snap, planned, *, log, run):
    """The merge cases: lote and caso groups, the clusters of 3e and step 0. Returns the counts."""
    from django.db import transaction
    written = Counter()
    lines_log = []
    with transaction.atomic():
        for group in planned['groups']:
            if group['route'] not in ('lote', 'caso'):
                continue
            lote = group['route'] == 'lote'
            spec = merge_case_for(snap, group['order'], evidence=group_evidence(snap, group), reason=group['reason'],
                                  guard='' if lote else group['motivo'], batch_key=BATCH_KEY if lote else '',
                                  route=group['route'], key=group_key(group) if lote else None)
            if spec is None:
                written['grupos_sin_caso'] += 1
                continue
            written[f"casos_{group['route']}"] += write_case(spec, log=lines_log, run=run, extra={'grupo': group['id']})
        for cluster in planned['clusters']['cases']:
            ordered = sorted(cluster['lines'], key=lambda pk: keeper_rank(snap, pk))
            copies = cluster['kind'] == 'copias'
            spec = merge_case_for(snap, ordered, evidence={'regla': '3e' if not copies else 'paso0', 'codes': cluster['codes'],
                                                           'codes_total': cluster['codes_total'], 'copies': cluster['copies']},
                                  reason=('Alternos copiados de un catálogo en este SKU: sus números OEM están en otra línea. Si son el mismo '
                                          'repuesto, unifícalas; si no, mantenlas separadas (la copia queda en disputa hasta que elijas su línea).'
                                          if copies else 'Un código de catálogo que un proveedor vende está en varias líneas: si son el mismo '
                                          'repuesto, unifícalas; si no, mantenlas separadas y elige la línea del código.'), guard='lineas')
            if spec is None:
                written['clusters_sin_caso'] += 1
                continue
            written['casos_codigos'] += write_case(spec, log=lines_log, run=run, extra={'regla': '3e'})
        if lines_log:
            write_manifest(lines_log)
    log.extend(lines_log)
    return written


def write_case(spec, *, log, run, extra=None):
    from .matching_models import MatchingCase
    from .reference_matching import case_key, open_case
    key = spec.get('key') or case_key(spec)
    if MatchingCase.objects.filter(key=key).exists():
        return 0
    case = open_case(spec)
    log.append({**run, 'hook': True, 'model': 'matching.MatchingCase', 'id': str(case.pk), 'action': 'create',
                'after': {'key': case.key, **(extra or {})}})
    return 1


def source_marks(snap, rows, citation):
    return sorted({mark for row in rows if citation in row.citations for mark in row.marks})


def write_code(snap, entry, rows, *, user, extra=None):
    """One code's 3e write: the Reference it becomes on its line (or the one there already), and a source per citation with its marks.
    Returns (reference, added, [source ids created])."""
    from .models import Reference, ReferenceSource
    if entry['outcome'] == 'convertir':
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


def apply_codes(snap, planned, *, user, chunk, log, run, outcomes):
    """3e and step 0, line by line in chunks: the References the codes become with their sources, the sources of codes already on
    their line, the copies that go to disputa. Returns the counts."""
    from django.db import transaction
    from .models import Reference
    written = Counter()
    rows_by_id = {row.id: row for row in snap.rows}
    by_line = defaultdict(list)
    for entry in planned['codes']['entries'].values():
        if entry['outcome'] in ('convertir', 'existe'):
            by_line[entry['line']].append(entry)
        for row_id in entry['rows']:
            outcomes.append({'row': row_id, 'brand': entry['brand'], 'number': entry['number'], 'outcome': {
                'convertir': 'converted', 'existe': 'converted', 'espera_grupo': 'case', 'caso': 'case'}.get(entry['outcome'], 'archive'),
                'detail': entry['outcome'], 'line': entry['line'], 'why': entry.get('why', '')})
    lines_log = []
    with transaction.atomic():
        for item in planned['step0']['rows']:
            if item['outcome'] != 'otra_linea' or not item.get('dispute'):
                continue
            ref = Reference.all_objects.select_for_update().filter(pk=item['reference']).first()
            if ref is None or ref.estado not in references.EXCLUSIVE or ref.is_main:
                continue
            before = {'estado': ref.estado, 'dispute_note': ref.dispute_note}
            note = f'Copiado de un catálogo en {item["sku"]}; sus números OEM están en otra línea: espera el {MERGE_DISPUTE}'
            references.update(ref, estado='disputa', dispute_note=note, actor=user)
            lines_log.append({**run, 'model': 'mall.Reference', 'id': ref.pk, 'action': 'update', 'before': before})
            written['copias_en_disputa'] += 1
        if lines_log:
            write_manifest(lines_log)
    log.extend(lines_log)
    line_ids = sorted(by_line)
    for start in range(0, len(line_ids), chunk):
        lines_log = []
        with transaction.atomic():
            for line_id in line_ids[start:start + chunk]:
                for entry in by_line[line_id]:
                    rows = [rows_by_id[pk] for pk in entry['rows'] if pk in rows_by_id]
                    ref, added, sources = write_code(snap, entry, rows, user=user)
                    if ref is None:
                        continue
                    if added:
                        lines_log.append({**run, 'model': 'mall.Reference', 'id': ref.pk, 'action': 'create', 'line': line_id})
                        written['referencias_creadas'] += 1
                        written[f"referencias_{entry['estado']}"] += 1
                    for pk in sources:
                        lines_log.append({**run, 'model': 'mall.ReferenceSource', 'id': pk, 'action': 'create'})
                    written['fuentes_creadas'] += len(sources)
            if lines_log:
                write_manifest(lines_log)
        log.extend(lines_log)
    return written


def apply_tombstones(snap, planned, *, user, log, run):
    """3f: the retired sources of the tombstones, and the inferido References left with no live source in disputa."""
    from django.db import transaction
    from django.utils import timezone
    from .models import Reference, ReferenceSource
    written = Counter()
    lines_log = []
    entries = planned['codes']['entries']
    with transaction.atomic():
        for item in planned['tombstones']['rows']:
            ref = None
            if item['reference']:
                ref = Reference.all_objects.filter(pk=item['reference']).first()
            if ref is None:
                brand, number = item['code_key']
                ref = (Reference.all_objects.filter(part_id=item['line'], clave=references.clave(references.code_key(entries[(brand, number)]['code'])),
                                                    brand=brand, estado__in=references.LIVE).first() if (brand, number) in entries else None)
            if ref is None:
                continue
            for citation in item['citations']:
                source, created = ReferenceSource.objects.get_or_create(
                    reference=ref, kind='admin' if citation.startswith(MANUAL) else 'catalogo', citation=citation[:500],
                    defaults={'retirada': True, 'retired_by': user, 'retired_at': timezone.now(),
                              'detail': {TAG: {'regla': '3f', 'tombstone': item['tombstone']}}})
                if created:
                    lines_log.append({**run, 'model': 'mall.ReferenceSource', 'id': source.pk, 'action': 'create'})
                    written['fuentes_retiradas'] += 1
            if item['dispute'] and ref.estado == 'inferido' and not ref.sources.filter(retirada=False).exists() and not ref.is_main:
                before = {'estado': ref.estado, 'dispute_note': ref.dispute_note}
                references.update(ref, estado='disputa', dispute_note='Su catálogo retiró la equivalencia en la que se basaba.', actor=user)
                lines_log.append({**run, 'model': 'mall.Reference', 'id': ref.pk, 'action': 'update', 'before': before})
                written['inferidas_a_disputa'] += 1
        if lines_log:
            write_manifest(lines_log)
    log.extend(lines_log)
    return written


def undo(*, log, run, stdout):
    """--rollback: newest first, the sources it created are deleted, the References it created retired and the ones it changed given
    their estado back, the mains it set given back; then each merge is split (Separar) and each case still in review deleted
    (mall.line_merge.s2b_rollback, on the hook lines). Meant for the same freeze as the apply: once the matching worker has run, a
    Reference of this pass that variants hang from cannot be retired; it is counted (referencias_con_variantes) and listed with its
    line, and stays there."""
    from django.db import transaction
    from .line_merge import detail_text, s2b_rollback
    from .models import Part, Reference, ReferenceSource
    entries = manifest_lines()
    result = Counter()
    with transaction.atomic():
        for line in reversed(entries):
            model, action = line.get('model'), line.get('action')
            if model == 'mall.ReferenceSource' and action == 'create':
                result['fuentes_borradas'] += ReferenceSource.objects.filter(pk=line['id']).delete()[0]
            elif model == 'mall.Reference' and action == 'create':
                ref = Reference.all_objects.filter(pk=line['id']).exclude(estado='retirada').first()
                if ref is not None and not ref.is_main:
                    try:
                        references.retire(ref, reason='Rollback S9', keep_library=False)
                        result['referencias_retiradas'] += 1
                    except references.ReferenceConflict as error:
                        # Variants hang from it: the matching worker ran after the apply (roll back while still frozen, plan §3 S9).
                        # It stays on its line, listed here, for a person to move with its variants or retire.
                        hung = isinstance(error, references.ReferenceHasVariants)
                        result['referencias_con_variantes' if hung else 'referencias_en_conflicto'] += 1
                        stdout.write(f'  referencia {ref.pk} ({ref.brand} {ref.code}, línea {ref.part_id}) queda: {detail_text(error)}\n')
            elif model == 'mall.Reference' and action == 'update':
                ref = Reference.all_objects.filter(pk=line['id']).first()
                if ref is not None and ref.estado != 'retirada':
                    references.update(ref, **line['before'])
                    result['referencias_devueltas'] += 1
        hooks = [line for line in entries if line.get('hook')]
        outcome = s2b_rollback(log=log, run=run, lines=hooks)
        result['unificaciones_separadas'] = outcome['split']
        result['casos_borrados'] = outcome['cases']
        for text in outcome['changed']:
            stdout.write(f'  {text}\n')
        for line in reversed(entries):
            if line.get('model') == 'mall.Reference' and line.get('action') == 'set_main' and line.get('before'):
                part = Part.objects.filter(pk=line['line']).first()
                if part is not None and not part.merged_into_id:
                    try:
                        references.set_main(part, line['before'], reason='Rollback S9')
                        result['principales_devueltos'] += 1
                    except references.ReferenceConflict as error:
                        stdout.write(f'  principal de {part.sku}: {error.message}\n')
        log.append({**run, 'model': 'manifest', 'action': 'rolled_back', 'rows': len(entries)})
    return dict(result)


# --- later changes (3f): imports, the Biblioteca, catalog replaces ---------------------------------------------------------------------

def line_scope(line_ids):
    """The scope (load) of 3e for these lines: their numbers and every aftermarket code filed under them."""
    from .models import Reference
    from .oem_reference_models import OEMCrossReference
    line_ids = {str(pk) for pk in line_ids if pk}
    numbers = list(Reference.objects.filter(references.library_q(), part_id__in=line_ids).values_list('pk', flat=True))
    codes = set(OEMCrossReference.objects.filter(reference_id__in=numbers).values_list('number', flat=True))
    return {'lines': line_ids, 'numbers': codes}


def settle_line(line_ids, *, actor=None, extra=None):
    """3e for some lines, from S9 on (a merge a person approved, a 3f merge, an import, a code added by hand): each aftermarket code
    filed under their numbers that sits on one of them only (no other line's number files it, no other line holds it) becomes its
    Reference, with its sources; a code already its Reference gains the sources. Returns how many References were added."""
    from django.db import transaction
    line_ids = {str(getattr(pk, 'pk', pk)) for pk in ([line_ids] if not isinstance(line_ids, (list, tuple, set)) else line_ids) if pk}
    if not line_ids:
        return 0
    snap = load(scope=line_scope(line_ids))
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


def lines_of_code(brand, code):
    """{line id: [library Reference ids]} of the live lines whose numbers file this code (brand, any spelling), and the lines that
    hold it as an exclusive Reference (with no number), after following merges."""
    from .brands import resolve
    from .lines import canonical_ids
    from .models import Reference
    from .oem_reference_models import OEMCrossReference
    name = resolve(brand)
    found = defaultdict(list)
    for ref_id, part_id, row_brand in OEMCrossReference.objects.filter(number=cc(code)).values_list('reference_id', 'reference__part_id', 'brand'):
        if resolve(row_brand) == name:
            found[str(part_id)].append(ref_id)
    key = references.clave(references.code_key(code))
    for part_id in Reference.all_objects.filter(clave=key, brand__in=[name, ''], supplier__isnull=True, estado__in=references.EXCLUSIVE
                                                ).values_list('part_id', flat=True):
        found.setdefault(str(part_id), [])
    canonical = canonical_ids(found)
    out = defaultdict(list)
    for pk, refs in found.items():
        target = canonical.get(pk, pk)
        out[target] += refs
    from .lines import LIVE, catalog_lines
    live = {str(pk) for pk in catalog_lines(tuple(estado for estado in LIVE if estado != 'rechazada')).filter(pk__in=list(out))
            .values_list('pk', flat=True)}
    return {pk: refs for pk, refs in out.items() if pk in live}


def quiet(snap, line_ids):
    """Whether every line is in referencia without variants (3f: only those merge on their own after S9)."""
    return all(pk in snap.lines and snap.lines[pk].estado == 'referencia' and not snap.lines[pk].variants for pk in line_ids)


def lines_case(snap, line_ids, *, evidence, reason):
    """The merge case for lines a code a person typed puts together (3f from the Biblioteca: it writes References, merge cases and
    Source retirements, never a merge; rule 3b: a hand-typed or competitor code joins no lines on its own), the keeper by 3g first and
    the failing guard on it. Returns ('case', case), or ('apart', None) when the lines are kept apart or a case already asks about them."""
    from .reference_matching import open_case
    line_ids = sorted(set(line_ids))
    if kept_apart(snap, line_ids) or covered(snap, line_ids):
        return 'apart', None
    info = evaluate(snap, line_ids, {}, {})
    order = sorted(line_ids, key=lambda pk: keeper_rank(snap, pk))
    spec = merge_case_for(snap, order, evidence={**evidence, 'flags': info['flags']}, reason=reason,
                          guard=info['guard'] or 'lineas', route='caso')
    if spec is None:
        return 'apart', None
    return 'case', open_case(spec)


def code_added(number, brand, code, citation, *, actor=None):
    """3e/3f for an aftermarket code a person added by hand to a library number (the Biblioteca's POST): on one line, it becomes that
    line's Reference (declarado, with an admin source); on two or more, a merge case lists the lines and the code waits there (never a
    merge on its own: rule 3b). Returns {'reference': id or None, 'case': id or None}."""
    from django.db import transaction
    from .brands import resolve
    name = resolve(brand)
    out = {'reference': None, 'case': None}
    with transaction.atomic():
        lines = lines_of_code(name, code)
        if not lines:
            return out
        if len(lines) > 1:
            snap = load(scope={'lines': set(lines), 'numbers': {cc(code)}})
            reason = (f'{name} {code}, registrado a mano, está en varias líneas: si son el mismo repuesto, unifícalas; si no, mantenlas '
                      'separadas y elige la línea del código.')
            outcome, value = lines_case(snap, list(lines), reason=reason,
                                        evidence={'regla': '3f', 'codes': [{'brand': name, 'code': code, 'manual': True}]})
            if outcome == 'case':
                out['case'] = str(value.pk)
            return out
        target = next(iter(lines))
        try:
            ref, _ = references.add(target, code, name, tipo='aftermarket', estado='declarado', actor=actor, absorb_library=False,
                                    detail={TAG: {'regla': '3e', 'manual': True}})
        except references.ReferenceConflict:
            return out
        references.cite(ref, citation, actor, kind='admin')
        out['reference'] = ref.pk
    return out


def join_numbers(ref_ids, *, actor=None, reason=''):
    """3f for a later catalog import: the groups the numbers `ref_ids` now belong to. A group whose lines are all in referencia without
    variants (and that passes the guards) merges on its own (merge_lines, library); any other gets a merge case (the code waits there),
    unless a case already asks about those lines. Then the codes now on one line become References (3e, settle_line). Returns
    {'merged', 'cases', 'references'}."""
    from django.db import transaction
    from .line_merge import merge_lines, service_actor
    from .models import Part
    from .reference_matching import open_case
    ref_ids = {int(pk) for pk in ref_ids if pk}
    out = Counter()
    if not ref_ids:
        return dict(out)
    snap = load()
    lines = {snap.numbers[pk].line for pk in ref_ids if pk in snap.numbers}
    if not lines:
        return dict(out)
    planned = plan(snap)
    actor = actor or service_actor()
    touched = set(lines)
    with transaction.atomic():
        for group in planned['groups']:
            members = set(group['lines']) | set(group['linked'])
            if not members & lines or group['route'] in ('separadas', 'cubierto'):
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
                open_case(spec)
                out['cases'] += 1
        from .lines import canonical_ids
        targets = set(canonical_ids(touched).values())
        out['references'] += settle_line(targets, actor=actor, extra={'regla_3f': True})
    return dict(out)


def retire_citation(ref, brand, code, citations, *, actor=None, exclude_row=None):
    """3f, when a code is retired from a number (the Biblioteca's DELETE, a catalog replace): the catalogo sources these citations gave
    the Reference the code is on the number's line become retired; an inferido Reference left with no live source goes to disputa, and
    so does a declarado one this pass made (3e from a catalog's own code, or a code added by hand in the Biblioteca); and
    when the code was one of the catalog's own codes behind an automatic merge of this pass, a merge case lists that merge's lines
    (Unificar keeps it, Mantener separadas takes it apart). A citation another row of the code still gives on that line (the same page
    filed under another of its numbers) stays live; exclude_row is the row being retired (still stored). Returns {'sources', 'disputed',
    'case'}."""
    from django.db import transaction
    from django.utils import timezone
    from .brands import resolve
    from .models import CatalogMerge, Reference, ReferenceSource
    from .reference_matching import open_case
    out = {'sources': 0, 'disputed': 0, 'case': None}
    name = resolve(brand) if brand else ''
    key = references.clave(references.code_key(code))
    with transaction.atomic():
        held = (Reference.all_objects.select_for_update().filter(part_id=ref.part_id, clave=key, brand=name, supplier__isnull=True,
                                                                 estado__in=references.LIVE).order_by('pk').first())
        if held is not None:
            from .oem_reference_models import OEMCrossReference
            now = timezone.now()
            still = set()
            for row_id, row_brand, row_citations in (OEMCrossReference.objects.filter(number=cc(code), reference__part_id=ref.part_id)
                                                     .exclude(pk=getattr(exclude_row, 'pk', exclude_row)).values_list('pk', 'brand', 'citations')):
                if resolve(row_brand) == name:
                    still.update(row_citations or [])
            for citation in [citation for citation in citations or [] if citation not in still]:
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
            merge = next((merge for merge in CatalogMerge.objects.filter(target_id=ref.part_id, summary__reason=MERGE_REASON).order_by('-created_at')
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
    """A merge case of this pass decided (mall.reference_matching.decide_merge). Merged: the codes now on the merged line only become
    its References (3e, settle_line), and the step 0 copies there were settled by the merge (their note ends "caso de unificación").
    Kept apart with a line chosen: the case's codes become References of that line only (a copy in disputa there is declarado again; a
    Reference this pass placed on another of the case's lines and the copies elsewhere leave them: retired, unless variants hang from
    them). Kept apart with no line (a step 0 case, from a screen that sends none): each copy leaves disputa, retired when another line
    holds its code (the code stays with its OEM numbers), declarado again on its SKU otherwise; no copy waits hidden with no way out.
    Returns how many References were added."""
    evidence = case.evidence or {}
    if evidence.get('regla') not in S9_RULES:
        return 0
    if action in ('approve', 'merge'):
        return settle_line([target], actor=actor, extra={'caso': str(case.pk)}) if target else 0
    if not line:
        if evidence.get('regla') == 'paso0':
            release_copies(case, actor=actor)
        return 0
    return place_case_codes(case, str(getattr(line, 'pk', line)), actor=actor)


def exclusive_holders(code, brand, exclude_line=None):
    """The live exclusive public References that hold a branded code (its brand, or none), on lines that are not unificada."""
    from .models import Reference
    rows = Reference.all_objects.filter(clave=references.clave(references.code_key(code)), brand__in=[brand, ''], supplier__isnull=True,
                                        estado__in=references.EXCLUSIVE, part__merged_into__isnull=True).exclude(part__estado='unificada')
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

ROWS = (('grupos', 'grupos (3b)'), ('numeros_en_grupos', 'números en grupos'),
        ('grupos_auto', 'automáticos'), ('numeros_auto', '  números'), ('auto_sin_sku', '  sin SKU'), ('auto_un_sku', '  en un SKU'),
        ('grupos_lote', 'en lote'), ('numeros_lote', '  números'), ('grupos_caso', 'casos (control)'), ('numeros_caso', '  números'),
        ('grupos_cubierto', 'ya en un caso'), ('grupos_separadas', 'líneas separadas'),
        ('refs_propias', 'referencias propias (3e)'), ('refs_competidor', 'referencias de competidor (3e)'),
        ('refs_manuales', 'referencias registradas a mano'), ('codigos_ya_referencia', 'códigos ya en su línea'),
        ('codigos_varias_lineas', 'códigos en 2+ líneas'), ('codigos_vendidos_varias_lineas', '  vendidos (OD15)'),
        ('codigos_esperan_su_grupo', '  esperan el caso de su grupo'), ('clusters', 'casos por códigos (clusters)'),
        ('paso0_copias', 'paso 0: copias de catálogo'), ('paso0_otra_linea', '  en otra línea (caso + disputa)'),
        ('lapidas_fuente_retirada', '3f: fuentes retiradas'), ('ocultos', 'códigos de tienda que dejan de verse'))


def print_summary(stdout, mode, planned, written=None):
    title = {'apply': 'Aplicado', 'dry_run': 'Simulación'}[mode]
    counts = planned['counts']
    stdout.write(f'{title} · S9 unificación por catálogo (3b–3g)\n')
    stdout.write(f"  {'':44} {'prototipo':>10} {'resultado':>10}\n")
    for key, label in ROWS:
        expected = EXPECTED.get(key)
        stdout.write(f"  {label:44} {'' if expected is None else f'{expected:,}':>10} {counts.get(key, 0):>10,}\n")
    for label, values in planned['projections'].items():
        stdout.write(f'  alcance {label}: ' + ' · '.join(f'{k} {v:,}' for k, v in sorted(values.items())) + '\n')
    if written:
        stdout.write('  Escrito: ' + ' · '.join(f'{key} {value:,}' for key, value in sorted(written.items())) + '\n')


def write_report(folder, planned, snap, *, mode, written=None):
    """The report files (see the module's docstring)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    def sku(pk):
        return snap.lines[pk].sku if pk in snap.lines else pk

    def dump(name, data):
        with open(folder / name, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1, default=str)
    groups = []
    for group in planned['groups']:
        groups.append({**{key: value for key, value in group.items() if key not in ('order',)},
                       'line_skus': [sku(pk) for pk in group['lines']], 'linked_skus': [sku(pk) for pk in group['linked']],
                       'target_sku': sku(group['target']) if group['target'] else None,
                       'keeper_sku': sku(group['order'][0]) if group.get('order') else None,
                       'numbers_list': [f'{num.brand} {num.code}' for pk in group['lines'] for num in snap.lines[pk].numbers][:60]})
    digest = [{'grupo': group['id'], 'target': group['target'], 'target_sku': group['target_sku'],
               'target_estado': snap.lines[group['target']].estado, 'main': group.get('main_code') if group['main'] else None,
               'sources': [sku(pk) for pk in sorted((set(group['lines']) | set(group['linked'])) - {group['target']})],
               'numbers': group['numbers_list'], 'codes': group['codes'][:30], 'flags': group['flags']}
              for group in groups if group['route'] == 'auto']
    cases = ([{'kind': 'grupo', 'route': group['route'], 'guard': group['motivo'], 'keeper_sku': group['keeper_sku'],
               'line_skus': group['line_skus'] + group['linked_skus'], 'reason': group['reason'], 'flags': group['flags']}
              for group in groups if group['route'] in ('lote', 'caso')]
             + [{'kind': 'codigos', 'route': 'caso', 'guard': 'lineas', 'line_skus': [sku(pk) for pk in case['lines']],
                 'codes': case['codes'][:50], 'codes_total': case['codes_total'], 'copies': case['copies']}
                for case in planned['clusters']['cases']])
    codes = [{key: value for key, value in entry.items() if key != 'rows'} | {'rows': len(entry['rows'])}
             for entry in planned['codes']['entries'].values()]
    summary = {'mode': mode, 'counts': dict(sorted(planned['counts'].items())), 'prototype': EXPECTED, 'projections': planned['projections'],
               'written': written or {}}
    dump('summary.json', summary)
    dump('groups.json', groups)
    dump('digest.json', digest)
    dump('cases.json', cases)
    dump('codes.json', codes)
    dump('hidden.json', planned['hidden']['rows'])
    dump('step0.json', planned['step0']['rows'])
    dump('tombstones.json', planned['tombstones']['rows'])
    return folder


def imports_running():
    """The Excel and catalog imports still applying (status importing, not expired): a merge into their suppliers' lines would be
    refused (line_merge.SupplierImporting), so --apply waits for them (plan D0)."""
    from django.utils import timezone
    from .models import CatalogImportJob
    from .supplier_import_models import SupplierInventoryImportJob
    now = timezone.now()
    return [f'{model.__name__} {pk}' for model in (CatalogImportJob, SupplierInventoryImportJob)
            for pk in model.objects.filter(status='importing', expires_at__gt=now).values_list('pk', flat=True)[:5]]


def read_only():
    """A dry run reads in a READ ONLY transaction: nothing it calls can write."""
    from django.db import connection
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')


def run(*, apply=False, rollback=False, username=None, report=None, chunk=CHUNK, stdout=None, marks_path=MARKS_FILE, positions_folder=None):
    """Plan, and with apply write it, or with rollback undo the applies on this database. None when a write is refused."""
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.utils import timezone
    from .line_merge import SupplierImporting, detail_text, service_actor
    from .matching_queue import matching_lock, matching_work
    stdout = stdout or sys.stdout
    user = None
    if apply or rollback:
        from .refactor.guard import refusal
        reason = refusal(True) or manifest_problem()
        if reason:
            stdout.write(reason + '\n')
            return None
        user = get_user_model().objects.filter(username=username or '').first()
        if apply and user is None:
            stdout.write('Indica con --user el usuario que figura como autor del cambio.\n' if not username else f'No existe el usuario {username}.\n')
            return None
        busy = imports_running()
        if busy:
            stdout.write(f"Hay importaciones a medias ({', '.join(busy)}): espera a que terminen o venzan y vuelve a ejecutar.\n")
            return None
    run_line = {'stage': 's9_merge_pass', 'database': current_database(), 'run': str(uuid.uuid4()), 'actor': user.username if user else '',
                'at': timezone.now().isoformat()}
    if not (apply or rollback):
        with transaction.atomic():
            read_only()
            snap = load(marks_path=marks_path, positions_folder=positions_folder)
            planned = plan(snap)
            print_summary(stdout, 'dry_run', planned)
            if report:
                stdout.write(f'Informe: {write_report(report, planned, snap, mode="dry_run")}\n')
            transaction.set_rollback(True)
        return {'mode': 'dry_run', 'counts': dict(planned['counts']), 'planned': planned, 'snapshot': snap}

    with matching_lock() as acquired, matching_work():
        if not acquired:
            stdout.write('El análisis de coincidencias está en curso; vuelve a intentarlo cuando termine.\n')
            return None
        log = []
        try:
            if rollback:
                result = undo(log=log, run=run_line, stdout=stdout)
                write_manifest([{**run_line, 'action': 'run', 'mode': 'rollback', 'rows': len(log)}, *log])
                stdout.write('Rollback · S9: ' + ' · '.join(f'{key} {value:,}' for key, value in sorted(result.items())) + '\n')
                return {'mode': 'rollback', **result}
            actor = service_actor()
            snap = first_snap = load(marks_path=marks_path, positions_folder=positions_folder)
            first = plan(snap)
            print_summary(stdout, 'dry_run', first)
            written = Counter()
            written.update(apply_merges(snap, first, actor=actor, user=user, chunk=chunk, log=log, run=run_line, stdout=stdout))
            snap = load(marks_path=marks_path, positions_folder=positions_folder)  # the catalog as the merges left it
            planned = plan(snap, after_merges=True)
            if planned['merged']:
                raise RuntimeError('S9: el segundo plan no debe proponer unificaciones automáticas.')
            written.update(apply_cases(snap, planned, log=log, run=run_line))
            outcomes = []
            written.update(apply_codes(snap, planned, user=user, chunk=chunk, log=log, run=run_line, outcomes=outcomes))
            written.update(apply_tombstones(snap, planned, user=user, log=log, run=run_line))
            write_manifest([{**run_line, **row} for row in outcomes], path=manifest_path('xref_outcomes.jsonl'))
            write_manifest([{**run_line, 'action': 'run', 'mode': 'apply', 'rows': len(log)}])
            print_summary(stdout, 'apply', planned, written)
            if report:
                stdout.write(f'Informe: {write_report(report, first, first_snap, mode="apply", written=dict(written))}\n')
            return {'mode': 'apply', 'counts': dict(first['counts']), 'written': dict(written)}
        except OSError as error:
            stdout.write(f'No se pudo escribir el manifiesto ({error}): el trozo en curso no se aplicó.\n')
            return None
        except SupplierImporting as error:
            stdout.write(f'{detail_text(error)} El trozo en curso no se aplicó; los anteriores quedan en el manifiesto. Vuelve a ejecutar '
                         '--apply cuando termine: sigue donde se quedó.\n')
            return None


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='S9: unificación por catálogo (reglas 3b–3g) y alternos de posventa como referencias (3e).')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Calcula todo sin escribir nada (transacción de solo lectura).')
    mode.add_argument('--apply', action='store_true', help='Aplica por trozos (en vivo requiere el catálogo congelado).')
    mode.add_argument('--rollback', action='store_true', help='Deshace con el manifiesto lo aplicado en esta base.')
    parser.add_argument('--user', help='Usuario que figura como autor (obligatorio con --apply).')
    parser.add_argument('--report', help='Carpeta del informe (summary.json, groups.json, digest.json, cases.json, codes.json, hidden.json...).')
    parser.add_argument('--chunk', type=int, default=CHUNK, help=f'Grupos (o líneas) por trozo atómico; {CHUNK} por omisión.')
    parser.add_argument('--json', action='store_true', help='Imprime también las cuentas en JSON.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .refactor.guard import require_freeze
    require_freeze(options.apply or options.rollback)
    stdout = stdout or sys.stdout
    result = run(apply=options.apply, rollback=options.rollback, username=options.user, report=options.report,
                 chunk=max(1, options.chunk), stdout=stdout)
    if result is not None and options.json:
        stdout.write(json.dumps({key: value for key, value in result.items() if key not in ('planned', 'snapshot')}, ensure_ascii=False,
                                default=str) + '\n')
    return 0 if result is not None else 1


if __name__ == '__main__':
    sys.exit(main())
