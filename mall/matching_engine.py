"""Shared, indexed matching across the whole catalog and every supplier.

Deterministic evidence can apply a match. Semantic similarity and AI only rank
review candidates. Stock ingestion never waits for an AI response.

When no SKU, alterno or remembered mapping carries a code, the OEM library is
read as a fallback: an OEM number linked to a SKU (by its code, a catalog or an
OEM alterno) or an aftermarket code a catalog files under such a number. It
applies only when it reaches exactly one SKU; duplicates go to review.

A supplier item that no code ties to any SKU (no exact code, remembered mapping, declared or labelled reference, library hit or
code family) is a new product: its case is unmatched and Productos nuevos takes it (owner decision D1, on with the setting
MATCHING_NO_CODE_EVIDENCE). The SKUs its description words find stay on the case only as hints, scored without makes, brands,
years, units or the supplier's own common words, and kept out of the case's fingerprint.
"""
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from .catalog_families import normalized_reference, supplier_base, application, family_candidates
from .catalog_suffixes import HEAD_SYN
from .lines import all_lines, create_line, storefront_lines
from .models import Account, Part, PartCode, SupplierItem, SupplierCodeMapping, MatchingCase, MatchingDecision
from .matching_queue import matching_work
from .oem_links import reach_index, same_make

VERSION = 'matching-3-oem-identity'
# Owner decision D1 (settings.MATCHING_NO_CODE_EVIDENCE): a supplier item no code ties to any SKU is a new product (Productos
# nuevos). Description words never link it; the SKUs they find stay on its case only as hints (possible duplicates for Revisión IA).
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
            # The main (S2b) is the codigo above: left out, so a case's fingerprint does not move when the line gets its main.
            'codes': sorted([{'code': c.code, 'brand': c.brand, 'kind': c.kind, 'ref_type': c.ref_type,
                              'reference_source': c.reference_source} for c in part.codes.all() if not c.is_main],
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


def main_alias(part):
    """A line's branded main (S2b) as a code row, or None. part_row leaves the main out (the codigo already is the row's sku, and a
    case's fingerprint must not move when the line gets its main), but its brand still says whose code the codigo is: a maker's OEM
    number (an OEM line is no unbranded master then) or a company's own code (GMB GWT-41A, X-FEB under FEBEST), as the branded alterno
    with the same code said before S2b."""
    from .references import main_in
    main = main_in(part.codes.all())
    if main is None or not main.brand:
        return None
    return {'code': main.code, 'brand': main.brand, 'kind': main.kind, 'ref_type': main.ref_type, 'reference_source': main.reference_source}


class MatchIndex:
    def __init__(self):
        lines = list(storefront_lines().prefetch_related('codes'))
        self.parts = {str(p.pk): part_row(p) for p in lines}
        self.mains = {str(p.pk): alias for p in lines if (alias := main_alias(p)) is not None}
        self.by_sku = {p['sku']: p for p in self.parts.values()}
        self.codes = defaultdict(set)
        self.literal_codes = defaultdict(set)
        self.words = defaultdict(set)
        self.descriptions = defaultdict(set)
        self.base = defaultdict(set)
        self.all_codes = defaultdict(set)
        self.oem_codes = defaultdict(set)
        self.unbranded_oem_masters = defaultdict(set)
        self.typed_codes = defaultdict(set)
        for row in self.parts.values():
            aliases = self.aliases(row)
            if row.get('is_OEM'):
                normalized = normalized_reference(row['sku'])
                self.oem_codes[normalized].add(row['id'])
                if not any(c['ref_type'] == 'oem' and c['brand'] and normalized_reference(c['code']) == normalized for c in aliases):
                    self.unbranded_oem_masters[normalized].add(row['id'])
            self.codes[(normalized_reference(row['sku']), '')].add(row['id'])
            self.literal_codes[(row['sku'], '')].add(row['id'])
            self.all_codes[normalized_reference(row['sku'])].add(row['id'])
            self.descriptions[description(row['description'])].add(row['id'])
            base = row['sku'] if row.get('is_OEM') else supplier_base(row['sku']) or row['sku']
            self.base[normalized_reference(base)].add(row['id'])
            for word in set(re.findall(r'[A-Z0-9]{3,}', description(row['description']))):
                self.words[word].add(row['id'])
            for alias in aliases:
                normalized = normalized_reference(alias['code'])
                self.codes[(normalized, alias['brand'])].add(row['id'])
                self.literal_codes[(alias['code'], alias['brand'])].add(row['id'])
                self.all_codes[normalized].add(row['id'])
                self.typed_codes[(normalized, alias['brand'], alias['ref_type'])].add(row['id'])
                # A full approved reference also identifies its supplier-suffixed
                # variants, even when the master SKU has a completely different name.
                self.base[normalized].add(row['id'])
                if alias['kind'] == 'oem':
                    self.oem_codes[normalized].add(row['id'])
        # SKUs reached through the OEM library: numbers linked to them and the aftermarket codes filed under those numbers.
        self.reaching, self.numbers, self.aftermarket = reach_index(self.parts)
        # Follow retired parents so reviewed mappings survive future catalog merges.
        self.mappings = {}
        self.portable = defaultdict(set)
        for mapping in SupplierCodeMapping.objects.select_related('part__merged_into'):
            part = mapping.part.merged_into or mapping.part
            if str(part.pk) in self.parts:
                self.mappings[(str(mapping.supplier_id), normalized_reference(mapping.code), mapping.brand)] = (str(part.pk), mapping.description, bool(mapping.actor_id))
                if mapping.actor_id and mapping.brand and detailed(mapping.description):
                    self.portable[(normalized_reference(mapping.code), mapping.brand, description(mapping.description))].add(str(part.pk))
        self.noise = None
        self.common = {}

    def aliases(self, row):
        """A line's codes as matching reads them: its alternos and its branded main (main_alias)."""
        main = self.mains.get(row['id'])
        return row['codes'] + [main] if main else row['codes']

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
        """SKUs by how many of the item's informative words their description shares. Only words few SKUs use count (rare words
        retrieve a bounded set; no full-catalog AI prompt), and an item with fewer than two such words finds nothing. Until D1 is
        switched on (MATCHING_NO_CODE_EVIDENCE) every word of three or more characters counts, as before."""
        if settings.MATCHING_NO_CODE_EVIDENCE:
            words = self.informative(item)
            words = words if len(words) >= HINT_WORDS else set()
        else:
            words = set(WORD.findall(description(item.description)))
        scores = Counter()
        for word in words:
            bucket = self.words.get(word, set())
            if len(bucket) <= HINT_BUCKET:
                scores.update(bucket)
        return scores

    def hints(self, item):
        """Possible duplicates of an item with no code evidence, never evidence themselves: SKUs with the same detailed description,
        then SKUs sharing at least two informative words with it."""
        scores = self.word_scores(item)
        order = lambda pk: (-scores[pk], self.parts[pk]['sku'])
        same = sorted(self.descriptions.get(description(item.description), set()), key=order) if detailed(item.description) else []
        ranked = same + sorted((pk for pk, score in scores.items() if score >= HINT_WORDS and pk not in same), key=order)
        return [self.parts[pk] for pk in ranked[:HINTS]]

    def oem_targets(self, refs):
        return set().union(*(self.reaching.get(ref, set()) for ref in refs))

    def number_targets(self, code, manufacturer=''):
        """SKUs linked to this OEM number; under the manufacturer's make when one is given."""
        return self.oem_targets(ref for ref, maker in self.numbers.get(code, ()) if not manufacturer or same_make(maker, manufacturer))

    def aftermarket_targets(self, code, brand):
        """SKUs reaching an OEM number a catalog prints this brand's code for (GMB GWT-41A -> TOYOTA 1610039315)."""
        entry = self.aftermarket.get((' '.join(brand.upper().split()), code)) if brand else None
        return self.oem_targets(entry[0]) if entry else set()

    def printed_aftermarket(self, codigo, brand):
        entry = self.aftermarket.get((' '.join(brand.upper().split()), normalized_reference(codigo))) if brand else None
        return bool(entry) and ' '.join(codigo.upper().split()) in entry[1]

    def equivalent_targets(self, code, brand='', ref_type='unknown'):
        """A reference through the OEM library: an OEM number linked to a SKU, or a brand's aftermarket code filed under one."""
        if ref_type == 'oem':
            return self.number_targets(code, brand)
        found = self.aftermarket_targets(code, brand)
        if found or ref_type == 'company':
            return found
        return self.number_targets(code, brand) if len(code) >= 8 else set()

    def reference_targets(self, rows):
        targets = set()
        for row in rows:
            code, brand = normalized_reference(row['code']), row.get('brand', '')
            ref_type = row.get('ref_type', 'unknown')
            if ref_type in ['oem', 'company']:
                if brand:
                    found = set(self.typed_codes.get((code, brand, ref_type), set()))
                    if ref_type == 'oem':
                        found |= self.unbranded_oem_masters.get(code, set())
                else:
                    found = set(self.oem_codes.get(code, set())) if ref_type == 'oem' else set()
            elif brand:
                found = self.codes.get((code, brand), set()) | self.codes.get((code, ''), set())
            else:
                found = set(self.all_codes.get(code, set()))
            # The OEM library only answers when no SKU or alterno carries the reference.
            targets |= found or self.equivalent_targets(code, brand, ref_type)
        return targets

    def resolve(self, item):
        """The candidate SKUs, the method, whether the match may apply on its own, and the reason."""
        return self.resolve_case(item)[:4]

    def resolve_case(self, item):
        """resolve() plus the candidates a code reached (None when every candidate did, or while D1 is off, so fingerprints stay as
        they were). The others are description hints, which move with every upload (each supplier's common words, the brands), so
        they stay out of the case's fingerprint (save_case)."""
        code = normalized_reference(item.codigo)
        candidates, reasons = set(), []
        from .catalog_identity import company_reference
        company = company_reference(item.codigo)
        company = company if company and item.brand in ['', company['brand']] else None
        known_company = self.codes.get((normalized_reference(company['code']), company['brand']), set()) if company else set()
        exact = known_company | self.codes.get((code, ''), set()) | self.codes.get((code, item.brand), set()) | self.oem_codes.get(code, set())
        remembered = self.mappings.get((str(item.supplier_id), code, item.brand))
        declared = self.reference_targets(item.references)
        text_refs = self.reference_targets([{'code': ref} for ref in references(item.description)])
        evidenced = exact | declared | text_refs
        if (declared or text_refs) and remembered:
            evidenced.add(remembered[0])
        if len(evidenced) > 1:
            return [self.parts[pk] for pk in sorted(evidenced)][:8], 'conflicting_references', False, \
                'El código y las referencias apuntan a varios SKU. Revisa la equivalencia antes de vincular.', None
        if declared:
            target = self.parts[next(iter(declared))]
            if spec_conflict(item.description, target['description']):
                return [target], 'reference_spec_conflict', False, 'La referencia coincide, pero hay una contradicción de posición, medida o aplicación.', None
            return [target], 'approved_reference', True, 'Referencia declarada coincidente con la biblioteca de equivalencias del SKU maestro.', None
        if remembered:
            target, previous_description, reviewed = remembered
            if (description(previous_description) == description(item.description)
                    and (reviewed or not (exact - {target}))
                    and not spec_conflict(item.description, self.parts[target]['description'])):
                return [self.parts[target]], 'reviewed', True, 'Coincidencia confirmada anteriormente para este proveedor.', None
            candidates.add(target)
            reasons.append('La descripción cambió desde la confirmación anterior.')
        candidates |= exact
        if len(exact) == 1 and not remembered:
            target = self.parts[next(iter(exact))]
            literal = target['id'] in (self.literal_codes.get((item.codigo, ''), set()) | self.literal_codes.get((item.codigo, item.brand), set()))
            if (literal or len(code) >= 8) and not spec_conflict(item.description, target['description']):
                return [target], 'known_code', True, 'Código único del catálogo o alterno aprobado (formato normalizado).', None
            reasons.append('El código coincide, pero las especificaciones requieren revisión.')
        learned = self.portable.get((code, item.brand, description(item.description)), set())
        candidates |= learned
        if len(learned) == 1 and not exact and not remembered:
            target = self.parts[next(iter(learned))]
            if not spec_conflict(item.description, target['description']):
                return [target], 'reviewed_brand_spec', True, 'Código y marca confirmados en otro proveedor con especificaciones idénticas.', None
        reached = set()
        if not exact and not remembered and not learned:
            # The OEM library: the code is an aftermarket code a catalog files under an OEM number a SKU reaches, or that number itself.
            aftermarket = self.aftermarket_targets(code, item.brand)
            if company:
                aftermarket |= self.aftermarket_targets(normalized_reference(company['code']), company['brand'])
            linked = self.number_targets(code) if len(code) >= 8 else set()
            reached = aftermarket | linked
            candidates |= reached
            if len(reached) == 1:
                target = self.parts[next(iter(reached))]
                literal = target['id'] in aftermarket and (self.printed_aftermarket(item.codigo, item.brand)
                                                           or bool(company) and self.printed_aftermarket(company['code'], company['brand']))
                if (literal or len(code) >= 8) and not spec_conflict(item.description, target['description']):
                    if target['id'] in aftermarket:
                        return [target], 'aftermarket_code', True, 'Código de marca de repuesto que un catálogo imprime para un número OEM del SKU.', None
                    return [target], 'linked_oem_number', True, 'El código es un número OEM vinculado al SKU, por su propio código o por un catálogo de repuesto.', None
                reasons.append('El código equivale por número OEM a un SKU, pero las especificaciones o un código corto requieren revisión.')
            elif reached:
                reasons.append('El código equivale por número OEM a varios SKU, posibles duplicados: agrúpalos con Agrupar SKU o elige el correcto.')
        base = supplier_base(item.codigo)
        family = self.base.get(normalized_reference(base or item.codigo), set())
        candidates |= family
        strong = {pk for pk in family if same_spec(item.description, self.parts[pk]['description'])}
        # A family containing contradictory records must not be picked arbitrarily.
        if len(family) == len(strong) == 1 and not exact and not remembered:
            target = self.parts[next(iter(strong))]
            if base or target['sku'] == item.codigo:
                return [target], 'base_and_spec', True, 'Código base completo y aplicación/especificaciones idénticas.', None
        refs = text_refs
        candidates |= refs
        if len(refs) == 1 and not exact and not family and not remembered:
            target = self.parts[next(iter(refs))]
            # OEM references are useful, but contradictory specifications block automatic linking.
            stripped = re.sub(r'(?:OEM|OE|REF(?:ERENCIA)?|REEMPLAZA)\s*[:#=]?\s*[A-Z0-9]+(?:[-./][A-Z0-9]+)+', '', item.description.upper()).strip(' ,;')
            if same_spec(stripped, target['description']):
                return [target], 'explicit_oem', True, 'Referencia OEM explícita y sin contradicciones de posición o medida.', None
        evidence = None
        if settings.MATCHING_NO_CODE_EVIDENCE:
            if not (exact or remembered or declared or text_refs or learned or reached or family):
                return self.hints(item), NO_CODE_EVIDENCE, False, NO_CODE_EVIDENCE_REASON, []
            evidence = [self.parts[pk] for pk in sorted(candidates)]
        candidates |= self.descriptions.get(description(item.description), set()) if detailed(item.description) else set()
        scores = self.word_scores(item)
        ranked = sorted(candidates, key=lambda pk: (-scores[pk], self.parts[pk]['sku']))
        ranked.extend(pk for pk, score in scores.most_common(16) if score >= 2 and pk not in candidates)
        return [self.parts[pk] for pk in ranked[:8]], 'needs_review', False, \
            ' '.join(reasons) or 'No hay evidencia suficiente para vincular automáticamente.', evidence


def save_case(*, key, kind, sources, candidates, target='', method='', reason='', item=None, status='review', evidence=None):
    """A changed fingerprint rewrites the case: new status, AI answer cleared. evidence: the candidates the case rests on when the
    others are description hints (resolve_case). Only those enter the fingerprint, so hints that move with an upload are refreshed
    in place, without reopening a case a person kept apart («Mantener separado») or clearing its AI answer."""
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


def reconcile_items(index, *, stats=None):
    matched = 0
    # A pending import retains its reviewed snapshot until its last batch commits.
    from .supplier_import_models import SupplierInventoryImportJob
    busy = SupplierInventoryImportJob.objects.filter(status='importing', expires_at__gt=timezone.now()).values_list('supplier_id', flat=True)
    items = SupplierItem.objects.exclude(supplier_id__in=busy).exclude(matching_status='matched').select_related('supplier')
    from .variants import pending_view
    for item in items.iterator(chunk_size=500):
        if stats is not None:
            stats['supplier_examined'] = stats.get('supplier_examined', 0) + 1
        # An item whose code change waits is resolved by its new code (codigo_pendiente) and only proposed (S3).
        subject = pending_view(item)
        candidates, method, strong, reason, evidence = index.resolve_case(subject)
        strong = strong and subject is item
        unmatched = not candidates
        if method == NO_CODE_EVIDENCE:
            if item.part_id:
                # A linked item no code ties to a SKU keeps its SKU until a person reviews it, never a new product: its current
                # SKU leads, the hints follow.
                current = index.parts.get(str(item.part_id))
                evidence = [current] if current else []
                candidates = (evidence + [row for row in candidates if row['id'] != str(item.part_id)])[:HINTS]
                method, reason, unmatched = 'needs_review', 'Ningún código lo vincula a un SKU. Se conserva el vínculo actual hasta su revisión.', False
            else:  # Productos nuevos takes it; its candidates are hints, so none is proposed as the target
                unmatched = True
        elif item.part_id and candidates and str(item.part_id) != candidates[0]['id']:
            strong = False
            reason = 'El código cambió de familia. Se conserva el vínculo anterior hasta su revisión.'
        case = save_case(key=f'supplier:{item.pk}', kind='supplier', item=item, sources=[item_row(item)],
                         candidates=candidates, target=candidates[0]['sku'] if candidates and not unmatched else '', method=method,
                         reason=reason, status='unmatched' if unmatched else 'review', evidence=evidence)
        if strong and case.status == 'review':
            try:
                apply_item(case, candidates[0], automatic=True)
                matched += 1
            except ValidationError:
                continue
    return matched


def catalog_families():
    rows = {p.sku: part_row(p) for p in storefront_lines().prefetch_related('codes')}
    bases = {supplier_base(sku) for sku in rows} - {''}
    blocked = set(all_lines().filter(sku__in=bases).values_list('sku', flat=True)) - set(rows)  # a base a line off sale holds
    blocked |= set(PartCode.objects.filter(code__in=bases).values_list('code', flat=True)) - set(rows)
    return family_candidates(rows, blocked)


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
    elif all_lines().filter(sku=case.target_sku).exists():
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


def reconcile_catalog(actor, *, stats=None):
    from .catalog_identity import mark_exact_matches, reconcile_identities
    promoted = reconcile_identities(actor)
    marked = len(mark_exact_matches(actor)) if settings.OEM_EXACT_MARK else 0  # a SKU whose code is in the OEM list is OEM
    if stats is not None:
        stats['promoted_oems'], stats['marked_exact_oems'] = promoted, marked
    count = reconcile_catalog_references(actor)
    from .catalog_split import pair, separated_pairs
    apart = separated_pairs()  # SKUs a split separated (Separar SKU): never merged again here
    for base, family in catalog_families().items():
        # Keep long families intact. The merge service itself has no batch boundary.
        sources, target = family['sources'], family['target']
        if target.get('id'):
            sources = [s for s in sources if pair(s['id'], target['id']) not in apart]
        if not sources:
            continue
        together = not any(pair(a['id'], b['id']) in apart for i, a in enumerate(sources) for b in sources[i + 1:])
        strong = together and not family['warnings'] and all(same_spec(s['description'], target['description']) for s in sources)
        case = save_case(key=f'catalog:{base}', kind='catalog', sources=sources, candidates=[target], target=base,
                         method='base_and_spec' if strong else 'family_review',
                         reason='Código base completo y aplicación/especificaciones idénticas.' if strong else
                         ' '.join(family['warnings']) or ('Confirma la aplicación de esta familia.' if together else
                                                          'Incluye SKU que se separaron con Separar SKU. Confirma cuáles son el mismo repuesto.'))
        if strong and case.status == 'review':
            try:
                apply_family(case, actor=actor, automatic=True)
                count += len(sources)
            except ValidationError as error:
                case.reason = str(error.detail)
                case.save(update_fields=['reason'])
    return count


def reconcile_catalog_references(actor):
    """Resolve legacy catalog variants through references of unrelated masters.

    Only direct links are considered. Cycles and transitive chains require
    review, so a bad alias cannot silently collapse a connected component.
    """
    from .catalog_split import pair, separated_pairs
    apart = separated_pairs()  # SKUs a split separated (Separar SKU): never merged again here
    index = MatchIndex()
    aliases = defaultdict(set)
    for target in index.parts.values():
        for ref in target['codes']:
            aliases[normalized_reference(ref['code'])].add(target['id'])
    plans = []
    for source in index.parts.values():
        base = '' if source.get('is_OEM') else supplier_base(source['sku'])
        direct = aliases.get(normalized_reference(source['sku']), set()) - {source['id']}
        targets = direct | (aliases.get(normalized_reference(base), set()) - {source['id']} if base else set())
        if targets:
            plans.append((source, targets, direct))
    # A legacy company master may own the verified OEM reference while an OEM
    # SKU already exists. Keep the existing OEM as MAIN, not the inverse.
    preferred = {}
    for source, targets, direct in plans:
        if len(targets) == 1 and targets == direct:
            owner = index.parts[next(iter(targets))]
            oem = source.get('is_OEM') or any(ref['ref_type'] == 'oem' and ref['brand'] and ref['reference_source']
                      and normalized_reference(ref['code']) == normalized_reference(source['sku'])
                      for ref in index.aliases(owner))
            if oem:
                preferred.setdefault(owner['id'], (owner, set(), set()))[1].add(source['id'])
                preferred[owner['id']][2].add(source['id'])
                continue
        entry = preferred.setdefault(source['id'], (source, set(), set()))
        entry[1].update(targets); entry[2].update(direct)
    plans = list(preferred.values())
    source_ids = {source['id'] for source, _, _ in plans}
    count = 0
    for source, ids, direct in plans:
        ids = {pk for pk in ids if pair(source['id'], pk) not in apart}
        direct = direct & ids
        if not ids:
            continue
        candidates = sorted((index.parts[pk] for pk in ids), key=lambda row: row['sku'])
        target = candidates[0]
        strong = (len(candidates) == 1 and target['id'] not in source_ids and
                  not spec_conflict(source['description'], target['description']) and
                  (target['id'] in direct or same_spec(source['description'], target['description'])))
        # Refresh snapshots when a preceding source merged into this target.
        candidates = [part_row(p) for p in storefront_lines().filter(pk__in=ids).prefetch_related('codes').order_by('sku')]
        if not candidates:
            continue
        case = save_case(key=f'catalog-reference:{source["id"]}', kind='catalog_reference', sources=[source],
                         candidates=candidates, target=target['sku'], method='catalog_reference',
                         reason='Código reconocido por una referencia equivalente del SKU maestro.' if strong else
                         'Las referencias sugieren equivalencia. Revisa los destinos, las especificaciones y posibles cadenas de equivalencias.')
        if strong and case.status == 'review':
            try:
                apply_family(case, actor=actor, automatic=True)
                count += 1
            except ValidationError as error:
                case.reason = str(error.detail)
                case.save(update_fields=['reason'])
    return count


def create_supplier_families():
    """Seed missing catalog parents from consistent codes across the entire feed.

    Never manufacture a parent from one unrecognized code, a generic description,
    or two contradictory applications. Individual supplier IDs remain children.
    """
    from . import references as reference_rules
    groups = defaultdict(list)
    from .supplier_import_models import SupplierInventoryImportJob
    busy = set(SupplierInventoryImportJob.objects.filter(
        status='importing', expires_at__gt=timezone.now()).values_list('supplier_id', flat=True))
    for item in SupplierItem.objects.filter(part__isnull=True, matching_status='pending').exclude(supplier_id__in=busy):
        if item.references:
            continue  # Resolve declared equivalences before considering a new parent.
        base = supplier_base(item.codigo)
        if base:
            groups[base].append(item)
    created = 0
    for base, items in groups.items():
        if len({i.codigo for i in items}) < 2 or not all(same_spec(i.description, items[0].description) for i in items):
            continue
        # A key another line holds (S2b: any spelling of it, a brandless one reserving it for every brand) is that line's: no parent.
        if (all_lines().filter(Q(sku=base) | Q(sku__in=[i.codigo for i in items])).exists() or PartCode.objects.filter(code=base).exists()
                or reference_rules.conflicts(base, '').exists()):
            continue
        try:
            with transaction.atomic():
                # Recheck under supplier locks before creating anything; no network calls here.
                list(Account.objects.select_for_update().filter(pk__in={i.supplier_id for i in items}).order_by('pk'))
                locked = list(SupplierItem.objects.select_for_update().filter(pk__in=[i.pk for i in items]).order_by('pk'))
                if sorted([item_row(i) for i in items], key=lambda r:r['id']) != sorted([item_row(i) for i in locked], key=lambda r:r['id']):
                    continue
                case_key = f'seed:{base}'
                sources = sorted([item_row(i) for i in items], key=lambda r:r['id'])
                case = save_case(key=case_key, kind='seed', sources=sources, candidates=[], target=base, method='base_and_spec',
                                 reason='Varios códigos de proveedor comparten código base y especificaciones completas.')
                if case.status == 'dismissed':
                    continue
                # strict: a key taken meanwhile refuses the parent (and its case rolls back) instead of a main left in disputa.
                part = create_line(base, origen='proveedor', name=base, description=items[0].description.upper(), strict=True)
                # Matching the children is done by the same resolver, with an audit per item.
                case.status = 'applied'
                case.save(update_fields=['status'])
                MatchingDecision.objects.create(case=case, action='created_parent', evidence={'sources': sources, 'target': part_row(part)})
                created += 1
        except reference_rules.ReferenceConflict:
            continue
    return created
