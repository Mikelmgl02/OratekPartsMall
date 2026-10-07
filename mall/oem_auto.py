"""'Pendientes de aplicación automática' (Inventario → Aplicación OEM): the AUTO rows the OEM finder found, kept in OEMAutoCandidate.

Every dry run (python -m mall.oem_finder --dry-run, the worker stage) and every completed apply run refreshes the table from the finder's
results; the first dry run after migration 0038 fills it (no data migration). A row is pending while the next apply run of its apply tier
would take it, applied once its OEMFinderChange stands and the SKU is OEM, excluded while O3 leaves it to a human (reverted before with
the same OEM, or an open apply conflict: a revert never puts it back as automatic), stale once it no longer grades AUTO, and
sent_to_review when a person took it out of automatic application: mall.oem_apply.handled() skips it and an OEMReviewCase (created, or
reopened) lets a human decide. Applying from the admin is oem_apply.apply_auto itself (canary per apply tier, halt rule, in stock first,
batches of at most 100, per-row transactions, spot-check, undo) under the matching advisory lock, idempotent per operation id. No AI.
"""
import logging
from collections import Counter, defaultdict

from django.db import connection, transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import oem_apply as oa
from . import oem_finder as of

TIERS = of.AUTO_TIERS  # finder tiers, one tab each: AUTO_FLAG_CURRENT and AUTO_RENAME_BASE (both rename apply tiers)
STATUSES = ('pending', 'applied', 'sent_to_review', 'excluded', 'stale')
SENT = 'sent_to_review'
REVIEW_TIER = {'AUTO_FLAG_CURRENT': 'CURRENT_REVIEW', 'AUTO_RENAME_BASE': 'PROBABLE_BASE', 'STRONG_PENDING_OWNER_TAGS': 'STRONG_PENDING_OWNER_TAGS'}
MODES = {'canary': 50, 'batch': oa.BATCH_SIZE}
ORDERINGS = ('stock', 'sku')
FILTER_KEYS = ('status', 'tier', 'apply_tier', 'chain', 'make', 'system', 'in_stock', 'search', 'ordering')
PAGE_SIZE, SEND_LIMIT = 25, 100
REASONS = {'previously_reverted': 'se revirtió antes con el mismo OEM', 'open_conflict': 'tiene un conflicto de aplicación abierto',
           'left_auto': 'ya no es automático en el último análisis'}
SEND_SKIPS = {'not_found': 'no está en los pendientes', 'applied': 'ya se aplicó', 'excluded': 'está excluido hasta una decisión humana',
              'stale': 'ya no es automático', 'case_applied': 'su caso de revisión ya se aprobó'}
FIELDS = ['run', 'tier', 'apply_tier', 'sku', 'candidate', 'written_form', 'brand', 'makes', 'system', 'grade', 'chain', 'chain_tokens', 'owner_tags',
          'evidence', 'snapshot', 'in_stock', 'available_quantity', 'fingerprint', 'rules_version', 'suffix_table_version', 'status', 'reason', 'updated_at']
log = logging.getLogger(__name__)


class Refused(Exception):
    """409: the apply cannot start (halted, no canary credit, operation id reused for another apply)."""

    def __init__(self, reason, detail, **extra):
        super().__init__(detail)
        self.reason, self.detail, self.extra = reason, detail, extra


def auto_tables_ready():
    """False until migration 0038: the app serves this code before the orchestrator migrates (dry runs then skip the refresh)."""
    from .oem_finder_models import OEMApplyHalt, OEMAutoCandidate, OEMFinderChange, OEMFinderRun, OEMReviewCase
    wanted = {m._meta.db_table for m in (OEMFinderRun, OEMReviewCase, OEMFinderChange, OEMApplyHalt, OEMAutoCandidate)}
    return wanted <= set(connection.introspection.table_names())


# ------------------------------------------------------------------ refresh (dry runs and apply runs)
def candidate_values(r, snap, avail, version, apply_tier, owner_tags):
    from .matching_engine import digest
    f = of.case_values(r, snap, avail, version)
    tokens = [{k: c.get(k) for k in ('sep', 'tok', 'token', 'class', 'kind', 'status', 'auto_eligible')} for c in r.get('suffix_chain') or []]
    return {'tier': r['tier'], 'apply_tier': apply_tier, 'sku': snap['sku'][:200], 'candidate': f['candidate'], 'written_form': f['written_form'],
            'brand': f['brand'], 'makes': list(r.get('makes') or []), 'system': f['system'], 'grade': f['grade'], 'chain': f['chain'], 'chain_tokens': tokens,
            'owner_tags': owner_tags, 'evidence': f['evidence'], 'snapshot': snap, 'in_stock': avail > 0, 'available_quantity': avail,
            'fingerprint': digest([f['fingerprint'], apply_tier, owner_tags]), 'rules_version': of.OEM_FINDER_VERSION, 'suffix_table_version': version}


def applied_parts():
    from .oem_finder_models import OEMFinderChange
    return {str(p) for p in OEMFinderChange.objects.filter(reverted_at__isnull=True, part__is_OEM=True).values_list('part_id', flat=True)}


def refresh(run, finder, results):
    """Sync OEMAutoCandidate with a full evaluation (tiers are always global). Idempotent: an unchanged row writes nothing; a person's
    sent_to_review is never undone. The caller holds the matching advisory lock, like every writer of these rows."""
    if not auto_tables_ready():
        return {'status': 'not_migrated'}
    from .oem_finder_models import OEMAutoCandidate
    table, version, now = finder.table, of.table_version(finder.table), timezone.now()
    reverted, conflicts, _ = oa.exclusions()
    applied, wanted = applied_parts(), {}
    for pid, r in results.items():
        tier = oa.apply_tier(table, r)
        if tier:
            wanted[pid] = candidate_values(r, of.part_snapshot(finder, pid), finder.avail[pid], version, tier,
                                           [] if tier == oa.FLAG else oa.owner_confirmed_tags(table, r))
    existing = {str(c['part_id']): c for c in OEMAutoCandidate.objects.values('id', 'part_id', 'fingerprint', 'status', 'reason', 'in_stock', 'available_quantity')}
    create, update, moved, unchanged = [], [], defaultdict(list), 0
    for pid, row in wanted.items():
        cur = existing.get(pid)
        if cur and cur['status'] == SENT:
            row.update(status=SENT, reason=cur['reason'])
        elif pid in applied:
            row.update(status='applied', reason='')
        elif pid in conflicts:
            row.update(status='excluded', reason='open_conflict')
        elif (pid, row['candidate']) in reverted:  # O3 never re-applies a reverted OEM: it does not come back as pending either
            row.update(status='excluded', reason='previously_reverted')
        else:
            row.update(status='pending', reason='')
        if cur is None:
            create.append(OEMAutoCandidate(part_id=pid, run=run, **row))
        elif any(cur[k] != row[k] for k in ('fingerprint', 'status', 'reason', 'in_stock', 'available_quantity')):
            update.append(OEMAutoCandidate(id=cur['id'], part_id=pid, run=run, updated_at=now, **row))
        else:
            unchanged += 1
    for pid, cur in existing.items():
        if pid not in wanted and cur['status'] != SENT:
            target = ('applied', '') if pid in applied else ('stale', 'left_auto')
            if (cur['status'], cur['reason']) != target:
                moved[target].append(cur['id'])
    with transaction.atomic():
        OEMAutoCandidate.objects.bulk_create(create, batch_size=500)
        OEMAutoCandidate.objects.bulk_update(update, FIELDS, batch_size=500)
        for (status, reason), ids in moved.items():
            for i in range(0, len(ids), 1000):
                OEMAutoCandidate.objects.filter(pk__in=ids[i:i + 1000]).update(status=status, reason=reason, run=run, updated_at=now)
    return {'created': len(create), 'updated': len(update), 'unchanged': unchanged, 'moved': sum(map(len, moved.values())), **totals()}


def refresh_after_dry_run(run, finder, results):
    """The dry run's refresh: a failure never fails the analysis (its review cases are already synced); it is logged, the run records
    it and the next run retries."""
    try:
        return refresh(run, finder, results)
    except Exception:
        log.exception('OEM pending refresh failed in run %s; the next run retries it.', run.pk)
        return {'status': 'failed'}


def refresh_after_apply(run, finder, results):
    """After a completed apply run: from the evaluation it applied, its applied rows leave the pending list and its conflicts are
    excluded; a run that wrote something is then graded again (a SKU flagged OEM leaves the class lexicon, so a neighbour may stop
    grading AUTO), so pending is what the next run would take. A failure here never fails the run: it is logged and the next dry run
    refreshes the table."""
    try:
        if not auto_tables_ready():
            return {'status': 'not_migrated'}
        stats = refresh(run, finder, results)
        s = run.applied or {}
        if s.get('applied') or s.get('conflicts'):
            fresh = of.OEMFinder(of.load_snapshot(), finder.table)
            stats = refresh(run, fresh, fresh.evaluate())
    except Exception:
        log.exception('OEM pending refresh failed after apply run %s; the next dry run refreshes it.', run.pk)
        return {'status': 'failed'}
    if 'statuses' in stats:
        from .oem_finder_models import OEMFinderRun
        run.counts = {**(run.counts or {}), 'auto': stats}
        OEMFinderRun.objects.filter(pk=run.pk).update(counts=run.counts)
    return stats


def mark_reverted(link_ids):
    """Called by oem_apply.revert_run: the reverted rows are excluded at once (the next refresh agrees: previously_reverted)."""
    if not link_ids or not auto_tables_ready():
        return 0
    from .oem_finder_models import OEMAutoCandidate, OEMFinderChange
    pairs, now, n = list(OEMFinderChange.objects.filter(pk__in=link_ids, reverted_at__isnull=False).values_list('part_id', 'code')), timezone.now(), 0
    for i in range(0, len(pairs), 200):  # a whole CLI run can revert thousands of rows: bounded OR chains (SQLite caps expression depth)
        match = Q()
        for part_id, code in pairs[i:i + 200]:
            match |= Q(part_id=part_id, candidate=code)
        n += OEMAutoCandidate.objects.filter(match, status__in=('pending', 'applied')).update(status='excluded', reason='previously_reverted', updated_at=now)
    return n


def totals():
    from .oem_finder_models import OEMAutoCandidate
    statuses, pending = Counter(), Counter()
    for status, tier, n in OEMAutoCandidate.objects.values_list('status', 'apply_tier').annotate(n=Count('id')).order_by():
        statuses[status] += n
        if status == 'pending':
            pending[tier] += n
    return {'statuses': dict(statuses), 'pending': dict(pending)}


# ------------------------------------------------------------------ sent to review
def review_values(r, snap, avail, version, apply_tier):
    """The OEMReviewCase of a SKU taken out of automatic application: a review tier a person can approve (CURRENT_REVIEW for a SKU that
    already is the OEM, PROBABLE_BASE or STRONG_PENDING_OWNER_TAGS for a rename), the AUTO tier as underlying_tier, blocker sent_to_review."""
    r = {**r, 'tier': REVIEW_TIER[apply_tier], 'underlying_tier': r['tier'], 'auto_blockers': [*(r.get('auto_blockers') or []), SENT]}
    return of.case_values(r, snap, avail, version)


def sent_case_fields(finder, results, selected, version):
    """For oem_finder.sync_cases: the review cases of SKUs sent to review that still grade AUTO stay open (and keep their decisions)."""
    if not auto_tables_ready():
        return {}
    from .oem_finder_models import OEMAutoCandidate
    sent = {str(p) for p in OEMAutoCandidate.objects.filter(status=SENT).values_list('part_id', flat=True)}
    out = {}
    for pid in sent & set(selected):
        tier = oa.apply_tier(finder.table, results[pid]) if pid in results else None
        if tier:
            out[pid] = review_values(results[pid], of.part_snapshot(finder, pid), finder.avail[pid], version, tier)
    return out


def send_to_review(part_ids, actor, note=''):
    """Take pending rows out of automatic application (apply_auto skips them from now on) and create or reopen their review case. The
    caller holds the matching advisory lock. Repeating it changes nothing."""
    from .oem_finder_models import OEMAutoCandidate, OEMReviewCase
    ids, now, note = list(dict.fromkeys(str(p) for p in part_ids)), timezone.now(), note.strip()[:300]
    out = {'sent': [], 'already': [], 'skipped': []}
    with transaction.atomic():
        rows = {str(c.part_id): c for c in OEMAutoCandidate.objects.select_for_update().filter(part_id__in=ids)}
        for pid in ids:
            c = rows.get(pid)
            reason = 'not_found' if c is None else '' if c.status in ('pending', SENT) else c.status
            if c is not None and c.status == SENT:
                out['already'].append({'part_id': pid, 'sku': c.sku})
                continue
            case = None if reason else OEMReviewCase.objects.select_for_update().filter(part_id=pid).first()
            if case is not None and case.status == 'applied':
                reason = 'case_applied'
            if reason:
                out['skipped'].append({'part_id': pid, 'sku': c.sku if c else '', 'reason': reason, 'label': SEND_SKIPS[reason]})
                continue
            case = case or OEMReviewCase(part_id=pid)
            for key, value in review_values(c.evidence, c.snapshot, c.available_quantity, c.suffix_table_version, c.apply_tier).items():
                setattr(case, key, value)
            case.run, case.status, case.ai, case.decided_by, case.decided_at = c.run, 'review', {}, actor, now
            case.decision = {'action': SENT, 'apply_tier': c.apply_tier, 'note': note}
            case.save()
            c.status, c.reason, c.note, c.decided_by, c.decided_at = SENT, '', note, actor, now
            c.save(update_fields=['status', 'reason', 'note', 'decided_by', 'decided_at', 'updated_at'])
            out['sent'].append({'part_id': pid, 'sku': c.sku, 'case': case.pk})
    return out


# ------------------------------------------------------------------ list, filters and state
def parse_filters(params):
    f = {key: str(params.get(key) or '').strip() for key in FILTER_KEYS}
    f['status'], f['ordering'] = f['status'] or 'pending', f['ordering'] or 'stock'
    for key, allowed in (('status', STATUSES), ('tier', TIERS), ('apply_tier', oa.APPLY_TIERS), ('in_stock', ('true', 'false')), ('ordering', ORDERINGS)):
        if f[key] and f[key] not in allowed:
            raise ValidationError({key: 'El filtro no es válido.'})
    for key in ('chain', 'make', 'system', 'search'):
        if len(f[key]) > 120:
            raise ValidationError({key: 'El filtro es demasiado largo.'})
        f[key] = f[key].upper()
    return f


def rows(f, *, skip=()):
    from .oem_finder_models import OEMAutoCandidate
    qs = OEMAutoCandidate.objects.filter(status=f['status'])
    for key, field in (('tier', 'tier'), ('apply_tier', 'apply_tier'), ('chain', 'chain'), ('make', 'brand'), ('system', 'system')):
        if f[key] and key not in skip:
            qs = qs.filter(**{field: f[key]})
    if f['in_stock']:
        qs = qs.filter(in_stock=f['in_stock'] == 'true')
    if f['search']:
        s = f['search']
        qs = qs.filter(Q(sku__icontains=s) | Q(part__sku__icontains=s) | Q(candidate__icontains=s) | Q(written_form__icontains=s) | Q(part__description__icontains=s))
    return qs


def ordered(qs, ordering):
    return qs.order_by('sku', 'id') if ordering == 'sku' else qs.order_by('-in_stock', '-available_quantity', 'sku', 'id')


def facet(qs, field, n=40):
    return [[v, c] for v, c in qs.exclude(**{field: ''}).values_list(field).annotate(c=Count('id')).order_by('-c', field)[:n]]


def lock_held():
    """Whether a matching pass or another finder run holds the advisory lock right now (read-only pg_locks probe)."""
    if connection.vendor != 'postgresql':
        return False
    from .matching_queue import MATCHING_LOCK
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted AND classid = %s AND objid = %s AND objsubid = 1",
                       [MATCHING_LOCK >> 32, MATCHING_LOCK & 0xFFFFFFFF])
        return cursor.fetchone() is not None


def apply_state():
    """Per apply tier: pending rows (all, in stock) and whether a canary of the current rules unlocked batches."""
    from .oem_finder_models import OEMAutoCandidate
    counts = {t: [0, 0] for t in oa.APPLY_TIERS}
    for tier, stock, n in OEMAutoCandidate.objects.filter(status='pending').values_list('apply_tier', 'in_stock').annotate(n=Count('id')).order_by():
        counts[tier][0] += n
        counts[tier][1] += n if stock else 0
    return {t: {'pending': counts[t][0], 'in_stock': counts[t][1], 'canary_done': oa.canary_done(t)} for t in oa.APPLY_TIERS}


def overview(f):
    from .oem_finder_models import OEMAutoCandidate, OEMFinderRun
    halt = oa.active_halt()
    last = OEMFinderRun.objects.filter(status='completed', counts__auto__has_key='statuses').order_by('-finished_at', '-id').first()
    running = OEMFinderRun.objects.filter(mode='apply_auto', status='running').exclude(scope__stage='review').order_by('-pk').values_list('pk', flat=True).first()
    return {
        'tiers': dict(rows(f, skip=('tier', 'apply_tier')).values_list('tier').annotate(c=Count('id')).order_by()),
        'apply_tiers': dict(rows(f, skip=('apply_tier',)).values_list('apply_tier').annotate(c=Count('id')).order_by()),
        'statuses': dict(OEMAutoCandidate.objects.values_list('status').annotate(c=Count('id')).order_by()),
        'facets': {'chains': facet(rows(f, skip=('chain',)), 'chain'), 'makes': facet(rows(f, skip=('make',)), 'brand'),
                   'systems': facet(rows(f, skip=('system',)), 'system')},
        'apply': apply_state(), 'sizes': MODES,
        'halt': halt and {'reason': halt.reason, 'run': halt.run_id, 'created_by': halt.created_by.username if halt.created_by_id else None,
                          'created_at': halt.created_at},
        'busy': {'lock': lock_held(), 'running': running},
        'last_refresh': last and {'id': last.pk, 'mode': last.mode, 'stage': (last.scope or {}).get('stage', ''), 'finished_at': last.finished_at},
    }


def page_rows(objs):
    from .models import Part
    from .oem_review import code_dicts
    objs = list(objs)
    ids = [c.part_id for c in objs]
    parts, codes = {str(p.pk): p for p in Part.objects.filter(pk__in=ids)}, code_dicts(ids)
    out = []
    for c in objs:
        pid, ev = str(c.part_id), c.evidence or {}
        part, p = parts.get(pid), ev.get('primary') or {}
        lex = p.get('lexicon') or {}
        stale = ('' if c.status != 'pending' else 'missing' if part is None else 'retired' if not part.active or part.merged_into_id
                 else 'snapshot_changed' if oa.current_snapshot(part, codes.get(pid, {})) != c.snapshot else '')
        label = SEND_SKIPS.get(c.status, '') if c.status in ('applied', 'stale') else REASONS.get(c.reason, '')
        out.append({
            'id': c.pk, 'part_id': c.part_id, 'sku': part.sku if part else c.sku, 'evaluated_sku': c.sku,
            'description': part.description if part else c.snapshot.get('description', ''), 'tier': c.tier, 'apply_tier': c.apply_tier,
            'candidate': c.candidate, 'written_form': c.written_form, 'brand': c.brand, 'makes': c.makes, 'system': c.system,
            'grade': f'F{c.grade}' if c.grade else '', 'chain': c.chain, 'chain_tokens': c.chain_tokens, 'owner_tags': c.owner_tags,
            'method': 'flag' if c.apply_tier == oa.FLAG else 'rename', 'in_stock': c.in_stock, 'available_quantity': c.available_quantity,
            'evidence': {'lexicon': {'result': lex.get('result'), 'share': lex.get('share'), 'n_other_keys': lex.get('n_other_keys'),
                                     'top_heads': (lex.get('top_heads') or [])[:3]},
                         'units': p.get('units') or [], 'attributions': p.get('attributions') or [], 'n_suppliers': p.get('n_suppliers', 0),
                         'verified_oem_alterno': bool(p.get('verified_oem_alterno')), 'second_unit': bool(p.get('second_unit')),
                         'genuine_tokens': p.get('genuine_tokens') or [], 'siblings': (ev.get('siblings_to_merge_into_this') or [])[:6],
                         'make_source': ev.get('make_source') or '', 'head': ev.get('head') or '', 'reasons': ev.get('reasons') or [],
                         'warnings': ev.get('warnings') or []},
            'status': c.status, 'reason': c.reason, 'reason_label': label, 'stale': stale,
            'stale_label': oa.SKIP_LABELS.get(stale, '') if stale else '', 'note': c.note,
            'decided_by': c.decided_by.username if c.decided_by_id else None, 'decided_at': c.decided_at, 'updated_at': c.updated_at, 'run': c.run_id,
        })
    return out


# ------------------------------------------------------------------ preview and apply (oem_apply.apply_auto itself)
def tier_label(tier):
    return {'AUTO_FLAG_CURRENT': 'SKU ya es el OEM', 'AUTO_RENAME_BASE': 'renombrar a la base OEM',
            'STRONG_PENDING_OWNER_TAGS': 'renombrar con etiquetas confirmadas'}[tier]


def size_kwargs(mode):
    return {'canary': MODES['canary']} if mode == 'canary' else {'limit': MODES['batch']}


def preview(tier, mode):
    """Read-only apply_auto of one apply tier and size: the exact rows the apply would take now and what the catalog would say."""
    outcome = oa.apply_auto(tier=tier, read_only=True, detail=True, **size_kwargs(mode))
    p = outcome['plan'][tier]
    return {'tier': tier, 'mode': mode, 'size': MODES[mode], 'eligible': outcome['eligible'], 'selected': p['selected'], 'would_apply': p['applied'],
            'conflicts': p['conflicts'], 'skipped': p['skipped'], 'excluded': outcome['excluded'], 'rows': p['rows'],
            'canary_done': oa.canary_done(tier), 'halt': (h := oa.active_halt()) and {'reason': h.reason, 'created_at': h.created_at},
            'suffix_table_version': outcome['suffix_table_version'], 'seconds': outcome['timings']['total']}


def operation_run(operation_id):
    from .oem_finder_models import OEMFinderRun
    return OEMFinderRun.objects.filter(mode='apply_auto', scope__operation_id=str(operation_id)).order_by('pk').first()


def apply(tier, mode, operation_id, actor):
    """One canary (50) or batch (100) apply_auto run of an apply tier, started by a superuser. The caller holds the matching advisory lock.
    The same operation id returns its run again (replayed=True) instead of applying twice."""
    run = operation_run(operation_id)
    if run is not None:
        scope = run.scope or {}
        if (scope.get('tier'), bool(scope.get('canary'))) != (tier, mode == 'canary'):
            raise Refused('operation_reused', 'Ese identificador de operación ya se usó para otra aplicación. Actualiza la página e inténtalo de nuevo.')
        return run, True
    halt = oa.active_halt()
    if halt:
        raise Refused('halted', f'La aplicación automática está detenida: {halt.reason}. Levanta la detención para continuar.')
    if mode == 'batch' and not oa.canary_done(tier):
        raise Refused('needs_canary', f'Primero aplica un canario de {MODES["canary"]} de «{tier_label(tier)}»: los lotes de {MODES["batch"]} se habilitan '
                                      'cuando el canario termina.', tier=tier)
    try:
        outcome = oa.apply_auto(tier=tier, actor=actor, stage='admin', operation_id=operation_id, **size_kwargs(mode))
    except oa.ApplyRefused as refused:
        raise Refused(refused.reason, str(refused), **refused.extra)
    return outcome['run'], False
