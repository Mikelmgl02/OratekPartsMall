"""Proposed new catalog SKUs (Administración → Inventario → Productos nuevos).

Supplier items the matching worker could tie to no SKU become one proposal per product (the same compact code and brand, whatever the
supplier). An administrator approves it, and the SKU is created with the same rules as «Crear SKU» and its items are linked. The
administrator can instead link it to an existing SKU or reject it. Nothing reaches the catalog without that decision.

Which items: pending ones with no SKU whose matching case found no code evidence (unmatched: no exact code, remembered mapping,
declared or labelled reference, OEM library hit or code family; description words only leave hints on the case, owner decision D1),
or whose candidates an administrator already turned down (dismissed). Items a code ties to SKUs stay in Agrupación inteligente, and
an item that gains such a code leaves its proposal. A short code, or a blank or placeholder brand (GENERICO, S/M...), is too common to say two suppliers sell the same product:
those items stay one proposal per supplier.

What a proposal starts with comes from the suppliers' own data. The SKU is the code as printed and the description the most detailed
one. When the brand is a vehicle make (vehicle_makes()), the code is that make's OEM number, as with a dealer's genuine parts. For an
aftermarket brand, the OEM library may know the OEM number the code replaces. Grupo, subgrupo and the rest wait for an administrator
or the AI investigation. `field_sources` records who filled each field, and a field a person edited is never overwritten by the
suppliers' data again.

The worker refreshes proposals at the end of each matching pass (proposals_stage); it writes only proposal rows. A stock change alone
does not touch a proposal: only a change to what the proposal shows (code, brand, description, references) refreshes it."""
import copy
import logging
import time
from collections import Counter, defaultdict
from types import SimpleNamespace

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone
from rest_framework import status as http
from rest_framework.exceptions import APIException, NotFound, ValidationError

from .catalog_families import normalized_reference
from .catalog_identity import reference_pattern
from .matching_engine import digest, item_row, part_row, remember
from .matching_models import MatchingCase, MatchingDecision
from .matching_queue import enqueue_matching, matching_work, request_another_pass
from .models import Account, Part, PartCode, PartType, SupplierItem
from .part_references import normalize_references
from .sku_proposal_models import SkuProposal, SkuProposalItem

logger = logging.getLogger(__name__)
BATCH = 2000          # new or changed items read at a time
PASS_SECONDS = 120    # a worker pass refreshes proposals this long at most, then asks for another pass
MAX_REFERENCES = 100
MIN_SHARED_CODE = 5   # compact codes shorter than this are one proposal per supplier
SUPPLIER_FIELDS = ['sku', 'description', 'oem', 'references']  # fields the suppliers' data may refill while no person edited them
AUTOMATIC = ('supplier', 'oem_library')  # field_sources an update from the suppliers' data may replace
OPEN = ('pending', 'rejected', 'resolved')  # a product has at most one open proposal; approved and linked ones are history
CANDIDATE_CASES = ('unmatched', 'dismissed')
PROPOSAL_LOCK = 724631112  # pg advisory lock class of an approval's codes (matching pass 724631109 ... suffix table 724631111)
PLACEHOLDER_BRANDS = {'GENERICO', 'GENERICA', 'GENERIC', 'S/M', 'SM', 'S/MARCA', 'SIN MARCA', 'N/A', 'NA', 'VARIOS', 'VARIAS', 'OTRO',
                      'OTROS', 'OTRA', 'OTRAS', 'NINGUNA', 'ALTERNO', 'ALTERNA', 'IMPORTADO', 'CHINA', 'TAIWAN'}
# Make tokens other words share (TOYO tires, HON, MIT, CHE...): as a supplier brand they say nothing about the make.
NOT_A_MAKE = {'TOYO', 'DAI', 'HON', 'MIT', 'MIST', 'CHE', 'CHEX', 'MAZ', 'ISU', 'SUZ', 'DAE', 'INFINITY', 'CHANG', 'CHERRY', 'WULIN',
              'VOLK', 'SSANG', 'BENZ', 'INTERNATIONAL', 'INTERNACIONAL', 'MINI'}
MAKE_NAMES = {'VW': 'VOLKSWAGEN', 'VOLKWAGEN': 'VOLKSWAGEN'}


class ProposalConflict(APIException):
    status_code = http.HTTP_409_CONFLICT
    default_detail = 'La propuesta cambió mientras la revisabas: vuelve a abrirla.'


class DuplicateProposal(Exception):
    """Approving would create a second SKU for a code the catalog already has: why, and the SKUs (id, sku, active) its items belong
    to. The views answer 400 with both, so the drawer can offer Vincular."""

    def __init__(self, detail, owners):
        super().__init__(detail)
        self.detail, self.owners = detail, owners


def clean(value):
    """Upper case, single spaces."""
    return ' '.join((value or '').upper().split())


# --- what a proposal starts with ---------------------------------------------------------------------------------------------------

def vehicle_makes():
    """Supplier brands that are a vehicle make (a dealer's genuine parts), each with the manufacturer its OEM numbers are filed under:
    the OEM finder's make names and their unambiguous abbreviations (TOY and TOYTA → TOYOTA, NIS → NISSAN, VW → VOLKSWAGEN), plus
    CATALOG_EXTRA_MAKES. A make inside a group keeps its own name (AUDI, JEEP, GMC), not the group's."""
    from .oem_finder import MAKE_TOKENS
    from .oem_links import EXTRA_MAKES
    makes = {}
    for token, canonical in {**MAKE_TOKENS, **EXTRA_MAKES}.items():
        if token in NOT_A_MAKE:
            continue
        abbreviation = token != canonical and len(token) < len(canonical) and canonical.startswith(token[:3])
        makes[token] = MAKE_NAMES.get(token) or (canonical if abbreviation else token)
    for make in settings.CATALOG_EXTRA_MAKES:
        make = clean(make)
        if make:
            makes.setdefault(make, make)
    return makes


def library_oem(code, brand):
    """(OEM number, manufacturer, citation) the OEM library files this aftermarket code under, when it names exactly one."""
    from .oem_reference_models import OEMCrossReference
    rows = list(OEMCrossReference.objects.filter(number=normalized_reference(code), brand__iexact=brand).select_related('reference')[:2])
    if len(rows) != 1:
        return '', '', ''
    reference, citations = rows[0].reference, rows[0].citations or []
    return ((reference.printed_forms or [reference.code])[0])[:120], reference.manufacturer[:120], str(citations[0])[:300] if citations else ''


def merge_references(declared):
    """The items' references one at a time: a malformed one is skipped, and a typed one (oem, company) wins over an untyped copy."""
    merged = {}
    for reference in declared:
        try:
            [row] = normalize_references([reference])
        except (ValidationError, ValueError):
            continue
        key = (row['brand'], row['code'])
        if key not in merged and len(merged) >= MAX_REFERENCES:
            continue
        if key not in merged or merged[key].get('ref_type', 'unknown') == 'unknown':
            merged[key] = row
    return [merged[key] for key in sorted(merged)]


def printed_forms(items):
    """The items' codes as printed, the best first: the form most suppliers use, then one without spaces, then the one with its
    separators (04465-02220 over 0446502220). Stock plays no part: it changes all the time."""
    codes = Counter(item.codigo.strip().upper() for item in items)
    return sorted(codes, key=lambda code: (-codes[code], ' ' in code, -len(code), code))


def suggest(items, makes):
    """The supplier-derived fields of a product sold as these items."""
    sku, brands = printed_forms(items)[0], Counter(clean(item.brand) for item in items)
    brand = sorted(brands, key=lambda value: (-brands[value], value))[0]
    description = max((item.description.strip() for item in items), key=len, default='')
    sources = {field: {'source': 'supplier'} for field in ('sku', 'description', 'references')}
    oem_code = oem_brand = ''
    if brand in makes:
        oem_code, oem_brand = sku, makes[brand]
        sources['oem'] = {'source': 'supplier'}
    elif brand:
        oem_code, oem_brand, note = library_oem(sku, brand)
        if oem_code:
            sources['oem'] = {'source': 'oem_library', **({'note': note} if note else {})}
    references = merge_references([reference for item in items for reference in (item.references or [])])
    return {'sku': sku[:200], 'brand': brand[:120], 'description': description, 'oem_code': oem_code, 'oem_brand': oem_brand,
            'references': references}, sources


def product_key(item, makes):
    """Items with the same key are one product: the compact code and the brand (a make under its manufacturer's name, so TOY and
    TOYOTA agree). A short code or a blank or placeholder brand stays within its supplier."""
    code = normalized_reference(item.codigo)
    if not code:
        return None
    brand = clean(item.brand)
    brand = makes.get(brand, brand)
    if not brand or brand in PLACEHOLDER_BRANDS or len(code) < MIN_SHARED_CODE:
        return f'{code}|{brand}|{item.supplier_id}'
    return f'{code}|{brand}'


def busy_suppliers():
    from .supplier_import_models import SupplierInventoryImportJob
    return SupplierInventoryImportJob.objects.filter(status='importing', expires_at__gt=timezone.now()).values('supplier_id')


def candidate_items(*, busy=False):
    """Items a proposal may hold: pending, no SKU, an active supplier, a code with letters or digits, and a supplier matching case that
    found no code evidence (unmatched, its word hints included) or whose candidates were turned down (dismissed). Suppliers mid-import
    wait, unless busy=True."""
    rows = (SupplierItem.objects.filter(part__isnull=True, matching_status='pending', supplier__active=True, codigo__regex=r'[0-9A-Za-z]')
            .filter(Exists(MatchingCase.objects.filter(item=OuterRef('pk'), kind='supplier', status__in=CANDIDATE_CASES))))
    return rows if busy else rows.exclude(supplier_id__in=busy_suppliers())


def current_row(item):
    try:
        return item.sku_proposal
    except SkuProposalItem.DoesNotExist:
        return None


# --- the worker's stage ------------------------------------------------------------------------------------------------------------

def propose(*, batch=BATCH, seconds=PASS_SECONDS):
    """Refresh proposals from the candidate items: items that are no longer candidates leave theirs, new and changed items join their
    product's proposal, and a proposal left without items is resolved. Returns counts."""
    stats, makes = Counter(), vehicle_makes()
    recompute(drop_gone(stats), makes, stats)
    started, after = time.monotonic(), None
    while True:
        rows = candidate_items().filter(Q(sku_proposal__isnull=True) | Q(updated_at__gt=F('sku_proposal__seen_at')))
        if after is not None:
            rows = rows.filter(pk__gt=after)
        fresh = list(rows.select_related('supplier', 'sku_proposal__proposal').order_by('pk')[:batch])
        if fresh:
            take(fresh, makes, stats)
            after = fresh[-1].pk
        if len(fresh) < batch:
            break
        if time.monotonic() - started > seconds:
            request_another_pass()
            stats['continues'] = 1
            break
    return dict(stats)


def drop_gone(stats):
    """Items that are no longer candidates (matched, given candidate SKUs, their supplier deactivated) leave pending proposals.
    Suppliers mid-import are left alone until it ends. Returns the proposals they left."""
    with transaction.atomic():
        gone = (SkuProposalItem.objects.filter(proposal__status='pending').exclude(item__supplier_id__in=busy_suppliers())
                .exclude(Exists(candidate_items(busy=True).filter(pk=OuterRef('item_id')))))
        touched = set(gone.values_list('proposal_id', flat=True))
        if touched:
            stats['left'] += gone.delete()[0]
    return touched


def take(fresh, makes, stats):
    """One batch of fresh items. One its proposal saw as it is now (a stock change) is only marked seen; the rest join their
    product's proposal, and the proposals they left follow the items they still have."""
    seen, groups = [], defaultdict(list)
    for item in fresh:
        row, key = current_row(item), product_key(item, makes)
        if row and row.proposal.status in ('pending', 'rejected') and row.proposal.key == key and row.snapshot == item_row(item):
            row.seen_at = item.updated_at
            seen.append(row)
        elif key:
            groups[key].append(item)
    SkuProposalItem.objects.bulk_update(seen, ['seen_at'], batch_size=500)
    stats['seen'] += len(seen)
    left = set()
    for key, items in groups.items():
        with transaction.atomic():
            outcome, moved_from = refresh(key, items, makes)
        stats[outcome] += 1
        left |= moved_from
    recompute(left, makes, stats)


def recompute(proposal_ids, makes, stats):
    for proposal_id in proposal_ids:
        with transaction.atomic():
            key = SkuProposal.objects.filter(pk=proposal_id, status='pending').values_list('key', flat=True).first()
            if key:
                stats[refresh(key, [], makes)[0]] += 1


def refresh(key, fresh, makes):
    """Bring a product's open proposal up to date with its fresh items, or create it. Its other items stay while they are still
    candidates under the same key. Returns (outcome, the pending proposals fresh items left for this one)."""
    proposal = SkuProposal.objects.select_for_update().filter(key=key, status__in=OPEN).first()
    members = {item.pk: item for item in fresh}
    left = set()
    for item in fresh:
        row = current_row(item)
        if row and row.proposal.status == 'pending' and (proposal is None or row.proposal_id != proposal.pk):
            left.add(row.proposal_id)
    if proposal:
        others = [row for row in proposal.items.select_related('item__supplier') if row.item_id not in members]
        still = set(candidate_items(busy=True).filter(pk__in=[row.item_id for row in others]).values_list('pk', flat=True)) if others else set()
        busy = set(busy_suppliers().values_list('supplier_id', flat=True)) if others else set()
        dropped = []
        for row in others:
            if row.item.supplier_id in busy or row.item_id in still and product_key(row.item, makes) == key:  # mid-import: wait for it
                members[row.item_id] = row.item
            else:
                dropped.append(row.pk)
        if dropped:
            SkuProposalItem.objects.filter(pk__in=dropped).delete()
    if not members:
        if proposal and proposal.status == 'pending':
            proposal.status, proposal.version = 'resolved', proposal.version + 1
            proposal.save(update_fields=['status', 'version', 'updated_at'])
            return 'resolved', left
        return 'unchanged', left
    items = sorted(members.values(), key=lambda item: str(item.pk))
    fingerprint = digest([item_row(item) for item in items])
    if proposal and proposal.status == 'rejected' and proposal.fingerprint == fingerprint:
        outcome = 'kept_rejected'
    else:
        values, sources = suggest(items, makes)
        if proposal is None:
            proposal = SkuProposal.objects.create(key=key, fingerprint=fingerprint, field_sources=sources, **values)
            outcome = 'created'
        else:
            outcome = 'refreshed' if update(proposal, values, sources, fingerprint) else 'unchanged'
    for item in fresh:
        SkuProposalItem.objects.update_or_create(item=item, defaults={'proposal': proposal, 'snapshot': item_row(item), 'seen_at': item.updated_at})
    return outcome, left - {proposal.pk}


def shown(proposal):
    return (proposal.sku, proposal.description, proposal.brand, proposal.oem_code, proposal.oem_brand, proposal.references,
            proposal.field_sources, proposal.status, proposal.fingerprint)


def update(proposal, values, sources, fingerprint):
    """Refill the fields no person edited, and reopen a rejected or resolved proposal whose items changed. The version moves only when
    something changed, so an administrator's open review or bulk approval is not refused for nothing."""
    before = copy.deepcopy(shown(proposal))
    edited = {field for field, source in proposal.field_sources.items() if source.get('source') not in AUTOMATIC
              and not (source.get('source') == 'ai' and field == 'description' and source.get('from') != values['description'])}
    for field in SUPPLIER_FIELDS:
        if field in edited or (field == 'oem' and proposal.internal_code):  # an internal code never gets the make's OEM number back
            continue
        if field == 'oem':
            proposal.oem_code, proposal.oem_brand = values['oem_code'], values['oem_brand']
            if 'oem' in sources:
                proposal.field_sources['oem'] = sources['oem']
            else:
                proposal.field_sources.pop('oem', None)
        else:
            setattr(proposal, field, values[field])
            proposal.field_sources[field] = sources[field]
    proposal.brand, proposal.fingerprint = values['brand'], fingerprint
    if proposal.status in ('rejected', 'resolved'):  # its items changed since: it needs a fresh look
        proposal.status, proposal.decided_by, proposal.decided_at, proposal.reject_reason, proposal.reject_note = 'pending', None, None, '', ''
    if shown(proposal) == before:
        return False
    proposal.version += 1
    proposal.save()
    return True


def proposals_stage():
    """propose() for the matching worker: a failure is logged and the pass goes on."""
    try:
        return propose()
    except Exception:
        logger.exception('Could not refresh the proposed SKUs; the matching pass continues.')
        return {'status': 'failed'}


# --- decisions ---------------------------------------------------------------------------------------------------------------------

def locked(proposal_id, expected_version):
    proposal = SkuProposal.objects.select_for_update().filter(pk=proposal_id).first()
    if proposal is None:
        raise NotFound('No existe esa propuesta.')
    if proposal.status != 'pending':
        raise ProposalConflict('Esta propuesta ya se decidió o se resolvió: actualiza la lista.')
    if expected_version != proposal.version:
        raise ProposalConflict()
    return proposal


def lock_accounts(rows):
    """The suppliers of the items, in id order (account → catalog → items, as matching does); none may be importing."""
    from .supplier_import_models import SupplierInventoryImportJob
    ids = sorted({row.item.supplier_id for row in rows}, key=str)
    accounts = list(Account.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
    busy = SupplierInventoryImportJob.objects.filter(supplier_id__in=ids, status='importing', expires_at__gt=timezone.now()).select_related('supplier').first()
    if busy:
        raise ValidationError({'detail': f'Espera a que termine la importación de {busy.supplier.name}.'})
    return accounts


def catalog_owners(code, brands=()):
    """Catalog SKUs this code already is once normalized (MR-968365 = MR968365): their own SKU, retired and merged ones included, or an
    alterno of theirs under one of these brands or with none. A merged SKU answers with the one it was merged into.
    [(id, sku, active)]: a retired one still blocks a second SKU, but items can only be linked to an active one."""
    key = normalized_reference(code)
    if not key:
        return []
    rx, brands = reference_pattern(key), {clean(brand) for brand in brands if brand}
    owners = {(merged_id or pk, merged or sku, merged_active if merged_id else active) for pk, sku, active, merged_id, merged, merged_active in
              Part.objects.filter(sku__iregex=rx).values_list('pk', 'sku', 'active', 'merged_into_id', 'merged_into__sku', 'merged_into__active')
              if normalized_reference(sku) == key}
    owners |= {(merged_id or pk, merged or sku, merged_active if merged_id else active)
               for found, brand, pk, sku, active, merged_id, merged, merged_active in PartCode.objects.filter(code__iregex=rx)
               .values_list('code', 'brand', 'part_id', 'part__sku', 'part__active', 'part__merged_into_id', 'part__merged_into__sku', 'part__merged_into__active')
               if normalized_reference(found) == key and (not brand or not brands or clean(brand) in brands)}
    return sorted(owners, key=lambda owner: owner[1])


def owner_label(owner):
    return f'{owner[1]}{"" if owner[2] else " (retirado)"}'


def duplicate_checks(proposal):
    """(code, brands) approving must find nowhere in the catalog: the SKU itself and, when it is another number, the OEM number."""
    sku_key = normalized_reference(proposal.sku)
    checks = [(proposal.sku, {proposal.brand, proposal.oem_brand, vehicle_makes().get(proposal.brand, '')})]
    if proposal.oem_code and normalized_reference(proposal.oem_code) != sku_key:
        checks.append((proposal.oem_code, {proposal.oem_brand}))
    return checks


def catalog_match(proposal):
    """The catalog SKUs the proposal's code or OEM number already belongs to: its items are variants of that SKU (Vincular)."""
    seen = {}
    for code, brands in duplicate_checks(proposal):
        for pk, sku, active in catalog_owners(code, brands):
            seen.setdefault(pk, {'id': str(pk), 'sku': sku, 'code': code, 'active': active})
    return list(seen.values())


def refuse_duplicates(proposal):
    """Approving never creates a second SKU for a code the catalog already has under another spelling, or for an OEM number another
    SKU carries: the proposal's items are variants of that SKU, and the administrator links them there. The refusal names it."""
    checks = duplicate_checks(proposal)
    if connection.vendor == 'postgresql':  # two approvals of the same code at once: the second sees the first's SKU
        with connection.cursor() as cursor:
            for key in sorted({normalized_reference(code) for code, _ in checks} - {''}):
                cursor.execute('SELECT pg_advisory_xact_lock(%s, hashtext(%s))', [PROPOSAL_LOCK, key])
    for code, brands in checks:
        owners = catalog_owners(code, brands)
        if owners:
            usable = any(active for _, _, active in owners)
            raise DuplicateProposal(f'{code} ya está en el catálogo como SKU {", ".join(owner_label(owner) for owner in owners[:3])}: '
                                    + ('usa «Vincular a SKU existente».' if usable else 'reactívalo para vincular ahí sus artículos.'),
                                    [{'id': str(pk), 'sku': sku, 'active': active} for pk, sku, active in owners[:3]])


def link(proposal, rows, part, user, action):
    items = {item.pk: item for item in SupplierItem.objects.select_for_update().filter(pk__in=[row.item_id for row in rows]).order_by('pk')}
    cases = {case.item_id: case for case in MatchingCase.objects.select_for_update().filter(key__in=[f'supplier:{row.item_id}' for row in rows]).order_by('pk')}
    for row in rows:
        item = items[row.item_id]
        if item.part_id or item.matching_status != 'pending' or item_row(item) != row.snapshot:
            raise ProposalConflict(f'El artículo {item.codigo} ({item.supplier_invent_id}) cambió desde que se propuso: actualiza la lista.')
        case = cases.get(item.pk)
        if case is None or case.status not in CANDIDATE_CASES:
            raise ProposalConflict(f'El artículo {item.codigo} ({item.supplier_invent_id}) ya tiene SKU candidatos en Agrupación inteligente: actualiza la lista.')
        item.part, item.matching_status = part, 'matched'
        item.save(update_fields=['part', 'matching_status', 'updated_at'])
        remember(item, part, user)
        case.status, case.target_sku = 'applied', part.sku
        case.save(update_fields=['status', 'target_sku', 'updated_at'])
        MatchingDecision.objects.create(case=case, actor=user, action=action, evidence={
            'proposal': str(proposal.pk), 'sources': [row.snapshot], 'target': part_row(part)})


def decide(proposal, outcome, user, *, part=None, reason='', note='', at=None):
    proposal.status, proposal.part, proposal.decided_by, proposal.decided_at = outcome, part, user, at or timezone.now()
    proposal.reject_reason, proposal.reject_note, proposal.version = reason, note, proposal.version + 1
    proposal.save()


def declared_by(proposal, rows):
    suppliers = sorted({row.item.supplier.name for row in rows})
    return f'Declarado por {", ".join(suppliers)} · producto nuevo {str(proposal.pk)[:8]}'[:500]


def citation(proposal, rows):
    """Where the proposal's OEM number came from, as the OEM alterno's reference_source."""
    source = proposal.field_sources.get('oem') or {}
    tag = f'producto nuevo {str(proposal.pk)[:8]}'
    if source.get('source') == 'oem_library':
        return f'Biblioteca OEM: {source.get("note") or "equivalencia de posventa"} · {tag}'[:500]
    if source.get('source') == 'admin':
        return f'Indicado por {source.get("by") or "un administrador"} · {tag}'[:500]
    if source.get('source') == 'ai':
        return f'Investigación IA: {source.get("note") or "sin cita"} · {tag}'[:500]
    return declared_by(proposal, rows)


def part_payload(proposal, rows):
    codes, seen, sku_key = [], set(), normalized_reference(proposal.sku)
    # A supplier's internal code (merchandise, tools) is no OEM number even under a make brand: its codes stay the brand's own.
    make = '' if proposal.internal_code else vehicle_makes().get(proposal.brand, '')

    def add(code, brand, ref_type, source=''):
        pair = (clean(brand), normalized_reference(code))
        if pair not in seen:
            seen.add(pair)
            codes.append({'code': code, 'brand': brand, 'ref_type': ref_type, **({'reference_source': source} if source else {})})

    if proposal.oem_code:
        same = normalized_reference(proposal.oem_code) == sku_key
        add(proposal.sku if same else proposal.oem_code, proposal.oem_brand, 'oem', citation(proposal, rows))  # the SKU's own spelling
    # The codes the suppliers print, whatever the SKU became: a dealer's code is its make's OEM number, any other (or a dealer's
    # internal code) a company code.
    for code in printed_forms([row.item for row in rows]):
        if make:
            add(code, make, 'oem', declared_by(proposal, rows))
        elif proposal.brand:
            add(code, proposal.brand, 'company')
    for reference in proposal.references:
        if reference.get('brand') or normalized_reference(reference['code']) != sku_key:  # a bare copy of the SKU adds nothing
            add(reference['code'], reference.get('brand', ''), reference.get('ref_type', 'unknown'))
    payload = {'sku': proposal.sku, 'description': proposal.description, 'codes': codes}
    if proposal.part_type_id:
        payload['part_type'] = proposal.part_type_id
    if proposal.oem_code and normalized_reference(proposal.oem_code) == sku_key:
        payload['main_reference'] = {'code': proposal.sku, 'brand': proposal.oem_brand}  # the SKU is the OEM number: OEM MAIN
    return payload


def approve(proposal_id, user, expected_version):
    """Create the SKU (the rules of «Crear SKU») and link the proposal's items to it, in one transaction."""
    from .management import ManagedPartSerializer
    with matching_work(), transaction.atomic():
        proposal = locked(proposal_id, expected_version)
        rows = list(proposal.items.select_related('item__supplier'))
        if not rows:
            raise ProposalConflict('La propuesta ya no tiene artículos: actualiza la lista.')
        lock_accounts(rows)
        refuse_duplicates(proposal)
        serializer = ManagedPartSerializer(data=part_payload(proposal, rows), context={'request': SimpleNamespace(user=user)})
        if not serializer.is_valid():
            raise ValidationError(serializer.errors)
        now = timezone.now()  # the line's decision is the proposal's (origen proveedor: it came from the suppliers' items)
        part = serializer.save(origen='proveedor', decided_by=user, decided_at=now)
        link(proposal, rows, part, user, 'proposal_approved')
        decide(proposal, 'approved', user, part=part, at=now)
    enqueue_matching()  # other suppliers' items with the same code can link now
    return proposal


def link_existing(proposal_id, user, expected_version, part_id):
    with matching_work(), transaction.atomic():
        proposal = locked(proposal_id, expected_version)
        rows = list(proposal.items.select_related('item__supplier'))
        if not rows:
            raise ProposalConflict('La propuesta ya no tiene artículos: actualiza la lista.')
        lock_accounts(rows)
        part = Part.objects.select_for_update().filter(pk=part_id, active=True, merged_into__isnull=True).first()
        if part is None:
            raise ValidationError({'part': 'Elige un SKU activo del catálogo.'})
        link(proposal, rows, part, user, 'proposal_linked')
        decide(proposal, 'linked', user, part=part)
    enqueue_matching()
    return proposal


def reject(proposal_id, user, expected_version, reason, note=''):
    if reason not in dict(SkuProposal.REJECT_REASONS):
        raise ValidationError({'reason': 'Elige un motivo.'})
    with transaction.atomic():
        proposal = locked(proposal_id, expected_version)
        decide(proposal, 'rejected', user, reason=reason, note=(note or '').strip()[:2000])
    return proposal


def edit(proposal_id, user, expected_version, data):
    """An administrator's corrections before deciding. A field whose value changed is theirs from now on; one sent unchanged keeps
    its source, so the suppliers' data (and later the AI) can still fill it."""
    allowed = {'sku', 'description', 'oem_code', 'oem_brand', 'part_type', 'references', 'internal_code'}
    if set(data) - allowed:
        raise ValidationError({'detail': f'Solo se pueden cambiar: {", ".join(sorted(allowed))}.'})
    with transaction.atomic():
        proposal = locked(proposal_id, expected_version)
        changed, restored, cleared = set(), None, None   # restored: the OEM number unticking brought back; cleared: the one ticking took away
        previous_oem = remembered_oem(proposal)   # before this edit's own OEM fields (the drawer clears them when it ticks «código interno»)
        if 'sku' in data:
            sku = clean(str(data['sku'] or ''))
            if not sku or len(sku) > 200:
                raise ValidationError({'sku': 'Escribe el SKU (hasta 200 caracteres).'})
            if sku != proposal.sku:
                proposal.sku = sku
                changed.add('sku')
        if 'description' in data:
            description = ' '.join(str(data['description'] or '').split()).upper()
            if description != ' '.join(proposal.description.split()).upper():
                proposal.description = description
                changed.add('description')
        if 'oem_code' in data or 'oem_brand' in data:
            code = clean(str(data.get('oem_code', proposal.oem_code) or ''))
            brand = clean(str(data.get('oem_brand', proposal.oem_brand) or ''))
            if bool(code) != bool(brand) or len(code) > 120 or len(brand) > 120:
                raise ValidationError({'oem_code': 'El número OEM y su fabricante van juntos (o ninguno).'})
            if (code, brand) != (proposal.oem_code, proposal.oem_brand):
                proposal.oem_code, proposal.oem_brand = code, brand
                changed.add('oem')
        if 'part_type' in data:
            part_type = None
            if data['part_type']:
                part_type = PartType.objects.filter(pk=data['part_type']).first()
                if part_type is None:
                    raise ValidationError({'part_type': 'Elige un grupo y subgrupo existentes.'})
            if (part_type.pk if part_type else None) != proposal.part_type_id:
                proposal.part_type = part_type
                changed.add('part_type')
        if 'references' in data:
            references = normalize_references(data['references'] or [])
            if references != proposal.references:
                proposal.references = references
                changed.add('references')
        if 'internal_code' in data:
            if not isinstance(data['internal_code'], bool):
                raise ValidationError({'internal_code': 'Indica sí o no.'})
            if data['internal_code'] and clean(str(data.get('oem_code') or '')):
                raise ValidationError({'internal_code': 'Un código interno no lleva número OEM: quita el número o desmarca «Código interno».'})
            if data['internal_code'] != proposal.internal_code:
                memory = (proposal.field_sources.get('internal_code') or {}).get('oem')
                cleared = previous_oem if data['internal_code'] else None
                proposal.internal_code = data['internal_code']
                changed.add('internal_code')
                if proposal.internal_code and proposal.oem_code:  # an internal code has no OEM number
                    proposal.oem_code = proposal.oem_brand = ''
                    changed.add('oem')
                elif not proposal.internal_code and not proposal.oem_code and 'oem_code' not in data:
                    restored = restore_oem(proposal, memory)  # not an internal code after all: its OEM number comes back
        if changed:
            mark = {'source': 'admin', 'by': user.username, 'at': timezone.now().isoformat()}
            for field in changed:
                proposal.field_sources[field] = mark
            if restored:
                proposal.field_sources['oem'] = restored
            if proposal.internal_code and 'internal_code' in changed and cleared:
                proposal.field_sources['internal_code'] = {**proposal.field_sources['internal_code'], 'oem': cleared}
            proposal.version += 1
            proposal.save()
    return proposal


def remembered_oem(proposal):
    """The OEM number «código interno» is about to clear, with its whole field source (who set it, a library citation), to bring back
    as it was if it is unticked."""
    if not proposal.oem_code:
        return None
    return {'code': proposal.oem_code, 'brand': proposal.oem_brand, 'field': dict(proposal.field_sources.get('oem') or {'source': 'supplier'})}


def restore_oem(proposal, memory=None):
    """The OEM number back on a proposal that is no longer an internal code: the one «código interno» cleared (the supplier's, the OEM
    library's, an administrator's), or else a make brand's code as the supplier printed it (suggest's rule). Returns the field's
    source, or None when there is nothing to bring back."""
    if memory and memory.get('code') and memory.get('brand'):
        proposal.oem_code, proposal.oem_brand = memory['code'][:120], memory['brand'][:120]
        field = memory.get('field') if isinstance(memory.get('field'), dict) and memory['field'].get('source') else {'source': 'supplier'}
        return dict(field)
    make = vehicle_makes().get(proposal.brand, '')
    rows = list(proposal.items.select_related('item'))
    if not make or not rows:
        return None
    proposal.oem_code, proposal.oem_brand = printed_forms([row.item for row in rows])[0][:120], make
    return {'source': 'supplier'}


AI_FIELDS = {'part_type': ['part_type'], 'description': ['description'], 'internal_code': ['internal_code'], 'oem': ['oem_code', 'oem_brand']}


def ai_fill(proposal, fill, *, note, run_id, model):
    """The Revisión IA's fills on a pending proposal the caller holds locked (mall.sku_review): grupo/subgrupo (a pair of the taxonomy,
    its PartType created on first use), a description with obvious typos fixed, and «código interno» (no OEM number: the one the make
    brand gave it is cleared). fill maps a field to (value, confidence 0-1[, note]); part_type's value is (category, name). A field an
    administrator set is never touched. The version moves only on a real change, and updated_at does not: the storefront reminder is
    about proposals waiting, not about the AI's work on them. Returns {'written': [...], 'kept': [...]} (kept: an administrator's)."""
    from .technical_data import part_type_for
    written, kept, at = [], [], timezone.now().isoformat()

    def owned(*fields):
        return any((proposal.field_sources.get(field) or {}).get('source') == 'admin' for field in fields)

    def stamp(field, confidence, why=None):
        proposal.field_sources[field] = {'source': 'ai', 'confidence': round(float(confidence), 2), 'note': (why if why is not None else note or '')[:300],
                                         'at': at, 'run': str(run_id), 'model': model}
        written.append(field)

    def unpack(field):
        value, confidence, *why = fill[field]
        return value, confidence, (why[0] if why else None)

    if 'part_type' in fill:
        (category, name), confidence, why = unpack('part_type')
        if owned('part_type'):
            kept.append('part_type')
        else:
            part_type = part_type_for(category, name)
            if part_type.pk != proposal.part_type_id:
                proposal.part_type = part_type
                stamp('part_type', confidence, why)
    if 'description' in fill:
        value, confidence, why = unpack('description')
        value = ' '.join(str(value or '').split()).upper()
        if owned('description'):
            kept.append('description')
        elif value and value != ' '.join(proposal.description.split()).upper():
            previous = proposal.field_sources.get('description') or {}
            before = previous.get('from', proposal.description) if previous.get('source') == 'ai' else proposal.description
            proposal.description = value
            stamp('description', confidence, why)
            proposal.field_sources['description']['from'] = before   # the supplier's text it corrected: a new one replaces the fix
    if 'internal_code' in fill:
        _, confidence, why = unpack('internal_code')
        if owned('internal_code', 'oem'):
            kept.append('internal_code')
        elif not proposal.internal_code:
            cleared = remembered_oem(proposal)
            proposal.internal_code = True
            stamp('internal_code', confidence, why)
            if proposal.oem_code:
                proposal.oem_code = proposal.oem_brand = ''
                stamp('oem', confidence, why)
            if cleared:
                proposal.field_sources['internal_code']['oem'] = cleared
    if written:
        proposal.version += 1
        proposal.save(update_fields=sorted({column for field in written for column in AI_FIELDS[field]} | {'field_sources', 'version'}))
    return {'written': written, 'kept': kept}
