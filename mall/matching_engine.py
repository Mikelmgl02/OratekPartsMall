"""Shared helpers of the matching (inventory refactor S6: the rules themselves are mall.reference_matching).

Deterministic evidence decides; description words and AI only rank or hint. Stock ingestion never waits for an AI response.

What stays here: the description helpers and spec_conflict (rule 5), the JSON rows a case keeps of an item and a line (item_row,
part_row) and digest; MatchIndex, the description-word index whose hints (possible duplicates, never evidence) Productos nuevos and
Revisión IA read for an item no code ties to any line (owner decision D1); and the legacy cases of the code before S6 (supplier,
catalog, catalog_reference, seed) with their decisions (apply_item, apply_family, save_case, remember) until S10. The worker's renames,
exact marks, automatic family merges and invented parents are gone (S6); catalog references open merge cases
(mall.reference_matching.open_reference_merge_cases).
"""
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from .catalog_families import normalized_reference, application
from .catalog_suffixes import HEAD_SYN
from .lines import all_lines, storefront_lines
from .models import Account, Part, PartCode, SupplierItem, SupplierCodeMapping, MatchingCase, MatchingDecision

VERSION = 'matching-4-references'  # S6: the engine on References (plan §3 S6.1)
# Owner decision D1 (S0.9, kept by S6's rule 4): a supplier item no code ties to any line is a new product (Productos nuevos).
# Description words never link it; the lines they find are only hints (possible duplicates for Revisión IA). The legacy supplier
# cases of the code before S6 carry this method.
NO_CODE_EVIDENCE = 'no_code_evidence'
NO_CODE_EVIDENCE_REASON = 'Sin evidencia de código: va a Productos nuevos'
WORD = re.compile(r'[A-Z0-9]{3,}')
YEAR = re.compile(r'^(?:19|20)\d\d$')
UNIT = re.compile(r'^\d+(?:MM|CM|MTS?|V|W|KW|A|AH|HP|CC|ML|L|LTS?|GR?|KGS?|LBS?|PSI|PCS|PZS?|OZ)$')
UNIT_WORDS = {'PCS', 'PZS', 'PZA', 'PZAS', 'LTS', 'LBS', 'KGS', 'MTS', 'UND', 'UNID', 'VOLT', 'VOLTS', 'WATTS', 'AMP', 'GAL', 'GALON',
              'LITRO', 'LITROS'}
HINTS = 8               # hints kept on a case
HINT_BUCKET = 200       # a word more SKUs than this describe retrieves nothing
HINT_WORDS = 2          # a hint shares at least this many informative words with the item
COMMON_SHARE = 0.05     # any word in more than this share of one supplier's items hints at nothing: its models (FX11, GS11...)
COMMON_MIN = 20         # and its common part words (PUERTA, MOLDURA...); only above this many items, so a small supplier keeps its words


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def description(value):
    text = unicodedata.normalize('NFKD', value.upper())
    return ' '.join(''.join(c for c in text if not unicodedata.combining(c)).split())


def detailed(value):
    text = description(value)
    return len(text.split()) >= 3 and any(c.isdigit() for c in text)


def same_spec(a, b):
    return detailed(a) and description(a) == description(b)


def part_row(part):
    return {'id': str(part.pk), 'sku': part.sku, 'name': part.name, 'description': part.description,
            **({'is_OEM': True} if part.is_OEM else {}),
            'category': part.category, 'subcategory': part.subcategory,
            'technical_revision': part.technical_revision,
            'active': part.active, 'merged_into': str(part.merged_into_id or ''),
            # The main (S2b) is the codigo above: left out, so a case's fingerprint does not move when the line gets its main; so are the
            # library numbers S4 puts on the line (detail.origen biblioteca), which are library data, not the line's own codes.
            'codes': sorted([{'code': c.code, 'brand': c.brand, 'kind': c.kind, 'ref_type': c.ref_type,
                              'reference_source': c.reference_source} for c in part.codes.all()
                             if not c.is_main and (c.detail or {}).get('origen') != 'biblioteca'],
                            key=lambda c: (c['brand'], c['code']))}


def item_row(item):
    row = {'id': str(item.pk), 'supplier': str(item.supplier_id), 'supplier_invent_id': item.supplier_invent_id,
           'sku': item.codigo, 'brand': item.brand, 'description': item.description, 'references': item.references,
           'part_id': str(item.part_id or ''), 'matching_status': item.matching_status}
    # A code change waiting for review (S3): only then, so the other cases' fingerprints stay as they were.
    if getattr(item, 'codigo_pendiente', None) is not None or getattr(item, 'brand_pendiente', None) is not None:
        row.update(codigo_pendiente=item.codigo_pendiente, brand_pendiente=item.brand_pendiente)
    return row


def references(text):
    # Only explicitly labelled OEM/replacement references are automatic evidence.
    return {normalized_reference(m) for m in re.findall(
        r'(?:OEM|OE|REF(?:ERENCIA)?|REEMPLAZA)\s*[:#=]?\s*([A-Z0-9]+(?:[-./][A-Z0-9]+)+)', text.upper())
        if len(normalized_reference(m)) >= 8}


SIDES = {**dict.fromkeys(['IZQ', 'IZQUIERDO', 'IZQUIERDA', 'LH', 'LEFT'], ('side', 'left')),
         **dict.fromkeys(['DER', 'DERECHO', 'DERECHA', 'RH', 'RIGHT'], ('side', 'right')),
         **dict.fromkeys(['DEL', 'DELANTERO', 'DELANTERA', 'FRONT'], ('axle', 'front')),
         **dict.fromkeys(['TRAS', 'TRASERO', 'TRASERA', 'REAR'], ('axle', 'rear'))}
KIT_WORDS = {'KIT', 'JUEGO', 'SET', 'PAR'}
SET_WORDS = {'JUEGO', 'SET'}           # pads and shoes are always sold as a set: between two pads «SET 4 PCS» or «JUEGO» is no kit
SOLD_AS_SET = {'PASTILLA', 'ZAPATA'}
TYPE_NOUNS = {'TAMBOR', 'DISCO', 'PASTILLA', 'ZAPATA', 'FILTRO', 'BUJIA', 'AMORTIGUADOR', 'ROTULA'}
PLURALS = {'TAMBORES': 'TAMBOR', 'DISCOS': 'DISCO', 'PASTILLAS': 'PASTILLA', 'ZAPATAS': 'ZAPATA', 'FILTROS': 'FILTRO',
           'BUJIAS': 'BUJIA', 'AMORTIGUADORES': 'AMORTIGUADOR', 'AMORTS': 'AMORT', 'ROTULAS': 'ROTULA'}


def type_noun(word):
    """A description word as a part-type noun, through the plural and the ERP abbreviations (AMORTIGUADOR = AMORT)."""
    word = PLURALS.get(word, word)
    return HEAD_SYN.get(word, word)


TYPES = {type_noun(word) for word in TYPE_NOUNS}


def sides(text):
    """The sides and axles a description names. One that names both of a pair, joined or not («LH/RH», «IZQ Y DER», «DEL LH RH»,
    «LH,RH»), is a part for either one (or reads DEL as the word «del»: «DEL PASO DE RUEDA TRASERA»), so it names neither."""
    named = {SIDES[token] for token in re.findall(r'[A-Z0-9]+', re.sub(r'\b([LR])/H\b', r'\1H', text)) if token in SIDES}
    return {value for value in named if not any(other[0] == value[0] and other != value for other in named)}


def part_types(words):
    """The part types a description names."""
    return {type_noun(word) for word in words} & TYPES


def spec_conflict(a, b):
    """Detect explicit contradictions; missing specifications are not evidence."""
    a, b = description(a), description(b)
    wa, wb = set(re.findall(r'\w+', a)), set(re.findall(r'\w+', b))
    ta, tb = part_types(wa), part_types(wb)
    kit = KIT_WORDS - (SET_WORDS if ta & tb & SOLD_AS_SET else set())  # both are pads, or both shoes
    if bool(wa & kit) != bool(wb & kit):
        return True
    sb = sides(b)
    for first in sides(a):
        if any(first[0] == second[0] and first != second for second in sb):
            return True
    for pattern in [r'\b\d+(?:[.,]\d+)?\s*(?:MM|CM|V|W)\b', r'\b\d+\s*(?:DIENTES|ESTRIAS|HUECOS)\b']:
        va, vb = set(re.findall(pattern, a)), set(re.findall(pattern, b))
        if va and vb and va != vb:
            return True
    _, years_a = application({'description': a})
    _, years_b = application({'description': b})
    if years_a and years_b and max(years_a[0], years_b[0]) > min(years_a[1], years_b[1]):
        return True
    if ta and tb and ta != tb:
        return True
    return False


def noise_words():
    """Words that name a vehicle make (TOY, NIS, KAIYI...) or a brand of the suppliers' items or the catalog's alternos (DFSK, FEBEST,
    GMB...): many products share them, so they hint at none. Only one-word brands: ELECTRO AIRE or JAPAN PARTS keep AIRE and PARTS."""
    from .oem_finder import MAKE_TOKENS
    from .oem_links import EXTRA_MAKES
    from .sku_proposals import vehicle_makes
    makes = vehicle_makes()
    words = set(MAKE_TOKENS) | set(EXTRA_MAKES) | set(makes) | set(makes.values())
    brands = (set(SupplierItem.objects.order_by().values_list('brand', flat=True).distinct())
              | set(PartCode.objects.order_by().values_list('brand', flat=True).distinct()))
    for brand in brands:
        tokens = description(brand).split()
        if len(tokens) == 1:
            words.add(tokens[0])
    return words


class MatchIndex:
    """The description words of the storefront's lines, for hints only (owner decision D1): the possible duplicates of an item no code
    ties to any line, which Productos nuevos and Revisión IA show (mall.reference_matching never links by them)."""

    def __init__(self):
        lines = list(storefront_lines())
        self.parts = {str(p.pk): part_row_light(p) for p in lines}
        self.words = defaultdict(set)
        self.descriptions = defaultdict(set)
        for row in self.parts.values():
            self.descriptions[description(row['description'])].add(row['id'])
            for word in set(re.findall(r'[A-Z0-9]{3,}', description(row['description']))):
                self.words[word].add(row['id'])
        self.noise = None
        self.common = {}

    def common_words(self, supplier_id):
        """Any word in more than 5% of one supplier's items (and in more than 20 of them): its vehicle models (FX11, GS11, PHEV...),
        but also its common part words (PUERTA, MOLDURA, AMORT...) and what it writes on everything."""
        key = str(supplier_id)
        if key not in self.common:
            counts, total = Counter(), 0
            for text in SupplierItem.objects.filter(supplier_id=supplier_id).values_list('description', flat=True):
                total += 1
                counts.update(set(WORD.findall(description(text))))
            limit = max(total * COMMON_SHARE, COMMON_MIN)
            self.common[key] = {word for word, count in counts.items() if count > limit}
        return self.common[key]

    def informative(self, item):
        """The item's description words that can hint at a product: not a make or brand, a year, a unit or a word its supplier puts
        on many items."""
        common = self.common_words(item.supplier_id)
        if self.noise is None:
            self.noise = noise_words()
        return {word for word in WORD.findall(description(item.description))
                if word not in self.noise and word not in common and word not in UNIT_WORDS and not YEAR.match(word) and not UNIT.match(word)}

    def word_scores(self, item):
        """Lines by how many of the item's informative words their description shares. Only words few lines use count (rare words
        retrieve a bounded set; no full-catalog AI prompt), and an item with fewer than two such words finds nothing."""
        words = self.informative(item)
        words = words if len(words) >= HINT_WORDS else set()
        scores = Counter()
        for word in words:
            bucket = self.words.get(word, set())
            if len(bucket) <= HINT_BUCKET:
                scores.update(bucket)
        return scores

    def hints(self, item):
        """Possible duplicates of an item with no code evidence, never evidence themselves: lines with the same detailed description,
        then lines sharing at least two informative words with it."""
        scores = self.word_scores(item)
        order = lambda pk: (-scores[pk], self.parts[pk]['sku'])
        same = sorted(self.descriptions.get(description(item.description), set()), key=order) if detailed(item.description) else []
        ranked = same + sorted((pk for pk, score in scores.items() if score >= HINT_WORDS and pk not in same), key=order)
        return [self.parts[pk] for pk in ranked[:HINTS]]


def part_row_light(part):
    """A line's row for hints (no codes: a hint is never code evidence)."""
    return {'id': str(part.pk), 'sku': part.sku, 'name': part.name, 'description': part.description,
            'category': part.category, 'subcategory': part.subcategory}


def save_case(*, key, kind, sources, candidates, target='', method='', reason='', item=None, status='review', evidence=None):
    """A legacy case (supplier, catalog, catalog_reference: the writers before S6 that remain, such as a brand rename's collision, a
    main whose key another line holds, S2b's compact duplicates). A changed fingerprint rewrites the case: new status, AI answer
    cleared. evidence: the candidates the case rests on when the others are description hints; only those enter the fingerprint, so
    hints that move are refreshed in place, without reopening a case a person kept apart («Mantener separado»). The S6 kinds are
    written by mall.reference_matching.open_case."""
    fingerprint = digest([VERSION, sources, candidates, target, method] if evidence is None else [VERSION, sources, evidence, method])
    case, created = MatchingCase.objects.get_or_create(key=key, defaults={
        'kind': kind, 'item': item, 'sources': sources, 'candidates': candidates, 'target_sku': target,
        'method': method, 'reason': reason, 'fingerprint': fingerprint, 'status': status})
    if created:
        return case
    if case.fingerprint != fingerprint:
        case.sources, case.candidates, case.target_sku = sources, candidates, target
        case.method, case.reason, case.fingerprint, case.status, case.ai = method, reason, fingerprint, status, {}
        case.save()
    elif evidence is not None and case.status in ['review', 'unmatched', 'dismissed'] and (case.candidates, case.target_sku) != (candidates, target):
        MatchingCase.objects.filter(pk=case.pk, fingerprint=fingerprint).update(candidates=candidates, target_sku=target)
        case.candidates, case.target_sku = candidates, target
    return case


def remember(item, part, actor):
    """A supplier's code confirmed on a line: a ReferenceSource(proveedor) on the Reference the item hangs from (S3), and the legacy
    SupplierCodeMapping the matching reads until S6, which the source cites ('Código aprendido · <proveedor> · <fecha>', as S2b's
    backfill turned the earlier ones into sources)."""
    mapping, _ = SupplierCodeMapping.objects.update_or_create(supplier_id=item.supplier_id, code=normalized_reference(item.codigo), brand=item.brand,
        defaults={'part': part, 'description': item.description, 'actor': actor})
    if item.reference_id and item.part_id == getattr(part, 'pk', part):
        from . import references
        citation = f'Código aprendido · {item.supplier.name} · {mapping.created_at.date().isoformat()}'
        references.cite(item.reference, citation, actor if getattr(actor, 'pk', None) else None, kind='proveedor',
                        detail={'legacy': 'SupplierCodeMapping', 'mapping': mapping.pk, 'supplier': str(item.supplier_id),
                                'code': mapping.code, 'brand': mapping.brand})
    return mapping


@transaction.atomic
def apply_item(case, target, *, actor=None, automatic=False):
    source = case.sources[0]
    # Same order as stock imports: account -> catalog -> inventory -> review case.
    Account.objects.select_for_update().get(pk=case.item.supplier_id)
    from .supplier_import_models import SupplierInventoryImportJob
    if SupplierInventoryImportJob.objects.filter(supplier_id=case.item.supplier_id, status='importing', expires_at__gt=timezone.now()).exists():
        raise ValidationError('Espera a que termine la importación de este proveedor.')
    part = Part.objects.select_for_update().get(pk=target['id'])
    item = SupplierItem.objects.select_for_update().get(pk=case.item_id)
    locked = MatchingCase.objects.select_for_update().get(pk=case.pk)
    if locked.fingerprint != case.fingerprint or item_row(item) != source or part_row(part) != target:
        raise ValidationError('Los datos cambiaron. Actualiza las coincidencias antes de confirmar.')
    if not part.active or part.merged_into_id:
        raise ValidationError('El SKU de destino ya no está disponible.')
    if locked.status in ['applied', 'dismissed']:
        return
    if automatic and item.part_id and str(item.part_id) != target['id']:
        return  # A changed supplier code cannot silently move historical stock to another part.
    if automatic and (item.codigo_pendiente is not None or item.brand_pendiente is not None):
        return  # a code change waiting for review is only proposed (S3), never applied on its own
    from .variants import VariantConflict, vincular
    try:
        vincular(item, part, status='matched', actor=actor)  # the line's Reference for its code, a waiting code applied (S3)
    except VariantConflict as error:
        raise ValidationError(error.message)
    remember(item, part, actor)
    # A supplier's own code remains on its stock item and scoped mapping.
    # Only catalog management can approve universal cross-references.
    locked.status = 'applied'
    locked.target_sku = part.sku
    locked.save(update_fields=['status', 'target_sku', 'updated_at'])
    MatchingDecision.objects.create(case=locked, actor=actor, action='automatic' if automatic else 'approved',
        evidence={'sources': case.sources, 'target': target, 'method': case.method, 'fingerprint': case.fingerprint})


@transaction.atomic
def apply_family(case, *, actor, automatic=False, selected_target=None):
    """Approve a catalog case (a family, a reference, a compact duplicate or a shared OEM number): the merge it proposes, through
    merge_catalog_parts and mall.line_merge.merge_lines, if its lines are still as the case saw them."""
    from . import line_merge
    from .catalog_grouping import merge_catalog_parts
    ids = [s['id'] for s in case.sources]
    target = selected_target or case.candidates[0]
    lock_ids = ids + ([target['id']] if target.get('id') else [])
    # The suppliers of every line involved, then the lines (plan §1.6.5, the order of ingestion); refused while one is importing.
    locked = line_merge.lock_lines(lock_ids)
    current = {str(pk): part_row(part) for pk, part in locked.items() if str(pk) in lock_ids}
    if any(current.get(s['id']) != s for s in case.sources):
        raise ValidationError('La familia cambió. Actualiza el análisis.')
    if target.get('id'):
        if current.get(target['id']) != target:
            raise ValidationError('El SKU padre cambió. Actualiza el análisis.')
    elif all_lines().filter(sku=case.target_sku).exclude(estado='referencia').exists():  # a line in referencia is promoted (S4)
        raise ValidationError('El SKU padre ya existe. Actualiza el análisis.')
    locked = MatchingCase.objects.select_for_update().get(pk=case.pk)
    if locked.status != 'review' or locked.fingerprint != case.fingerprint:
        return
    taxonomy = {}
    for field in ['category', 'subcategory']:
        values = {s.get(field) for s in case.sources if s.get(field)}
        taxonomy[field] = target.get(field) or (next(iter(values)) if len(values) == 1 else '')
    result = merge_catalog_parts(actor=actor, target_sku=target['sku'], source_skus=[s['sku'] for s in case.sources],
                                 create_target=not target.get('id'), automatic=automatic, **taxonomy)  # mall.line_merge.merge_lines
    locked.status = 'applied'
    locked.target_sku = target['sku']
    locked.save(update_fields=['status', 'target_sku', 'updated_at'])
    MatchingDecision.objects.create(case=locked, actor=actor, action='automatic' if automatic else 'approved',
        evidence={'sources': case.sources, 'target': target, 'method': case.method, 'merge_id': result['merge_id']})
