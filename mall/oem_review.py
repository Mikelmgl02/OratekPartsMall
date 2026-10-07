"""OEM finder phase 2C: the 'Revisión OEM' queue over OEMReviewCase (Agrupación inteligente -> Revisión OEM).

Filters (tier, blocker, suffix chain, make, system, stock, search) with counts per tier and facets; one decision per case under a
fingerprint compare-and-set (approve, choose_oem for MULTI_OEM, send_to_merge for CONFLICT, dismiss with a reason, reopen), each
audited in OEMReviewDecision. Approving goes through the O3 apply path: an apply_auto OEMFinderRun with scope.stage='review', the
Part locked (Account -> Part) and re-checked against the case snapshot, the verified OEM PartCode written and catalog_identity.
prefer_oem flagging or renaming the SAME Part, an OEMFinderChange holding the evidence and the undo, so the run reverts like any
other. Bulk approval of a filter is one run executed in batches by the caller (preview, start, step). Nothing here calls an AI.
"""
import hashlib
import json
import time
import uuid
from collections import Counter
from contextlib import contextmanager

from django.db import connection, transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import oem_apply as oa
from . import oem_finder as of
from .catalog_families import family_candidates, normalized_reference
from .catalog_identity import prefer_oem, reference_pattern
from .models import CatalogIdentityChange, Part, PartCode, SupplierItem

APPROVE_TIERS = ('STRONG_PENDING_OWNER_TAGS', 'CURRENT_REVIEW', 'PROBABLE_BASE', 'CURRENT_LIKELY_OEM', 'LOCAL_XREF', 'VARIANT_REVIEW', 'WEAK')
BULK_TIERS = ('STRONG_PENDING_OWNER_TAGS', 'CURRENT_REVIEW', 'PROBABLE_BASE', 'CURRENT_LIKELY_OEM', 'LOCAL_XREF')
CHOOSE_TIERS, MERGE_TIERS = ('MULTI_OEM',), ('CONFLICT',)
CLAIM_TIERS = ('STRONG_PENDING_OWNER_TAGS', 'PROBABLE_BASE', 'LOCAL_XREF')  # a base another SKU claimed would have graded them CONFLICT
STATUSES = ('review', 'applied', 'dismissed', 'resolved')
DISMISS_REASONS = {'not_oem': 'El número propuesto no es el OEM de esta pieza', 'other_part': 'El OEM es de otra pieza, lado o variante',
                   'keep_code': 'Conservar el código actual como MAIN', 'data_error': 'El SKU o la descripción tienen un error',
                   'other': 'Otro motivo'}
SKIP_LABELS = {**oa.SKIP_LABELS, 'case_changed': 'el caso cambió o ya tiene otra decisión',
               'suffix_changed': 'la tabla de sufijos reclasificó un sufijo de la cadena desde el análisis'}
FILTER_KEYS = ('status', 'tier', 'blocker', 'chain', 'make', 'system', 'in_stock', 'search')
FULL_BLOCKERS = ('system', 'lexicon_not_strong', 'make_source', 'make_groups', 'flag', 'desc_variant', 'unknown', 'unconfirmed_tag', 'conflict',
                 'assembly_or_kit_head')
PAGE_SIZE, BULK_LIMIT, BULK_BATCH, SAMPLE, LOCK_WAIT = 25, 100, 50, 20, 10.0  # design 2C: approve_batch takes at most 100 rows


class Refused(Exception):
    """400: the request does not fit the case (wrong action for its tier, a number that is not one of its OEM candidates)."""

    def __init__(self, field, detail):
        super().__init__(detail)
        self.field, self.detail = field, detail


class CaseConflict(Exception):
    """409: the case changed, already has another decision, or the catalog refused the change (detail is shown as is)."""

    def __init__(self, detail, **extra):
        super().__init__(detail)
        self.detail, self.extra = detail, extra


def review_tables_ready():
    from .oem_finder_models import OEMReviewDecision
    return oa.apply_tables_ready() and OEMReviewDecision._meta.db_table in connection.introspection.table_names()


@contextmanager
def write_lock(wait=None):
    """The matching advisory lock, waiting up to LOCK_WAIT seconds for a running matching pass (about 7 s) to finish."""
    deadline = time.monotonic() + (LOCK_WAIT if wait is None else wait)
    while True:
        with of.matching_lock() as acquired:
            if acquired or time.monotonic() >= deadline:
                yield acquired
                return
        time.sleep(0.25)


# ------------------------------------------------------------------ filters, counts and facets
def parse_filters(params):
    f = {key: str(params.get(key) or '').strip() for key in FILTER_KEYS}
    f['status'] = f['status'] or 'review'
    if f['status'] not in STATUSES:
        raise ValidationError({'status': 'El estado no es válido.'})
    if f['tier'] and f['tier'] not in of.REVIEW_TIERS:
        raise ValidationError({'tier': 'El nivel no es válido.'})
    if f['in_stock'] not in ('', 'true', 'false'):
        raise ValidationError({'in_stock': 'Usa true o false.'})
    for key in ('blocker', 'chain', 'make', 'system', 'search'):
        if len(f[key]) > 120:
            raise ValidationError({key: 'El filtro es demasiado largo.'})
    for key in ('chain', 'make', 'system', 'search'):
        f[key] = f[key].upper()
    return f


def blocker_key(blocker):
    head = blocker.split(':', 1)[0]
    return blocker if head in FULL_BLOCKERS else head


def cases(f, *, skip=()):
    from .oem_finder_models import OEMReviewCase
    qs = OEMReviewCase.objects.filter(status=f['status'])
    if f['tier'] and 'tier' not in skip:
        qs = qs.filter(tier=f['tier'])
    if f['blocker'] and 'blocker' not in skip:  # an element equal to the key, or the key with a detail (system:MAZDA, unknown:U)
        qs = qs.filter(Q(blockers__icontains=f'"{f["blocker"]}"') | Q(blockers__icontains=f'"{f["blocker"]}:'))
    if f['chain'] and 'chain' not in skip:
        qs = qs.filter(chain=f['chain'])
    if f['make'] and 'make' not in skip:
        qs = qs.filter(brand=f['make'])
    if f['system'] and 'system' not in skip:
        qs = qs.filter(system=f['system'])
    if f['in_stock']:
        qs = qs.filter(in_stock=f['in_stock'] == 'true')
    if f['search']:
        s = f['search']
        qs = qs.filter(Q(part__sku__icontains=s) | Q(part__description__icontains=s) | Q(candidate__icontains=s) | Q(written_form__icontains=s))
    return qs


def counted(qs, field, n=40):
    return [[v, c] for v, c in qs.exclude(**{field: ''}).values_list(field).annotate(c=Count('id')).order_by('-c', field)[:n]]


def overview(f):
    from .oem_finder_models import OEMFinderRun, OEMReviewCase
    blockers = Counter(k for bl in cases(f, skip=('blocker',)).values_list('blockers', flat=True) for k in {blocker_key(b) for b in bl or []})
    last = OEMFinderRun.objects.filter(mode='dry_run', status='completed').order_by('-finished_at', '-id').first()
    return {
        'tiers': dict(cases(f, skip=('tier',)).values_list('tier').annotate(c=Count('id'))),
        'statuses': dict(OEMReviewCase.objects.values_list('status').annotate(c=Count('id'))),
        'facets': {'blockers': [[k, c] for k, c in sorted(blockers.items(), key=lambda x: (-x[1], x[0]))[:60]],
                   'chains': counted(cases(f, skip=('chain',)), 'chain'), 'makes': counted(cases(f, skip=('make',)), 'brand'),
                   'systems': counted(cases(f, skip=('system',)), 'system')},
        'last_run': last and {'id': last.pk, 'finished_at': last.finished_at, 'suffix_table_version': last.suffix_table_version,
                              'tiers': (last.counts or {}).get('tiers', {}), 'scenario': (last.counts or {}).get('scenario', {}),
                              'cases': (last.counts or {}).get('cases', {})},
    }


# ------------------------------------------------------------------ rows
def code_dicts(part_ids):
    out = {}
    for c in PartCode.objects.filter(part_id__in=part_ids).order_by('pk').values('id', 'part_id', 'code', 'brand', 'ref_type', 'reference_source'):
        out.setdefault(str(c.pop('part_id')), {})[c['id']] = c
    return out


def chain_drift(case, table):
    """Suffix tokens the analysis read as strippable TAGs that the suffix table no longer classifies so (relabelled VARIANT, UNKNOWN or
    lifecycle since, with no new analysis): approving would strip a suffix that may change the part."""
    from .catalog_suffixes import description_head
    chain = [c for c in (case.evidence or {}).get('suffix_chain') or [] if c.get('class') == 'TAG' and c.get('kind') != 'lifecycle']
    if not chain:
        return []
    head = description_head((case.snapshot or {}).get('description') or '')
    return [c['tok'] for c in chain for e in [table.classify(c.get('sep') or '-', c['tok'], head)] if e.cls != 'TAG' or e.role == 'lifecycle']


def live_state(rows):
    """The Part as it is now for each case: current SKU, alternos, supplier codes and whether it changed since the analysis (the Part
    itself, or the suffix table relabelled a TAG of its chain)."""
    from .catalog_suffixes import current_suffix_table
    ids, table = [r.part_id for r in rows], current_suffix_table()
    parts = {str(p.pk): p for p in Part.objects.filter(pk__in=ids)}
    codes, items = code_dicts(ids), {}
    for s in SupplierItem.objects.filter(part_id__in=ids).select_related('supplier').order_by('pk'):
        items.setdefault(str(s.part_id), []).append({'codigo': s.codigo, 'brand': s.brand, 'supplier': s.supplier.name,
                                                     'available': max(0, s.reported_quantity - s.reserved_quantity)})
    out = {}
    for r in rows:
        pid, part = str(r.part_id), parts.get(str(r.part_id))
        mine = codes.get(pid, {})
        stale = ('' if getattr(r, 'status', 'review') == 'applied' else 'missing' if part is None else 'retired' if not part.active or part.merged_into_id
                 else 'snapshot_changed' if oa.current_snapshot(part, mine) != r.snapshot
                 else 'suffix_changed' if chain_drift(r, table) else '')  # an approval changes its own Part
        out[pid] = {'part': part, 'codes': list(mine.values()), 'items': items.get(pid, [])[:12], 'stale': stale}
    return out


def brand_for(system, makes, fallback):
    brand = of.SYSTEM_BRAND.get(system, fallback)
    if brand == 'HYUNDAI/KIA':
        return 'KIA' if 'KIA' in makes and 'HYUNDAI' not in makes else 'HYUNDAI'
    return brand


def choices(case):
    """The OEM numbers of a case: the primary first, then the other candidates (MULTI_OEM), in manufacturer form with their brand. An
    alternate is found by its key or by its catalog spelling (14510-PEO-005 is the candidate 14510-PE0-005); one no format system parsed
    has no known manufacturer form, so it is never offered as a MAIN nor written as an alterno."""
    ev = case.evidence or {}
    primary = ev.get('primary') or {}
    found = {}
    for c in ev.get('candidates') or []:
        found.setdefault(c['key'], c)
        found.setdefault(normalized_reference(c.get('written') or ''), c)
    out = []
    for key in dict.fromkeys(filter(None, [primary.get('key')] + list(ev.get('other_oem_candidates') or []))):
        c = found.get(key)
        if c is None or any(o['key'] == c['key'] for o in out):
            continue
        brand = case.brand if c['system'] == primary.get('system') else brand_for(c['system'], ev.get('makes') or [], case.brand)
        out.append({'key': key, 'code': c['manufacturer_form'], 'written': c['written'], 'system': c['system'], 'grade': c['grade'], 'brand': brand})
    if not out and case.candidate:
        out.append({'key': normalized_reference(case.candidate), 'code': case.candidate, 'written': case.written_form or case.candidate,
                    'system': case.system, 'grade': case.grade, 'brand': case.brand})
    return out


def row_actions(case, stale):
    ev = case.evidence or {}
    if case.status == 'review':
        actions = []
        if not stale:
            if case.tier in CHOOSE_TIERS:
                actions.append('choose_oem')
            elif case.tier in APPROVE_TIERS and case.candidate:
                actions.append('approve')
            if case.tier in MERGE_TIERS and not ev.get('family_incompatible'):
                actions.append('send_to_merge')
        return actions + ['dismiss']
    if case.status == 'dismissed' or (case.status == 'resolved' and (case.decision or {}).get('action') == 'send_to_merge'):
        return ['reopen']
    return []


def case_row(case, live):
    ev, state = case.evidence or {}, live[str(case.part_id)]
    p, part = ev.get('primary') or {}, state['part']
    lex = p.get('lexicon') or {}
    sku = part.sku if part else case.snapshot.get('sku', '')
    return {
        'id': case.pk, 'part_id': case.part_id, 'sku': sku, 'description': part.description if part else case.snapshot.get('description', ''),
        'evaluated_sku': case.snapshot.get('sku', ''), 'evaluated_description': case.snapshot.get('description', ''),
        'tier': case.tier, 'tier_label': of.TIER_LABELS.get(case.tier, case.tier), 'underlying_tier': case.underlying_tier,
        'status': case.status, 'fingerprint': case.fingerprint, 'candidate': case.candidate, 'written_form': case.written_form,
        'brand': case.brand, 'system': case.system, 'grade': f'F{case.grade}' if case.grade else '', 'chain': case.chain,
        'chain_tokens': [{k: c.get(k) for k in ('sep', 'tok', 'token', 'class', 'kind', 'status', 'auto_eligible')} for c in ev.get('suffix_chain') or []],
        'blockers': case.blockers or [], 'in_stock': case.in_stock, 'available_quantity': ev.get('available_quantity', 0),
        'method': 'flag' if case.candidate and case.candidate == sku else 'rename',
        'evidence': {
            'lexicon': {k: lex.get(k) for k in ('result', 'share', 'n_other_keys', 'top_heads', 'side')},
            'units': p.get('units') or [], 'attributions': p.get('attributions') or [], 'n_suppliers': p.get('n_suppliers', 0),
            'source_counts': p.get('source_counts') or {}, 'genuine_tokens': p.get('genuine_tokens') or [], 'flags': p.get('flags') or [],
            'contradictions': p.get('contradictions') or [], 'verified_oem_alterno': bool(p.get('verified_oem_alterno')),
            'second_unit': bool(p.get('second_unit')), 'siblings': [s['sku'] for s in p.get('siblings') or []],
            'conflicts': [{'text': c['text'], 'kind': c['kind']} for c in p.get('conflicts') or []], 'conflict_kind': ev.get('conflict_kind') or '',
            'shared_with': ev.get('shared_with') or [], 'family_incompatible': ev.get('family_incompatible') or [],
            'siblings_to_merge_into_this': ev.get('siblings_to_merge_into_this') or [], 'inconsistent_siblings': ev.get('inconsistent_siblings') or [],
            'warnings': ev.get('warnings') or [], 'secondary_company_refs': ev.get('secondary_company_refs') or [],
            'pending_owner_tags': ev.get('pending_owner_tags') or [], 'blocking_unknowns': ev.get('blocking_unknowns') or [],
            'reasons': ev.get('reasons') or [], 'makes': ev.get('makes') or [], 'make_source': ev.get('make_source') or '',
            'head': ev.get('head') or '', 'aftermarket_family': ev.get('aftermarket_family') or [], 'apply_conflict': ev.get('apply_conflict') or None,
        },
        'choices': choices(case) if case.tier in CHOOSE_TIERS else [],
        'codes': [{k: c[k] for k in ('code', 'brand', 'ref_type')} for c in state['codes']], 'supplier_codes': state['items'],
        'stale': state['stale'], 'stale_label': SKIP_LABELS.get(state['stale'], '') if state['stale'] else '',
        'actions': row_actions(case, state['stale']), 'decision': case.decision or {},
        'decided_by': case.decided_by.username if case.decided_by_id else None, 'decided_at': case.decided_at,
    }


def page_rows(rows):
    rows = list(rows)
    live = live_state(rows)
    return [case_row(c, live) for c in rows]


# ------------------------------------------------------------------ decisions without a catalog change
def record(case, action, actor, *, run=None, change=None, code='', brand='', reason='', evidence=None):
    from .oem_finder_models import OEMReviewDecision
    return OEMReviewDecision.objects.create(case=case, part_id=case.part_id, action=action, tier=case.tier, fingerprint=case.fingerprint, run=run,
                                            change=change, code=code[:120], brand=brand[:120], reason=reason[:300], actor=actor,
                                            evidence={'snapshot': case.snapshot, 'candidate': case.candidate, 'blockers': case.blockers, **(evidence or {})})


def locked_case(case_id, fingerprint, *, statuses=('review',)):
    from .oem_finder_models import OEMReviewCase
    case = OEMReviewCase.objects.select_for_update(of=('self',)).select_related('part').get(pk=case_id)
    if case.fingerprint != fingerprint or case.status not in statuses:
        raise CaseConflict('El caso cambió o ya tiene otra decisión. Actualiza la lista antes de continuar.')
    return case


def dismiss(case_id, fingerprint, actor, *, reason, note=''):
    if reason not in DISMISS_REASONS:
        raise ValidationError({'reason': 'Elige un motivo para descartar.'})
    if reason == 'other' and not note.strip():
        raise ValidationError({'note': 'Describe el motivo.'})
    with transaction.atomic():
        case = locked_case(case_id, fingerprint, statuses=('review', 'dismissed'))
        if case.status == 'dismissed':
            return case  # the same decision again changes nothing
        case.status, case.decided_by, case.decided_at = 'dismissed', actor, timezone.now()
        case.decision = {'action': 'dismiss', 'reason': reason, 'label': DISMISS_REASONS[reason], 'note': note.strip()[:300]}
        case.save(update_fields=['status', 'decided_by', 'decided_at', 'decision', 'updated_at'])
        record(case, 'dismiss', actor, reason=f'{reason}: {note.strip()}' if note.strip() else reason)
    return case


def reopen(case_id, fingerprint, actor):
    with transaction.atomic():
        case = locked_case(case_id, fingerprint, statuses=('review', 'dismissed', 'resolved'))
        if case.status == 'review':
            return case
        if case.status == 'resolved' and (case.decision or {}).get('action') != 'send_to_merge':
            raise CaseConflict('El caso se resolvió en el último análisis OEM; ya no está en la revisión.')
        previous = (case.decision or {}).get('action', '')
        case.status, case.decided_by, case.decided_at, case.decision = 'review', actor, timezone.now(), {'action': 'reopen', 'previous': previous}
        case.save(update_fields=['status', 'decided_by', 'decided_at', 'decision', 'updated_at'])
        record(case, 'reopen', actor, reason=previous)
    return case


def uuids(values):
    out = []
    for v in values:
        try:
            out.append(uuid.UUID(str(v)))
        except ValueError:
            continue
    return out


def grouping_row(part):
    return {'id': str(part.pk), 'sku': part.sku, 'name': part.name, 'is_OEM': part.is_OEM, 'description': part.description,
            'category': part.category, 'subcategory': part.subcategory,
            'codes': [{'brand': c.brand, 'code': c.code, 'ref_type': c.ref_type, 'reference_source': c.reference_source} for c in part.codes.all()]}


def merge_family(case):
    """The Agrupar SKU family of a CONFLICT case, in the shape of a grouping candidate: the Part that already owns the OEM (its SKU or
    an alterno) as the target; else the OEM as a new parent when Agrupar SKU can create it; else this SKU (the target can be changed)."""
    ev = case.evidence or {}
    primary, part, refused = ev.get('primary') or {}, Part.objects.prefetch_related('codes').get(pk=case.part_id), ev.get('apply_conflict') or {}
    oem, brand = refused.get('code') or case.candidate, refused.get('brand') or case.brand  # the number an approval was refused for
    key = normalized_reference(oem) if refused.get('code') else primary.get('key') or normalized_reference(case.candidate)
    ids = {c['part_id'] for c in primary.get('conflicts') or [] if c.get('kind') in ('active_sku', 'partcode')}
    ids |= set(refused.get('owners') or [])
    members = list(Part.objects.filter(Q(pk__in=uuids(ids)) | Q(sku__in=ev.get('shared_with') or []), active=True, merged_into__isnull=True)
                   .exclude(pk=part.pk).prefetch_related('codes').order_by('sku')[:49])
    rows = {m.sku: grouping_row(m) for m in [part, *members]}
    owner = (next((m for m in members if normalized_reference(m.sku) == key), None)
             or next((m for m in members if any(normalized_reference(c.code) == key for c in m.codes.all())), None))
    code = oem if refused.get('code') else case.written_form or case.candidate
    family = None if owner else family_candidates({s: {**r, 'is_OEM': False} for s, r in rows.items()}).get(code)
    if owner:
        target = rows[owner.sku]
    elif family and family['target'].get('proposed_parent') and {r['sku'] for r in family['sources']} == set(rows):
        target = {**family['target'], 'codes': []}
    else:
        target = rows[part.sku]
    sources = [r for s, r in rows.items() if s != target['sku']]
    warnings = list((family or {}).get('warnings') or [])
    warnings.append(f'Después de agrupar, el próximo análisis OEM propondrá {oem} ({brand}) como MAIN del SKU resultante.')
    return {'target_sku': target['sku'], 'target': target, 'sources': sources, 'source_skus': [r['sku'] for r in sources], 'source_count': len(sources),
            'reason': f'Conflicto OEM {oem}: {ev.get("conflict_kind") or "otro SKU usa este número"}. Revisa si son el mismo repuesto antes de agrupar.',
            'warnings': warnings, 'needs_review': True, 'oem': {'code': oem, 'brand': brand}}


def send_to_merge(case_id, fingerprint, actor):
    from .oem_finder_models import OEMReviewCase
    current = OEMReviewCase.objects.get(pk=case_id)
    if current.tier not in MERGE_TIERS:
        raise ValidationError({'action': 'Solo los casos en conflicto se envían a Agrupar SKU.'})
    with transaction.atomic():
        case = locked_case(case_id, fingerprint, statuses=('review', 'resolved'))
        if case.status == 'resolved' and (case.decision or {}).get('action') != 'send_to_merge':
            raise CaseConflict('El caso cambió o ya tiene otra decisión. Actualiza la lista antes de continuar.')
        bad = (case.evidence or {}).get('family_incompatible') or []
        if bad:
            raise CaseConflict('La familia mezcla piezas incompatibles (%s): no se puede agrupar. Corrige los datos o descarta el caso.' % ', '.join(bad[:4]))
        family = merge_family(case)
        if not family['sources'] and not family['target'].get('proposed_parent'):
            raise CaseConflict('Los SKU de esta familia ya no están activos. El próximo análisis OEM actualizará el caso.')
        if case.status == 'review':
            case.status, case.decided_by, case.decided_at = 'resolved', actor, timezone.now()
            case.decision = {'action': 'send_to_merge', 'target_sku': family['target_sku'], 'source_skus': family['source_skus']}
            case.save(update_fields=['status', 'decided_by', 'decided_at', 'decision', 'updated_at'])
            record(case, 'send_to_merge', actor, code=case.candidate, brand=case.brand, evidence={'family': {k: family[k] for k in ('target_sku', 'source_skus')}})
    return case, family


# ------------------------------------------------------------------ approve (the O3 apply path)
def key_owners(part, code):
    """Other Parts (retired included, except those merged into this one) whose SKU or alterno is this reference once normalized."""
    key = normalized_reference(code)
    if not key:
        return []
    rx = reference_pattern(key)
    owners = [(str(p), s) for p, s in Part.objects.filter(sku__iregex=rx).exclude(pk=part.pk).exclude(merged_into=part.pk).values_list('pk', 'sku')
              if normalized_reference(s) == key]
    return owners + [(str(p), c) for p, c in PartCode.objects.filter(code__iregex=rx).exclude(part=part.pk).values_list('part_id', 'code')
                     if normalized_reference(c) == key]


class Claimed(ValidationError):
    """The OEM already identifies another SKU, or a SKU that shares its base appeared since the analysis: like a prefer_oem refusal,
    the case goes to merge review (CONFLICT) with those SKUs as its family."""

    def __init__(self, detail, owners, oem):
        super().__init__(detail)
        self.owners, self.oem = owners, oem


def sku_claimants(part, case, key):
    """Active non-OEM SKUs that begin with the OEM key plus a separator (48654-0K040-NAK for 48654-0K040) and that the analysis did not
    list as siblings. The finder grades a base another SKU claims as CONFLICT (CLAIM_TIERS), so such a SKU is new or changed since."""
    from .catalog_identity import NON_ALNUM
    from .catalog_suffixes import clean_code, strip_lifecycle
    if not key:
        return []
    known = {s.get('sku') for s in ((case.evidence or {}).get('primary') or {}).get('siblings') or []}
    rx = '^' + NON_ALNUM + NON_ALNUM.join(key) + '([-/._+( ]|$)'
    rows = Part.objects.filter(sku__iregex=rx, active=True, merged_into__isnull=True, is_OEM=False).exclude(pk=part.pk).values_list('pk', 'sku')
    return [(str(p), s) for p, s in rows if s not in known and normalized_reference(s) != key and not strip_lifecycle(clean_code(s))[1]]


def claimed(part, case, chosen):
    """Raises Claimed when another SKU holds the OEM (its SKU or an alterno, retired included) or newly shares its base."""
    code, oem = chosen['code'], {'code': chosen['code'], 'brand': chosen['brand']}
    owners = key_owners(part, code)
    if owners:
        raise Claimed(f'El OEM {code} ya identifica otro SKU: {", ".join(sorted({s for _, s in owners})[:3])}.', owners, oem)
    shared = sku_claimants(part, case, chosen['key']) if case.tier in CLAIM_TIERS else []
    if shared:
        raise Claimed(f'{", ".join(sorted(s for _, s in shared)[:3])} comparte la base {code} y no estaba en el análisis.', shared, oem)


def pick(case, code):
    """The OEM this decision writes: choose_oem names one of the case's numbers; approve takes the proposed MAIN."""
    if case.tier in CHOOSE_TIERS:
        options = choices(case)
        chosen = next((o for o in options if o['code'] == (code or '').strip().upper()), None)
        if chosen is None:
            raise Refused('code', 'Elige uno de los números OEM del caso.')
        return chosen, [o for o in options if o is not chosen]
    if case.tier not in APPROVE_TIERS or not case.candidate:
        raise Refused('action', 'Este nivel no se aprueba desde la revisión: ' + of.TIER_LABELS.get(case.tier, case.tier) + '.')
    if code and code.strip().upper() != case.candidate:
        raise Refused('code', 'El OEM no coincide con la propuesta del caso.')
    return {'key': normalized_reference(case.candidate), 'code': case.candidate, 'written': case.written_form or case.candidate,
            'system': case.system, 'grade': case.grade, 'brand': case.brand}, []


def keep_alternos(part, case, others):
    """The other MULTI_OEM numbers stay as (unverified) OEM alternos and the company codes of extra SKU segments as company alternos,
    when no other Part holds them; a held one is reported, never moved."""
    ev, kept, skipped = case.evidence or {}, [], []
    wanted = [(o['code'], o['brand'], 'oem') for o in others]
    refs = list(ev.get('secondary_company_refs') or []) + ([ev['company_reference']] if ev.get('company_reference') else [])
    wanted += [(r['code'], r['brand'], 'company') for r in refs if r.get('code') and r.get('brand')]
    for code, brand, ref_type in dict.fromkeys((c.strip().upper(), b.strip().upper(), t) for c, b, t in wanted):
        if len(code) > 120 or PartCode.objects.filter(brand=brand, code=code).exclude(part=part).exists() or key_owners(part, code):
            skipped.append(f'{brand} {code}'.strip())
        elif not PartCode.objects.filter(part=part, brand=brand, code=code).exists():
            PartCode.objects.create(part=part, code=code, brand=brand, ref_type=ref_type)
            kept.append(f'{brand} {code}'.strip())
    return kept, skipped


def apply_case(run, case_id, fingerprint, actor, *, code=None, batch=1, bulk=False, table=None):
    """One reviewed case through the O3 apply path, in its own transaction (a savepoint inside a caller's). Raises CaseConflict
    (decided or changed case), oa.Skip (nothing written) or the catalog's ValidationError (the caller routes it to CONFLICT)."""
    from .catalog_suffixes import current_suffix_table
    from .oem_finder_models import OEMFinderChange, OEMReviewCase
    table = table or current_suffix_table()
    part_id = OEMReviewCase.objects.values_list('part_id', flat=True).get(pk=case_id)
    with transaction.atomic():
        part = oa.lock_part(part_id)
        case = locked_case(case_id, fingerprint)
        if bulk and case.tier not in BULK_TIERS:
            raise oa.Skip('case_changed')
        chosen, others = pick(case, code)
        if part is None:
            raise oa.Skip('missing')
        if not part.active or part.merged_into_id:
            raise oa.Skip('retired')
        codes = oa.code_rows(part.pk)
        if oa.current_snapshot(part, codes) != case.snapshot:
            raise oa.Skip('snapshot_changed')
        drift = chain_drift(case, table)
        if drift:
            raise oa.Skip('suffix_changed', ', '.join(drift))
        claimed(part, case, chosen)
        flag = chosen['code'] == part.sku
        tier = 'AUTO_FLAG_CURRENT' if flag else 'STRONG_PENDING_OWNER_TAGS' if case.tier == 'STRONG_PENDING_OWNER_TAGS' else 'AUTO_RENAME_BASE'
        source = f'algo:oem-finder:{oa.SOURCE_VERSION}:review-{case.tier.lower().replace("_", "-")}:{run.pk}'
        oa.ensure_code(part, chosen['code'], chosen['brand'], 'oem', source)
        if chosen['written'] and chosen['written'] != chosen['code']:  # the catalog spelling stays searchable, unverified
            oa.ensure_code(part, chosen['written'], chosen['brand'], 'oem', '')
        kept, held = keep_alternos(part, case, others)
        before = {'sku': part.sku, 'name': part.name, 'is_OEM': part.is_OEM}
        part = prefer_oem(part, actor=actor, selected={'code': chosen['code'], 'brand': chosen['brand']}, confirm_current=flag)
        if part.sku != chosen['code'] or not part.is_OEM:
            raise RuntimeError(f'prefer_oem dejó {part.sku} sin aplicar {chosen["code"]}.')
        if flag:
            change = CatalogIdentityChange.objects.create(part=part, previous_sku=part.sku, sku=part.sku, actor=actor, reference={
                'code': chosen['code'], 'brand': chosen['brand'], 'ref_type': 'oem', 'reference_source': source, 'action': 'confirm_current'})
        else:
            change = CatalogIdentityChange.objects.filter(part=part, previous_sku=before['sku'], sku=chosen['code']).order_by('-pk').first()
        after = oa.code_rows(part.pk)
        undo = {**before, 'created_codes': [after[k] for k in sorted(after.keys() - codes.keys())],
                'updated_codes': [{'before': codes[k], 'after': after[k]} for k in sorted(after.keys() & codes.keys()) if after[k] != codes[k]]}
        action = 'choose_oem' if case.tier in CHOOSE_TIERS else 'approve'
        review = {'case': case.pk, 'tier': case.tier, 'action': action, 'fingerprint': fingerprint, 'chosen': chosen, 'kept': kept, 'held': held,
                  'bulk': bulk}
        link = OEMFinderChange.objects.create(change=change, run=run, part=part, tier=tier, method=oa.METHODS[tier], batch=batch, code=chosen['code'],
                                              brand=chosen['brand'], evidence={**(case.evidence or {}), 'snapshot': case.snapshot, 'review': review},
                                              undo=undo)
        case.status, case.decided_by, case.decided_at = 'applied', actor, timezone.now()
        case.decision = {'action': action, 'run': run.pk, 'change': link.pk, 'code': chosen['code'], 'brand': chosen['brand'],
                         'method': 'flag' if flag else 'rename', 'previous_sku': before['sku'], 'bulk': bulk}
        case.save(update_fields=['status', 'decided_by', 'decided_at', 'decision', 'updated_at'])
        record(case, action, actor, run=run, change=link, code=chosen['code'], brand=chosen['brand'], evidence={'kept': kept, 'held': held, 'bulk': bulk})
    return link


def to_conflict(case_id, fingerprint, message, actor, run=None, owners=None, oem=None):
    """A change the catalog refused (prefer_oem, a held alterno or a claimed OEM) turns the case into CONFLICT (merge review), like O3;
    never retried. oem is the number the approval wrote (choose_oem may pick another than the proposed one)."""
    from .matching_engine import digest
    with transaction.atomic():
        try:
            case = locked_case(case_id, fingerprint)
        except CaseConflict:
            return None
        part, oem = case.part, oem or {'code': case.candidate or '', 'brand': case.brand}
        if owners is None:
            owners = (key_owners(part, oem['code']) + key_owners(part, part.sku)) if part.active and not part.merged_into_id else []
        blockers, ids = list(dict.fromkeys((case.blockers or []) + ['apply_conflict'])), sorted({o[0] for o in owners})
        record(case, 'apply_conflict', actor, run=run, code=oem['code'], brand=oem['brand'], reason=message, evidence={'owners': ids})
        case.evidence = {**(case.evidence or {}), 'apply_conflict': {'run': run.pk if run else None, 'tier': case.tier, 'detail': message, 'source': 'review',
                                                                     'owners': ids, 'code': oem['code'], 'brand': oem['brand']}}
        case.underlying_tier, case.tier, case.tier_rank, case.blockers = case.tier, 'CONFLICT', of.TIER_ORDER.index('CONFLICT'), blockers
        case.fingerprint = digest([of.OEM_FINDER_VERSION, case.snapshot, case.candidate, 'CONFLICT', blockers, 'apply', message])
        case.decided_by, case.decided_at, case.decision = actor, timezone.now(), {'action': 'apply_conflict', 'detail': message}
        case.save()
    return case


def new_run(actor, scope, version=''):
    from .oem_finder_models import OEMFinderRun
    return OEMFinderRun.objects.create(mode='apply_auto', status='running', rules_version=of.OEM_FINDER_VERSION, suffix_table_version=version,
                                       scope={'stage': 'review', 'in_stock_first': True, **scope}, actor=actor)


def summary(total=0):
    return {'applied': 0, 'conflicts': 0, 'skipped': {}, 'errors': 0, 'tiers': {}, 'batches': [], 'stopped': None, 'excluded': {}, 'selected': total,
            'done': 0, 'total': total, 'spot_check': []}


def approve(case_id, fingerprint, actor, *, action='approve', code=None):
    """approve / choose_oem of one case: its own run (so 'Deshacer ejecución' undoes exactly this decision). An empty run is rolled back."""
    from .oem_finder_models import OEMReviewCase
    case = OEMReviewCase.objects.get(pk=case_id)
    if (action == 'choose_oem') != (case.tier in CHOOSE_TIERS):
        raise Refused('action', 'Elige uno de los números OEM del caso.' if case.tier in CHOOSE_TIERS else 'Solo los casos con varios OEM se resuelven eligiendo uno.')
    decision = case.decision or {}
    if case.fingerprint == fingerprint and not (case.status == 'applied' and code):
        pick(case, code)  # a request that does not fit the case is refused before anything is written
    if case.status == 'applied' and case.fingerprint == fingerprint and decision.get('run'):
        if code and code.strip().upper() != decision.get('code'):
            raise CaseConflict('El caso ya se aprobó con otro OEM.')
        return decision['run'], None  # the same decision again changes nothing
    try:
        with transaction.atomic():
            run = new_run(actor, {'action': 'choose_oem' if case.tier in CHOOSE_TIERS else 'approve', 'case': case.pk, 'sku': case.snapshot.get('sku', ''),
                                  'tier': None, 'limit': 1, 'batch_size': 1}, case.suffix_table_version)
            link = apply_case(run, case_id, fingerprint, actor, code=code)
            s = summary(1)
            s.update(applied=1, done=1, tiers={case.tier: 1}, batches=[{'index': 1, 'applied': 1, 'conflicts': 0, 'skipped': 0, 'errors': 0}],
                     spot_check=[link.pk])
            run.status, run.applied, run.finished_at = 'completed', s, timezone.now()
            run.counts = {'tiers': {case.tier: [1, int(case.in_stock)]}}
            run.save(update_fields=['status', 'applied', 'finished_at', 'counts'])
    except oa.Skip as skip:
        raise CaseConflict(f'No se aplicó: {SKIP_LABELS.get(skip.reason, skip.reason)}{f" ({skip.detail})" if skip.detail else ""}. '
                           'El próximo análisis OEM actualizará el caso.', reason=skip.reason)
    except ValidationError as error:
        message = oa.error_text(error.detail)[:500]
        oem = getattr(error, 'oem', None) or next(({'code': o['code'], 'brand': o['brand']} for o in choices(case)
                                                   if code and o['code'] == code.strip().upper()), None)
        if to_conflict(case_id, fingerprint, message, actor, owners=getattr(error, 'owners', None), oem=oem):
            raise CaseConflict(f'{message} El caso pasó a Conflicto (agrupar).', reason='conflict')
        raise
    return run.pk, link


# ------------------------------------------------------------------ bulk approval of a filter
def reverted_pairs():
    from .oem_finder_models import OEMFinderChange
    return {(str(p), c) for p, c in OEMFinderChange.objects.filter(reverted_at__isnull=False).values_list('part_id', 'code')}


def selection(f):
    """The bulk-approvable cases of a filter, in queue order (in stock first): unchanged since the analysis, never reverted before with
    the same OEM, at most BULK_LIMIT. Returns (rows, excluded counts, digest)."""
    base = cases({**f, 'status': 'review'})
    excluded = Counter({'tier': base.exclude(tier__in=BULK_TIERS).count()})
    qs = base.filter(tier__in=BULK_TIERS).exclude(candidate='').order_by('-in_stock', 'tier_rank', 'id')
    total, reverted, rows, seen = qs.count(), reverted_pairs(), [], 0
    for start in range(0, total, 500):
        chunk = list(qs.only('id', 'part_id', 'status', 'fingerprint', 'snapshot', 'evidence', 'candidate', 'tier', 'brand', 'in_stock',
                             'suffix_table_version')[start:start + 500])  # evidence: the suffix chain the stale check re-reads
        live = live_state(chunk)
        for c in chunk:
            seen += 1
            if live[str(c.part_id)]['stale']:
                excluded['stale'] += 1
            elif (str(c.part_id), c.candidate) in reverted:
                excluded['previously_reverted'] += 1
            else:
                rows.append((c, live[str(c.part_id)]['part']))
                if len(rows) >= BULK_LIMIT:
                    break
        if len(rows) >= BULK_LIMIT:
            break
    excluded['over_limit'] = total - seen
    digest = hashlib.sha256(json.dumps([[c.pk, c.fingerprint] for c, _ in rows]).encode()).hexdigest()
    return rows, {k: v for k, v in excluded.items() if v}, digest


def plan_case(case, part):
    """Read-only preview of apply_case: what the catalog would say today (no lock, no write)."""
    try:
        claimed(part, case, {'code': case.candidate, 'brand': case.brand, 'key': normalized_reference(case.candidate)})
    except Claimed as error:
        return 'conflict', oa.error_text(error.detail)
    if case.candidate == part.sku:
        return 'applied', ''
    problem = oa.code_conflict(part, case.candidate, case.brand)
    if not problem:
        from .catalog_identity import rename_conflicts
        held = rename_conflicts(part, case.candidate)
        problem = ('El OEM o el SKU anterior ya identifica otro SKU: ' + ', '.join(held[:3])) if held else ''
    return ('conflict', problem) if problem else ('applied', '')


def unfinished_bulk():
    from .oem_finder_models import OEMFinderRun
    return OEMFinderRun.objects.filter(mode='apply_auto', status='running', scope__stage='review', scope__action='approve_batch').order_by('-pk').first()


def preview(f):
    rows, excluded, digest = selection(f)
    sample = []
    for case, part in rows[:SAMPLE]:
        outcome, detail = plan_case(case, part)
        sample.append({'case': case.pk, 'sku': part.sku, 'description': part.description, 'tier': case.tier, 'code': case.candidate, 'brand': case.brand,
                       'method': 'flag' if case.candidate == part.sku else 'rename', 'outcome': outcome, 'detail': detail})
    halt, running = oa.active_halt(), unfinished_bulk()
    return {'count': cases({**f, 'status': 'review'}).count(), 'selected': len(rows), 'limit': BULK_LIMIT, 'batch_size': BULK_BATCH,
            'excluded': excluded, 'tiers': dict(Counter(c.tier for c, _ in rows)),
            'methods': dict(Counter('flag' if c.candidate == p.sku else 'rename' for c, p in rows)), 'sample': sample, 'selection': digest,
            'halt': halt and {'reason': halt.reason, 'created_at': halt.created_at}, 'running': running.pk if running else None}


def start_bulk(f, digest, actor):
    halt = oa.active_halt()
    if halt:
        raise CaseConflict(f'La aplicación OEM está detenida: {halt.reason}. Levanta la detención antes de aprobar en lote.', reason='halted')
    running = unfinished_bulk()
    if running:
        raise CaseConflict(f'La aprobación en lote #{running.pk} no ha terminado: continúala o detenla en Ejecuciones.', reason='running', run=running.pk)
    rows, excluded, current = selection(f)
    if current != digest:
        raise CaseConflict('La selección cambió desde la vista previa. Revisa la vista previa de nuevo.', reason='selection_changed')
    if not rows:
        raise ValidationError({'detail': 'No hay casos aprobables en lote con estos filtros.'})
    tiers = Counter()
    for c, _ in rows:
        tiers[c.tier] += 1
    run = new_run(actor, {'action': 'approve_batch', 'filters': {k: v for k, v in f.items() if v and k != 'status'}, 'tier': f['tier'] or None,
                          'limit': BULK_LIMIT, 'batch_size': BULK_BATCH, 'selection': digest, 'cases': [[c.pk, c.fingerprint] for c, _ in rows]},
                  rows[0][0].suffix_table_version if rows else '')
    s = summary(len(rows))
    s['excluded'] = excluded
    run.applied = s
    run.counts = {'tiers': {t: [n, sum(1 for c, _ in rows if c.tier == t and c.in_stock)] for t, n in tiers.items()}, 'selection': digest}
    run.save(update_fields=['applied', 'counts'])
    return run


def finish(run, s, stopped=None):
    s['stopped'] = stopped or s.get('stopped')
    s['spot_check'] = oa.spot_check_sample(run)
    run.status, run.applied, run.finished_at = 'completed', s, timezone.now()
    run.save(update_fields=['status', 'applied', 'finished_at'])


def step_bulk(run_id, actor):
    """The next batch (at most BULK_BATCH rows) of a bulk run; the caller holds the matching lock. Progress is saved after every row,
    so an interrupted request loses nothing: a row already applied fails its compare-and-set and is counted as changed."""
    from .oem_finder_models import OEMFinderRun
    run = OEMFinderRun.objects.get(pk=run_id)
    s = {**summary(), **(run.applied or {})}
    if run.status != 'running':
        return run, ''
    if oa.active_halt():
        finish(run, s, 'halted')
        return run, ''
    if of.import_running():
        return run, 'catalog_import'
    from .catalog_suffixes import current_suffix_table
    rows, table = (run.scope or {}).get('cases') or [], current_suffix_table()
    chunk = rows[s['done']:s['done'] + BULK_BATCH]
    stats, errors = Counter(), list(run.errors or [])
    batch = len(s['batches']) + 1
    for case_id, fingerprint in chunk:
        try:
            link = apply_case(run, case_id, fingerprint, actor, batch=batch, bulk=True, table=table)
        except (CaseConflict, Refused, oa.Skip) as skip:
            reason = getattr(skip, 'reason', None) if isinstance(skip, oa.Skip) else 'case_changed'
            stats['skipped'] += 1
            s['skipped'][reason] = s['skipped'].get(reason, 0) + 1
        except ValidationError as error:
            to_conflict(case_id, fingerprint, oa.error_text(error.detail)[:500], actor, run, owners=getattr(error, 'owners', None), oem=getattr(error, 'oem', None))
            stats['conflicts'] += 1
            s['conflicts'] += 1
        except Exception as error:  # rolled back and recorded; the run stops after this batch and raises the halt flag
            stats['errors'] += 1
            s['errors'] += 1
            errors.append({'case': case_id, 'error': type(error).__name__, 'detail': str(error)[:300]})
        else:
            stats['applied'] += 1
            s['applied'] += 1
            tier = (link.evidence.get('review') or {}).get('tier', link.tier)
            s['tiers'][tier] = s['tiers'].get(tier, 0) + 1
        s['done'] += 1
        OEMFinderRun.objects.filter(pk=run.pk).update(applied=s, errors=errors[:50])
    s['batches'] = s['batches'] + [{'index': batch, **{k: stats[k] for k in ('applied', 'conflicts', 'skipped', 'errors')}}]
    run.applied, run.errors = s, errors[:50]
    if stats['errors']:
        oa.halt(f'La aprobación en lote {run.pk} tuvo {stats["errors"]} errores inesperados en el lote {batch}; revisa antes de continuar.', actor=actor, run=run)
        finish(run, s, 'errors')
    elif s['done'] >= len(rows):
        finish(run, s)
    else:
        run.save(update_fields=['applied', 'errors'])
    return run, ''


def stop_bulk(run_id):
    from .oem_finder_models import OEMFinderRun
    run = OEMFinderRun.objects.get(pk=run_id)
    if run.status == 'running':
        finish(run, {**summary(), **(run.applied or {})}, 'cancelled')
    return run


def reopen_reverted(link, actor):
    """Called by oem_apply.revert_link: a reviewed change that was undone puts its case back in the queue (same evidence, so its
    fingerprint and snapshot hold again) instead of leaving it 'applied' forever."""
    from .oem_finder_models import OEMReviewCase
    review = (link.evidence or {}).get('review') or {}
    case = OEMReviewCase.objects.select_for_update().filter(pk=review.get('case'), part_id=link.part_id).first()
    if case is None:
        return
    if case.status == 'applied' and (case.decision or {}).get('change') == link.pk:
        case.status, case.decided_by, case.decided_at = 'review', actor, timezone.now()
        case.decision = {'action': 'reverted', 'run': link.run_id, 'change': link.pk}
        case.save(update_fields=['status', 'decided_by', 'decided_at', 'decision', 'updated_at'])
    record(case, 'reverted', actor, run=link.run, change=link, code=link.code, brand=link.brand, evidence={'revert_change': link.revert_change_id})
