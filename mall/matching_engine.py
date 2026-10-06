"""Shared, indexed matching across the whole catalog and every supplier.

Deterministic evidence can apply a match. Semantic similarity and AI only rank
review candidates. Stock ingestion never waits for an AI response.
"""
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from .catalog_families import normalized_reference, supplier_base, application, family_candidates
from .models import Account, Part, PartCode, SupplierItem, SupplierCodeMapping, MatchingCase, MatchingDecision
from .matching_queue import matching_work

VERSION = 'matching-3-oem-identity'


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
            'codes': sorted([{'code': c.code, 'brand': c.brand, 'kind': c.kind, 'ref_type': c.ref_type,
                              'reference_source': c.reference_source} for c in part.codes.all()],
                            key=lambda c: (c['brand'], c['code']))}


def item_row(item):
    return {'id': str(item.pk), 'supplier': str(item.supplier_id), 'supplier_invent_id': item.supplier_invent_id,
            'sku': item.codigo, 'brand': item.brand, 'description': item.description, 'references': item.references,
            'part_id': str(item.part_id or ''), 'matching_status': item.matching_status}


def references(text):
    # Only explicitly labelled OEM/replacement references are automatic evidence.
    return {normalized_reference(m) for m in re.findall(
        r'(?:OEM|OE|REF(?:ERENCIA)?|REEMPLAZA)\s*[:#=]?\s*([A-Z0-9]+(?:[-./][A-Z0-9]+)+)', text.upper())
        if len(normalized_reference(m)) >= 8}


def spec_conflict(a, b):
    """Detect explicit contradictions; missing specifications are not evidence."""
    a, b = description(a), description(b)
    pairs = [({'IZQ', 'IZQUIERDO', 'IZQUIERDA', 'LH', 'LEFT'}, {'DER', 'DERECHO', 'DERECHA', 'RH', 'RIGHT'}),
             ({'DEL', 'DELANTERO', 'DELANTERA', 'FRONT'}, {'TRAS', 'TRASERO', 'TRASERA', 'REAR'})]
    wa, wb = set(re.findall(r'\w+', a)), set(re.findall(r'\w+', b))
    if bool(wa & {'KIT', 'JUEGO', 'SET', 'PAR'}) != bool(wb & {'KIT', 'JUEGO', 'SET', 'PAR'}):
        return True
    for left, right in pairs:
        if (wa & left and wb & right) or (wa & right and wb & left):
            return True
    for pattern in [r'\b\d+(?:[.,]\d+)?\s*(?:MM|CM|V|W)\b', r'\b\d+\s*(?:DIENTES|ESTRIAS|HUECOS)\b']:
        va, vb = set(re.findall(pattern, a)), set(re.findall(pattern, b))
        if va and vb and va != vb:
            return True
    _, years_a = application({'description': a})
    _, years_b = application({'description': b})
    if years_a and years_b and max(years_a[0], years_b[0]) > min(years_a[1], years_b[1]):
        return True
    types = {'TAMBOR', 'DISCO', 'PASTILLA', 'ZAPATA', 'FILTRO', 'BUJIA', 'AMORTIGUADOR', 'ROTULA'}
    if wa & types and wb & types and wa & types != wb & types:
        return True
    return False


class MatchIndex:
    def __init__(self):
        self.parts = {str(p.pk): part_row(p) for p in Part.objects.filter(active=True, merged_into__isnull=True).prefetch_related('codes')}
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
            if row.get('is_OEM'):
                normalized = normalized_reference(row['sku'])
                self.oem_codes[normalized].add(row['id'])
                if not any(c['ref_type'] == 'oem' and c['brand'] and normalized_reference(c['code']) == normalized for c in row['codes']):
                    self.unbranded_oem_masters[normalized].add(row['id'])
            self.codes[(normalized_reference(row['sku']), '')].add(row['id'])
            self.literal_codes[(row['sku'], '')].add(row['id'])
            self.all_codes[normalized_reference(row['sku'])].add(row['id'])
            self.descriptions[description(row['description'])].add(row['id'])
            base = row['sku'] if row.get('is_OEM') else supplier_base(row['sku']) or row['sku']
            self.base[normalized_reference(base)].add(row['id'])
            for word in set(re.findall(r'[A-Z0-9]{3,}', description(row['description']))):
                self.words[word].add(row['id'])
            for alias in row['codes']:
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
        # Follow retired parents so reviewed mappings survive future catalog merges.
        self.mappings = {}
        self.portable = defaultdict(set)
        for mapping in SupplierCodeMapping.objects.select_related('part__merged_into'):
            part = mapping.part.merged_into or mapping.part
            if str(part.pk) in self.parts:
                self.mappings[(str(mapping.supplier_id), normalized_reference(mapping.code), mapping.brand)] = (str(part.pk), mapping.description, bool(mapping.actor_id))
                if mapping.actor_id and mapping.brand and detailed(mapping.description):
                    self.portable[(normalized_reference(mapping.code), mapping.brand, description(mapping.description))].add(str(part.pk))

    def reference_targets(self, rows):
        targets = set()
        for row in rows:
            code, brand = normalized_reference(row['code']), row.get('brand', '')
            ref_type = row.get('ref_type', 'unknown')
            if ref_type in ['oem', 'company']:
                if brand:
                    targets |= self.typed_codes.get((code, brand, ref_type), set())
                    if ref_type == 'oem':
                        targets |= self.unbranded_oem_masters.get(code, set())
                else:
                    targets |= self.oem_codes.get(code, set()) if ref_type == 'oem' else set()
            elif brand:
                targets |= self.codes.get((code, brand), set()) | self.codes.get((code, ''), set())
            else:
                targets |= self.all_codes.get(code, set())
        return targets

    def resolve(self, item):
        code = normalized_reference(item.codigo)
        candidates, reasons = set(), []
        from .catalog_identity import company_reference
        company = company_reference(item.codigo)
        known_company = self.codes.get((normalized_reference(company['code']), company['brand']), set()) if company and item.brand in ['', company['brand']] else set()
        exact = known_company | self.codes.get((code, ''), set()) | self.codes.get((code, item.brand), set()) | self.oem_codes.get(code, set())
        remembered = self.mappings.get((str(item.supplier_id), code, item.brand))
        declared = self.reference_targets(item.references)
        text_refs = self.reference_targets([{'code': ref} for ref in references(item.description)])
        evidenced = exact | declared | text_refs
        if (declared or text_refs) and remembered:
            evidenced.add(remembered[0])
        if len(evidenced) > 1:
            return [self.parts[pk] for pk in sorted(evidenced)][:8], 'conflicting_references', False, \
                'El código y las referencias apuntan a varios SKU. Revisa la equivalencia antes de vincular.'
        if declared:
            target = self.parts[next(iter(declared))]
            if spec_conflict(item.description, target['description']):
                return [target], 'reference_spec_conflict', False, 'La referencia coincide, pero hay una contradicción de posición, medida o aplicación.'
            return [target], 'approved_reference', True, 'Referencia declarada coincidente con la biblioteca de equivalencias del SKU maestro.'
        if remembered:
            target, previous_description, reviewed = remembered
            if (description(previous_description) == description(item.description)
                    and (reviewed or not (exact - {target}))
                    and not spec_conflict(item.description, self.parts[target]['description'])):
                return [self.parts[target]], 'reviewed', True, 'Coincidencia confirmada anteriormente para este proveedor.'
            candidates.add(target)
            reasons.append('La descripción cambió desde la confirmación anterior.')
        candidates |= exact
        if len(exact) == 1 and not remembered:
            target = self.parts[next(iter(exact))]
            literal = target['id'] in (self.literal_codes.get((item.codigo, ''), set()) | self.literal_codes.get((item.codigo, item.brand), set()))
            if (literal or len(code) >= 8) and not spec_conflict(item.description, target['description']):
                return [target], 'known_code', True, 'Código único del catálogo o alterno aprobado (formato normalizado).'
            reasons.append('El código coincide, pero las especificaciones requieren revisión.')
        learned = self.portable.get((code, item.brand, description(item.description)), set())
        candidates |= learned
        if len(learned) == 1 and not exact and not remembered:
            target = self.parts[next(iter(learned))]
            if not spec_conflict(item.description, target['description']):
                return [target], 'reviewed_brand_spec', True, 'Código y marca confirmados en otro proveedor con especificaciones idénticas.'
        base = supplier_base(item.codigo)
        family = self.base.get(normalized_reference(base or item.codigo), set())
        candidates |= family
        strong = {pk for pk in family if same_spec(item.description, self.parts[pk]['description'])}
        # A family containing contradictory records must not be picked arbitrarily.
        if len(family) == len(strong) == 1 and not exact and not remembered:
            target = self.parts[next(iter(strong))]
            if base or target['sku'] == item.codigo:
                return [target], 'base_and_spec', True, 'Código base completo y aplicación/especificaciones idénticas.'
        refs = text_refs
        candidates |= refs
        if len(refs) == 1 and not exact and not family and not remembered:
            target = self.parts[next(iter(refs))]
            # OEM references are useful, but contradictory specifications block automatic linking.
            stripped = re.sub(r'(?:OEM|OE|REF(?:ERENCIA)?|REEMPLAZA)\s*[:#=]?\s*[A-Z0-9]+(?:[-./][A-Z0-9]+)+', '', item.description.upper()).strip(' ,;')
            if same_spec(stripped, target['description']):
                return [target], 'explicit_oem', True, 'Referencia OEM explícita y sin contradicciones de posición o medida.'
        candidates |= self.descriptions.get(description(item.description), set()) if detailed(item.description) else set()
        # Rare tokens retrieve a bounded candidate set; no full-catalog AI prompt.
        scores = Counter()
        for word in set(re.findall(r'[A-Z0-9]{3,}', description(item.description))):
            bucket = self.words.get(word, set())
            if len(bucket) <= 200:
                scores.update(bucket)
        ranked = sorted(candidates, key=lambda pk: (-scores[pk], self.parts[pk]['sku']))
        ranked.extend(pk for pk, score in scores.most_common(16) if score >= 2 and pk not in candidates)
        return [self.parts[pk] for pk in ranked[:8]], 'needs_review', False, ' '.join(reasons) or 'No hay evidencia suficiente para vincular automáticamente.'


def save_case(*, key, kind, sources, candidates, target='', method='', reason='', item=None, status='review'):
    fingerprint = digest([VERSION, sources, candidates, target, method])
    case, created = MatchingCase.objects.get_or_create(key=key, defaults={
        'kind': kind, 'item': item, 'sources': sources, 'candidates': candidates, 'target_sku': target,
        'method': method, 'reason': reason, 'fingerprint': fingerprint, 'status': status})
    if not created and case.fingerprint != fingerprint:
        case.sources, case.candidates, case.target_sku = sources, candidates, target
        case.method, case.reason, case.fingerprint, case.status, case.ai = method, reason, fingerprint, status, {}
        case.save()
    return case


def remember(item, part, actor):
    SupplierCodeMapping.objects.update_or_create(supplier_id=item.supplier_id, code=normalized_reference(item.codigo), brand=item.brand,
        defaults={'part': part, 'description': item.description, 'actor': actor})


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
    item.part, item.matching_status = part, 'matched'
    item.save(update_fields=['part', 'matching_status'])
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
    for item in items.iterator(chunk_size=500):
        if stats is not None:
            stats['supplier_examined'] = stats.get('supplier_examined', 0) + 1
        candidates, method, strong, reason = index.resolve(item)
        if item.part_id and candidates and str(item.part_id) != candidates[0]['id']:
            strong = False
            reason = 'El código cambió de familia. Se conserva el vínculo anterior hasta su revisión.'
        case = save_case(key=f'supplier:{item.pk}', kind='supplier', item=item, sources=[item_row(item)],
                         candidates=candidates, target=candidates[0]['sku'] if candidates else '', method=method,
                         reason=reason, status='review' if candidates else 'unmatched')
        if strong and case.status == 'review':
            try:
                apply_item(case, candidates[0], automatic=True)
                matched += 1
            except ValidationError:
                continue
    return matched


def catalog_families():
    rows = {p.sku: part_row(p) for p in Part.objects.filter(active=True, merged_into__isnull=True).prefetch_related('codes')}
    bases = {supplier_base(sku) for sku in rows} - {''}
    blocked = set(Part.objects.filter(sku__in=bases).exclude(active=True, merged_into__isnull=True).values_list('sku', flat=True))
    blocked |= set(PartCode.objects.filter(code__in=bases).values_list('code', flat=True)) - set(rows)
    return family_candidates(rows, blocked)


@transaction.atomic
def apply_family(case, *, actor, automatic=False, selected_target=None):
    from .catalog_grouping import merge_catalog_parts
    # Acquire affected suppliers before catalog locks, consistent with ingestion.
    ids = [s['id'] for s in case.sources]
    accounts = SupplierItem.objects.filter(part_id__in=ids).values_list('supplier_id', flat=True)
    list(Account.objects.select_for_update().filter(pk__in=accounts).order_by('pk'))
    target = selected_target or case.candidates[0]
    lock_ids = ids + ([target['id']] if target.get('id') else [])
    current = {str(p.pk): part_row(p) for p in Part.objects.select_for_update().filter(pk__in=lock_ids).order_by('pk')}
    if any(current.get(s['id']) != s for s in case.sources):
        raise ValidationError('La familia cambió. Actualiza el análisis.')
    if target.get('id'):
        current_target = Part.objects.select_for_update().get(pk=target['id'])
        if part_row(current_target) != target:
            raise ValidationError('El SKU padre cambió. Actualiza el análisis.')
    elif Part.objects.filter(sku=case.target_sku).exists():
        raise ValidationError('El SKU padre ya existe. Actualiza el análisis.')
    locked = MatchingCase.objects.select_for_update().get(pk=case.pk)
    if locked.status != 'review' or locked.fingerprint != case.fingerprint:
        return
    taxonomy = {}
    for field in ['category', 'subcategory']:
        values = {s.get(field) for s in case.sources if s.get(field)}
        taxonomy[field] = target.get(field) or (next(iter(values)) if len(values) == 1 else '')
    result = merge_catalog_parts(actor=actor, target_sku=target['sku'],
        source_skus=[s['sku'] for s in case.sources], create_target=not target.get('id'), **taxonomy)
    locked.status = 'applied'
    locked.target_sku = target['sku']
    locked.save(update_fields=['status', 'target_sku', 'updated_at'])
    MatchingDecision.objects.create(case=locked, actor=actor, action='automatic' if automatic else 'approved',
        evidence={'sources': case.sources, 'target': target, 'method': case.method, 'merge_id': result['merge_id']})


def reconcile_catalog(actor, *, stats=None):
    from .catalog_identity import reconcile_identities
    promoted = reconcile_identities(actor)
    if stats is not None:
        stats['promoted_oems'] = promoted
    count = reconcile_catalog_references(actor)
    for base, family in catalog_families().items():
        # Keep long families intact. The merge service itself has no batch boundary.
        sources, target = family['sources'], family['target']
        strong = not family['warnings'] and all(same_spec(s['description'], target['description']) for s in sources)
        case = save_case(key=f'catalog:{base}', kind='catalog', sources=sources, candidates=[target], target=base,
                         method='base_and_spec' if strong else 'family_review',
                         reason='Código base completo y aplicación/especificaciones idénticas.' if strong else
                         ' '.join(family['warnings']) or 'Confirma la aplicación de esta familia.')
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
                      for ref in owner['codes'])
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
        candidates = sorted((index.parts[pk] for pk in ids), key=lambda row: row['sku'])
        target = candidates[0]
        strong = (len(candidates) == 1 and target['id'] not in source_ids and
                  not spec_conflict(source['description'], target['description']) and
                  (target['id'] in direct or same_spec(source['description'], target['description'])))
        # Refresh snapshots when a preceding source merged into this target.
        candidates = [part_row(p) for p in Part.objects.filter(pk__in=ids, active=True, merged_into__isnull=True).prefetch_related('codes').order_by('sku')]
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
        if Part.objects.filter(Q(sku=base) | Q(sku__in=[i.codigo for i in items])).exists() or PartCode.objects.filter(code=base).exists():
            continue
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
            part = Part.objects.create(sku=base, name=base, description=items[0].description.upper())
            # Matching the children is done by the same resolver, with an audit per item.
            case.status = 'applied'
            case.save(update_fields=['status'])
            MatchingDecision.objects.create(case=case, action='created_parent', evidence={'sources': sources, 'target': part_row(part)})
            created += 1
    return created
