"""OEM finder phase 2B: auto-apply the AUTO tiers with an audit per row, a spot-check export, a halt rule and an undo.

apply_auto() grades the catalog like a dry run and applies only AUTO_FLAG_CURRENT, AUTO_RENAME_BASE and the renames whose tags the
owner confirmed (STRONG_PENDING_OWNER_TAGS in the design), in stock first, in batches of at most 100, after a canary. Each row runs
in its own transaction: lock the Part's supplier Accounts then the Part, re-check it against the snapshot (SKU, description,
taxonomy, OEM flag, codes, no new claimant), write the verified OEM PartCode and let catalog_identity.prefer_oem flag or rename the
SAME Part UUID (the old SKU stays as an alterno). Every applied row links its CatalogIdentityChange to the run in OEMFinderChange
(evidence plus what an undo needs). A ValidationError from prefer_oem turns the row into a CONFLICT review case, never a retry.
revert_run() undoes a run or one Part of it, idempotently. Nothing here calls an AI provider.
"""
import csv
import io
import random
import time
from collections import Counter

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import oem_finder as of
from .catalog_families import normalized_reference
from .catalog_identity import prefer_oem, reference_owners, reference_pattern, rename_conflicts
from .models import Account, CatalogIdentityChange, Part, PartCode, SupplierItem

APPLY_TIERS = of.APPLY_TIERS
FLAG = 'AUTO_FLAG_CURRENT'
METHODS = {'AUTO_FLAG_CURRENT': 'flag_current', 'AUTO_RENAME_BASE': 'rename_base', 'STRONG_PENDING_OWNER_TAGS': 'rename_confirmed_tags'}
BATCH_SIZE = 100
SPOT_CHECK_ROWS = 20
SERVICE_USER = 'motionpartes-oem-finder-service'
SOURCE_VERSION = 'v' + of.OEM_FINDER_VERSION.rsplit('-', 1)[-1]  # oem-finder-2 -> v2
SKIP_LABELS = {
    'missing': 'el SKU ya no existe', 'retired': 'el SKU se retiró o se agrupó', 'snapshot_changed': 'el SKU cambió desde el análisis',
    'new_claimant': 'otro SKU reclama ahora el mismo número', 'canonical_differs': 'el número del fabricante no es el SKU actual',
    'previously_reverted': 'se revirtió antes con el mismo OEM', 'open_conflict': 'tiene un conflicto de aplicación abierto',
    'sku_changed': 'el SKU cambió después de aplicarse', 'old_sku_taken': 'el SKU anterior ya identifica a otro SKU',
    'codes_changed': 'se editaron los códigos que creó la aplicación', 'sent_to_review': 'se envió a la revisión OEM',
}


class Skip(Exception):
    """The row is not applied (or not reverted): nothing was written; reason is a SKIP_LABELS key."""

    def __init__(self, reason, detail=''):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


class ApplyRefused(Exception):
    """The run cannot start: an active halt or a tier without a canary run."""

    def __init__(self, reason, message, **extra):
        super().__init__(message)
        self.reason, self.extra = reason, extra


def apply_tables_ready():
    from .oem_finder_models import OEMApplyHalt, OEMFinderChange
    return of.tables_ready() and {OEMFinderChange._meta.db_table, OEMApplyHalt._meta.db_table} <= set(connection.introspection.table_names())


def service_user():
    """The disabled, non-login identity that signs automated identity changes (like the matching worker's)."""
    user, created = get_user_model().objects.get_or_create(username=SERVICE_USER, defaults={
        'email': 'oem-finder-service@motionpartes.invalid', 'is_active': False})
    if created:
        user.set_unusable_password()
        user.save(update_fields=['password'])
    if user.is_active or user.has_usable_password():
        raise RuntimeError('La identidad de servicio del buscador OEM debe estar desactivada.')
    return user


# ------------------------------------------------------------------ halt rule and canary gate
def active_halt():
    from .oem_finder_models import OEMApplyHalt
    return OEMApplyHalt.objects.filter(cleared_at__isnull=True).select_related('created_by', 'run').first()


def halt(reason, *, actor=None, run=None):
    from .oem_finder_models import OEMApplyHalt
    with transaction.atomic():
        current = OEMApplyHalt.objects.select_for_update().filter(cleared_at__isnull=True).first()
        return current or OEMApplyHalt.objects.create(reason=reason[:300], run=run, created_by=actor)


def resume(*, actor=None):
    from .oem_finder_models import OEMApplyHalt
    return OEMApplyHalt.objects.filter(cleared_at__isnull=True).update(cleared_at=timezone.now(), cleared_by=actor)


def canary_done(tier):
    """A completed canary run of the current rules applied rows of this tier that were not all reverted since (a failed spot-check
    is undone and needs a new canary), or was scoped to the tier and found none."""
    from .oem_finder_models import OEMFinderChange, OEMFinderRun
    for pk, scope, applied in OEMFinderRun.objects.filter(mode='apply_auto', status='completed', rules_version=of.OEM_FINDER_VERSION).values_list('pk', 'scope', 'applied'):
        scope, applied = scope or {}, applied or {}
        if not scope.get('canary'):
            continue
        if applied.get('tiers', {}).get(tier):
            if OEMFinderChange.objects.filter(run_id=pk, tier=tier, reverted_at__isnull=True).exists():
                return True
        elif scope.get('tier') == tier and not applied.get('selected'):
            return True
    return False


# ------------------------------------------------------------------ selection
def owner_confirmed_tags(table, r):
    entries = [c for e in r['_cs']['tag_entries'] for c in (e.components or [e])]
    entries += [table.exact[t] for t in r['primary']['genuine_tokens'] if t in table.exact]
    return sorted({e.token for e in entries if e.owner_confirmed})


def apply_tier(table, r):
    """AUTO_FLAG_CURRENT; AUTO_RENAME_BASE when every tag is a confident seed row; STRONG_PENDING_OWNER_TAGS when the rename rests on
    a tag the owner confirmed (G, NP...): the design's pending tier, auto once confirmed. Every other tier is never applied."""
    if r['tier'] == FLAG:
        return FLAG
    if r['tier'] == 'AUTO_RENAME_BASE':
        return 'STRONG_PENDING_OWNER_TAGS' if owner_confirmed_tags(table, r) else 'AUTO_RENAME_BASE'
    return None


def candidates(finder, results, tiers):
    """[(part_id, apply tier)] in stock first (owner decision), then tier order, most stock, SKU."""
    rows = [(pid, t) for pid, r in results.items() for t in [apply_tier(finder.table, r)] if t in tiers]
    rows.sort(key=lambda x: (finder.avail[x[0]] <= 0, APPLY_TIERS.index(x[1]), -finder.avail[x[0]], finder.by_id[x[0]]['sku']))
    return rows


def exclusions():
    """What apply_auto leaves to a human: (part, OEM) pairs reverted before, Parts with an open apply-conflict case and Parts a person
    sent to the review queue from the pending list (OEMAutoCandidate, migration 0038)."""
    from .oem_auto import auto_tables_ready
    from .oem_finder_models import OEMAutoCandidate, OEMFinderChange, OEMReviewCase
    reverted = {(str(p), c) for p, c in OEMFinderChange.objects.filter(reverted_at__isnull=False).values_list('part_id', 'code')}
    conflicts = {str(p) for p, b in OEMReviewCase.objects.filter(tier='CONFLICT', status__in=['review', 'dismissed']).values_list('part_id', 'blockers')
                 if 'apply_conflict' in (b or [])}
    sent = {str(p) for p in OEMAutoCandidate.objects.filter(status='sent_to_review').values_list('part_id', flat=True)} if auto_tables_ready() else set()
    return reverted, conflicts, sent


def handled(rows, results):
    """Rows a human must look at first: sent to review, an open apply-conflict case, or the same OEM reverted before (never re-applied
    silently). Before migration 0036 (read-only preview) none can exist."""
    if not apply_tables_ready():
        return rows, {}
    reverted, conflicts, sent = exclusions()
    keep, out = [], Counter()
    for pid, tier in rows:
        if pid in sent:
            out['sent_to_review'] += 1
        elif pid in conflicts:
            out['open_conflict'] += 1
        elif (pid, results[pid]['proposed_main']) in reverted:
            out['previously_reverted'] += 1
        else:
            keep.append((pid, tier))
    return keep, dict(out)


# ------------------------------------------------------------------ one row
class Known:
    """What the run's snapshot knew, to tell a new claimant from one the finder already weighed."""

    def __init__(self, finder):
        self.parts = {p['id']: (p['sku'], bool(p['active']), p['merged_into_id']) for p in finder.snapshot.parts}
        self.codes = {(c['part_id'], c['code'], c['brand']) for c in finder.snapshot.codes}
        self.items = {(s['part_id'], s['codigo']) for s in finder.snapshot.items}

    def absorb(self, pid, sku, codes):
        """This run's own committed writes are not new claimants for its later rows."""
        self.parts[pid] = (sku, True, None)
        self.codes.update((pid, c['code'], c['brand']) for c in codes.values())


def new_claimant(known, pid, keys):
    """A Part SKU, alterno or linked supplier code containing the key that the snapshot did not have (a new or renamed SKU could
    now claim the number): the finder never weighed it."""
    for key in filter(None, keys):
        rx = reference_pattern(key, anchored=False)
        for id_, sku, active, merged in Part.objects.filter(sku__iregex=rx).exclude(pk=pid).values_list('id', 'sku', 'active', 'merged_into_id'):
            if key in normalized_reference(sku) and known.parts.get(str(id_)) != (sku, active, str(merged) if merged else None):
                return sku
        for part_id, code, brand in PartCode.objects.filter(code__iregex=rx).exclude(part_id=pid).values_list('part_id', 'code', 'brand'):
            if key in normalized_reference(code) and (str(part_id), code, brand) not in known.codes:
                return code
        for part_id, codigo in SupplierItem.objects.filter(codigo__iregex=rx, part__isnull=False).exclude(part_id=pid).values_list('part_id', 'codigo'):
            if key in normalized_reference(codigo) and (str(part_id), codigo) not in known.items:
                return codigo
    return None


def code_rows(pid):
    return {c['id']: c for c in PartCode.objects.filter(part_id=pid).values('id', 'code', 'brand', 'ref_type', 'reference_source')}


def current_snapshot(part, codes):
    return {'sku': part.sku, 'description': part.description, 'category': part.category or '', 'subcategory': part.subcategory or '',
            'is_OEM': bool(part.is_OEM), 'codes': sorted([c['code'], c['brand'], c['ref_type']] for c in codes.values())}


def reference_source(tier, r, run_id):
    rule = 'current' if tier == FLAG else 'strong-' + '-'.join(c['tok'] for c in r.get('suffix_chain') or [])
    return f'algo:oem-finder:{SOURCE_VERSION}:{rule}:{run_id}'


def code_conflict(part, code, brand):
    if len(code) > 120:
        return 'El OEM supera 120 caracteres y no puede guardarse como referencia.'
    owner = PartCode.objects.filter(brand=brand, code=code).exclude(part=part.pk).values_list('part__sku', flat=True).first()
    return f'{brand} {code} ya es un alterno de {owner}.' if owner else ''


def ensure_code(part, code, brand, ref_type, source):
    """The (brand, code) reference on this Part: created, or retyped to a verified OEM when it existed unverified."""
    problem = code_conflict(part, code, brand)
    if problem:
        raise ValidationError(problem)
    existing = PartCode.objects.filter(part=part, brand=brand, code=code).first()
    if existing is None:
        return PartCode.objects.create(part=part, code=code, brand=brand, ref_type=ref_type, reference_source=source)
    if source and (existing.ref_type != ref_type or not existing.reference_source.strip()):
        existing.ref_type, existing.reference_source = ref_type, source
        existing.save(update_fields=['ref_type', 'reference_source'])
    return existing


def lock_part(pid):
    """Lock order Account -> Part, as supplier imports and the matching engine take them."""
    suppliers = SupplierItem.objects.filter(part_id=pid).values_list('supplier_id', flat=True)
    list(Account.objects.select_for_update().filter(pk__in=suppliers).order_by('pk'))
    return Part.objects.select_for_update().filter(pk=pid).first()


def apply_row(run, finder, known, pid, r, tier, batch, actor):
    from .oem_finder_models import OEMFinderChange
    code, brand = r['proposed_main'], r['proposed_brand']
    source = reference_source(tier, r, run.pk)
    with transaction.atomic():
        part = lock_part(pid)
        if part is None:
            raise Skip('missing')
        if not part.active or part.merged_into_id:
            raise Skip('retired')
        codes = code_rows(pid)
        if current_snapshot(part, codes) != of.part_snapshot(finder, pid):
            raise Skip('snapshot_changed')
        who = new_claimant(known, pid, [r['primary']['key']] + ([] if tier == FLAG else [normalized_reference(part.sku)]))
        if who:
            raise Skip('new_claimant', who)
        if tier == FLAG and code != part.sku:
            raise Skip('canonical_differs', code)
        ensure_code(part, code, brand, 'oem', source)
        written = r.get('written_form') or ''
        if written and written != code:  # the catalog's spelling stays searchable; unverified, so it is never promoted on its own
            ensure_code(part, written, brand, 'oem', '')
        before = {'sku': part.sku, 'name': part.name, 'is_OEM': part.is_OEM}
        part = prefer_oem(part, actor=actor, selected={'code': code, 'brand': brand}, confirm_current=tier == FLAG)
        if part.sku != code or not part.is_OEM:
            raise RuntimeError(f'prefer_oem dejó {part.sku} sin aplicar {code}.')
        if tier == FLAG:
            change = CatalogIdentityChange.objects.create(part=part, previous_sku=part.sku, sku=part.sku, actor=actor, reference={
                'code': code, 'brand': brand, 'ref_type': 'oem', 'reference_source': source, 'action': 'confirm_current'})
        else:
            change = CatalogIdentityChange.objects.filter(part=part, previous_sku=before['sku'], sku=code).order_by('-pk').first()
        after = code_rows(pid)
        undo = {**before, 'created_codes': [after[k] for k in sorted(after.keys() - codes.keys())],
                'updated_codes': [{'before': codes[k], 'after': after[k]} for k in sorted(after.keys() & codes.keys()) if after[k] != codes[k]]}
        link = OEMFinderChange.objects.create(change=change, run=run, part=part, tier=tier, method=METHODS[tier], batch=batch, code=code,
                                              brand=brand, evidence={**of.evidence(finder, pid, r), 'snapshot': of.part_snapshot(finder, pid)},
                                              undo=undo)
    known.absorb(pid, part.sku, after)
    return link


def error_text(detail):
    if isinstance(detail, dict):
        return ' '.join(error_text(v) for v in detail.values())
    if isinstance(detail, (list, tuple)):
        return ' '.join(error_text(v) for v in detail)
    return str(detail)


def conflict_case(run, finder, pid, r, tier, message):
    """CONFLICT review case (merge review) for a row prefer_oem refused; the dry run keeps it open until someone decides."""
    from .matching_engine import digest
    from .oem_finder_models import OEMReviewCase
    fields = of.case_fields(finder, pid, r, of.table_version(finder.table))
    blockers = fields['blockers'] + ['apply_conflict']
    fields.update(tier='CONFLICT', tier_rank=of.TIER_ORDER.index('CONFLICT'), underlying_tier=r['tier'], blockers=blockers,
                  evidence={**fields['evidence'], 'apply_conflict': {'run': run.pk, 'tier': tier, 'detail': message}},
                  fingerprint=digest([of.OEM_FINDER_VERSION, fields['snapshot'], fields['candidate'], 'CONFLICT', blockers, 'apply', message]))
    with transaction.atomic():
        case = OEMReviewCase.objects.select_for_update().filter(part_id=pid).first() or OEMReviewCase(part_id=pid)
        for key, value in fields.items():
            setattr(case, key, value)
        case.run, case.status, case.ai, case.decided_by, case.decided_at, case.decision = run, 'review', {}, None, None, {}
        case.save()
    return case


# ------------------------------------------------------------------ runs
def plan_row(pid, r, tier):
    """Read-only preview of apply_row: the conflicts prefer_oem would raise today (no lock, no write)."""
    part = Part.objects.filter(pk=pid).first()
    if part is None or not part.active or part.merged_into_id:
        return 'skipped', 'retired'
    code, brand = r['proposed_main'], r['proposed_brand']
    if tier == FLAG and code != part.sku:
        return 'skipped', 'canonical_differs'
    problem = code_conflict(part, code, brand) or ('; '.join(rename_conflicts(part, code)[:3]) if tier != FLAG else '')
    return ('conflict', problem) if problem else ('applied', '')


def plan(finder, results, rows, detail=False):
    """Per tier: selected, would apply, conflicts (first 10 as examples), skipped by reason; detail=True also lists every row."""
    outermost = not connection.in_atomic_block
    with transaction.atomic():
        if outermost and connection.vendor == 'postgresql':  # a preview never writes, whatever plan_row grows into
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION READ ONLY')
        out = {t: {'selected': 0, 'applied': 0, 'conflicts': 0, 'skipped': {}, 'examples': [], **({'rows': []} if detail else {})} for t in APPLY_TIERS}
        for pid, tier in rows:
            outcome, why = plan_row(pid, results[pid], tier)
            o, p, r = out[tier], finder.by_id[pid], results[pid]
            o['selected'] += 1
            if outcome == 'applied':
                o['applied'] += 1
            elif outcome == 'conflict':
                o['conflicts'] += 1
                if len(o['examples']) < 10:
                    o['examples'].append({'sku': p['sku'], 'oem': r['proposed_main'], 'detail': why})
            else:
                o['skipped'][why] = o['skipped'].get(why, 0) + 1
            if detail:
                o['rows'].append({'part_id': pid, 'sku': p['sku'], 'description': p['description'], 'oem': r['proposed_main'], 'brand': r['proposed_brand'],
                                  'method': 'flag' if tier == FLAG else 'rename', 'outcome': outcome, 'detail': why,
                                  'available_quantity': finder.avail[pid], 'in_stock': finder.avail[pid] > 0})
    return out


def apply_auto(*, tier=None, limit=None, canary=None, actor=None, stage='command', read_only=False, snapshot=None, table=None,
               batch_size=BATCH_SIZE, operation_id=None, detail=False):
    """One apply_auto run (the caller holds the matching advisory lock). read_only=True only previews: no run, no write (detail=True
    lists every previewed row). operation_id (the admin's idempotency key) is kept in the run's scope. A completed run refreshes the
    pending AUTO rows (OEMAutoCandidate)."""
    from .catalog_suffixes import suffix_table
    from .oem_finder_models import OEMFinderRun
    tiers = [tier] if tier else list(APPLY_TIERS)
    if tier and tier not in APPLY_TIERS:
        raise ValueError(f'{tier} no es un nivel automático.')
    batch_size = max(1, min(batch_size, BATCH_SIZE))
    if not read_only:
        current = active_halt()
        if current:
            raise ApplyRefused('halted', f'La aplicación automática está detenida: {current.reason}', halt=current.pk)
        missing = [] if canary else [t for t in tiers if not canary_done(t)]
        if missing:
            raise ApplyRefused('needs_canary', 'Primero ejecuta un canario (--canary 50) de ' + ', '.join(missing) + '.', tiers=missing)
    t0 = time.monotonic()
    table = table or suffix_table()
    scope = {'tier': tier, 'limit': limit, 'canary': canary, 'in_stock_first': True, 'batch_size': batch_size, 'stage': stage,
             **({'operation_id': str(operation_id)} if operation_id else {})}
    run = None
    if not read_only:
        actor = actor or service_user()
        run = OEMFinderRun.objects.create(mode='apply_auto', rules_version=of.OEM_FINDER_VERSION, suffix_table_version=of.table_version(table),
                                          scope=scope, actor=actor)
    try:
        snapshot = snapshot or of.load_snapshot()
        finder = of.OEMFinder(snapshot, table)
        results = finder.evaluate()
        t_eval = time.monotonic()
        rows, excluded = handled(candidates(finder, results, tiers), results)
        eligible, take = len(rows), canary or limit
        rows = rows[:take] if take else rows
        counts = of.report(finder, results, [pid for pid, _ in rows])
        if read_only:
            return {'run': None, 'plan': plan(finder, results, rows, detail), 'excluded': excluded, 'eligible': eligible, 'counts': counts, 'finder': finder,
                    'results': results, 'rows': rows, 'timings': {'evaluate': round(t_eval - t0, 2), 'total': round(time.monotonic() - t0, 2)},
                    'suffix_table_version': of.table_version(table)}
        summary = execute_batches(run, finder, results, rows, batch_size, actor)
    except Exception as error:
        if run is not None:
            run.status, run.finished_at = 'failed', timezone.now()
            run.errors = (run.errors or []) + [{'error': type(error).__name__, 'detail': str(error)[:500]}]
            run.save(update_fields=['status', 'finished_at', 'errors'])
            halt(f'La ejecución {run.pk} falló ({type(error).__name__}); revisa antes de continuar.', actor=actor, run=run)
        raise
    summary.update(excluded=excluded, selected=len(rows))
    summary['spot_check'] = spot_check_sample(run)
    run.status, run.counts, run.applied, run.finished_at = 'completed', counts, summary, timezone.now()
    run.timings = {'evaluate': round(t_eval - t0, 2), 'total': round(time.monotonic() - t0, 2)}
    run.save(update_fields=['status', 'counts', 'applied', 'timings', 'finished_at', 'errors'])
    from .oem_auto import refresh_after_apply
    return {'run': run, 'summary': summary, 'counts': counts, 'finder': finder, 'results': results, 'rows': rows, 'timings': run.timings,
            'suffix_table_version': of.table_version(table), 'auto': refresh_after_apply(run, finder, results)}


def execute_batches(run, finder, results, rows, batch_size, actor):
    known, tiers, skipped, batches, errors = Known(finder), Counter(), Counter(), [], []
    applied = conflicts = 0
    stopped = None
    for index in range(0, len(rows), batch_size):
        current = active_halt()
        if current:
            stopped = 'halted'
            break
        if of.import_running():
            stopped = 'catalog_import'
            break
        stats = Counter()
        for pid, tier in rows[index:index + batch_size]:
            r = results[pid]
            try:
                apply_row(run, finder, known, pid, r, tier, len(batches) + 1, actor)
            except Skip as skip:
                stats['skipped'] += 1
                skipped[skip.reason] += 1
            except ValidationError as error:
                conflict_case(run, finder, pid, r, tier, error_text(error.detail)[:500])
                stats['conflicts'] += 1
                conflicts += 1
            except Exception as error:  # rolled back; recorded; the run stops after this batch and raises the halt flag
                stats['errors'] += 1
                errors.append({'part': pid, 'sku': finder.by_id[pid]['sku'], 'error': type(error).__name__, 'detail': str(error)[:300]})
            else:
                stats['applied'] += 1
                tiers[tier] += 1
                applied += 1
        batches.append({'index': len(batches) + 1, **{k: stats[k] for k in ('applied', 'conflicts', 'skipped', 'errors')}})
        if stats['errors']:
            stopped = 'errors'
            halt(f'La ejecución {run.pk} tuvo {stats["errors"]} errores inesperados en el lote {len(batches)}; revisa antes de continuar.',
                 actor=actor, run=run)
            break
    run.errors = errors[:50]
    return {'applied': applied, 'conflicts': conflicts, 'skipped': dict(skipped), 'errors': len(errors), 'tiers': dict(tiers),
            'batches': batches, 'stopped': stopped}


def spot_check_sample(run):
    ids = list(run.changes.order_by('pk').values_list('pk', flat=True))
    return sorted(random.Random(f'oem-spot-check:{run.pk}').sample(ids, min(SPOT_CHECK_ROWS, len(ids))))


# ------------------------------------------------------------------ spot-check export
SPOT_COLUMNS = [('run', 'ejecucion'), ('change', 'cambio'), ('batch', 'lote'), ('tier', 'nivel'), ('method', 'metodo'), ('part_id', 'id_sku'),
                ('previous_sku', 'sku_anterior'), ('sku', 'sku_nuevo'), ('current_sku', 'sku_actual'), ('code', 'oem'), ('brand', 'marca'),
                ('description', 'descripcion'), ('system', 'sistema'), ('grade', 'grado'), ('lexicon', 'lexico'), ('lexicon_share', 'lexico_coincidencia'),
                ('lexicon_keys', 'lexico_claves'), ('class_heads', 'sustantivos_de_la_clase'), ('units', 'unidades'), ('attributions', 'atribuciones'),
                ('suppliers', 'proveedores'), ('available_quantity', 'existencias'), ('reference_source', 'fuente'), ('reasons', 'motivos'),
                ('reverted_at', 'revertido'), ('verdict', 'correcto_si_no')]


def spot_check_rows(run):
    from .oem_finder_models import OEMFinderChange
    ids = (run.applied or {}).get('spot_check') or []
    rows = []
    for link in OEMFinderChange.objects.filter(run=run, pk__in=ids).select_related('change', 'part').order_by('batch', 'pk'):
        ev = link.evidence or {}
        primary, snap = ev.get('primary') or {}, ev.get('snapshot') or {}
        lex = primary.get('lexicon') or {}
        rows.append({'run': run.pk, 'change': link.change_id, 'batch': link.batch, 'tier': link.tier, 'method': link.method, 'part_id': str(link.part_id),
                     'previous_sku': link.change.previous_sku, 'sku': link.change.sku, 'current_sku': link.part.sku, 'code': link.code,
                     'brand': link.brand, 'description': snap.get('description', link.part.description), 'system': primary.get('system', ''),
                     'grade': primary.get('grade_label', ''), 'lexicon': lex.get('result') or '', 'lexicon_share': lex.get('share'),
                     'lexicon_keys': lex.get('n_other_keys'), 'class_heads': ', '.join(f'{h} ({n})' for h, n in lex.get('top_heads') or []),
                     'units': ', '.join(primary.get('units') or []), 'attributions': ', '.join(primary.get('attributions') or []),
                     'suppliers': primary.get('n_suppliers', 0), 'available_quantity': ev.get('available_quantity', 0),
                     'reference_source': (link.change.reference or {}).get('reference_source', ''), 'reasons': ' | '.join(ev.get('reasons') or []),
                     'reverted_at': link.reverted_at.isoformat() if link.reverted_at else '', 'verdict': '', 'evidence': ev})
    return rows


def csv_cell(value):
    """Catalog text a spreadsheet would run as a formula (=, +, -, @) stays text when the owner opens the sample."""
    if value is None:
        return ''
    if isinstance(value, str) and value[:1] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + value
    return value


def spot_check_csv(rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([header for _, header in SPOT_COLUMNS])
    for row in rows:
        writer.writerow([csv_cell(row.get(key)) for key, _ in SPOT_COLUMNS])
    return out.getvalue()


# ------------------------------------------------------------------ undo
CODE_KEYS = ('id', 'code', 'brand', 'ref_type', 'reference_source')


def written_rows(part, c):
    """This Part's rows for one PartCode the apply wrote: the same id (edited since?) or the same brand and code (re-created since)."""
    return list(PartCode.objects.filter(Q(pk=c['id']) | Q(brand=c['brand'], code=c['code']), part=part).values(*CODE_KEYS))


def same_code(row, c):
    return all(row[k] == c[k] for k in CODE_KEYS[1:])


def revert_link(link_id, actor):
    """Undo one applied row: the old SKU back (when nobody else holds it), the PartCodes the run created deleted and the ones it
    retyped restored, the OEM flag cleared, a reverse CatalogIdentityChange written and reverted_at set. All or nothing: a row whose
    SKU, grouping or written PartCodes were edited since is skipped untouched. Repeating it changes nothing."""
    from .oem_finder_models import OEMFinderChange
    part_id = OEMFinderChange.objects.values_list('part_id', flat=True).get(pk=link_id)
    with transaction.atomic():
        part = lock_part(part_id)
        link = OEMFinderChange.objects.select_for_update().select_related('change').get(pk=link_id)
        if link.reverted_at:
            return 'already_reverted'
        change, undo = link.change, link.undo or {}
        if not part.active or part.merged_into_id:
            raise Skip('retired')  # a later grouping decision owns the identity now
        if part.sku != change.sku:
            raise Skip('sku_changed', part.sku)
        rename = change.previous_sku != change.sku
        if rename and (reference_owners(change.previous_sku, part, merged_here=False)
                       or Part.objects.filter(sku__iexact=change.previous_sku).exclude(pk=part.pk).exists()):
            raise Skip('old_sku_taken', change.previous_sku)
        delete, restore, edited = [], [], []
        for c in undo.get('created_codes') or []:
            for row in written_rows(part, c):
                (delete if same_code(row, c) else edited).append(row)
        for u in undo.get('updated_codes') or []:
            for row in written_rows(part, u['after']):
                if same_code(row, u['after']):
                    restore.append((row['id'], u['before']))
                elif not same_code(row, u['before']):
                    edited.append(row)
        if edited:  # all or nothing: a kept algo OEM would let reference reconciliation rename the SKU again
            raise Skip('codes_changed', ', '.join(sorted({f'{r["brand"]} {r["code"]}'.strip() for r in edited})))
        PartCode.objects.filter(pk__in=[r['id'] for r in delete]).delete()
        for pk, before in restore:
            PartCode.objects.filter(pk=pk).update(**{k: before[k] for k in CODE_KEYS[1:]})
        fields = ['is_OEM']
        part.is_OEM = bool(undo.get('is_OEM'))
        if rename:
            part.sku = change.previous_sku
            fields.append('sku')
            if part.name == change.sku and undo.get('name') == change.previous_sku:
                part.name = change.previous_sku
                fields.append('name')
        part.save(update_fields=fields)
        reverse = CatalogIdentityChange.objects.create(part=part, previous_sku=change.sku, sku=part.sku, actor=actor, reference={
            'action': 'revert', 'reverts': change.pk, 'run': link.run_id, 'code': link.code, 'brand': link.brand, 'method': link.method})
        link.reverted_at, link.reverted_by, link.revert_change = timezone.now(), actor, reverse
        link.save(update_fields=['reverted_at', 'reverted_by', 'revert_change'])
        if (link.evidence or {}).get('review'):  # an approval from the review queue: its case goes back to the queue
            from .oem_review import reopen_reverted
            reopen_reverted(link, actor)
    return 'reverted'


def revert_run(run_id, *, actor=None, part=None):
    """Undo every row of a run (or one Part of it), newest first; rows already reverted are left as they are."""
    from .oem_finder_models import OEMFinderChange
    links = OEMFinderChange.objects.filter(run_id=run_id).order_by('-pk')
    if part:
        links = links.filter(part_id=part)
    actor = actor or service_user()
    outcome, skipped, undone = Counter(), [], []
    for link_id, sku in links.values_list('pk', 'change__sku'):
        try:
            result = revert_link(link_id, actor)
        except Skip as skip:
            skipped.append({'change': link_id, 'sku': sku, 'reason': skip.reason, 'detail': skip.detail})
        else:
            outcome[result] += 1
            undone.append(link_id)
    from .oem_auto import mark_reverted
    mark_reverted(undone)  # a reverted OEM never comes back to the pending list as automatic
    return {'run': run_id, 'reverted': outcome['reverted'], 'already_reverted': outcome['already_reverted'], 'skipped': skipped}
