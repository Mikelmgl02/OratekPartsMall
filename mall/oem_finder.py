"""Deterministic OEM finder (phase 2), ported from the validated v2 prototype (dry run of 2026-10-06 over 31,637 SKUs).

OEMFinder is pure computation over a read-only Snapshot (Part, PartCode and SupplierItem rows) and the admin suffix table
(mall.catalog_suffixes): OEM format systems and aftermarket patterns, makes and curated model hints, a head/qualifier class
lexicon built per run in memory, evaluate_key, decide, pass-3 claimants and the auto gates. It never renames a Part, never
writes a PartCode and never calls an AI provider. execute() records an OEMFinderRun and syncs the review tiers into
OEMReviewCase; read-only mode writes nothing and works before the tables are migrated. Applying the AUTO tiers (phase 2B) lives
in mall.oem_apply; this module only grades.

Run: python -m mall.oem_finder --dry-run [--in-stock-first] [--limit N] [--tier T] [--read-only] [--json PATH]
     python -m mall.oem_finder --apply-auto [--tier AUTO_FLAG_CURRENT|AUTO_RENAME_BASE|STRONG_PENDING_OWNER_TAGS] [--limit N]
                               [--canary N] [--read-only] [--spot-check-dir DIR]
     python -m mall.oem_finder --halt MOTIVO | --resume        (undo: python -m mall.oem_finder_revert --run ID [--part UUID])
It takes the matching worker's PostgreSQL advisory lock, so it never overlaps a matching pass, and like a pass it waits out a
catalog import (exit 2). Before migration 0035 only --read-only runs (exit 1 otherwise); applying also needs 0036 and, per tier,
a completed canary (exit 1), and refuses while the halt rule is active (exit 3).
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager

from .catalog_families import normalized_reference
from .catalog_suffixes import (ALPHA_RE, chain_summary, classify_chain, clean_code, description_head, generic_base,
                               shape, strip_lifecycle, tokenize_chain, up)

OEM_FINDER_VERSION = 'oem-finder-2'  # rules version; independent of the matching VERSION


def norm_key(s):
    return normalized_reference(up(s))


def words(s):
    return re.findall(r'[A-Z0-9]+', up(s))


# =============================================================== description heads and sides
def head_str(t):
    return t[0] + ('|' + t[1] if t[1] else '')


def head_parse(s):
    h, _, q = s.partition('|')
    return (h, q)


def head_compat(a, b):
    """(head, qualifier) pairs: same head after the abbreviation map; qualifiers must match when both exist."""
    if not a[0] or not b[0] or a[0] != b[0]:
        return False
    return not a[1] or not b[1] or a[1] == b[1]


R_TOK = {'RH', 'DER', 'DERECHO', 'DERECHA', 'DERECHOS', 'RIGHT'}
L_TOK = {'LH', 'IZQ', 'IZQUIERDO', 'IZQUIERDA', 'IZQUIERDOS', 'LEFT'}
BOTH_TOK = {'LH/RH', 'RH/LH', 'L/R', 'R/L', 'PAR'}


def side_of(desc, chain_tokens=()):
    toks = set(re.findall(r'[A-Z/]+', up(desc)))
    r = bool(toks & R_TOK) or any(t in ('RH', 'R', 'DER') for t in chain_tokens)
    l = bool(toks & L_TOK) or any(t in ('LH', 'L', 'IZQ') for t in chain_tokens)
    if toks & BOTH_TOK or (r and l):
        return 'B'
    return 'R' if r else ('L' if l else '')


def sides_clash(a, b):
    return a in ('R', 'L') and b in ('R', 'L') and a != b


# =============================================================== makes
MAKE_TOKENS = {
    'TOY': 'TOYOTA', 'TOYOTA': 'TOYOTA', 'TOYTA': 'TOYOTA', 'TOYO': 'TOYOTA', 'LEX': 'LEXUS', 'LEXUS': 'LEXUS',
    'SCION': 'SCION', 'DAI': 'DAIHATSU', 'DAIH': 'DAIHATSU', 'DAIHA': 'DAIHATSU', 'DAIHATSU': 'DAIHATSU',
    'DAITHAT': 'DAIHATSU', 'HINO': 'HINO',
    'NIS': 'NISSAN', 'NISS': 'NISSAN', 'NISSAN': 'NISSAN', 'DATSUN': 'NISSAN', 'INFINITI': 'INFINITI', 'INFINITY': 'INFINITI',
    'HYU': 'HYUNDAI', 'HYUN': 'HYUNDAI', 'HYUNDAI': 'HYUNDAI', 'HYUDAI': 'HYUNDAI', 'KIA': 'KIA',
    'HON': 'HONDA', 'HONDA': 'HONDA', 'ACURA': 'ACURA',
    'MAZ': 'MAZDA', 'MAZDA': 'MAZDA',
    'MIT': 'MITSUBISHI', 'MITS': 'MITSUBISHI', 'MITSU': 'MITSUBISHI', 'MITSUBISHI': 'MITSUBISHI', 'MIST': 'MITSUBISHI',
    'FUSO': 'MITSUBISHI',
    'ISU': 'ISUZU', 'ISUZ': 'ISUZU', 'ISUZU': 'ISUZU',
    'SUZ': 'SUZUKI', 'SUZUKI': 'SUZUKI', 'MARUTI': 'SUZUKI',
    'CHEV': 'CHEVROLET', 'CHE': 'CHEVROLET', 'CHEX': 'CHEVROLET', 'CHEVROLET': 'CHEVROLET', 'CHEVY': 'CHEVROLET',
    'GMC': 'CHEVROLET', 'PONTIAC': 'CHEVROLET', 'CADILLAC': 'CHEVROLET', 'BUICK': 'CHEVROLET', 'SATURN': 'CHEVROLET',
    'DAE': 'DAEWOO', 'DAEWOO': 'DAEWOO', 'FORD': 'FORD', 'LINCOLN': 'FORD',
    'JEEP': 'MOPAR', 'DODGE': 'MOPAR', 'CHRYSLER': 'MOPAR', 'RAM': 'MOPAR',
    'VW': 'VAG', 'VOLKSWAGEN': 'VAG', 'VOLK': 'VAG', 'VOLKWAGEN': 'VAG', 'AUDI': 'VAG', 'SKODA': 'VAG',
    'SUBARU': 'SUBARU', 'MERCEDES': 'MB', 'BENZ': 'MB', 'BMW': 'BMW',
    'CHERY': 'CN', 'CHERRY': 'CN', 'GEELY': 'CN', 'JAC': 'CN', 'JMC': 'CN', 'CHANGAN': 'CN', 'CHANG': 'CN',
    'HAVAL': 'CN', 'BYD': 'CN', 'FOTON': 'CN', 'DFSK': 'CN', 'BAIC': 'CN', 'DONGFENG': 'CN', 'MAXUS': 'CN',
    'GWM': 'CN', 'GAC': 'CN', 'WULING': 'CN', 'WULIN': 'CN', 'JETOUR': 'CN',
    'PEUGEOT': 'OTHER', 'RENAULT': 'OTHER', 'FIAT': 'OTHER', 'MAHINDRA': 'OTHER', 'OPEL': 'OTHER', 'ROVER': 'OTHER',
    'INTERNATIONAL': 'OTHER', 'INTERNACIONAL': 'OTHER', 'FREIGHTLINER': 'OTHER', 'VOLVO': 'OTHER', 'SSANG': 'OTHER',
    'SSANGYONG': 'OTHER',
}
# INF = inferior and DEL = delantero: deliberately NOT make tokens.
MAKE_GROUP = {'TOYOTA': 'TOYOTA', 'LEXUS': 'TOYOTA', 'SCION': 'TOYOTA', 'DAIHATSU': 'TOYOTA', 'HINO': 'HINO',
              'NISSAN': 'NISSAN', 'INFINITI': 'NISSAN', 'HYUNDAI': 'HMG', 'KIA': 'HMG', 'MITSUBISHI': 'MITSUBISHI',
              'HONDA': 'HONDA', 'ACURA': 'HONDA', 'SUZUKI': 'SUZUKI', 'MAZDA': 'MAZDA', 'FORD': 'FORD',
              'CHEVROLET': 'GM', 'DAEWOO': 'GM', 'ISUZU': 'ISUZU', 'MOPAR': 'MOPAR', 'VAG': 'VAG',
              'SUBARU': 'SUBARU', 'MB': 'MB', 'BMW': 'BMW', 'CN': 'CN', 'OTHER': 'OTHER'}
GROUP_MAKES = {'TOYOTA': ['TOYOTA'], 'HMG': ['HYUNDAI', 'KIA'], 'GM': ['CHEVROLET'], 'NISSAN': ['NISSAN']}
# make -> (own OEM systems, platform-partner systems)
MAKE_SYSTEMS = {
    'TOYOTA': (['TOYOTA'], []), 'LEXUS': (['TOYOTA'], []), 'SCION': (['TOYOTA'], []), 'DAIHATSU': (['TOYOTA'], []),
    'HINO': (['TOYOTA', 'HINO54'], []), 'NISSAN': (['NISSAN'], []), 'INFINITI': (['NISSAN'], []),
    'HYUNDAI': (['HMG'], ['MITSU_OLD', 'MITSU_NEW']), 'KIA': (['HMG', 'KIA_LEGACY'], ['MAZDA']),
    'MITSUBISHI': (['MITSU_OLD', 'MITSU_NEW'], ['HMG']), 'HONDA': (['HONDA'], []), 'ACURA': (['HONDA'], []),
    'SUZUKI': (['SUZUKI'], []), 'MAZDA': (['MAZDA'], ['FORD']), 'FORD': (['FORD'], ['MAZDA']),
    'CHEVROLET': (['GM'], ['SUZUKI', 'ISUZU']), 'DAEWOO': (['GM'], ['SUZUKI']), 'ISUZU': (['ISUZU'], []),
    'MOPAR': (['MOPAR'], []), 'VAG': (['VAG'], []), 'SUBARU': (['SUBARU'], []), 'MB': (['MB'], []),
    'BMW': (['BMW'], []), 'CN': (['CN'], []), 'OTHER': ([], []),
}
SYSTEM_BRAND = {'TOYOTA': 'TOYOTA', 'NISSAN': 'NISSAN', 'HMG': 'HYUNDAI/KIA', 'KIA_LEGACY': 'KIA', 'HONDA': 'HONDA',
                'MITSU_OLD': 'MITSUBISHI', 'MITSU_NEW': 'MITSUBISHI', 'SUZUKI': 'SUZUKI', 'MAZDA': 'MAZDA',
                'ISUZU': 'ISUZU', 'GM': 'GM', 'FORD': 'FORD', 'SUBARU': 'SUBARU', 'VAG': 'VAG', 'MB': 'MERCEDES-BENZ',
                'BMW': 'BMW', 'MOPAR': 'MOPAR', 'CN': 'CN', 'HINO54': 'HINO'}
EXCLUSIVE_SYSTEM_MAKE = {'HONDA': 'HONDA', 'MITSU_OLD': 'MITSUBISHI', 'MITSU_NEW': 'MITSUBISHI', 'ISUZU': 'ISUZU',
                         'NISSAN': 'NISSAN', 'MAZDA': 'MAZDA', 'KIA_LEGACY': 'KIA', 'SUZUKI': 'SUZUKI', 'FORD': 'FORD', 'MB': 'MB'}
KIA_MODELS = set('PICANTO RIO SPORTAGE CERATO SORENTO SOUL OPTIMA CARNIVAL FORTE SELTOS SONET SEPHIA PRIDE BONGO '
                 'K2500 K2700 K3000 STINGER MOHAVE NIRO CARENS SOLUTO PREGIO BESTA'.split())
HINT_STOP = set('TRAS DEL SUP INF RH LH DER IZQ KIT JUEGO BASE TERM AMORT FILTRO CORREA SENSOR MOTOR BOMBA '
                'CABLE DISCO TACO TIJERA BUJE EMP ACEITE AIRE AGUA RADIADOR TODOS ALL MOD NEW OLD AUT ATM MEC '
                '4X4 4X2 4WD 2WD DIESEL GAS GASOLINA TURBO STD UNIV UNIVERSAL CON SIN PARA'.split())
# mined hints that are not model or engine names ('RACING'/'TECHO' would make a roof rack a Toyota)
HINT_DROP = set('LAND ADVANCE BRAZIL WAO TECHO FIJA ACTIVE MOBIS ESTABILIZADOR A01 VOLT MEX MAMON RACER INFINITY '
                'DINA RACING PARRILLA NEGRO ROJO AZUL CROMADO GENUINO ORIGINAL'.split())

# =============================================================== OEM formats
B = r'(?=$|[-/._+( ])'
T5 = r'(?=[0-9A-Z]{0,4}\d)[0-9A-Z]{5}'
SYSTEM_DEFS = [
    ('TOYOTA', r'(?P<cls>\d{5})(?P<s1>-?)(?P<tail>' + T5 + r')(?:-(?P<var>0\d|[A-HJ-NP-Z]\d|[A-F]O))?', 'cls', True),
    ('NISSAN', r'(?P<cls>[0-9A-Z]\d{3}[0-9A-Z])(?P<s1>-?)(?P<tail>' + T5 + r')', 'cls', True),
    ('HMG', r'(?P<cls>\d{5})(?P<s1>-?)(?P<tail>' + T5 + r')', 'cls', True),
    ('SUZUKI', r'(?P<cls>\d{5})(?P<s1>-?)(?P<tail>M\d{2}[A-Z][0-9A-Z]{2}|' + T5 + r')', 'cls', True),
    ('KIA_LEGACY', r'(?P<cls>[0O]K[0-9A-Z]{3}|KK[0-9A-Z]{3}|1K[0-9A-Z]{3})(?P<s1>-?)(?P<grp>\d{2})-?(?P<num>[0-9A-Z]{3})(?P<rev>[A-HJ-NP-Z])?', 'grpnum', True),
    ('HONDA', r'(?P<cls>\d{5})(?P<s1>-?)(?P<model>(?=[0-9]{0,2}[A-Z])[0-9A-Z]{3})(?P=s1)(?P<var>[0-9A-Z]{3})(?P<var4>\d)?(?P<col>[A-Z]{2})?', 'cls', True),
    # Mitsubishi legacy: only real prefixes (MA/MT/MW are not Mitsubishi)
    ('MITSU_OLD', r'(?P<cls>M[BCDEFHKNQRSUZ])(?P<s1>-?)(?P<num>\d{6})(?P<col>[A-Z]{2})?', None, False),
    ('MITSU_NEW', r'(?P<cls>\d{4}[A-D])(?P<num>\d{3})(?P<col>[A-Z]{2})?', 'cls', False),
    ('MAZDA', r'(?P<cls>(?=[0-9A-Z]{0,3}\d)[0-9A-Z]{4})-(?P<grp>\d{2})-(?P<num>(?=[0-9A-Z]{0,2}\d)[0-9A-Z]{3})(?P<rev>[A-HJ-NP-Z])?', 'grpnum', True),
    ('ISUZU', r'(?P<lead>[1589])(?P<s1>-?)(?P<a>\d{5})-?(?P<b>\d{3})(?:-?(?P<rev>\d))?', None, True),
    ('GM', r'(?P<num>[129]\d{7})', None, False),
    # Ford: the design-level suffix starts A-H, so tags such as -UM are not swallowed
    ('FORD', r'(?P<pre>(?=[0-9A-Z]{0,2}\d)[0-9A-Z]{3}[A-Z])-?(?P<base>\d{4,5}[A-Z]?|\d[A-Z]\d{3})-?(?P<suf>[A-H][A-Z]?\d?[A-Z]?)', 'base', True),
    ('SUBARU', r'(?P<cls>\d{5})-?(?P<tail>[A-HJ-NP-Z]{2}(?=[0-9A-Z]{0,2}\d)[0-9A-Z]{3})', 'cls', False),
    ('VAG', r'(?P<a>(?=[0-9A-Z]{0,2}\d)[0-9A-Z]{3})[-.]?(?P<b>\d{3})[-.]?(?P<c>\d{3})(?:[-.]?(?P<idx>[A-Z]{1,2}))?', None, False),
    ('MB', r'(?P<p>[ABNQ])-?(?P<a>\d{3})-?(?P<b>\d{3})-?(?P<c>\d{2})-?(?P<d>\d{2})', None, False),
    ('BMW', r'(?P<mg>\d{2})-?(?P<sg>\d{2})-?(?P<x>\d)-?(?P<i1>\d{3})-?(?P<i2>\d{3})', None, False),
    ('MOPAR', r'(?P<num>\d{7,10})(?P<rev>[A-Z]{2})', None, False),
    ('CN', r'(?P<pre>(?=[0-9A-Z]{0,7}[A-Z])[0-9A-Z]{2,8})-?(?P<func>[1-9]\d{3})(?P<seq>\d{3})(?:-(?P<chg>[0-9A-Z]{1,4}))?', None, False),
    ('HINO54', r'(?P<cls>S?\d{4,5})-(?P<tail>\d{4}[A-Z]?)', None, True),
]
SYSTEMS = [(n, re.compile('^' + body + B), re.compile('^' + body), lex, sep) for n, body, lex, sep in SYSTEM_DEFS]
LENIENT_SYSTEMS = {'TOYOTA', 'NISSAN', 'HMG', 'SUZUKI', 'HONDA'}
O_ZERO_GROUPS = {'TOYOTA': ('tail', 'var'), 'NISSAN': ('tail',), 'HMG': ('tail',), 'SUZUKI': ('tail',),
                 'HONDA': ('model', 'var'), 'MAZDA': ('num',), 'KIA_LEGACY': ('num',)}
NISSAN_ONLY_TAILS = {'9AA9A', 'AA99A', '99A9A', '9A99A', '99AAA'}
STD_CLASSES = {('TOYOTA', '13041'), ('TOYOTA', '13011'), ('TOYOTA', '11701'), ('TOYOTA', '11704'), ('HONDA', '13011'), ('HONDA', '13010')}
CAP_F1_SYSTEMS = {'CN', 'HINO54'}
PART_NAME_CLASS_SYSTEMS = {'TOYOTA', 'NISSAN', 'HMG', 'SUZUKI', 'HONDA', 'SUBARU'}
MAJOR_SYSTEMS = {'TOYOTA', 'NISSAN', 'HMG', 'SUZUKI', 'HONDA', 'MITSU_OLD', 'MITSU_NEW', 'MAZDA', 'ISUZU', 'KIA_LEGACY'}
# Systems allowed in the auto gates: never MAZDA, FORD, GM, KIA_LEGACY, MITSU_OLD, ISUZU, MB, BMW, VAG, MOPAR, CN, HINO54.
AUTO_FLAG_SYSTEMS = {'TOYOTA', 'HMG', 'NISSAN', 'HONDA', 'SUZUKI', 'MITSU_NEW', 'SUBARU'}
AUTO_RENAME_SYSTEMS = {'TOYOTA', 'HMG', 'NISSAN', 'HONDA', 'SUZUKI', 'SUBARU'}
# drive-shaft assembly classes: a component head on them is a different part
ASSEMBLY_CLASSES = {('TOYOTA', '43410'), ('TOYOTA', '43420'), ('TOYOTA', '43430'), ('TOYOTA', '43440'), ('NISSAN', '39100'),
                    ('NISSAN', '39101'), ('NISSAN', '39600'), ('HONDA', '44305'), ('HONDA', '44306'), ('HONDA', '44011'),
                    ('HMG', '49500'), ('HMG', '49501'), ('HMG', '49600'), ('SUZUKI', '44101'), ('SUZUKI', '44102'),
                    ('MITSU_NEW', '3815A'), ('MAZDA', '25-40'), ('MAZDA', '25-50'), ('MAZDA', '25-60')}
COMPONENT_HEADS = set('PUNTA BOTA KIT JUEGO TRICETA CUBREPOLVO BUJE BOLA TOPE RETENEDORA BALINERA CRUZETA PISTON SELLO '
                      'ESCOBILLA BENDIX RECTIFICADOR REGULADOR SELENOIDE POLEA ASPA BASE ORING GUIA TAPA EMP ANILLO '
                      'CAUCHO DIAFRAGMA RESORTE'.split())
ASSEMBLY_HEADS = set('EJE TIJERA BRAZO BARRA AMORT CREMALLERA BOMBA ALTERNADOR MOTOR RADIADOR HUB CALIPER MAST CIL '
                     'ESCL BOOSTER COMPRESOR DISTRIBUIDOR CARBURADOR ABANICO TURBO FAN'.split())
BLOCKING_FLAGS = {'platform_partner', 'make_resolved_by_lexicon', 'make_resolved_by_exclusive_shape', 'make_ambiguous',
                  'make_from_format', 'o_as_zero', 'glued_suffix', 'no_separator', 'isuzu_no_revision_digit',
                  'ford_typed_as_mazda', 'prefix_stripped'}
# The manufacturer writes these numbers differently from the catalog (MR-968365 -> MR968365, 6L8Z-30-78AA -> 6L8Z-3078-AA).
CANONICAL_MAIN_SYSTEMS = {'MITSU_OLD', 'MITSU_NEW', 'FORD', 'KIA_LEGACY'}


def canonical(system, g):
    if system == 'TOYOTA':
        return f"{g['cls']}-{g['tail']}" + (f"-{g['var']}" if g.get('var') else '')
    if system in ('NISSAN', 'HMG', 'SUZUKI'):
        return f"{g['cls']}-{g['tail']}"
    if system == 'KIA_LEGACY':  # only a letter O typed for the zero is fixed: KK150 and 1K2N1 are real Kia prefixes
        return f"{'0' + g['cls'][1:] if g['cls'][0] == 'O' else g['cls']}-{g['grp']}-{g['num']}{g.get('rev') or ''}"
    if system == 'HONDA':
        return f"{g['cls']}-{g['model']}-{g['var']}{g.get('var4') or ''}{g.get('col') or ''}"
    if system in ('MITSU_OLD', 'MITSU_NEW'):
        return f"{g['cls']}{g['num']}{g.get('col') or ''}"
    if system == 'MAZDA':
        return f"{g['cls']}-{g['grp']}-{g['num']}{g.get('rev') or ''}"
    if system == 'ISUZU':
        return f"{g['lead']}-{g['a']}-{g['b']}" + (f"-{g['rev']}" if g.get('rev') else '')
    if system == 'GM':
        return g['num']
    if system == 'FORD':
        return f"{g['pre']}-{g['base']}-{g['suf']}"
    if system == 'SUBARU':
        return f"{g['cls']}{g['tail']}"
    if system == 'VAG':
        return f"{g['a']} {g['b']} {g['c']}" + (f" {g['idx']}" if g.get('idx') else '')
    if system == 'MB':
        return f"{g['p']} {g['a']} {g['b']} {g['c']} {g['d']}"
    if system == 'BMW':
        return f"{g['mg']} {g['sg']} {g['x']} {g['i1']} {g['i2']}"
    if system == 'MOPAR':
        return f"{g['num']}{g['rev']}"
    if system == 'CN':
        return (f"{g['pre']}-" if g.get('pre') else '') + f"{g['func']}{g['seq']}" + (f"-{g['chg']}" if g.get('chg') else '')
    if system == 'HINO54':
        return f"{g['cls']}-{g['tail']}"
    raise KeyError(system)


def is_exclusive(system, g, written):
    if system in ('HONDA', 'MITSU_OLD', 'MITSU_NEW', 'KIA_LEGACY', 'MAZDA', 'MB'):
        return True
    if system == 'ISUZU':
        return '-' in written
    if system == 'NISSAN':
        return shape(g['tail']) in NISSAN_ONLY_TAILS or not g['cls'].isdigit()
    if system == 'SUZUKI':
        return bool(re.match(r'M\d{2}[A-Z]', g['tail']))
    if system == 'FORD':
        return g['pre'][3] == 'Z'
    return False


def lex_key_of(lexgroup, g):
    if not lexgroup:
        return None
    if lexgroup == 'grpnum':  # Mazda / Kia legacy: the group alone mixes part types (99 = tie rods + thermostats)
        return f"{g['grp']}-{g['num'][:2]}"
    return g.get(lexgroup)


def system_hits(seg, table):
    """Every OEM system whose shape matches the head of seg (shape only; never identity)."""
    hits = []
    for name, rx, rx_loose, lexgroup, expects_sep in SYSTEMS:
        m, flags = rx.match(seg), []
        if not m and name in LENIENT_SYSTEMS:
            m2 = rx_loose.match(seg)
            if m2:
                glued = re.match(r'[A-Z]{2,}', seg[m2.end():])
                if glued:
                    ge = table.classify('', glued.group(0))
                    if ge.cls in ('TAG', 'VARIANT') and not ge.derived:
                        m, flags = m2, ['glued_suffix']
        if not m:
            continue
        g = {k: v for k, v in m.groupdict().items() if v}
        written, end = m.group(0), m.end()
        # the colour group must not swallow a suffix-table token (MB-699175XX-JAPA: XX is the kit marker)
        if name in ('MITSU_OLD', 'MITSU_NEW', 'HONDA') and g.get('col') and g['col'] in table.exact:
            col = g.pop('col')
            written, end = written[:-len(col)], end - len(col)
        for k in O_ZERO_GROUPS.get(name, ()):
            if 'O' in g.get(k, ''):
                g[k] = g[k].replace('O', '0')
                flags.append('o_as_zero')
        if name == 'KIA_LEGACY' and g['cls'].startswith('O'):
            flags.append('o_as_zero')
        if name == 'MAZDA' and (g['cls'][3] == 'Z' or re.fullmatch(r'\d[A-Z]\d[A-Z]', g['cls'])):
            # Ford engineering/service prefix typed in Mazda 4-2-3 form: 6L8Z-30-78AA is Ford 6L8Z-3078-AA
            mm = re.fullmatch(r'(\d{4,5})([A-H][A-Z]?\d?[A-Z]?)', g['grp'] + g['num'] + (g.get('rev') or ''))
            if mm:
                fg = {'pre': g['cls'], 'base': mm.group(1), 'suf': mm.group(2)}
                canon = canonical('FORD', fg)
                hits.append({'system': 'FORD', 'written': written, 'canonical': canon, 'key': norm_key(canon), 'groups': fg,
                             'flags': sorted(set(flags + ['ford_typed_as_mazda'])), 'exclusive': fg['pre'][3] == 'Z',
                             'lexkey': fg['base'], 'rest': seg[end:], 'packed': False, 'all_digits': False})
            continue
        packed = expects_sep and '-' not in written
        if packed:
            flags.append('no_separator')
        canon = canonical(name, g)
        hits.append({'system': name, 'written': written, 'canonical': canon, 'key': norm_key(canon), 'groups': g,
                     'flags': sorted(set(flags)), 'exclusive': is_exclusive(name, g, written), 'lexkey': lex_key_of(lexgroup, g),
                     'rest': seg[end:], 'packed': packed, 'all_digits': written.replace('-', '').isdigit()})
    return hits


def manufacturer_form(system, canon, written, flags):
    """Proposed MAIN text (owner decision): the manufacturer's canonical form wherever the catalog writes it differently;
    the written form stays searchable as an alterno. Never contains spaces (VAG/MB/BMW numbers are packed)."""
    if system in CANONICAL_MAIN_SYSTEMS or 'o_as_zero' in flags or 'no_separator' in flags or not re.fullmatch(r'[0-9A-Z-]+', written):
        return canon.replace(' ', '')
    return written


AFTERMARKET = [
    ('KYB', r'^(3[34][0-9]|55[0-9]|63[0-9]|66[0-9])\d{3,4}(?![0-9])', 'AMOR'),
    ('TOKICO', r'^[BEUP]\d{4,5}(?![0-9])', 'AMOR'),
    ('MONROE', r'^(\d{5}(ST)?|[GDREO][A-Z]?\d{4,5}|SC\d{4})(?=-MO\b|$)', 'AMOR'),
    ('555', r'^S[BERL]-?[0-9A-Z]\d{3}[LR]?(?:-M)?(?![0-9])', None),
    ('CTR', r'^C[A-Z](?:T|N|HO|KH|KK|KD|M|MZ|IS|F|S|SU|D|H|K)-?\d{1,3}[LR]?(?![0-9])|^G[A-Z]\d{4}(?![0-9])', None),
    ('FEBEST', r'^(?:T|N|M|H|MZ|HY|K|S|SZ|SB|IS|D|F|BM|VW)(?:AB|SB|CB|M|SS|RB|BS)-?[0-9A-Z]{2,8}(?:-KIT)?(?![0-9A-Z])', None),
    ('GMB', r'^G[WU](?:T|N|M|HO|HY|MZ|IS|S|D|H|K|F)-?\d{2,3}[A-Z]?(?![0-9])|^1[4-9]\d{5}(?![0-9])', None),
    ('AISIN', r'^(?:WP[TNMZYHSKG]|CT|DT|CN|DN|CM|DM|CZ|DZ|CY|DY|CH|DH|CS|DS|CI|DI|CK|DK)-?\d{3}[A-Z]?(?![0-9])', None),
    ('EXEDY', r'^(?:TY|NS|MB|IS|HC|MZ|HY|KI|SZ|DH|FM|GM|DW|FJ|SB)[DCSK]\d{3,4}[A-Z]?(?![0-9])', 'CLUT'),
    ('NIBK', r'^(?:PN|FN)\d{4,5}[A-Z]?(?![0-9])', None),
    ('FMSI', r'^\d{4}[A-Z]?-(?:M?D|S)\d{3,4}(?![0-9])|^M?D\d{3,4}(?:-\d{4})?(?![0-9])', None),
    ('MK_KASHIYAMA', r'^[KD]\d{4}[A-Z]?(?=-MK\b|$)', None),
    ('MANDO', r'^(?:MCC|DCC|MLH|MLK|EWT[A-Z])\d{2,6}[A-Z]?(?![0-9])', None),
    ('SKF', r'^VK[A-Z]{2}\s?\d{4,6}[A-Z]{0,2}(?![0-9])', None),
    ('FAG_SCHAEFFLER', r'^713\d{6}(?![0-9])|^S\d{8}(?![0-9])|^D\d{9}(?![0-9])|^B[FR][DR]\d{6}(?![0-9])', None),
    ('BEARING_ISO', r'^(?:DAC\d{4,8}[A-Z0-9-]*|\d{4,5}(?:ZZ|2RS|DDU|LLU|2NSE)|6[023]\d{2}(?:-?(?:ZZ|2RS|DDU|LLU|C3))*|3[02]\d{3}(?:JR)?|L?M\d{5,6}/\d{2}|HM\d{5,6}|TR\d{6}|ST\d{4})(?![0-9])', 'BALI'),
    ('POLY_V_BELT', r'^\d{1,2}PK\d{3,4}(?![0-9])', 'CORR'),
    ('TYC_DEPO_LAMP', r'^(?:1[1-9]|20)-[0-9A-Z]{4,5}-\d{2}(?:-\d{1,2})?(?![0-9])', None),
    ('BOSCH', r'^(?:0\d{9}|1\d{9}|F\d{9}|F[0-9A-Z]{3}[A-Z]{1,2}\d{4,5})(?![0-9])', None),
    ('DENSO', r'^(?:234|673|210|280|471|477|950)-\d{4}(?![0-9])|^\d{6}-\d{4}(?![0-9])', None),
    ('NGK_DENSO_PLUG', r'^(?:[A-Z]{1,7}\d{1,2}[A-Z0-9]{0,6}(?:-\d{1,2}[A-Z]?)?)$', 'BUJI'),
    ('FILTER_BRANDS', r'^(?:PH|CA|CF|CH)-?\d{2,5}[A-Z]?(?![0-9])|^(?:LFP|LAF|LFF|HU|CUK|JT|EO)\w*', 'FILT'),
    ('WURTH', r'^0\d{3}[-\s]?\d{3}[-\s]?\d{3}(?![0-9])', None),
    ('RADIATOR_CODE', r'^(?:HON|NIS|TOY|HYU|MIT|MAZ|SUZ|ISU|KIA)-\d{4,5}(?![0-9])', None),
    ('CL_FAN', r'^CL-\d{4}[A-Z]?(?![0-9])', None),
]
AFTERMARKET_RE = [(b, re.compile(p), gate) for b, p, gate in AFTERMARKET]


def aftermarket_matches(code, head):
    return [(brand, m.end()) for brand, rx, gate in AFTERMARKET_RE for m in [rx.match(code)]
            if m and (gate is None or head.startswith(gate))]


DESC_CODE_RE = re.compile(
    r'(?<![0-9A-Z])('
    r'\d{5}-[0-9A-Z]{3}-[0-9A-Z]{3,4}'
    r'|\d{5}-[0-9A-Z]{5,6}'
    r'|[A-Z]\d{4}-[0-9A-Z]{5}'
    r'|M[A-Z]-?\d{6}'
    r'|\d{4}[A-D]\d{3}'
    r'|[1589]-\d{5}-\d{3}(?:-\d)?'
    r'|[0O]K[0-9A-Z]{3}-\d{2}-[0-9A-Z]{3,4}'
    r'|[0-9A-Z]{4}-\d{2}-[0-9A-Z]{3,4}'
    r'|[129]\d{7}'
    r')(?![0-9A-Z])')
# description wording that says the item is NOT the plain OEM part
DESC_VARIANT = [
    ('AJUSTABLE', r'\bAJUSTABLE\b'), ('REFORZADO', r'\bREFORZAD[OA]S?\b'), ('TEFLON', r'\bTEFLON\b'),
    ('POLIURETANO', r'\bPOLIURETANO\b'), ('RACING', r'\bRACING\b'), ('UNIVERSAL', r'\bUNIVERSAL\b|\bUNIV\b'),
    ('ADAPTABLE', r'\bADAPTABLE\b'), ('REMANUFACTURADO', r'\bREMANUFACTURAD[OA]\b|\bREMAN\b'),
    ('CADA_UNA', r'\bCADA UNA\b|\bC/U\b'), ('SET', r'\bSET\b'), ('JUEGO', r'\bJUEGO\b|\bJGO\b'),
    ('PIECES', r'\b\d+\s?(?:PZ|PZS|PCS|PIEZAS)\b|\*\d+PZ\*'), ('PAR', r'\bPAR\b|\(EL PAR\)'),
    ('SIZE', r'\bSTD\b|\bO/S\b|\bU/S\b|\bOVERSIZE\b|\b0\.(?:25|50|75)\b|\b1\.00\b'),
    ('KIT', r'\bKIT\b'), ('COMPLETO', r'\bCOMPLET[OA]\b'),
]
DESC_VARIANT_RE = [(k, re.compile(rx)) for k, rx in DESC_VARIANT]


def desc_variant_keywords(desc):
    d = up(desc)
    return [k for k, rx in DESC_VARIANT_RE if rx.search(d)]


# =============================================================== skip and standard-code rules
HARD_SKIP = {
    'chemicals_lubricants': set('ACEITE ADITIVO COOLANT GRASA SILICON SILICONE LIMPIADOR LUCAS FLAMINGO RISLONE BRAKE '
                                'LIQUIDO REFRIGERANTE ANTICONGELANTE SELLADOR PEGA CERA SHAMPOO ABRILLANTADOR DESENGRASANTE '
                                'LUBRICANTE PENETRANTE WD40 FIJADOR LOCTITE PERMATEX BARDAHL PRESTONE ABRO STP MOTUL '
                                'CASTROL PULIMENTO PASTA ESMALTE PINTURA SPRAY AMBIENTADOR TRATAMIENTO ANTIOXIDANTE '
                                'DESCARBONIZANTE REFRIGERANT FREON'.split()),
    'tools_equipment': set('LLAVE PINZA ALICATE ALICACTE DESTORNILLADOR DESARMADOR MACHO PROBADOR SACA DADO MARTILLO '
                           'GATO TORQUIMETRO MULTIMETRO CAUTIL EXTRACTOR HERRAMIENTA CARGADOR COMPROBADOR CORTADORA '
                           'PISTOLA CINCEL LIMA SIERRA TALADRO ESCOBA CEPILLO TARRAJA RATCHET MANERAL CALIBRADOR '
                           'EXTINTOR TRIANGULO LINTERNA ESCALERA EXTENSION ADAPTADOR'.split()),
    'services_labor': set('CAMBIO AJUSTE ALINIAMIENTO ALINEAMIENTO BALANCEO SERVICIO MANO REVISION INSTALACION '
                          'DIAGNOSTICO LAVADO ESCANEO REPARACION RECTIFICACION'.split()),
    'tires_batteries': set('LLANTA BATERIA NEUMATICO RIN PARCHE'.split()),
    'generic_electrical': set('BOMBILLO FUSIBLE HID FLASHER'.split()),
    'cosmetic_accessories': set('LOGO EMBLEMA PAPEL FORRO ANTENA COPA POLLERA VICERA ALFOMBRA TAPETE CALCOMANIA STICKER '
                                'PARLANTE RADIO ALARMA FUNDA PORTAPLACA TAPACUBO CANDADO CALZA BOCINA CORNETA'.split()),
    'generic_hardware': set('ABRAZADERA WACHA CLAVO REMACHE GRAPA PRENSA'.split()),
}
SOFT_SKIP = {
    'generic_electrical_soft': set('LED TERMINAL CONECTOR BOTON CABLEADO INTERRUPTOR EMPALME BROCHA LUZ HALOGENA'.split()),
    'accessories_soft': set('ESPEJO PERILLA PALANCA MATA EMBELLECEDOR CHAGLE RELOJ CINTURON SUJETADOR SPOILER '
                            'ESTRIBO MOLDURA CUBRE'.split()),
    'hardware_soft': set('TORNILLO ARANDELA MANGUERA CODO NIPLE TUERCA CINTA MEDIDOR SOCKET JUEGO'.split()),
}
CATEGORY_SKIP_WORDS = ('QUIMIC', 'LUBRICANT', 'HERRAMIENT', 'SERVICIO', 'ACCESORIO', 'LLANTA', 'BATERIA', 'LIMPIEZA')
TOOL_SOCKET = re.compile(r'\b(1/2|3/8|1/4|3/4)\b|\bMM\b|\d+MM')


def skip_group(sku, desc, category, subcategory, makes):
    d = up(desc)
    ws = re.findall(r'[A-Z0-9/]+', d)
    h = ws[0] if ws else ''
    cat = up(category + ' ' + subcategory)
    if any(w in cat for w in CATEGORY_SKIP_WORDS):
        return 'category_field', 'hard'
    if sku.startswith('SAE'):
        return 'chemicals_lubricants', 'hard'
    if sku.startswith('LL-'):
        return 'tires_batteries', 'hard'
    if h == 'SOCKET' and TOOL_SOCKET.search(d):
        return 'tools_equipment', 'hard'
    if (h == 'PLACA' and 'DECORATIVA' in d) or (h == 'PARRILLA' and 'TECHO' in d):
        return 'cosmetic_accessories', 'hard'
    if h == 'CINTA' and ('TIMON' in d or 'TIMIN' in d):
        return None
    if h == 'TUERCA' and re.search(r'RUEDA|LLANTA|ROSCA|SEGURIDAD', d):
        return None
    if h == 'MEDIDOR' and 'ACEITE' in d:
        return None
    if h == 'JUEGO' and re.search(r'EMP|ANILLO|BALINERA|CASQ|PISTON|VALV', d):
        return None
    for g, s in HARD_SKIP.items():
        if h in s:
            return g, 'hard'
    for g, s in SOFT_SKIP.items():
        if h in s:
            return (g, 'soft') if not makes else (g, 'soft_overridden_by_make')
    return None


def standard_group(desc, base):
    d = up(desc)
    h = (re.findall(r'[A-Z]+', d) or [''])[0]
    if h in ('CORREA', 'BANDA') and 'TIEMPO' not in d and 'DISTRIB' not in d and (
            re.match(r'^\d{0,2}PK\d{3,4}', base) or re.match(r'^\d{4,5}$', base)
            or re.match(r'^[ABM]\d{2,3}$', base) or re.match(r'^\d?L\d{3}$', base)):
        return 'belt_size_coded'
    if h in ('BALINERA', 'BAL') and re.match(r'^(\d{3,5}(-?2RS|-?ZZ|-?2NSE|-?RS|-?2RSH|-?DDU|-?LLU|LLK|LLU|ZZ)?|HR\d{5}|LM\d+|L\d{5}|'
                                             r'\d{5}/\d+|DAC\w+|RCT\w+|[0-9]{4,6}[A-Z]{0,4}\d*)(-|/|$)', base):
        return 'bearing_iso_coded'
    if h == 'BUJIA':
        return 'spark_plug_brand_coded'
    return None


def reference_strings(refs):
    out = []
    for r in refs or []:
        if isinstance(r, str):
            out.append(r)
        elif isinstance(r, dict):
            v = next((r[k] for k in ('code', 'reference', 'value', 'codigo', 'oem') if r.get(k)), None)
            if v:
                out.append(str(v))
    return out


PREFIX_RE = re.compile(r'^([A-Z]{2,6}|\d{2})-(?=[0-9A-Z])')


def assembly_or_kit_head(head, lex):
    h, q = head
    if h in ('ABANICO', 'MODULO', 'CONJUNTO', 'JUEGO', 'SET'):
        return h
    if h == 'EJE' and q == 'COMPLETO':
        return 'EJE COMPLETO'
    if h == 'KIT' and lex.get('result') != 'agree_strong':
        return 'KIT'
    return ''


def eligible(entry, confirm=frozenset()):
    """A TAG an auto-applied rename may strip; confirm = tokens the owner is assumed to confirm (STRONG_PENDING scenario)."""
    if entry.cls != 'TAG' or entry.role == 'lifecycle':
        return False
    if entry.components is not None:
        return all(eligible(c, confirm) for c in entry.components)
    return (entry.auto_eligible or entry.token in confirm
            or (entry.alias_of is not None and entry.alias_of in confirm and entry.status != 'needs_owner_label'))


# =============================================================== tiers
TIER_ORDER = ['AUTO_FLAG_CURRENT', 'AUTO_RENAME_BASE', 'STRONG_PENDING_OWNER_TAGS', 'CURRENT_REVIEW', 'PROBABLE_BASE',
              'CURRENT_LIKELY_OEM', 'LOCAL_XREF', 'MULTI_OEM', 'CONFLICT', 'VARIANT_REVIEW', 'UNKNOWN_SUFFIX', 'WEAK',
              'NEEDS_AI', 'NO_MAKE', 'STANDARD_CODE', 'SKIP_NO_OEM', 'HOLD_LIFECYCLE']
TIER_LABELS = {
    'AUTO_FLAG_CURRENT': 'Automático: el SKU ya es el OEM', 'AUTO_RENAME_BASE': 'Automático: renombrar a la base OEM',
    'STRONG_PENDING_OWNER_TAGS': 'Pendiente de etiquetas', 'CURRENT_REVIEW': 'Actual (revisar)', 'PROBABLE_BASE': 'Base probable',
    'CURRENT_LIKELY_OEM': 'Actual probable', 'LOCAL_XREF': 'Referencia local', 'MULTI_OEM': 'Varios OEM',
    'CONFLICT': 'Conflicto (agrupar)', 'VARIANT_REVIEW': 'Variante', 'UNKNOWN_SUFFIX': 'Sufijo desconocido', 'WEAK': 'Débil',
    'NEEDS_AI': 'Requiere IA', 'NO_MAKE': 'Sin marca', 'STANDARD_CODE': 'Código estándar', 'SKIP_NO_OEM': 'Sin OEM (omitido)',
    'HOLD_LIFECYCLE': 'Anulado (excluido)',
}
AUTO_TIERS = ('AUTO_FLAG_CURRENT', 'AUTO_RENAME_BASE')
# What mall.oem_apply may apply: STRONG_PENDING_OWNER_TAGS there means the AUTO_RENAME_BASE rows resting on an owner-confirmed tag.
APPLY_TIERS = ('AUTO_FLAG_CURRENT', 'AUTO_RENAME_BASE', 'STRONG_PENDING_OWNER_TAGS')
# Review tiers become OEMReviewCase rows. NO_MAKE waits for the owner to set a make; the last three are not OEM work.
REVIEW_TIERS = ('STRONG_PENDING_OWNER_TAGS', 'CURRENT_REVIEW', 'PROBABLE_BASE', 'CURRENT_LIKELY_OEM', 'LOCAL_XREF', 'MULTI_OEM',
                'CONFLICT', 'VARIANT_REVIEW', 'UNKNOWN_SUFFIX', 'WEAK', 'NEEDS_AI')
PRE_CLAIM = {'CURRENT_IS_OEM', 'CURRENT_LIKELY_OEM', 'BASE_CLAIM', 'LOCAL_XREF'}
SKU_SOURCES = ('sku', 'sku_segment', 'sku_prefixed')


class Snapshot:
    """Read-only rows: parts {id, sku, name, description, category, subcategory, active, merged_into_id, is_OEM} (every Part,
    retired included), codes {part_id, code, brand, ref_type, reference_source}, items {supplier_id, part_id, codigo, brand,
    references, reported_quantity, reserved_quantity}. Ids are strings."""

    def __init__(self, parts, codes=(), items=(), source='rows'):
        self.parts, self.codes, self.items, self.source = list(parts), list(codes), list(items), source


def load_snapshot():
    """ORM .values() reads only, in a stable order so lexicon tie-breaks (and fingerprints) repeat run to run. On PostgreSQL the
    three reads share one REPEATABLE READ READ ONLY transaction: an upload committed meanwhile never splits Parts from codes."""
    from django.db import connection, transaction
    from .models import Part, PartCode, SupplierItem
    outermost = not connection.in_atomic_block
    with transaction.atomic():
        if outermost and connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        # sorted here by code point, not by the database collation (en_US.utf8 ignores punctuation and changes with glibc)
        parts = sorted((dict(p, id=str(p['id']), merged_into_id=str(p['merged_into_id']) if p['merged_into_id'] else None)
                        for p in Part.objects.values('id', 'sku', 'name', 'description', 'category', 'subcategory', 'active',
                                                     'merged_into_id', 'is_OEM').iterator(chunk_size=5000)), key=lambda p: p['sku'])
        codes = [dict(c, part_id=str(c['part_id'])) for c in PartCode.objects.order_by('pk').values(
            'part_id', 'code', 'brand', 'ref_type', 'reference_source').iterator(chunk_size=5000)]
        items = [dict(s, part_id=str(s['part_id']), supplier_id=str(s['supplier_id'])) for s in SupplierItem.objects.filter(
            part__isnull=False).order_by('pk').values('supplier_id', 'part_id', 'codigo', 'brand', 'references', 'reported_quantity',
                                                      'reserved_quantity').iterator(chunk_size=5000)]
    return Snapshot(parts, codes, items, source='orm')


class OEMFinder:
    """Every derived index of one run, built in memory from a Snapshot and a SuffixTable; evaluate() returns {part_id: result}."""

    def __init__(self, snapshot, table):
        self.snapshot, self.table = snapshot, table
        self.by_id = {p['id']: p for p in snapshot.parts}
        self.parts = [p for p in snapshot.parts if p['active'] and not p['merged_into_id'] and not p['is_OEM']]
        self.items_by_part, self.codes_by_part = defaultdict(list), defaultdict(list)
        for s in snapshot.items:
            if s['part_id']:
                self.items_by_part[s['part_id']].append(s)
        for c in snapshot.codes:
            self.codes_by_part[c['part_id']].append(c)
        self.avail = {p['id']: sum(max(0, s['reported_quantity'] - s['reserved_quantity']) for s in self.items_by_part.get(p['id'], []))
                      for p in self.parts}
        self.merged_into = {p['id']: p['merged_into_id'] for p in snapshot.parts}
        self.owners = defaultdict(list)
        for p in snapshot.parts:
            kind = 'active_sku' if (p['active'] and not p['merged_into_id']) else 'retired_sku'
            self.owners[norm_key(p['sku'])].append((p['id'], kind, p['sku']))
        for c in snapshot.codes:
            self.owners[norm_key(c['code'])].append((c['part_id'], 'partcode', c['code']))
        self.model_hints = self._model_hints()
        self.makes = {p['id']: self.detect_make(p) for p in self.parts}
        self.parsed = {p['id']: self.parse_part(p) for p in self.parts}
        self._build_lexicon()

    # ---------------------------------------------------------- makes
    @staticmethod
    def explicit_makes(text):
        ws = words(text)
        ms = {MAKE_TOKENS[w] for w in ws if w in MAKE_TOKENS}
        if 'LR' in ws and ('ROVER' in ws or 'RANGE' in ws or 'DISCOVERY' in ws):
            ms.add('OTHER')
        return sorted(ms)

    def _model_hints(self):
        tok_group = defaultdict(Counter)
        for p in self.parts:
            grps = {MAKE_GROUP[m] for m in self.explicit_makes(p['description'])}
            if len(grps) == 1:
                g = grps.pop()
                for w in set(words(p['description'])):
                    if len(w) >= 3 and not w.isdigit() and w not in MAKE_TOKENS and w not in HINT_STOP:
                        tok_group[w][g] += 1
        hints = {}
        for w, c in tok_group.items():
            g, n = c.most_common(1)[0]
            if n >= 8 and n / sum(c.values()) >= 0.9 and w not in HINT_DROP:
                hints[w] = g
        hints.update({w: 'HMG' for w in KIA_MODELS})
        return hints

    def detect_make(self, p):
        text = p['description'] + (' ' + p['name'] if p.get('name') and p['name'] != p['sku'] else '')
        ms = self.explicit_makes(text)
        src = 'description' if ms else ''
        for s in self.items_by_part.get(p['id'], []):
            b = up(s.get('brand') or '')
            if b in MAKE_TOKENS and MAKE_TOKENS[b] not in ms:
                ms.append(MAKE_TOKENS[b])
                src = (src + '+supplier_brand').strip('+')
        if not ms:
            ws = words(text)
            grps = [] if 'ROVER' in ws else sorted({self.model_hints[w] for w in ws if w in self.model_hints})
            if grps:
                ms = sorted({m for g in grps for m in GROUP_MAKES.get(g, [g])})
                src = 'model_hint'
        return sorted(set(ms)), src

    @staticmethod
    def allowed_systems(ms):
        own, partner = set(), set()
        for m in ms:
            o, pa = MAKE_SYSTEMS.get(m, ([], []))
            own.update(o)
            partner.update(pa)
        return own, partner - own

    # ---------------------------------------------------------- parsing
    def full_alternate(self, rest):
        """'27060-16160/27060-16170': a complete second OEM number after the slash."""
        full = [h for h in system_hits(rest, self.table) if len(h['written']) >= 8]
        return max(full, key=lambda x: len(x['written']))['written'] if full else None

    def interpret_segment(self, seg, own, partner, source, head, extra=None):
        out, foreign = [], []
        for h in system_hits(seg, self.table):
            if h['system'] in own or h['system'] in partner:
                chain = classify_chain(h['rest'], self.table, h['written'], head, full_alternate=self.full_alternate)
                out.append(dict(h, source=source, seg=seg, partner=h['system'] in partner and h['system'] not in own,
                                chain=chain, cs=chain_summary(chain), **(extra or {})))
            else:
                foreign.append(h['system'])
        return out, foreign

    def parse_part(self, p):
        ms, msrc = self.makes[p['id']]
        own, partner = self.allowed_systems(ms)
        head = description_head(p['description'])
        make_from_format = False
        sku_clean, lifecycle = strip_lifecycle(clean_code(p['sku']))
        segs = [s for s in sku_clean.replace('_', '-').split(' ') if s]
        if not ms and segs:
            ex = {EXCLUSIVE_SYSTEM_MAKE[h['system']] for h in system_hits(segs[0], self.table)
                  if h['exclusive'] and h['system'] in EXCLUSIVE_SYSTEM_MAKE and not h['packed']}
            if len(ex) == 1:
                ms = sorted(ex)
                own, partner = self.allowed_systems(ms)
                make_from_format = True
        interps, foreign, prefix = [], [], ''
        if segs:
            i0, f0 = self.interpret_segment(segs[0], own, partner, 'sku', head)
            interps += i0
            foreign += f0
            if not i0 and not f0:
                m = PREFIX_RE.match(segs[0])
                if m:
                    ip, _ = self.interpret_segment(segs[0][m.end():], own, partner, 'sku_prefixed', head, {'prefix': m.group(1)})
                    if ip:
                        prefix = m.group(1)
                        for it in ip:
                            it['flags'] = sorted(set(it['flags']) | {'prefix_stripped'})
                        interps += ip
            for s in segs[1:]:
                interps += self.interpret_segment(s, own, partner, 'sku_segment', head)[0]
        for it in self.items_by_part.get(p['id'], []):
            c, _ = strip_lifecycle(clean_code(it['codigo']))
            for s in c.replace('_', '-').split(' '):
                if s:
                    interps += self.interpret_segment(s, own, partner, 'codigo', head,
                                                      {'supplier': it['supplier_id'], 'supplier_brand': up(it.get('brand') or '')})[0]
            for ref in reference_strings(it.get('references')):
                for s in clean_code(ref).split(' '):
                    if s:
                        interps += self.interpret_segment(s, own, partner, 'supplier_reference', head, {'supplier': it['supplier_id']})[0]
        for c in self.codes_by_part.get(p['id'], []):
            cc, _ = strip_lifecycle(clean_code(c['code']))
            for s in cc.replace('_', '-').split(' '):
                if s:
                    interps += self.interpret_segment(s, own, partner, 'alterno', head,
                                                      {'ref_type': c['ref_type'], 'alt_brand': up(c['brand']),
                                                       'reference_source': c['reference_source']})[0]
        sku_text = norm_key(p['sku'])
        for m in DESC_CODE_RE.finditer(up(p['description'])):
            code = m.group(1)
            interps += self.interpret_segment(code, own, partner, 'description', head, {'in_sku_text': norm_key(code) in sku_text})[0]
        malformed = ''
        if segs and not interps and re.match(r'^[1589]-\d{6}-\d{3}-\d(?=$|[-/ ])', segs[0]) and 'ISUZU' in own | partner:
            malformed = 'ISUZU'  # 8-980084-422-0: a mis-dashed Isuzu number (6-digit group) goes to WEAK, not AI
        return {'makes': ms, 'make_source': 'format_exclusive' if make_from_format else msrc, 'malformed': malformed,
                'make_from_format': make_from_format, 'own': own, 'partner': partner, 'head': head, 'segs': segs,
                'lifecycle': lifecycle, 'interps': interps, 'foreign': sorted(set(foreign)),
                'foreign_major': sorted(set(foreign) & MAJOR_SYSTEMS), 'prefix': prefix}

    # ---------------------------------------------------------- sibling claims and class lexicon
    def _build_lexicon(self):
        self.base_claims = defaultdict(list)
        lex_keys = defaultdict(lambda: defaultdict(lambda: [Counter(), Counter()]))
        for p in self.parts:
            P = self.parsed[p['id']]
            for it in P['interps']:
                if it['source'] in ('sku', 'sku_prefixed'):
                    self.base_claims[it['key']].append({
                        'part_id': p['id'], 'sku': p['sku'], 'attribution': it['cs']['attribution'],
                        'chain_class': it['cs']['chain_class'], 'system': it['system'],
                        'genuine_tokens': it['cs']['genuine_tokens'], 'lifecycle': bool(P['lifecycle'])})
            # built only from rows with a single explicit make group, so 'leave own key out' holds
            grps = {MAKE_GROUP[m] for m in P['makes']}
            sku_hits = [it for it in P['interps'] if it['source'] == 'sku' and not it['partner']]
            if len(grps) == 1 and P['make_source'] == 'description' and len(sku_hits) == 1 and sku_hits[0]['lexkey'] \
                    and 'o_as_zero' not in sku_hits[0]['flags'] and P['head'][0]:
                h = sku_hits[0]
                e = lex_keys[(h['system'], h['lexkey'])][h['key']]
                e[0][head_str(P['head'])] += 1
                sd = side_of(p['description'], h['cs']['tokens'])
                if sd:
                    e[1][sd] += 1
        self.lex = {k: [(key, c[0].most_common(1)[0][0] if c[0] else '', c[1].most_common(1)[0][0] if c[1] else '')
                        for key, c in per_key.items()] for k, per_key in lex_keys.items()}

    def lexicon_check(self, system, lexkey, key, head, side):
        rows = self.lex.get((system, lexkey))
        if not rows:
            return {'result': 'no_data', 'n_other_keys': 0}
        others = [r for r in rows if r[0] != key]
        n = len(others)
        heads = Counter(r[1] for r in others)
        hits = sum(v for hs, v in heads.items() if head_compat(head_parse(hs), head))
        share = hits / n if n else 0.0
        top_share = heads.most_common(1)[0][1] / n if n else 0.0
        ho = Counter(head_parse(r[1])[0] for r in others)
        top_h, top_hn = ho.most_common(1)[0] if ho else ('', 0)
        if n == 0:
            res = 'no_data'
        elif n >= 5 and share >= 0.8:
            res = 'agree_strong'
        elif n >= 3 and share >= 0.5:
            res = 'agree'
        elif n >= 5 and hits == 0 and top_share >= 0.6 and system in PART_NAME_CLASS_SYSTEMS:
            res = 'disagree'
        else:
            res = 'weak'
        sides = Counter(r[2] for r in others if r[2] in ('R', 'L'))
        side_res, ns = '', sum(sides.values())
        if ns >= 4:
            dom, dn = sides.most_common(1)[0]
            if dn / ns >= 0.85 and ns >= 0.6 * n:
                side_res = ('consistent' if side == dom else 'conflict') if side in ('R', 'L') else 'class_is_side_' + dom
        return {'result': res, 'n_other_keys': n, 'head': head_str(head), 'share': round(share, 2),
                'top_heads': heads.most_common(3), 'top_head': top_h, 'top_head_share': round(top_hn / n, 2) if n else 0.0,
                'side': side_res}

    @staticmethod
    def component_conflict(system, lexkey, head, lex):
        h = head[0]
        if h not in COMPONENT_HEADS:
            return None
        if (system, lexkey) in ASSEMBLY_CLASSES and h != 'EJE':
            return 'drive_shaft_assembly_class'
        if (lex.get('n_other_keys', 0) >= 3 and lex.get('top_head') in ASSEMBLY_HEADS and lex.get('top_head_share', 0) >= 0.6
                and lex.get('top_head') != h and lex.get('result') not in ('agree', 'agree_strong')):
            return 'assembly_class:' + lex['top_head']
        return None

    # ---------------------------------------------------------- grading one candidate key
    def evaluate_key(self, p, P, key, interps, desc_side, confirm):
        own, head, table = P['own'], P['head'], self.table
        by_system = defaultdict(list)
        for it in interps:
            by_system[it['system']].append(it)
        options = []
        for system, its in by_system.items():
            rep = next((i for i in its if i['source'] in ('sku', 'sku_prefixed')), its[0])
            lex = self.lexicon_check(system, rep['lexkey'], key, head, desc_side) if rep['lexkey'] else {'result': 'n/a'}
            options.append((system, rep, lex))

        def opt_rank(o):
            system, rep, lex = o
            return ((lex['result'] in ('agree', 'agree_strong')) * 4 + rep['exclusive'] * 2 + (system in own) * 1
                    - (lex['result'] == 'disagree') * 8)
        options.sort(key=opt_rank, reverse=True)
        system, rep, lex = options[0]
        flags = set(rep['flags'])
        make_ambiguous = False
        if len(options) > 1 and opt_rank(options[0]) == opt_rank(options[1]):
            make_ambiguous = True
            flags.add('make_ambiguous')
        elif len(options) > 1 and SYSTEM_BRAND[options[1][0]] != SYSTEM_BRAND[system]:
            flags.add('make_resolved_by_exclusive_shape' if rep['exclusive'] else 'make_resolved_by_lexicon')
        if rep['partner']:
            flags.add('platform_partner')
        if P['make_from_format']:
            flags.add('make_from_format')
        attributions, sources, suppliers, verified_oem, desc_indep, genuine_tokens = set(), Counter(), set(), False, False, set()
        for it in interps:
            if it['system'] != system and SYSTEM_BRAND.get(it['system']) != SYSTEM_BRAND[system]:
                continue
            sources[it['source']] += 1
            if it.get('supplier'):
                suppliers.add(it['supplier'])
            if it['source'] == 'description':
                if not it.get('in_sku_text'):
                    attributions.add(('DESC',))
                    desc_indep = True
                continue
            if it['source'] == 'alterno' and it.get('ref_type') == 'oem':
                attributions.add(('OEM_REF:' + (it.get('alt_brand') or '?'),))
                if it.get('alt_brand') and it.get('reference_source'):
                    verified_oem = True
                continue
            if it['cs']['chain_class'] in ('bare', 'tag'):
                attr = it['cs']['attribution']
                if it.get('supplier_brand') and it['supplier_brand'] not in MAKE_TOKENS:
                    attr = tuple(sorted(set(attr) - {'BARE'} | {it['supplier_brand']}))
                attributions.add(attr)
                genuine_tokens.update(it['cs']['genuine_tokens'])
        siblings = []
        for sc in self.base_claims.get(key, []):
            if sc['part_id'] == p['id'] or SYSTEM_BRAND.get(sc['system']) != SYSTEM_BRAND[system]:
                continue
            siblings.append(sc)
            if sc['chain_class'] in ('bare', 'tag') and not sc['lifecycle']:
                attributions.add(sc['attribution'])
                genuine_tokens.update(sc['genuine_tokens'])
        n_attr = len(attributions)
        genuine_confirmed = any(eligible(table.exact[t], confirm) for t in genuine_tokens if t in table.exact)
        corroborated = n_attr >= 2 or verified_oem or len(suppliers) >= 2
        units, caps, contradictions = [], [], []
        if lex['result'] in ('agree', 'agree_strong'):
            units.append('lexicon_agree')
        if corroborated:
            units.append('corroborated')
        if genuine_tokens:
            units.append('genuine_claim')
        if lex['result'] == 'disagree':
            contradictions.append('lexicon_disagree')
        if lex.get('side', '') == 'conflict':
            contradictions.append('side_conflict')
        am = aftermarket_matches(rep['seg'], head[0][:4])
        am_brands = [b for b, _ in am]
        if am and (rep['packed'] or max(e for _, e in am) > len(rep['written'])):
            contradictions.append('aftermarket_pattern_wins:' + ','.join(am_brands))
        comp = self.component_conflict(system, rep['lexkey'], head, lex) if rep['lexkey'] else None
        grade = 2 + min(len(units), 2)
        if system in CAP_F1_SYSTEMS:
            caps.append(1)
        if rep['packed']:
            caps.append(1 if rep['all_digits'] else 2)
        if contradictions:
            caps.append(1)
        if make_ambiguous:
            caps.append(2)
        downgrade = sum(1 for f in ('o_as_zero', 'glued_suffix', 'make_resolved_by_lexicon', 'ford_typed_as_mazda', 'prefix_stripped')
                        if f in flags)
        if system == 'ISUZU' and not rep['groups'].get('rev'):
            downgrade += 1
            flags.add('isuzu_no_revision_digit')
        grade = max(1, min([grade - downgrade] + caps))
        conflict = []
        for o in self.owners.get(key, []):
            if o[0] == p['id']:
                continue
            if o[1] == 'retired_sku' and self.merged_into.get(o[0]) == p['id']:
                o = (o[0], 'retired_sku_merged_into_this', o[2])
            conflict.append(o)
        flags = sorted(flags)
        return {
            'key': key, 'system': system, 'brand': SYSTEM_BRAND[system], 'written': rep['written'], 'canonical': rep['canonical'],
            'manufacturer_form': manufacturer_form(system, rep['canonical'], rep['written'], flags), 'lexkey': rep['lexkey'],
            'grade': grade, 'grade_label': 'F%d' % grade, 'units': units, 'flags': flags, 'contradictions': contradictions,
            'lexicon': lex, 'lexicon_strong': lex['result'] == 'agree_strong', 'component_conflict': comp,
            'exclusive_shape': rep['exclusive'], 'aftermarket_lookalike': am_brands, 'n_independent_attributions': n_attr,
            'attributions': sorted('+'.join(a) for a in attributions), 'genuine_tokens': sorted(genuine_tokens),
            'genuine_confirmed': genuine_confirmed, 'verified_oem_alterno': verified_oem, 'description_independent': desc_indep,
            'n_suppliers': len(suppliers), 'source_counts': dict(sources),
            'second_unit': bool(genuine_confirmed or verified_oem or len(suppliers) >= 2 or n_attr >= 3),
            'siblings': [{'sku': s['sku'], 'chain_class': s['chain_class'], 'attribution': '+'.join(s['attribution'])}
                         for s in siblings[:12]],
            'n_siblings': len(siblings),
            'conflicts': [{'part_id': o[0], 'kind': o[1], 'text': o[2]} for o in conflict[:8]], 'n_conflicts': len(conflict),
        }

    # ---------------------------------------------------------- per-Part decision (before pass 3 and the gates)
    def decide(self, p, P, confirm=frozenset()):
        table, ms, head, interps = self.table, P['makes'], P['head'], P['interps']
        sku_interps = [i for i in interps if i['source'] in ('sku', 'sku_prefixed')]
        desc_side = side_of(p['description'])
        keys = defaultdict(list)
        for it in interps:
            keys[it['key']].append(it)
            for alt in it['cs']['alt_codes']:
                for h in system_hits(alt, table):
                    if h['system'] == it['system']:
                        keys[h['key']].append(dict(it, key=h['key'], written=h['written'], canonical=h['canonical'],
                                                   source=it['source'] + ':alt_tail', cs=dict(it['cs'], alt_codes=[])))
        evs = {k: self.evaluate_key(p, P, k, v, desc_side, confirm) for k, v in keys.items()}
        res = {'makes': ms, 'make_source': P['make_source'], 'head': head_str(head), 'reasons': [],
               'candidates': [{k: e[k] for k in ('key', 'system', 'grade', 'manufacturer_form', 'written')}
                              for e in sorted(evs.values(), key=lambda e: -e['grade'])[:6]]}
        sku_first = P['segs'][0] if P['segs'] else ''
        base_for_std = generic_base(sku_first, head, table)[0] if sku_first else ''
        has_oem_sku = any(evs[i['key']]['grade'] >= 2 for i in interps if i['source'] in SKU_SOURCES)
        if sku_first and not sku_interps:
            gb, stripped = generic_base(sku_first, head, table)
            brands = [e for e in stripped if e.company]
            if brands and gb and len(norm_key(gb)) >= 3:
                res['company_reference'] = {'code': gb, 'brand': brands[-1].brand_name(), 'ref_type': 'company',
                                            'suffix_tag': brands[-1].token}
        res['aftermarket_family'] = [b for b, _ in aftermarket_matches(sku_first, head[0][:4])]
        oem_segs = {i['seg'] for i in interps if i['source'] in ('sku', 'sku_segment')}
        sec = []
        for s in P['segs']:
            if s in oem_segs:
                continue
            gb, stripped = generic_base(s, head, table)
            brands = [e for e in stripped if e.company]
            fam = [b for b, _ in aftermarket_matches(s, head[0][:4])]
            if brands or fam:
                sec.append({'code': gb if brands else s, 'brand': brands[-1].brand_name() if brands else fam[0], 'ref_type': 'company',
                            'segment': s, 'via': 'suffix_tag' if brands else 'aftermarket_pattern'})
        if sec:
            res['secondary_company_refs'] = sec

        if P['lifecycle'] or any(i['cs']['lifecycle'] for i in sku_interps):
            res['tier'] = 'HOLD_LIFECYCLE'
            res['reasons'].append('marcador de ciclo de vida %s: código ERP anulado o eliminado que sigue activo'
                                  % (P['lifecycle'] or 'en la cadena'))
            return res
        sg = skip_group(p['sku'], p['description'], p.get('category') or '', p.get('subcategory') or '', ms)
        if sg and sg[1] in ('hard', 'soft') and not has_oem_sku:
            res['tier'], res['skip_group'] = 'SKIP_NO_OEM', sg[0]
            res['reasons'].append('categoría sin OEM %s (regla %s, sustantivo %s)' % (sg[0], sg[1], res['head']))
            return res
        if sg:
            res['skip_group_overridden'] = '%s/%s' % sg
        std = standard_group(p['description'], base_for_std)
        if std and not has_oem_sku:
            res['tier'], res['standard_group'] = 'STANDARD_CODE', std
            res['reasons'].append('código estándar universal (%s)' % std)
            return res

        good = {k: e for k, e in evs.items() if e['grade'] >= 2}
        if not good:
            if evs:
                best = max(evs.values(), key=lambda e: e['grade'])
                res['tier'], res['primary'] = 'WEAK', best
                res['reasons'].append('solo hay candidatos OEM F1: ' + ','.join(best['contradictions'] + best['flags']))
            elif P.get('malformed'):
                res['tier'] = 'WEAK'
                res['reasons'].append('el SKU parece un número OEM %s mal escrito; corrige el número primero' % P['malformed'])
            elif P['foreign_major']:
                res['tier'] = 'WEAK'
                res['reasons'].append(('el SKU solo tiene forma OEM de sistemas que no corresponden a la marca %s: %s'
                                       % ('/'.join(ms), ','.join(P['foreign_major']))) if ms else
                                      ('no se nombra la marca; el código tiene forma de %s' % ','.join(P['foreign_major'])))
            elif ms:
                res['tier'] = 'NEEDS_AI'
                res['reasons'].append('sin candidato OEM determinista (código de empresa o aftermarket, sin evidencia local)')
            else:
                res['tier'] = 'NO_MAKE'
                res['reasons'].append('no se nombra la marca del vehículo ni hay un código con forma OEM')
            return res

        sku_keys = [i['key'] for i in sku_interps if i['key'] in good]
        pk = (max(sku_keys, key=lambda k: good[k]['grade']) if sku_keys
              else max(good, key=lambda k: (good[k]['grade'], good[k]['n_independent_attributions'])))
        ev = good[pk]
        res['primary'] = ev
        res['proposed_main'] = ev['manufacturer_form']
        res['written_form'] = ev['written']
        if ev['brand'] == 'HYUNDAI/KIA':
            res['proposed_brand'] = 'HYUNDAI' if 'HYUNDAI' in ms else ('KIA' if 'KIA' in ms else 'HYUNDAI')
        else:
            res['proposed_brand'] = ev['brand']
        res['other_f2_candidates'] = sorted(k for k in good if k != pk)
        strong_others = sorted(k for k, e in good.items() if k != pk and (
            e['grade'] >= 3 or (e['system'] == ev['system'] and e['lexkey'] and e['lexkey'] == ev['lexkey'])))
        alt_tail = any(i['cs']['alt_codes'] for i in sku_interps)
        sku_int = next((i for i in sku_interps if i['key'] == pk), None)
        if sku_int is not None:
            cs = sku_int['cs']
            res['_cs'] = cs
            exact = (not cs['tokens'] and len(P['segs']) == 1 and not P['prefix'] and 'o_as_zero' not in ev['flags']
                     and 'glued_suffix' not in ev['flags'] and sku_int['written'] == up(p['sku']).strip())
            if exact:
                if ev['component_conflict']:
                    tier = 'VARIANT_REVIEW'
                    res['reasons'].append('sustantivo de componente %s en una clase de conjunto (%s): el número OEM es del conjunto'
                                          % (head[0], ev['component_conflict']))
                else:
                    tier = 'CURRENT_IS_OEM' if ev['grade'] >= 3 else 'CURRENT_LIKELY_OEM'
                    res['reasons'].append('el SKU está escrito en el formato OEM de %s (%s)' % (ev['system'], ev['grade_label']))
            else:
                variants, redundant, lexr = list(cs['variants']), [], ev['lexicon']
                for c in sku_int['chain']:
                    e = c['entry']
                    if e.cls != 'VARIANT' or e.kind == 'alt_oem_tail':
                        continue
                    if e.kind == 'position' and lexr.get('side') == 'consistent':
                        redundant.append(c['tok'])
                    elif e.kind == 'size' and c['tok'] == 'STD' and (ev['system'], sku_int['lexkey']) in STD_CLASSES:
                        redundant.append(c['tok'])
                    elif e.kind == 'kit' and head[0] == 'KIT' and lexr.get('top_head') == 'KIT' and lexr.get('result') in ('agree', 'agree_strong'):
                        redundant.append(c['tok'])
                variants = [v for v in variants if v not in redundant]
                res['redundant_variants'] = redundant
                if ev['component_conflict']:
                    tier = 'VARIANT_REVIEW'
                    res['reasons'].append('sustantivo de componente %s en una clase de conjunto (%s)' % (head[0], ev['component_conflict']))
                elif variants:
                    tier = 'VARIANT_REVIEW'
                    res['reasons'].append('sufijo VARIANTE %s: la base %s puede ser otra pieza' % ('-'.join(variants), res['proposed_main']))
                elif cs['unknowns']:
                    tier = 'UNKNOWN_SUFFIX'
                    res['blocking_unknowns'] = sorted(set(cs['unknowns']))
                    res['reasons'].append('el sufijo DESCONOCIDO %s bloquea la base (falta la etiqueta del propietario)' % ','.join(cs['unknowns']))
                elif ev['grade'] >= 3:
                    tier = 'BASE_CLAIM'
                    res['reasons'].append('base OEM + sufijo solo de ETIQUETAS, %s con unidades %s' % (ev['grade_label'], ','.join(ev['units'])))
                else:
                    tier = 'WEAK'
                    res['reasons'].append('base con forma OEM pero solo %s (%s)' % (ev['grade_label'], ','.join(ev['flags']) or 'sin apoyo de clase ni léxico'))
                res['suffix_chain'] = [{'sep': c['sep'], 'tok': c['tok'], **c['entry'].brief()} for c in sku_int['chain']]
                res['dropped_segments'] = P['segs'][1:]
                if P['prefix']:
                    res['prefix_stripped'] = P['prefix']
        else:
            src_its = [i for i in keys[pk] if i['source'].split(':')[0] in ('sku_segment', 'codigo', 'alterno', 'supplier_reference')]
            classes = {i['cs']['chain_class'] for i in src_its}
            first_var = [t for sep, t in tokenize_chain(sku_first, table)[1:] if ALPHA_RE.match(t) and table.classify(sep, t, head).cls == 'VARIANT']
            only_desc = all(i['source'].split(':')[0] == 'description' for i in keys[pk])
            if src_its:
                best_src = max(src_its, key=lambda i: len(i['chain']))
                res['suffix_chain'] = [{'sep': c['sep'], 'tok': c['tok'], **c['entry'].brief()} for c in best_src['chain']]
            if 'variant' in classes or first_var:
                tier = 'VARIANT_REVIEW'
                res['reasons'].append('OEM %s por referencia cruzada, pero el segmento o código de empresa de origen lleva una VARIANTE (%s)'
                                      % (res['proposed_main'], ','.join(first_var + [t for i in src_its for t in i['cs']['variants']])))
            elif 'unknown' in classes:
                tier = 'UNKNOWN_SUFFIX'
                res['blocking_unknowns'] = sorted({t for i in src_its for t in i['cs']['unknowns']})
                res['reasons'].append('OEM %s por referencia cruzada bloqueado por el sufijo DESCONOCIDO %s'
                                      % (res['proposed_main'], ','.join(res['blocking_unknowns'])))
            elif ev['component_conflict']:
                tier = 'VARIANT_REVIEW'
                res['reasons'].append('sustantivo de componente en una clase de conjunto (%s)' % ev['component_conflict'])
            elif only_desc and ev['lexicon'].get('result') not in ('agree', 'agree_strong'):
                tier = 'WEAK'
                res['reasons'].append('la descripción cita %s, pero su clase no coincide con el sustantivo de la pieza (aplica o se '
                                      'relaciona, no es la identidad)' % res['proposed_main'])
            elif ev['grade'] >= 3:
                tier = 'LOCAL_XREF'
                res['reasons'].append('candidato OEM encontrado en %s mientras el SKU es un código de empresa o aftermarket (%s)'
                                      % ('/'.join(sorted(ev['source_counts'])), ev['grade_label']))
            else:
                tier = 'WEAK'
                res['reasons'].append('OEM por referencia cruzada solo %s' % ev['grade_label'])
        if (strong_others or alt_tail) and tier in PRE_CLAIM | {'WEAK'}:
            res['underlying_tier'], tier = tier, 'MULTI_OEM'
            res['other_oem_candidates'] = sorted(set(strong_others) | {norm_key(a) for i in sku_interps for a in i['cs']['alt_codes']})
            res['reasons'].append('varios números OEM (alternos, reemplazos o un número de la misma clase): elegir OEM')
        dv = desc_variant_keywords(p['description'])
        if dv:
            res['warnings'] = ['desc_variant:' + k for k in dv]
        res['tier'] = tier
        return res

    # ---------------------------------------------------------- pass 3 and gates
    def build_claimants(self, results):
        cl = defaultdict(set)
        for p in self.parts:
            r, P = results[p['id']], self.parsed[p['id']]
            if r['tier'] == 'HOLD_LIFECYCLE' or P['lifecycle']:
                continue
            for it in P['interps']:
                if it['source'] in SKU_SOURCES:
                    cl[it['key']].add(p['id'])
            if r.get('primary') and r['tier'] in PRE_CLAIM | {'MULTI_OEM'}:
                cl[r['primary']['key']].add(p['id'])
            for k in r.get('other_oem_candidates') or []:
                cl[k].add(p['id'])
        return cl

    def apply_conflicts(self, p, P, r, cl):
        if r['tier'] not in PRE_CLAIM:
            return
        ev = r['primary']
        kinds = sorted({c['kind'] for c in ev['conflicts']})
        shared = sorted(x for x in cl.get(ev['key'], ()) if x != p['id'])
        if r['tier'] in ('CURRENT_IS_OEM', 'CURRENT_LIKELY_OEM'):
            kinds = [k for k in kinds if k != 'retired_sku_merged_into_this']
            r['_siblings'], shared = shared, []
        if not (kinds or shared):
            return
        r['underlying_tier'], r['tier'] = r['tier'], 'CONFLICT'
        if 'active_sku' in kinds:
            ck = 'base_is_existing_active_part'
        elif 'retired_sku' in kinds:
            ck = 'base_is_retired_part_sku'
        elif 'retired_sku_merged_into_this' in kinds:
            ck = 'base_is_retired_sku_already_merged_into_this_part'
        elif 'partcode' in kinds:
            ck = 'base_is_alterno_of_other_part'
        else:
            ck = 'shared_base_family'
        r['conflict_kind'] = ck
        others = shared + [c['part_id'] for c in ev['conflicts'] if c['kind'] in ('active_sku', 'partcode')]
        r['shared_with'] = [self.by_id[x]['sku'] for x in shared[:10] if x in self.by_id]
        ds, bad = side_of(p['description']), []
        for x in dict.fromkeys(others):
            q = self.by_id.get(x)
            if q and (not head_compat(description_head(q['description']), P['head']) or sides_clash(side_of(q['description']), ds)):
                bad.append(q['sku'])
        r['family_incompatible'] = bad[:10]
        r['reasons'].append('el candidato %s ya está en uso: %s%s' % (ev['key'], ck, (' (miembros INCOMPATIBLES: %s)' % ', '.join(bad[:4])) if bad else ''))

    def common_blockers(self, p, P, r, ev, systems):
        b = []
        if ev['system'] not in systems:
            b.append('system:' + ev['system'])
        if not ev['lexicon_strong']:
            b.append('lexicon_not_strong:' + str(ev['lexicon'].get('result')))
        b += ['flag:' + f for f in ev['flags'] if f in BLOCKING_FLAGS]
        if P['make_source'] != 'description':
            b.append('make_source:' + (P['make_source'] or 'none'))
        ng = len({MAKE_GROUP[m] for m in P['makes']})
        if ng != 1:
            b.append('make_groups:%d' % ng)
        for k in desc_variant_keywords(p['description']):
            if not (k == 'KIT' and P['head'][0] == 'KIT' and ev['lexicon_strong']):
                b.append('desc_variant:' + k)
        ah = assembly_or_kit_head(P['head'], ev['lexicon'])
        if ah:
            b.append('assembly_or_kit_head:' + ah)
        if ev['contradictions']:
            b.append('contradiction:' + ','.join(ev['contradictions']))
        if ev['component_conflict']:
            b.append('component_conflict')
        if r.get('other_f2_candidates'):
            b.append('other_oem_candidates')
        if side_of(p['description']) == 'B' and str(ev['lexicon'].get('side', '')).startswith('class_is_side'):
            b.append('both_sides_on_side_specific_class')
        return b

    def gate(self, p, P, r, confirm):
        t = r['tier']
        if t == 'CURRENT_IS_OEM':
            ev = r['primary']
            b = self.common_blockers(p, P, r, ev, AUTO_FLAG_SYSTEMS)
            ds = side_of(p['description'])
            bad = [self.by_id[x]['sku'] for x in r.get('_siblings', [])
                   if not head_compat(description_head(self.by_id[x]['description']), P['head'])
                   or sides_clash(side_of(self.by_id[x]['description']), ds)]
            if bad:
                b.append('sibling_family_inconsistent')
                r['inconsistent_siblings'] = bad[:6]
            if r.get('_siblings'):
                r['siblings_to_merge_into_this'] = [self.by_id[x]['sku'] for x in r['_siblings'][:10]]
            r['auto_blockers'] = b
            r['tier'] = 'CURRENT_REVIEW' if b else 'AUTO_FLAG_CURRENT'
        elif t == 'BASE_CLAIM':
            ev = r['primary']
            b = self.common_blockers(p, P, r, ev, AUTO_RENAME_SYSTEMS)
            b += ['unconfirmed_tag:' + e.token for e in r['_cs']['tag_entries'] if not eligible(e, confirm)]
            if not ev['second_unit']:
                b.append('no_second_unit')
            if P['segs'][1:]:
                b.append('extra_sku_segment')
            if r.get('redundant_variants'):
                b.append('redundant_variant')
            if side_of(p['description']) in ('R', 'L') and ev['lexicon'].get('side') != 'consistent':
                b.append('side_not_confirmed_by_class')
            r['auto_blockers'] = b
            r['tier'] = 'PROBABLE_BASE' if b else 'AUTO_RENAME_BASE'
        elif t == 'CURRENT_LIKELY_OEM' and r.get('_siblings'):
            r['siblings_to_merge_into_this'] = [self.by_id[x]['sku'] for x in r['_siblings'][:10]]

    def tiers(self, confirm=frozenset()):
        results = {p['id']: self.decide(p, self.parsed[p['id']], confirm) for p in self.parts}
        cl = self.build_claimants(results)
        for p in self.parts:
            self.apply_conflicts(p, self.parsed[p['id']], results[p['id']], cl)
        for p in self.parts:
            self.gate(p, self.parsed[p['id']], results[p['id']], confirm)
        return results

    def scenario_confirm(self):
        """TAG tokens still waiting for the owner's confirmation: confirming them yields STRONG_PENDING_OWNER_TAGS."""
        return frozenset(r['token'] for r in self.table.rows if r['cls'] == 'TAG' and r.get('status') == 'needs_owner_confirmation'
                         and r.get('tag_kind') != 'lifecycle' and r.get('match', 'exact') == 'exact')

    def evaluate(self):
        confirm = self.scenario_confirm()
        results = self.tiers()
        scenario = self.tiers(confirm) if confirm else results
        for pid, r in results.items():
            r['tier_if_owner_confirms'] = scenario[pid]['tier']
            if r['tier'] == 'PROBABLE_BASE' and scenario[pid]['tier'] == 'AUTO_RENAME_BASE':
                r['tier'] = 'STRONG_PENDING_OWNER_TAGS'
                r['pending_owner_tags'] = sorted({x.split(':', 1)[1] for x in r['auto_blockers'] if x.startswith('unconfirmed_tag:')}
                                                 | {t for t in r['primary']['genuine_tokens'] if t in self.table.exact
                                                    and not eligible(self.table.exact[t])})
        self.scenario_tokens = sorted(confirm)
        return results


# =============================================================== reports, review cases and runs
def table_version(table):
    return '%s:%s' % (table.source, table.version) if table.version is not None else table.source


def counts_by_tier(finder, results):
    out = {t: [0, 0] for t in TIER_ORDER}
    for pid in results:
        out[results[pid]['tier']][0] += 1
        out[results[pid]['tier']][1] += finder.avail[pid] > 0
    return out


def report(finder, results, selected):
    """Tier counts (all, in stock) over the whole catalog, the selection, the owner-confirms scenario and blocker breakdowns."""
    def top(tiers, keyfn, n=15):
        c = Counter(k for pid, r in results.items() if r['tier'] in tiers for k in keyfn(r))
        return c.most_common(n)
    scenario = Counter(r['tier_if_owner_confirms'] for r in results.values())
    return {
        'n_parts': len(results), 'n_in_stock': sum(1 for pid in results if finder.avail[pid] > 0),
        'tiers': counts_by_tier(finder, results), 'scope': dict(Counter(results[pid]['tier'] for pid in selected)),
        'scenario': {'tokens': finder.scenario_tokens, 'tiers': dict(scenario)} if finder.scenario_tokens else {},
        'breakdowns': {
            'blockers': top({'CURRENT_REVIEW', 'PROBABLE_BASE', 'STRONG_PENDING_OWNER_TAGS'},
                            lambda r: {x if x.startswith(('system', 'lexicon', 'make_source', 'flag', 'desc_variant')) else x.split(':')[0]
                                       for x in r.get('auto_blockers') or []}, 25),
            'pending_owner_tags': top({'STRONG_PENDING_OWNER_TAGS'}, lambda r: ['+'.join(r['pending_owner_tags'])]),
            'unknown_tokens': top({'UNKNOWN_SUFFIX'}, lambda r: r.get('blocking_unknowns') or []),
            'conflict_kinds': top({'CONFLICT'}, lambda r: [r['conflict_kind'] + (' (incompatible)' if r.get('family_incompatible') else '')]),
            'needs_ai_families': top({'NEEDS_AI'}, lambda r: r['aftermarket_family'] or ['(sin familia)']),
            'auto_systems': top(set(AUTO_TIERS), lambda r: [r['tier'] + ':' + r['primary']['system']]),
        },
        'lexicon': {'classes': len(finder.lex), 'class_keys': sum(len(v) for v in finder.lex.values()), 'model_hints': len(finder.model_hints)},
    }


def select(finder, results, *, in_stock_first=False, limit=None, tier=None):
    """Part ids of the run's scope in priority order: in stock first (when asked), tier order, most stock, SKU."""
    ids = [pid for pid in results if tier is None or results[pid]['tier'] == tier]
    ids.sort(key=lambda pid: (-(finder.avail[pid] > 0) if in_stock_first else 0, TIER_ORDER.index(results[pid]['tier']),
                              -finder.avail[pid], finder.by_id[pid]['sku']))
    return ids[:limit] if limit else ids


def part_snapshot(finder, pid):
    p = finder.by_id[pid]
    return {'sku': p['sku'], 'description': p['description'], 'category': p.get('category') or '',
            'subcategory': p.get('subcategory') or '', 'is_OEM': bool(p['is_OEM']),
            'codes': sorted([c['code'], c['brand'], c['ref_type']] for c in finder.codes_by_part.get(pid, []))}


def case_blockers(r):
    return (list(r.get('auto_blockers') or []) + ['unknown:' + t for t in r.get('blocking_unknowns') or []]
            + (['conflict:' + r['conflict_kind']] if r.get('conflict_kind') else [])
            + (['family_incompatible'] if r.get('family_incompatible') else []))


def evidence(finder, pid, r):
    return evidence_of(r, finder.avail[pid])


def evidence_of(r, avail):
    out = {k: v for k, v in r.items() if not k.startswith('_')}
    out.update(available_quantity=avail, in_stock=avail > 0, tier_label=TIER_LABELS[r['tier']])
    return out


def case_fields(finder, pid, r, version):
    return case_values(r, part_snapshot(finder, pid), finder.avail[pid], version)


def case_values(r, snap, avail, version):
    """The OEMReviewCase fields of a result r (or of a stored evidence dict, which holds the same public keys)."""
    from .matching_engine import digest
    ev = r.get('primary') or {}
    blockers = case_blockers(r)
    candidate = {'main': r.get('proposed_main') or '', 'written': r.get('written_form') or '', 'brand': r.get('proposed_brand') or '',
                 'system': ev.get('system') or '', 'key': ev.get('key') or '', 'others': r.get('other_oem_candidates') or [],
                 'shared_with': r.get('shared_with') or []}
    # Only the suffix entries this decision read (not the table-wide version): labelling an unrelated token keeps decisions.
    suffixes = [r.get('suffix_chain') or [], r.get('pending_owner_tags') or []]
    return {'fingerprint': digest([OEM_FINDER_VERSION, snap, candidate, r['tier'], blockers, suffixes]), 'tier': r['tier'],
            'tier_rank': TIER_ORDER.index(r['tier']), 'underlying_tier': r.get('underlying_tier') or '',
            'candidate': candidate['main'][:200], 'written_form': candidate['written'][:200], 'brand': candidate['brand'][:40],
            'system': candidate['system'], 'grade': ev.get('grade') or 0,
            'chain': '-'.join(c['tok'] for c in r.get('suffix_chain') or [])[:300], 'blockers': blockers,
            'evidence': evidence_of(r, avail), 'snapshot': snap, 'in_stock': avail > 0, 'suffix_table_version': version}


CASE_FIELDS = ['fingerprint', 'tier', 'tier_rank', 'underlying_tier', 'candidate', 'written_form', 'brand', 'system', 'grade', 'chain',
               'blockers', 'evidence', 'snapshot', 'in_stock', 'suffix_table_version', 'run', 'status', 'ai', 'decided_by', 'decided_at',
               'decision', 'updated_at']


def sync_cases(run, finder, results, selected, *, full):
    """Idempotent: an unchanged fingerprint writes nothing (decisions are kept); a changed one replaces the evidence and reopens
    the case; a review case whose Part left the review tiers is resolved (within the run's scope only)."""
    from django.db import transaction
    from django.utils import timezone
    from .oem_finder_models import OEMReviewCase
    version, now, stats = table_version(finder.table), timezone.now(), Counter()
    wanted = {pid: case_fields(finder, pid, results[pid], version) for pid in selected if results[pid]['tier'] in REVIEW_TIERS}
    from .oem_auto import sent_case_fields  # SKUs a person took out of automatic application keep their case while they grade AUTO
    wanted.update(sent_case_fields(finder, results, selected, version))
    existing = {str(c['part_id']): c for c in OEMReviewCase.objects.values('id', 'part_id', 'fingerprint', 'status', 'in_stock', 'blockers')}
    scope = set(selected)
    create, update, stock = [], [], defaultdict(list)
    for pid, row in wanted.items():
        cur = existing.get(pid)
        if cur is None:
            create.append(OEMReviewCase(part_id=pid, run=run, **row))
        elif cur['fingerprint'] != row['fingerprint'] or cur['status'] == 'resolved':
            update.append((cur, row))
        elif cur['in_stock'] != row['in_stock']:
            stock[row['in_stock']].append(cur['id'])
        else:
            stats['unchanged'] += 1
    # A CONFLICT that prefer_oem raised while applying stays open: the finder alone cannot see why the rename was refused.
    stale = [c['id'] for pid, c in existing.items() if c['status'] == 'review' and pid not in wanted and (full or pid in scope)
             and 'apply_conflict' not in (c['blockers'] or [])]
    with transaction.atomic():
        OEMReviewCase.objects.bulk_create(create, batch_size=500)
        stats['created'] = len(create)
        for i in range(0, len(update), 500):
            chunk = update[i:i + 500]
            locked = OEMReviewCase.objects.select_for_update().in_bulk([cur['id'] for cur, _ in chunk])
            objs = []
            for cur, row in chunk:
                obj = locked.get(cur['id'])
                # compare-and-set: a decision recorded since the read keeps its row until the next run
                if obj is None or (obj.fingerprint, obj.status) != (cur['fingerprint'], cur['status']):
                    stats['skipped'] += 1
                    continue
                for k, v in row.items():
                    setattr(obj, k, v)
                obj.run, obj.status, obj.ai, obj.decided_by, obj.decided_at, obj.decision, obj.updated_at = run, 'review', {}, None, None, {}, now
                objs.append(obj)
            OEMReviewCase.objects.bulk_update(objs, CASE_FIELDS, batch_size=500)
            stats['updated'] += len(objs)
        for value, ids in stock.items():
            for i in range(0, len(ids), 1000):
                stats['stock_changed'] += OEMReviewCase.objects.filter(pk__in=ids[i:i + 1000]).update(in_stock=value)
        for i in range(0, len(stale), 1000):
            stats['resolved'] += OEMReviewCase.objects.filter(pk__in=stale[i:i + 1000], status='review').update(status='resolved', updated_at=now)
    return {k: stats[k] for k in ('created', 'updated', 'unchanged', 'stock_changed', 'resolved', 'skipped')}


def execute(*, mode='dry_run', in_stock_first=False, limit=None, tier=None, actor=None, stage='command', store=True, snapshot=None, table=None):
    """One finder pass. The caller holds the matching advisory lock (matching_lock() or the worker pass). store=False writes
    nothing at all; otherwise the run is recorded and the selected review tiers are synced into OEMReviewCase."""
    if mode != 'dry_run':
        raise ValueError('execute() solo simula (dry_run); la aplicación automática es mall.oem_apply.apply_auto.')
    from django.utils import timezone
    from .catalog_suffixes import suffix_table
    t0 = time.monotonic()
    table = table or suffix_table()
    scope = {'in_stock_first': in_stock_first, 'limit': limit, 'tier': tier, 'stage': stage}
    run = None
    if store:
        from .oem_finder_models import OEMFinderRun
        run = OEMFinderRun.objects.create(mode=mode, rules_version=OEM_FINDER_VERSION, suffix_table_version=table_version(table),
                                          scope=scope, actor=actor)
    try:
        snapshot = snapshot or load_snapshot()
        t_load = time.monotonic()
        finder = OEMFinder(snapshot, table)
        results = finder.evaluate()
        t_eval = time.monotonic()
        selected = select(finder, results, in_stock_first=in_stock_first, limit=limit, tier=tier)
        counts = report(finder, results, selected)
        if store:
            from .oem_auto import refresh_after_dry_run
            counts['cases'] = sync_cases(run, finder, results, selected, full=not (limit or tier))
            counts['auto'] = refresh_after_dry_run(run, finder, results)  # 'Pendientes de aplicación automática': tiers are always global
        timings = {'load': round(t_load - t0, 2), 'evaluate': round(t_eval - t_load, 2), 'total': round(time.monotonic() - t0, 2)}
    except Exception as error:
        if run is not None:
            run.status, run.finished_at = 'failed', timezone.now()
            run.errors = [{'error': type(error).__name__, 'detail': str(error)[:500]}]
            run.save(update_fields=['status', 'finished_at', 'errors'])
        raise
    if run is not None:
        run.status, run.counts, run.timings, run.finished_at = 'completed', counts, timings, timezone.now()
        run.save(update_fields=['status', 'counts', 'timings', 'finished_at'])
    return {'run': run, 'counts': counts, 'timings': timings, 'finder': finder, 'results': results, 'selected': selected,
            'suffix_table_version': table_version(table)}


def tables_ready():
    """False until migration 0035 runs: the app container serves this code before the orchestrator migrates."""
    from django.db import connection
    from .oem_finder_models import OEMFinderRun, OEMReviewCase
    return {OEMFinderRun._meta.db_table, OEMReviewCase._meta.db_table} <= set(connection.introspection.table_names())


def import_running():
    from django.utils import timezone
    from .models import CatalogImportJob
    return CatalogImportJob.objects.filter(status='importing', expires_at__gt=timezone.now()).exists()


def worker_stage(actor):
    """Matching worker stage 'oem' (settings.OEM_FINDER_AUTO_STAGE, off by default): a dry run inside the pass, which already
    holds the advisory lock. A failure is logged and recorded on its run; the matching pass continues."""
    import logging
    if not tables_ready():
        logging.getLogger(__name__).warning('OEM finder stage skipped: its tables are not migrated yet.')
        return {'status': 'not_migrated'}
    try:
        outcome = execute(in_stock_first=True, actor=actor, stage='worker')
    except Exception:
        logging.getLogger(__name__).exception('OEM finder stage failed; the matching pass continues.')
        return {'status': 'failed'}
    return {'status': 'completed', 'run': outcome['run'].pk, 'cases': outcome['counts']['cases']}


@contextmanager
def matching_lock():
    """The matching worker's session lock (pg_try_advisory_lock): yields False while a matching pass runs. No-op off PostgreSQL."""
    from .matching_queue import matching_lock as lock, matching_work
    with lock() as acquired, matching_work():
        yield acquired


def result_row(finder, pid, r, rank):
    ev = r.get('primary') or {}
    return {'priority': rank, 'part_id': pid, 'sku': finder.by_id[pid]['sku'], 'description': finder.by_id[pid]['description'][:120],
            'in_stock': finder.avail[pid] > 0, 'available_qty': finder.avail[pid], 'tier': r['tier'],
            'underlying_tier': r.get('underlying_tier'), 'tier_if_owner_confirms': r.get('tier_if_owner_confirms'), 'make': r['makes'],
            'make_source': r['make_source'], 'candidate_oem': r.get('proposed_main'), 'written_form': r.get('written_form'),
            'candidate_brand': r.get('proposed_brand'), 'system': ev.get('system'), 'grade': ev.get('grade_label'),
            'lexicon': (ev.get('lexicon') or {}).get('result'), 'suffix_tokens': [c['tok'] + ':' + c['class'] for c in r.get('suffix_chain') or []],
            'auto_blockers': r.get('auto_blockers'), 'pending_owner_tags': r.get('pending_owner_tags'), 'conflict_kind': r.get('conflict_kind'),
            'family_incompatible': r.get('family_incompatible'), 'other_oem_candidates': r.get('other_oem_candidates'),
            'blocking_unknowns': r.get('blocking_unknowns'), 'warnings': r.get('warnings'), 'reasons': r['reasons']}


def render(outcome):
    c, lines = outcome['counts'], []
    lines.append('Buscador OEM %s · tabla de sufijos %s · %d SKU evaluados (%d con existencias) · %.1f s'
                 % (OEM_FINDER_VERSION, outcome['suffix_table_version'], c['n_parts'], c['n_in_stock'], outcome['timings']['total']))
    lines.append('%-26s %7s %9s %8s' % ('Nivel', 'Total', 'En stock', 'Alcance'))
    for t in TIER_ORDER:
        lines.append('%-26s %7d %9d %8d' % (t, c['tiers'][t][0], c['tiers'][t][1], c['scope'].get(t, 0)))
    if c['scenario']:
        lines.append('Si el propietario confirma %s: %s' % (', '.join(c['scenario']['tokens']), ', '.join(
            '%s %d' % (t, c['scenario']['tiers'][t]) for t in TIER_ORDER if c['scenario']['tiers'].get(t))))
    if 'cases' in c:
        k = c['cases']
        lines.append('Casos de revisión: %d nuevos, %d actualizados, %d sin cambios, %d resueltos, %d con cambio de existencias%s · ejecución %s'
                     % (k['created'], k['updated'], k['unchanged'], k['resolved'], k['stock_changed'],
                        ', %d omitidos por una decisión reciente' % k['skipped'] if k['skipped'] else '', outcome['run'].pk))
    else:
        lines.append('Solo lectura: no se guardaron la ejecución ni los casos.')
    lines += render_auto(c.get('auto'))
    return '\n'.join(lines)


def render_auto(auto):
    """The refresh of 'Pendientes de aplicación automática' (mall.oem_auto) in one line."""
    if not auto:
        return []
    if 'statuses' not in auto:
        return ['Pendientes de aplicación automática: %s.' % ('falta la migración 0038, no se actualizaron' if auto.get('status') == 'not_migrated'
                                                             else 'no se pudieron actualizar; el próximo análisis lo reintenta')]
    st, p = auto['statuses'], auto['pending']
    return ['Pendientes de aplicación automática: %d (%s) · %d aplicados · %d excluidos · %d enviados a revisión · %d ya no automáticos'
            % (st.get('pending', 0), ', '.join('%s %d' % (t, p[t]) for t in APPLY_TIERS if p.get(t)) or 'ninguno', st.get('applied', 0),
               st.get('excluded', 0), st.get('sent_to_review', 0), st.get('stale', 0))]


def render_apply(outcome):
    from .oem_apply import SKIP_LABELS
    lines = []
    if outcome['run'] is None:
        lines.append('Vista previa de la aplicación OEM %s · tabla de sufijos %s · %.1f s (solo lectura: no se escribió nada)'
                     % (OEM_FINDER_VERSION, outcome['suffix_table_version'], outcome['timings']['total']))
        lines.append('%-26s %9s %9s %10s %9s' % ('Nivel', 'Selección', 'Aplicaría', 'Conflictos', 'Omitidos'))
        for t in APPLY_TIERS:
            p = outcome['plan'][t]
            lines.append('%-26s %9d %9d %10d %9d' % (t, p['selected'], p['applied'], p['conflicts'], sum(p['skipped'].values())))
        for t in APPLY_TIERS:
            for ex in outcome['plan'][t]['examples'][:3]:
                lines.append('  conflicto %s: %s -> %s (%s)' % (t, ex['sku'], ex['oem'], ex['detail']))
    else:
        run, s = outcome['run'], outcome['summary']
        lines.append('Aplicación OEM %s · ejecución %d%s · tabla de sufijos %s · %.1f s'
                     % (OEM_FINDER_VERSION, run.pk, ' · canario %d' % run.scope['canary'] if run.scope.get('canary') else '',
                        run.suffix_table_version, run.timings['total']))
        lines.append('Seleccionados %d · aplicados %d (%s) · conflictos %d (a revisión) · omitidos %d · errores %d · lotes %d'
                     % (s['selected'], s['applied'], ', '.join('%s %d' % (t, s['tiers'][t]) for t in APPLY_TIERS if s['tiers'].get(t)) or 'ninguno',
                        s['conflicts'], sum(s['skipped'].values()), s['errors'], len(s['batches'])))
        for reason, n in sorted(s['skipped'].items()):
            lines.append('  omitidos %d: %s' % (n, SKIP_LABELS.get(reason, reason)))
        if s['stopped']:
            lines.append('Detenida antes de terminar: %s.' % {'halted': 'regla de detención activa', 'catalog_import': 'importación del catálogo en curso',
                                                               'errors': 'errores inesperados (se activó la regla de detención)'}[s['stopped']])
        lines.append('Muestra de control: %d filas · GET /api/v1/management/oem-finder/runs/%d/spot-check/ (?download=csv o json)'
                     % (len(s['spot_check']), run.pk))
        lines.append('Deshacer: python -m mall.oem_finder_revert --run %d [--part UUID]' % run.pk)
        lines += render_auto(outcome.get('auto'))
    excluded = outcome.get('excluded') or {}
    if excluded:
        lines.append('Excluidos hasta una decisión humana: %s' % ', '.join('%d %s' % (n, SKIP_LABELS.get(k, k)) for k, n in sorted(excluded.items())))
    return '\n'.join(lines)


def write_spot_check(run, directory):
    from pathlib import Path
    from .oem_apply import spot_check_csv, spot_check_rows
    rows, folder = spot_check_rows(run), Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'oem-run-{run.pk}-spot-check.json').write_text(json.dumps({'run': run.pk, 'rows': rows}, ensure_ascii=False, default=str), encoding='utf-8')
    (folder / f'oem-run-{run.pk}-spot-check.csv').write_text(spot_check_csv(rows), encoding='utf-8-sig')  # BOM: Excel reads the accents


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Buscador OEM determinista: simulación, aplicación automática y regla de detención.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Calcula los niveles y sincroniza la cola de revisión; no renombra nada.')
    mode.add_argument('--apply-auto', action='store_true', help='Aplica los niveles automáticos (con existencias primero, lotes de 100).')
    mode.add_argument('--halt', metavar='MOTIVO', help='Activa la regla de detención: ninguna aplicación automática empieza ni continúa.')
    mode.add_argument('--resume', action='store_true', help='Levanta la regla de detención.')
    parser.add_argument('--in-stock-first', action='store_true', help='Ordena primero los SKU con existencias (la aplicación siempre lo hace).')
    parser.add_argument('--limit', type=int, help='Limita el alcance a los primeros N SKU.')
    parser.add_argument('--tier', choices=TIER_ORDER, help='Limita el alcance a un nivel.')
    parser.add_argument('--canary', type=int, metavar='N', help='--apply-auto: canario que aplica solo N filas (obligatorio antes de los lotes).')
    parser.add_argument('--read-only', action='store_true', help='No escribe nada (funciona sin migrar); con --apply-auto es una vista previa.')
    parser.add_argument('--json', metavar='PATH', help="Escribe los resultados del alcance en JSON ('-' = salida estándar).")
    parser.add_argument('--spot-check-dir', metavar='DIR', help='--apply-auto: también escribe la muestra de control (JSON y CSV) en DIR.')
    options = parser.parse_args(argv)
    for value, name in ((options.limit, '--limit'), (options.canary, '--canary')):
        if value is not None and value < 1:
            parser.error(f'{name} debe ser mayor que cero.')
    if options.apply_auto and options.tier and options.tier not in APPLY_TIERS:
        parser.error('Con --apply-auto, --tier es uno de: ' + ', '.join(APPLY_TIERS) + '.')
    if (options.canary or options.spot_check_dir) and not options.apply_auto:
        parser.error('--canary y --spot-check-dir solo aplican con --apply-auto.')
    if options.canary and options.limit:
        parser.error('Usa --canary o --limit, no ambos.')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    out = stdout or sys.stdout
    log = sys.stderr if options.json == '-' else out
    if options.dry_run and not options.read_only and not tables_ready():
        print('Las tablas del buscador OEM aún no están migradas: aplica las migraciones o usa --read-only.', file=log)
        return 1
    if (options.halt or options.resume or (options.apply_auto and not options.read_only)):
        from .oem_apply import apply_tables_ready
        if not apply_tables_ready():
            print('Las tablas de la aplicación OEM aún no están migradas: aplica las migraciones (o usa --apply-auto --read-only).', file=log)
            return 1
    if options.halt or options.resume:
        from .oem_apply import halt, resume, service_user
        if options.halt:
            row = halt(options.halt, actor=service_user())
            print('Regla de detención activa desde %s: %s' % (row.created_at.isoformat(timespec='seconds'), row.reason), file=log)
        else:
            print('Regla de detención levantada.' if resume(actor=service_user()) else 'No había una regla de detención activa.', file=log)
        return 0
    with matching_lock() as acquired:
        if not acquired:
            print('Hay un análisis de coincidencias o una búsqueda OEM en curso; vuelve a intentarlo cuando termine.', file=log)
            return 2
        if not options.read_only and import_running():  # like a matching pass: never grade a half-imported catalog
            print('Hay una importación del catálogo en curso; vuelve a intentarlo cuando termine.', file=log)
            return 2
        if options.apply_auto:
            from .oem_apply import ApplyRefused, apply_auto
            try:
                outcome = apply_auto(tier=options.tier, limit=options.limit, canary=options.canary, read_only=options.read_only)
            except ApplyRefused as refused:
                print(str(refused), file=log)
                return 3 if refused.reason == 'halted' else 1
        else:
            outcome = execute(in_stock_first=options.in_stock_first, limit=options.limit, tier=options.tier, store=not options.read_only)
    if options.apply_auto and outcome['run'] is not None and outcome['summary']['applied']:
        from .matching_queue import enqueue_matching
        enqueue_matching()  # like any catalog edit: new OEM MAINs and alternos may match pending supplier codes
    print(render_apply(outcome) if options.apply_auto else render(outcome), file=log)
    if options.spot_check_dir and outcome['run'] is not None:
        write_spot_check(outcome['run'], options.spot_check_dir)
    if options.json:
        selected = [pid for pid, _ in outcome['rows']] if options.apply_auto else outcome['selected']
        summary = {**outcome['counts'], 'apply': outcome.get('summary') or outcome.get('plan')} if options.apply_auto else outcome['counts']
        doc = json.dumps({'summary': {**summary, 'timings': outcome['timings'], 'version': OEM_FINDER_VERSION,
                                      'suffix_table_version': outcome['suffix_table_version']},
                          'results': [result_row(outcome['finder'], pid, outcome['results'][pid], i)
                                      for i, pid in enumerate(selected, 1)]}, ensure_ascii=False, default=str)
        if options.json == '-':
            out.write(doc + '\n')
        else:
            with open(options.json, 'w', encoding='utf-8') as f:
                f.write(doc)
    return 0


if __name__ == '__main__':
    sys.exit(main())
