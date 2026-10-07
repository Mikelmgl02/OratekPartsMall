"""OEM reference library service: compact numbers, idempotent upserts with provenance, the status rule, the PartCode mirror and the
catalog SKUs that carry a number.

The PartCode signal handlers mirror every OEM alterno (ref_type='oem' with a brand) wherever it is saved (OEM finder apply, Revisión
OEM approval, catalog editor, AI lookup approval): an 'algo:' reference_source is the OEM finder (inferred), an http(s) one an AI
lookup approved into the catalog (verified) and any other, blank included, a catalog approval (declared). Each automatic source lists
the alternos that back it (detail.part_codes); deleting or retyping one (an undo, an edit) removes exactly that, the source when no
alterno backs it any more and the reference when it has no sources left. The handlers run in a savepoint and never break the PartCode
write; before migration 0039 they skip quietly. Nothing here renames a SKU, writes a PartCode or calls an AI provider.
"""
import logging
import re
from collections import defaultdict
from functools import reduce

from django.db import IntegrityError, OperationalError, ProgrammingError, connection, transaction
from django.db.models import CharField, Func, Q, Value
from django.db.models.functions import Concat, Replace, Upper
from django.utils import timezone

from .oem_reference_models import AUTOMATIC_KINDS, OEMReference, OEMReferenceSource, compact, manufacturer_name, spellings

log = logging.getLogger(__name__)
VERIFIED_KINDS = {'price_list', 'manufacturer_catalog'}
DECLARED_KINDS = {'aftermarket_catalog', 'supplier_declaration', 'catalog_approval', 'manual'}
FILLABLE = ('description', 'part_type', 'applications')
TRACKED = ('code', 'brand', 'ref_type', 'reference_source')
ALGO_RE = re.compile(r'^algo:(?P<finder>[^:]+):(?P<version>[^:]+):(?P<rule>.+):(?P<run>\d+)$', re.I)
# Characters the database strips where it has no REGEXP_REPLACE (SQLite): every separator the catalog's SKUs and codes use.
SEPARATORS = ' -/_.+()#,:;*&\'"[]{}|\\°!?@$%=<>~`^\t'
LINKED_LIMIT = 20
_state = {'warned': False}


def tables_ready():
    """False until migration 0039 runs: the live app serves this code before the orchestrator migrates."""
    return {OEMReference._meta.db_table, OEMReferenceSource._meta.db_table} <= set(connection.introspection.table_names())


def derived_status(sources):
    """verified: a distributor price list, the manufacturer's catalog, a manual entry checked against one, or an AI lookup approved
    into the catalog; declared: an aftermarket catalog, a supplier, a catalog approval or a manual entry; inferred: only the finder."""
    status = 'inferred'
    for kind, detail in sources:
        detail = detail or {}
        if kind in VERIFIED_KINDS or (kind == 'manual' and detail.get('verified')) or (kind == 'ai_lookup' and detail.get('approved')):
            return 'verified'
        if kind in DECLARED_KINDS:
            status = 'declared'
    return status


def source_of(text):
    """(kind, citation, detail) of a PartCode.reference_source: 'algo:oem-finder:v2:current:7' is the OEM finder (run 7), an http(s)
    URL an AI lookup approved into the catalog, anything else (blank included) a catalog approval."""
    text = (text or '').strip()[:500]
    if text.lower().startswith('algo:'):
        match = ALGO_RE.match(text)
        return 'oem_finder', text, {'run': int(match['run']), 'rule': match['rule'], 'rules_version': match['version']} if match else {}
    if text.lower().startswith(('http://', 'https://')):
        return 'ai_lookup', text, {'approved': True}
    return 'catalog_approval', text, {}


def values_of(partcode):
    return {field: getattr(partcode, field) for field in TRACKED}


def partcode_key(row):
    """(manufacturer, compact code, kind, citation) of an OEM alterno, or None: other reference types are not OEM facts, and a
    brandless OEM alterno names no manufacturer to file it under."""
    if not row or row.get('ref_type') != 'oem':
        return None
    manufacturer, number = manufacturer_name(row.get('brand')), compact(row.get('code'))
    if not manufacturer or not number or len(manufacturer) > 120 or len(number) > 60:
        return None
    kind, citation, _ = source_of(row.get('reference_source'))
    return manufacturer, number, kind, citation


def classify_number(manufacturer, code, forms=(), table=None):
    """(system, family) under the manufacturer's own OEM numbering systems (the OEM finder's shapes, read-only): TOYOTA 1780130070
    -> ('TOYOTA', '17801'). Blank when the manufacturer has no known system or no spelling fits one completely."""
    from . import oem_finder as of
    name = manufacturer_name(manufacturer)
    make = next((of.MAKE_TOKENS[word] for word in re.findall(r'[A-Z0-9]+', name) if word in of.MAKE_TOKENS), None)
    own = of.MAKE_SYSTEMS.get(make, ([], []))[0] if make else [system for system, brand in of.SYSTEM_BRAND.items() if brand == name]
    if not own:
        return '', ''
    if table is None:
        from .catalog_suffixes import suffix_table
        table = suffix_table()
    for form in [*forms, code]:
        hits = [hit for hit in of.system_hits(form, table) if hit['system'] in own and not hit['rest']]
        if hits:
            hit = min(hits, key=lambda h: own.index(h['system']))
            return hit['system'], (hit['lexkey'] or '')[:20]
    return '', ''


def part_defaults(description):
    """Description and part type (head noun and qualifier: FILTRO AIRE) read from a catalog description, for blank fields only."""
    from . import oem_finder as of
    from .catalog_suffixes import description_head
    text = ' '.join((description or '').upper().split())
    head, qualifier = description_head(text)
    kind = f'{head} {qualifier}'.strip() if len(head) > 2 and head not in of.MAKE_TOKENS else ''
    return {'description': text[:300], 'part_type': kind[:60]}


def merge_detail(old, new):
    """Source detail after another sighting: the catalog alternos add up, verified/approved only ever turn on, other keys stay."""
    merged = dict(old or {})
    for key, value in (new or {}).items():
        if key == 'part_codes':
            entries = {entry['id']: entry for entry in merged.get('part_codes') or []}
            entries.update({entry['id']: entry for entry in value or []})
            merged['part_codes'] = sorted(entries.values(), key=lambda entry: entry['id'])
        elif key in ('verified', 'approved'):
            merged[key] = bool(merged.get(key)) or bool(value)
        else:
            merged.setdefault(key, value)
    return merged


def resolve(defaults):
    return (defaults() if callable(defaults) else defaults) or {}


def refresh_status(ref, actor=None, *, changed=False):
    """Re-derive the status from the sources (a disputed number stays disputed until clear_dispute) and save the reference, taking the
    next version, when it changed or the caller changed something else (changed=True). Returns the status."""
    before = ref.status
    if ref.status != 'disputed':
        ref.status = derived_status(ref.sources.values_list('kind', 'detail'))
    if changed or ref.status != before:
        ref.updated_by = actor
        ref.save()
    return ref.status


def upsert_reference(manufacturer, code, *, printed=None, source_kind, citation='', detail=None, actor=None, defaults=None):
    """The reference for (manufacturer, compact code) with this source, created or merged in place: new spellings join printed_forms,
    the source detail merges, blank descriptive fields are filled (never overwritten) and the status is re-derived. Idempotent: a
    repeat changes nothing. defaults: {description, part_type, applications} or a callable returning it, read only when needed.
    Returns (reference, created)."""
    manufacturer, number = manufacturer_name(manufacturer), compact(code)
    if not manufacturer or len(manufacturer) > 120:
        raise ValueError('Indica el fabricante (hasta 120 caracteres).')
    if not number or len(number) > 60:
        raise ValueError('El número OEM debe tener entre 1 y 60 letras o dígitos.')
    forms = spellings([*([printed] if isinstance(printed, str) else printed or []), code], number)
    citation, detail = (citation or '').strip()[:500], dict(detail or {})
    with transaction.atomic():
        ref = OEMReference.objects.select_for_update().filter(manufacturer=manufacturer, code=number).first()
        if ref is None:
            system, family = classify_number(manufacturer, number, forms)
            fields = {key: value for key, value in resolve(defaults).items() if key in FILLABLE and value}
            try:
                with transaction.atomic():
                    ref = OEMReference.objects.create(manufacturer=manufacturer, code=number, printed_forms=forms, system=system, family=family,
                                                      status=derived_status([(source_kind, detail)]), created_by=actor, updated_by=actor,
                                                      **fields)
                    OEMReferenceSource.objects.create(reference=ref, kind=source_kind, citation=citation, detail=detail, created_by=actor)
                return ref, True
            except IntegrityError:  # another writer created it first: merge into theirs
                ref = OEMReference.objects.select_for_update().get(manufacturer=manufacturer, code=number)
        changed = False
        merged = spellings([*ref.printed_forms, *forms], ref.code)
        if merged != ref.printed_forms:
            ref.printed_forms, changed = merged, True
            if not ref.system:
                ref.system, ref.family = classify_number(ref.manufacturer, ref.code, merged)
        blank = [field for field in FILLABLE if not getattr(ref, field)]
        if blank:
            values = resolve(defaults)
            for field in blank:
                if values.get(field):
                    setattr(ref, field, values[field])
                    changed = True
        source = ref.sources.filter(kind=source_kind, citation=citation).first()
        if source is None:
            OEMReferenceSource.objects.create(reference=ref, kind=source_kind, citation=citation, detail=detail, created_by=actor)
            changed = True
        elif merge_detail(source.detail, detail) != source.detail:
            source.detail = merge_detail(source.detail, detail)
            source.save(update_fields=['detail'])
            changed = True
        refresh_status(ref, actor if changed else ref.updated_by, changed=changed)
    return ref, False


def curated(ref):
    """Someone disputed, annotated or chained it: worth keeping even after its last automatic source is withdrawn."""
    return (ref.status == 'disputed' or bool(ref.notes.strip() or ref.applications.strip()) or bool(ref.superseded_by_id)
            or ref.supersedes.exists())


def remove_partcode_source(partcode, number=None, keep=None):
    """Drop one PartCode from the automatic sources of a number (its own by default) that list it, except keep (the source it backs
    now, whose reference the caller settles): a source goes when no alterno backs it any more and the reference when it has no
    sources left (unless curated); otherwise the status is re-derived. Returns the references that changed."""
    number = compact(partcode.code if number is None else number)
    touched = {}
    for source_id, ref_id, kind, citation, detail in (OEMReferenceSource.objects.filter(reference__code=number, kind__in=AUTOMATIC_KINDS)
                                                      .order_by('pk').values_list('pk', 'reference_id', 'kind', 'citation', 'detail')):
        if not any(entry.get('id') == partcode.pk for entry in (detail or {}).get('part_codes') or []):
            continue
        ref = touched.get(ref_id) or OEMReference.objects.select_for_update().filter(pk=ref_id).first()
        source = OEMReferenceSource.objects.select_for_update().filter(pk=source_id).first()  # re-read under the reference lock
        if ref is None or source is None or keep == (ref.manufacturer, ref.code, kind, citation):
            continue
        rest = [entry for entry in source.detail.get('part_codes') or [] if entry.get('id') != partcode.pk]
        if rest:
            source.detail = {**source.detail, 'part_codes': rest}
            source.save(update_fields=['detail'])
        else:
            source.delete()
        touched[ref_id] = ref
    for ref in touched.values():
        if keep and (ref.manufacturer, ref.code) == keep[:2]:
            continue  # the caller adds the new source next and re-derives the status then
        if not ref.sources.exists() and not curated(ref):
            ref.delete()
        else:
            refresh_status(ref, changed=True)
    return list(touched.values())


def sync_from_partcode(partcode, before=None, created=False):
    """Mirror one saved PartCode: an OEM alterno upserts its reference with the source its reference_source names, listing itself in
    that source's part_codes; whatever it backed before (another type, number, brand or source) loses it."""
    from .models import Part
    now, then = partcode_key(values_of(partcode)), partcode_key(before)
    if not created:
        for number in {key[1] for key in (now, then) if key}:
            remove_partcode_source(partcode, number=number, keep=now)
    if now is None:
        return None
    manufacturer, number, kind, citation = now
    detail = {**source_of(partcode.reference_source)[2], 'part_codes': [{'id': partcode.pk, 'part': str(partcode.part_id), 'code': partcode.code}]}

    def described():
        return part_defaults(Part.objects.filter(pk=partcode.part_id).values_list('description', flat=True).first())
    return upsert_reference(manufacturer, partcode.code, printed=[partcode.code], source_kind=kind, citation=citation, detail=detail,
                            defaults=described)[0]


def guarded(sync, *args):
    """Run a library sync in a savepoint so it never breaks the PartCode write that triggered it; while the tables are not migrated
    it is skipped quietly (one warning per process)."""
    try:
        with transaction.atomic():
            return sync(*args)
    except (ProgrammingError, OperationalError):
        try:
            ready = tables_ready()
        except Exception:
            ready = True
        if ready:
            log.exception('La biblioteca OEM no pudo reflejar un alterno OEM.')
        elif not _state['warned']:
            _state['warned'] = True
            log.warning('La biblioteca OEM aún no está migrada (0039): se omite la sincronización de alternos OEM.')
    except Exception:
        log.exception('La biblioteca OEM no pudo reflejar un alterno OEM.')
    return None


def partcode_pre_save(sender, instance, raw=False, update_fields=None, **kwargs):
    """What an existing PartCode was before this save, so a retyped or renamed OEM alterno leaves the source it backed."""
    if raw or instance._state.adding or instance.pk is None or (update_fields is not None and not set(update_fields) & set(TRACKED)):
        return
    instance._oem_reference_before = sender.objects.filter(pk=instance.pk).values(*TRACKED).first()


def partcode_saved(sender, instance, created=False, raw=False, **kwargs):
    before = instance.__dict__.pop('_oem_reference_before', None)
    if raw or (instance.ref_type != 'oem' and (before or {}).get('ref_type') != 'oem'):
        return
    guarded(sync_from_partcode, instance, before, created)


def partcode_deleted(sender, instance, **kwargs):
    if instance.ref_type == 'oem':
        guarded(remove_partcode_source, instance)


def compact_sql(field):
    """The compact form computed by the database: REGEXP_REPLACE on PostgreSQL, the catalog's separators stripped elsewhere."""
    if connection.vendor == 'postgresql':
        return Func(Upper(field), Value('[^A-Z0-9]+'), Value(''), Value('g'), function='REGEXP_REPLACE', output_field=CharField())
    return reduce(lambda expression, char: Replace(expression, Value(char), Value('')), SEPARATORS, Upper(field))


def linked_parts(refs):
    """{reference id: [{id, sku, is_OEM, active, via, code}]}: the canonical catalog SKUs that carry each number, in two queries for any
    page: a SKU whose compact form is the number (via 'sku') or an alterno with that compact form (via its ref_type), an OEM alterno
    only under the same manufacturer. Merged SKUs are left out: their codes moved to the SKU they were grouped into."""
    from .models import Part, PartCode
    refs = list(refs)
    out, seen, by_number = {ref.pk: [] for ref in refs}, defaultdict(set), defaultdict(list)
    for ref in refs:
        by_number[ref.code].append(ref)
    if not by_number:
        return out

    def add(ref, part_id, sku, is_oem, active, via, code):
        if part_id not in seen[ref.pk]:
            seen[ref.pk].add(part_id)
            out[ref.pk].append({'id': part_id, 'sku': sku, 'is_OEM': is_oem, 'active': active, 'via': via, 'code': code})

    numbers = list(by_number)
    for row in (Part.objects.filter(merged_into__isnull=True).annotate(number=compact_sql('sku')).filter(number__in=numbers)
                .order_by('sku').values_list('id', 'sku', 'is_OEM', 'active', 'number')):
        for ref in by_number[row[4]]:
            add(ref, *row[:4], 'sku', row[1])
    for row in (PartCode.objects.filter(part__merged_into__isnull=True).annotate(number=compact_sql('code')).filter(number__in=numbers)
                .order_by('part__sku', 'code', 'pk').values_list('part_id', 'part__sku', 'part__is_OEM', 'part__active', 'ref_type', 'code',
                                                                  'brand', 'number')):
        for ref in by_number[row[7]]:
            if row[4] != 'oem' or row[6] == ref.manufacturer:
                add(ref, *row[:6])
    return out


def linked_q():
    """Q for references some canonical catalog SKU carries (the linked_parts rule) on a queryset annotated with pair (manufacturer:code),
    as uncorrelated IN subqueries the database evaluates once."""
    from .models import Part, PartCode
    alternos = PartCode.objects.filter(part__merged_into__isnull=True)
    return (Q(code__in=Part.objects.filter(merged_into__isnull=True).annotate(number=compact_sql('sku')).values('number'))
            | Q(code__in=alternos.exclude(ref_type='oem').annotate(number=compact_sql('code')).values('number'))
            | Q(pair__in=alternos.filter(ref_type='oem').annotate(pair=Concat('brand', Value(':'), compact_sql('code'), output_field=CharField()))
                .values('pair')))


def reference_pair():
    return Concat('manufacturer', Value(':'), 'code', output_field=CharField())


def seed_from_partcodes(Reference, Source, PartCode, *, using='default', table=None):
    """Migration 0039 seed, idempotent and safe with historical models: one reference per (brand, compact code) of the OEM alternos
    already in the catalog, each with the source its reference_source names (the OEM finder rows of 2026-10-06 are inferred).
    Existing references and sources are merged, never duplicated. Returns the counts it created."""
    plan = {}
    for row in (PartCode.objects.using(using).filter(ref_type='oem').order_by('pk')
                .values('pk', 'part_id', 'code', 'brand', 'ref_type', 'reference_source', 'part__description')):
        key = partcode_key(row)
        if key is None:
            continue
        manufacturer, number, kind, citation = key
        entry = plan.setdefault((manufacturer, number), {'forms': [], 'description': row['part__description'], 'sources': {}})
        entry['forms'].append(row['code'])
        detail = entry['sources'].setdefault((kind, citation), source_of(row['reference_source'])[2])
        detail.setdefault('part_codes', []).append({'id': row['pk'], 'part': str(row['part_id']), 'code': row['code']})
    if not plan:
        return {'references': 0, 'sources': 0}
    existing = {(ref.manufacturer, ref.code): ref for ref in Reference.objects.using(using).filter(code__in={number for _, number in plan})
                if (ref.manufacturer, ref.code) in plan}
    new = []
    for (manufacturer, number), entry in plan.items():
        if (manufacturer, number) not in existing:
            forms = spellings(entry['forms'], number)
            system, family = classify_number(manufacturer, number, forms, table)
            fields = {key: value for key, value in part_defaults(entry['description']).items() if value}
            new.append(Reference(manufacturer=manufacturer, code=number, printed_forms=forms, system=system, family=family,
                                 status=derived_status([(kind, detail) for (kind, _), detail in entry['sources'].items()]), **fields))
    Reference.objects.using(using).bulk_create(new, batch_size=500)
    refs = {**existing, **{(ref.manufacturer, ref.code): ref for ref in new}}
    have = {(s.reference_id, s.kind, s.citation): s for s in Source.objects.using(using).filter(reference_id__in=[r.pk for r in refs.values()])}
    created, merged, touched = [], [], set()
    for key, entry in plan.items():
        ref = refs[key]
        for (kind, citation), detail in entry['sources'].items():
            source = have.get((ref.pk, kind, citation))
            if source is None:
                created.append(Source(reference_id=ref.pk, kind=kind, citation=citation, detail=detail))
                touched.add(ref.pk)
            elif merge_detail(source.detail, detail) != source.detail:
                source.detail = merge_detail(source.detail, detail)
                merged.append(source)
                touched.add(ref.pk)
    Source.objects.using(using).bulk_create(created, batch_size=500)
    Source.objects.using(using).bulk_update(merged, ['detail'], batch_size=500)
    stale = [ref for key, ref in existing.items()
             if ref.pk in touched or spellings([*ref.printed_forms, *plan[key]['forms']], ref.code) != ref.printed_forms]
    if stale:
        sources = defaultdict(list)
        for ref_id, kind, detail in Source.objects.using(using).filter(reference_id__in=[r.pk for r in stale]).values_list('reference_id', 'kind', 'detail'):
            sources[ref_id].append((kind, detail))
        now = timezone.now()
        for ref in stale:
            ref.printed_forms = spellings([*ref.printed_forms, *plan[(ref.manufacturer, ref.code)]['forms']], ref.code)
            ref.status = ref.status if ref.status == 'disputed' else derived_status(sources[ref.pk])
            ref.version, ref.updated_at = ref.version + 1, now
        Reference.objects.using(using).bulk_update(stale, ['printed_forms', 'status', 'version', 'updated_at'], batch_size=500)
    return {'references': len(new), 'sources': len(created)}
