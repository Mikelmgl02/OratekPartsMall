"""References (inventory refactor S2a): the codes of a line, the rows of mall_partcode (class Reference, PartCode until S10), and the
only module that creates, changes or retires them. Nothing deletes a Reference: a code taken off a line is retired and stays as
history (Reference.objects and every line's `codes` leave retired rows out, so readers skip them).

    build(line, code, brand)     an unsaved Reference with every column filled, for the bulk paths (bulk_create saves them)
    add(line, code, brand)       the live Reference (clave, brand) on the line: found (a new spelling joins printed_forms), brought
                                 back when it was retired there, or created; refuses a key another line holds (validate)
    update(ref, **fields)        change one (code, brand, tipo/ref_type, reference_source, estado...), validated again
    retire(refs)                 take References off their line (estado retirada, reemplazado_por when another one replaces it)
    validate(code, brand, line)  the brandless reservation and the one-live-line rule, under the clave's advisory lock
    check(tipo, estado, ...)     what a Reference's columns must agree on (a dispute says why, interno ⇔ supplier), as 0074's CHECKs
    ensure_brand(spelling)       every brand a Reference carries is a Brand nombre or alias (Appendix C, I12): add() and update() call it
    brandless_q(clave, brand)    the rows a (clave, brand) is reserved against; live_q() the live estados through a relation
    clave(code)                  the database's generated mall_partcode.clave, in Python
    cite(ref, text)              upsert the ReferenceSource a reference_source text names (A3)

clave. The compact code ([A-Z0-9] only) when the code is OEM-like (8+ such characters and 3+ digits: 17801-30070 = 1780130070),
otherwise the code as printed, upper case, trimmed, with its spaces collapsed (EC-01 is not EC01). PostgreSQL computes it (a stored
generated column, mall.models.CLAVE), so no writer can get it wrong; clave() mirrors it to check a key before writing.

tipo and estado. tipo is oem, aftermarket, interno or otro; until S10 the legacy ref_type stays in step with it (oem = oem, company =
aftermarket, unknown = interno or otro), which Reference.save() keeps for the writers that only know ref_type (pair_tipo). estado is
verificado, declarado or inferido (the exclusive estados: their key belongs to one line), disputa (shown, not exclusive) or retirada. A
new Reference is declarado unless its source is the OEM finder (algo:...: inferido).

The brandless reservation (plan §2, rule 2). A code with no brand reserves it for every brand: an exclusive (clave, '') conflicts with
any exclusive (clave, X) on another line, and the reverse; on one line both may coexist. An interno Reference (a supplier's own code)
is exempt and only clashes with the same supplier's. The database holds the rest from 0074 (reference_one_live_line: one exclusive row
per clave, brand and supplier); validate() checks both under pg_advisory_xact_lock(724631114, hashtext(clave)), so two writers adding
one key wait for each other. Lock order: supplier Accounts → Parts → References → SupplierItems (plan §1.6).
"""
import re

from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

REFERENCE_LOCK = 724631114  # pg advisory lock class of a clave (matching 724631109 ... Revisión IA 724631113)
TIPO_OF = {'oem': 'oem', 'company': 'aftermarket', 'unknown': 'otro'}            # ref_type -> tipo (plan A2)
REF_TYPE_OF = {'oem': 'oem', 'aftermarket': 'company', 'interno': 'unknown', 'otro': 'unknown'}  # tipo -> ref_type, kept until S10
TIPOS = tuple(REF_TYPE_OF)
ESTADOS = ('verificado', 'declarado', 'inferido', 'disputa', 'retirada')
EXCLUSIVE = ('verificado', 'declarado', 'inferido')
LIVE = EXCLUSIVE + ('disputa',)
SOURCE_KINDS = ('catalogo', 'biblioteca', 'proveedor', 'admin', 'ia', 'buscador')
UPDATABLE = ('code', 'brand', 'tipo', 'ref_type', 'estado', 'reference_source', 'dispute_note', 'notes', 'detail', 'printed_forms',
             'supplier', 'part')
MAX_PRINTED = 20
# What PostgreSQL's \s matches under the database's locale (en_US.utf8, glibc iswspace): ASCII whitespace and the Unicode spaces
# U+1680, U+2000-U+2006, U+2008-U+200A, U+2028, U+2029, U+205F and U+3000; never the no-break spaces U+00A0, U+2007 and U+202F.
SPACES = re.compile('[\t\n\v\f\r \u1680\u2000-\u2006\u2008-\u200a\u2028\u2029\u205f\u3000]+')
UNIQUE_PRINTED = 'unique_brand_code'  # the uniqueness of one printed (brand, code) until 0074 drops it
CATALOG_PAGE = re.compile(r' · pág\. ')  # how mall.supplier_catalog_import cites a catalog page


class ReferenceConflict(ValidationError):
    """A Reference this write cannot make: its key belongs to another line, or the request is not a Reference. It is a DRF
    ValidationError, so an API answers 400 with the message and the callers that catch the catalog's ValidationError keep working."""
    def __init__(self, detail, reference=None):
        super().__init__(detail)
        self.reference = reference

    @property
    def message(self):
        return ' '.join(str(item) for item in self.detail) if isinstance(self.detail, list) else str(self.detail)


def _models():
    from .models import Reference, ReferenceSource
    return Reference, ReferenceSource


# --- keys ----------------------------------------------------------------------------------------------------------------------------

def _upper(text):
    """upper() as PostgreSQL applies it: character by character, so ß stays ß instead of becoming SS."""
    return ''.join(char if len(char.upper()) != 1 else char.upper() for char in text)


def clave(code):
    """mall_partcode.clave for a code (mall.models.CLAVE): compact when OEM-like (8+ [A-Z0-9] characters, 3+ digits), else the
    printed form, upper case, trimmed of spaces, inner whitespace collapsed to one space. mall.test_references compares it with the
    column."""
    code = code or ''
    compact = re.sub(r'[^A-Z0-9]', '', _upper(code))
    if len(compact) >= 8 and len(re.sub(r'[^0-9]', '', code)) >= 3:
        return compact
    return SPACES.sub(' ', _upper(code.strip(' ')))


def code_key(code):
    """A code as References store it: trimmed and upper case (Reference.save)."""
    return (code or '').strip().upper()


def brand_key(brand):
    """A brand as References store it until S6 (trimmed, upper case: spaces and aliases are S6's), '' meaning any brand."""
    return (brand or '').strip().upper()


def tipo_of(ref_type):
    return TIPO_OF.get(ref_type or 'unknown', 'otro')


def pair_tipo(ref):
    """Put a Reference's tipo and ref_type in step before it is saved: a tipo changed since it was loaded wins, otherwise its ref_type
    decides; a new one takes the tipo it was given, or the one its ref_type names when tipo was left at its default (otro)."""
    tipo, ref_type = ref.tipo, ref.ref_type
    if tipo not in REF_TYPE_OF:
        raise ReferenceConflict(f'Tipo de referencia desconocido: {tipo}.')
    if REF_TYPE_OF[tipo] == ref_type:
        return
    loaded_tipo = getattr(ref, '_tipo_pair', (None, None))[0]
    # A loaded row whose tipo nobody touched follows its ref_type: the code before S2a wrote ref_type alone (tipo kept 0073's default).
    tipo_wins = tipo != 'otro' if ref._state.adding or loaded_tipo is None else tipo != loaded_tipo
    if tipo_wins or ref_type not in TIPO_OF:
        ref.ref_type = REF_TYPE_OF[tipo]
    else:
        ref.tipo = TIPO_OF[ref_type]


def live_q(prefix=''):
    """The References that are not retired, through a relation when prefix names one ('codes__', 'part__codes__'): a join reaches
    every row, unlike Reference.objects and a line's codes."""
    return Q(**{f'{prefix}estado__in': LIVE})


def brandless_q(key, brand='', prefix=''):
    """The rows a key (a clave) under `brand` is reserved against: under a brand, the same brand and no brand; with no brand, any
    brand (a brandless code reserves it for every brand)."""
    found = Q(**{f'{prefix}clave': key})
    return found & Q(**{f'{prefix}brand__in': [brand, '']}) if brand else found


def lock(*keys):
    """Take the advisory lock of each clave for this transaction (PostgreSQL), in one order so two writers never cross."""
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        for key in sorted({key for key in keys if key}):
            cursor.execute('SELECT pg_advisory_xact_lock(%s, hashtext(%s))', [REFERENCE_LOCK, key])


# --- reads ---------------------------------------------------------------------------------------------------------------------------

def conflicts(code, brand='', *, line=None, supplier=None, estado='declarado', exclude=None):
    """The exclusive References on other lines a live (code, brand) would clash with, and on its own line one with the same key
    (clave, brand, supplier). Empty for an estado that holds no key (disputa, retirada)."""
    Reference, _ = _models()
    if estado not in EXCLUSIVE:
        return Reference.objects.none()
    key, brand = clave(code_key(code)), brand_key(brand)
    rows = Reference.objects.filter(clave=key, estado__in=EXCLUSIVE)
    if exclude is not None:
        rows = rows.exclude(pk=getattr(exclude, 'pk', exclude))
    line_id = getattr(line, 'pk', line)
    supplier_id = getattr(supplier, 'pk', supplier)
    if supplier_id is not None:  # an interno code: only the same supplier's same key
        clash = Q(supplier_id=supplier_id, brand=brand)
    else:
        clash = Q(supplier__isnull=True) & brandless_q(key, brand)
    if line_id is None:
        return rows.filter(clash)
    same = Q(part_id=line_id, brand=brand) & (Q(supplier_id=supplier_id) if supplier_id is not None else Q(supplier__isnull=True))
    return rows.filter((clash & ~Q(part_id=line_id)) | same)


def validate(code, brand='', *, line=None, supplier=None, estado='declarado', exclude=None):
    """Refuse (ReferenceConflict) a live (code, brand) another line holds, or one its own line already has under the same key.
    Takes the clave's advisory lock for the caller's transaction. line None: a new line, so every line counts."""
    code = code_key(code)
    if not code:
        raise ReferenceConflict('Ingresa el código de la referencia.')
    if len(code) > 120:
        raise ReferenceConflict('El código supera 120 caracteres.')
    lock(clave(code))
    other = conflicts(code, brand, line=line, supplier=supplier, estado=estado, exclude=exclude).select_related('part').order_by('pk').first()
    if other is None:
        return
    line_id = getattr(line, 'pk', line)
    if line_id is not None and other.part_id == line_id:
        raise ReferenceConflict(f'{f"{other.brand} " if other.brand else ""}{other.code} ya es una referencia de este SKU.', other)
    raise ReferenceConflict(f'El código {code} ya pertenece a otro SKU ({other.part.sku}). Si son equivalentes, usa Agrupar SKU para '
                            'unificarlos primero.', other)


# --- writes --------------------------------------------------------------------------------------------------------------------------

def default_estado(source):
    """The estado of a new Reference: inferido when the OEM finder wrote it (algo:...), declarado otherwise."""
    return 'inferido' if (source or '').strip().lower().startswith('algo:') else 'declarado'


def check(tipo, estado, supplier=None, dispute_note=''):
    """Refuse (ReferenceConflict) columns a Reference cannot hold together, as 0074's CHECKs do: an unknown tipo or estado, an interno
    code without its supplier (or a supplier on another tipo), a dispute without its note. build() and update() call it on the
    values the row ends up with, so a write never reaches the database to fail there."""
    if tipo not in REF_TYPE_OF:
        raise ReferenceConflict(f'Tipo de referencia desconocido: {tipo}.')
    if estado not in ESTADOS:
        raise ReferenceConflict(f'Estado de referencia desconocido: {estado}.')
    if (tipo == 'interno') != (supplier is not None):
        raise ReferenceConflict('Un código interno lleva su proveedor, y solo un código interno lo lleva.')
    if estado == 'disputa' and not (dispute_note or '').strip():
        raise ReferenceConflict('Una referencia en disputa lleva el motivo de la disputa.')


def ensure_brand(spelling, tipo='otro'):
    """Make the brand spelling a Reference carries a Brand nombre or alias (Appendix C, I12), as mall.refactor.s2a_references seeds
    them: the brand it names (mall.brands.lookup), else a new brand under its normalized name (vehiculo for an OEM code, repuesto
    otherwise); and the spelling as an alias when it differs from the nombre. Returns the brand, or None for '' (any brand)."""
    from . import brands
    from .brand_models import Brand, BrandAlias
    spelling = brand_key(spelling)
    if not spelling:
        return None
    brand = brands.lookup(spelling)
    if brand is None:
        brand, _ = Brand.objects.get_or_create(nombre=brands.normalize(spelling), defaults={
            'tipo': 'vehiculo' if tipo == 'oem' else 'repuesto', 'notas': 'Creada al guardar una referencia con esta marca.'})
    if spelling != brand.nombre and not BrandAlias.objects.filter(alias=spelling).exists():
        BrandAlias.objects.get_or_create(alias=spelling, defaults={'brand': brand})
    return brand


def build(line, code, brand='', *, tipo=None, ref_type=None, estado=None, supplier=None, source='', actor=None, detail=None, notes='',
          printed_forms=None, dispute_note=''):
    """An unsaved Reference of `line` (a Part or its id) with every column the transition needs: code and brand normalized, tipo
    (or the one ref_type names) with ref_type in step, estado (disputa with its note), the supplier of an interno code, created_by.
    Bulk paths save it with bulk_create (which checks the keys again); add() saves one."""
    Reference, _ = _models()
    tipo = tipo or tipo_of(ref_type)
    estado = estado or default_estado(source)
    if estado == 'retirada':
        raise ReferenceConflict('Una referencia nueva no puede estar retirada.')
    check(tipo, estado, supplier, dispute_note)
    fields = {'part' if hasattr(line, 'pk') else 'part_id': line, 'supplier' if hasattr(supplier, 'pk') else 'supplier_id': supplier}
    return Reference(code=code_key(code), brand=brand_key(brand), tipo=tipo, ref_type=REF_TYPE_OF[tipo], estado=estado,
                     reference_source=(source or '').strip()[:500], created_by=actor if getattr(actor, 'pk', None) else None,
                     detail=dict(detail or {}), notes=notes or '', printed_forms=list(printed_forms or []),
                     dispute_note=(dispute_note or '').strip(), **fields)


def bulk_create(references, *, batch_size=500, actor=None):
    """Insert built References in bulk (no signals, as before S2a's catalog import), then their sources. The caller has planned the
    keys (the catalog import's plan); here they are checked again under each clave's advisory lock (plan §1.6.5), so a writer adding
    the same key elsewhere meanwhile waits and one of the two is refused (ReferenceConflict), and every brand becomes a brand (I12).
    The database refuses a duplicate key from 0074."""
    Reference, _ = _models()
    references = list(references)
    if not references:
        return references
    keys = sorted({clave(ref.code) for ref in references})
    with transaction.atomic():
        lock(*keys)
        held = {}
        for start in range(0, len(keys), 2000):
            for row in Reference.objects.filter(clave__in=keys[start:start + 2000], estado__in=EXCLUSIVE).select_related('part').order_by('pk'):
                held.setdefault(row.clave, []).append(row)
        seen = set()
        for ref in references:
            if ref.estado not in EXCLUSIVE:
                continue
            key = (clave(ref.code), ref.brand, ref.supplier_id)
            if key in seen:
                raise ReferenceConflict(f'El código {ref.code} se repite con otra grafía en esta carga.')
            seen.add(key)
            for row in held.get(key[0], []):  # conflicts(), on the rows read once
                if ref.supplier_id is not None:
                    clash = row.supplier_id == ref.supplier_id and row.brand == ref.brand
                else:
                    clash = row.supplier_id is None and (not ref.brand or row.brand in (ref.brand, ''))
                if row.part_id == ref.part_id and row.brand == ref.brand and row.supplier_id == ref.supplier_id:
                    raise ReferenceConflict(f'{ref.code} ya es una referencia de este SKU.', row)
                if clash and row.part_id != ref.part_id:
                    raise ReferenceConflict(f'El código {ref.code} ya pertenece a otro SKU ({row.part.sku}).', row)
        for spelling, tipo in {ref.brand: ref.tipo for ref in references if ref.brand}.items():
            ensure_brand(spelling, tipo)
        Reference.objects.bulk_create(references, batch_size=batch_size)
        for ref in references:
            if ref.reference_source:
                cite(ref, ref.reference_source, actor)
    return references


def add_spelling(ref, code):
    """Keep another printed spelling of the same key on the Reference (printed_forms, at most MAX_PRINTED)."""
    code = code_key(code)
    if code and code != ref.code and code not in ref.printed_forms and len(ref.printed_forms) < MAX_PRINTED:
        ref.printed_forms = [*ref.printed_forms, code]
        ref.save(update_fields=['printed_forms'])


def add(line, code, brand='', *, tipo=None, ref_type=None, estado=None, supplier=None, source='', actor=None, detail=None, notes='',
        dispute_note=''):
    """(reference, added) for a live (code, brand) on the line. When the line already holds that key (clave, brand, supplier) the
    existing Reference is returned unchanged (a new spelling joins its printed_forms; added False); one retired from this line comes
    back with the tipo, estado and source given (added True); otherwise a new one is created after validate(). Its brand becomes a
    brand (ensure_brand). The caller locks the line (Account → Part) when it needs to."""
    Reference, _ = _models()
    ref = build(line, code, brand, tipo=tipo, ref_type=ref_type, estado=estado, supplier=supplier, source=source, actor=actor,
                detail=detail, notes=notes, dispute_note=dispute_note)
    if not ref.code:
        raise ReferenceConflict('Ingresa el código de la referencia.')
    if len(ref.code) > 120:
        raise ReferenceConflict('El código supera 120 caracteres.')
    key = clave(ref.code)
    with transaction.atomic():
        lock(key)
        same = list(Reference.all_objects.select_for_update().filter(part_id=ref.part_id, clave=key, brand=ref.brand,
                                                                     supplier_id=ref.supplier_id).order_by('pk'))
        live = next((row for row in same if row.estado != 'retirada'), None)
        if live is not None:
            add_spelling(live, ref.code)
            return live, False
        validate(ref.code, ref.brand, line=ref.part_id, supplier=ref.supplier_id, estado=ref.estado)
        ensure_brand(ref.brand, ref.tipo)
        retired = next((row for row in reversed(same) if row.code == ref.code), None) or (same[-1] if same else None)
        if retired is not None:
            return revive(retired, ref, actor), True
        try:
            with transaction.atomic():
                ref.save()
        except IntegrityError as error:
            # Until 0074 drops unique_brand_code, one printed (brand, code) is a single row wherever it sits: a code retired on another
            # line comes back here instead. Any other refusal (a key a concurrent bulk write took) is a conflict, never a move.
            elsewhere = None
            if constraint_of(error) == UNIQUE_PRINTED:
                elsewhere = Reference.all_objects.select_for_update().filter(brand=ref.brand, code=ref.code, estado='retirada').first()
            if elsewhere is None:
                raise ReferenceConflict(f'El código {ref.code} ya está registrado.')
            moved_from = str(elsewhere.part_id)
            elsewhere.part_id = ref.part_id
            ref = revive(elsewhere, ref, actor, extra={'moved_from': moved_from})
            return ref, True
        if ref.reference_source:
            cite(ref, ref.reference_source, actor)
        return ref, True


def constraint_of(error):
    """The name of the constraint an IntegrityError reports (PostgreSQL's diagnostics), or ''."""
    return getattr(getattr(error.__cause__, 'diag', None), 'constraint_name', '') or ''


def revive(row, wanted, actor, extra=None):
    """Bring a retired Reference back as `wanted` says (tipo, estado, source, supplier; its printed spelling joins printed_forms)."""
    if wanted.code != row.code:
        forms = [form for form in [row.code, *row.printed_forms] if form != wanted.code]
        row.code, row.printed_forms = wanted.code, forms[:MAX_PRINTED]
    row.tipo, row.ref_type, row.estado, row.reemplazado_por = wanted.tipo, wanted.ref_type, wanted.estado, None
    row.dispute_note = wanted.dispute_note
    if wanted.reference_source:
        row.reference_source = wanted.reference_source
    returned = {'by': getattr(actor, 'username', ''), 'at': timezone.now().isoformat(), **(extra or {})}
    row.detail = {**(row.detail or {}), 'returned': returned}
    row.save(update_fields=['part', 'code', 'printed_forms', 'tipo', 'ref_type', 'estado', 'dispute_note', 'reemplazado_por',
                            'reference_source', 'detail'])
    if row.reference_source:
        cite(row, row.reference_source, actor)
    return row


def update(ref, *, actor=None, **fields):
    """Change a Reference and save only what changed: code, brand, tipo or ref_type (kept in step), estado (not retirada: use
    retire), reference_source (its source is upserted), dispute_note, notes, detail, printed_forms, supplier, part. The values the row
    ends up with are checked (check(): a dispute says why, interno ⇔ supplier) and a change of key, line or estado is validated again
    (ReferenceConflict), so a retired Reference brought back is validated like a new one (and replaces nothing any more); a new brand
    becomes a brand (ensure_brand). The caller holds the line's lock when it needs one. Returns the Reference."""
    unknown = set(fields) - set(UPDATABLE)
    if unknown:
        raise ReferenceConflict(f'No se puede cambiar {", ".join(sorted(unknown))} de una referencia aquí.')
    if fields.get('estado') == 'retirada':
        raise ReferenceConflict('Para retirar una referencia usa mall.references.retire.')
    if 'code' in fields:
        fields['code'] = code_key(fields['code'])
    if 'brand' in fields:
        fields['brand'] = brand_key(fields['brand'])
    if 'reference_source' in fields:
        fields['reference_source'] = (fields['reference_source'] or '').strip()[:500]
    if 'tipo' in fields and 'ref_type' not in fields:
        fields['ref_type'] = REF_TYPE_OF.get(fields['tipo'], ref.ref_type)
    elif 'ref_type' in fields and 'tipo' not in fields:
        fields['tipo'] = tipo_of(fields['ref_type'])
    for relation in ('part', 'supplier'):  # a model or its id
        if relation in fields:
            value = fields.pop(relation)
            fields[f'{relation}_id'] = getattr(value, 'pk', value)
    changed = {name: value for name, value in fields.items() if getattr(ref, name) != value}
    if not changed:
        return ref
    # Checked on the values the row ends up with: a retired Reference brought back (estado declarado...) is validated like a new one.
    estado = changed.get('estado', ref.estado)
    check(changed.get('tipo', ref.tipo), estado, changed.get('supplier_id', ref.supplier_id), changed.get('dispute_note', ref.dispute_note))
    with transaction.atomic():
        if {'code', 'brand', 'estado', 'part_id', 'supplier_id'} & set(changed) and estado != 'retirada':
            validate(changed.get('code', ref.code), changed.get('brand', ref.brand), line=changed.get('part_id', ref.part_id),
                     supplier=changed.get('supplier_id', ref.supplier_id), estado=estado, exclude=ref.pk)
        if ref.estado == 'retirada' and estado != 'retirada':  # back on its line: it replaces nothing, and says who brought it back
            if ref.reemplazado_por_id is not None:
                changed['reemplazado_por_id'] = None
            returned = {'by': getattr(actor, 'username', '') or '', 'at': timezone.now().isoformat()}
            changed['detail'] = {**(changed.get('detail', ref.detail) or {}), 'returned': returned}
        if changed.get('brand'):
            ensure_brand(changed['brand'], changed.get('tipo', ref.tipo))
        if 'code' in changed and ref.code and clave(ref.code) == clave(changed['code']):
            forms = [form for form in [ref.code, *ref.printed_forms] if form != changed['code']]
            changed['printed_forms'] = forms[:MAX_PRINTED]
        for name, value in changed.items():
            setattr(ref, name, value)
        names = [name[:-3] if name in ('part_id', 'supplier_id', 'reemplazado_por_id') else name for name in changed]
        ref.save(update_fields=names)
        if 'reference_source' in changed and ref.reference_source:
            cite(ref, ref.reference_source, actor)
    return ref


def retire(refs, *, actor=None, replaced_by=None, reason=''):
    """Take References off their line: estado retirada (readers skip them), reemplazado_por when another Reference replaces it, who
    and why in detail['retired']. One save() each, so the matching queue and the OEM library mirror follow (the mirror drops the
    source an OEM alterno backed, and keeps the number). Accepts a Reference, its id, a list or a queryset (of any manager). Returns
    how many were retired; retired ones are left as they are."""
    Reference, _ = _models()
    if refs is None:
        return 0
    if hasattr(refs, 'values_list'):
        ids = list(refs.values_list('pk', flat=True))
    else:
        ids = [getattr(ref, 'pk', ref) for ref in (refs if isinstance(refs, (list, tuple, set)) else [refs])]
    replaced_id = getattr(replaced_by, 'pk', replaced_by)
    count = 0
    stamp = {'by': getattr(actor, 'username', '') or '', 'at': timezone.now().isoformat(), **({'reason': reason} if reason else {})}
    with transaction.atomic():
        for row in Reference.all_objects.select_for_update().filter(pk__in=ids).exclude(estado='retirada').order_by('pk'):
            row.estado, row.is_main = 'retirada', False
            if replaced_id is not None and replaced_id != row.pk:
                row.reemplazado_por_id = replaced_id
            row.detail = {**(row.detail or {}), 'retired': stamp}
            row.save(update_fields=['estado', 'is_main', 'reemplazado_por', 'detail'])
            count += 1
    return count


def purge(ids):
    """Delete References outright: only the S2a backfill's --rollback, for rows the code retired that nothing points at, which the code
    before S2a would show as live codes again. Returns how many went."""
    Reference, _ = _models()
    ids = list(ids)
    if not ids:
        return 0
    with transaction.atomic():
        return Reference._base_manager.filter(pk__in=ids).exclude(reemplaza__isnull=False).delete()[1].get(Reference._meta.label, 0)


# --- sources -------------------------------------------------------------------------------------------------------------------------

def catalog_citations():
    """The citations of the supplier catalogs mall.supplier_catalog_import loaded (their OEM table sources name the catalog)."""
    from .oem_reference_models import OEMReferenceSource
    return sorted(set(OEMReferenceSource.objects.filter(kind='aftermarket_catalog', detail__has_key='catalog')
                      .values_list('citation', flat=True)) - {''})


def source_of(text, catalogs=None):
    """(kind, citation, detail) of a reference_source text, by its prefix (plan A3): algo: the OEM finder (buscador), a URL an AI
    lookup (ia), 'Declarado por' a supplier (proveedor), 'CONFIRMADO POR' or 'Marcado como OEM por' an administrator, a supplier
    catalog's citation (GMB, ASVA, ATE: catalogo), anything else an administrator. None for a blank text."""
    text = (text or '').strip()[:500]
    if not text:
        return None
    lower = text.lower()
    if lower.startswith('algo:'):
        from .oem_reference import source_of as library_source
        return 'buscador', text, library_source(text)[2]
    if lower.startswith(('http://', 'https://')):
        return 'ia', text, {'approved': True}
    if text.startswith('Declarado por'):
        return 'proveedor', text, {}
    if text.upper().startswith('CONFIRMADO POR') or text.startswith('Marcado como OEM por'):
        return 'admin', text, {}
    if catalogs is None:
        catalogs = catalog_citations() if not CATALOG_PAGE.search(text) else ()
    if CATALOG_PAGE.search(text) or any(text == catalog or text.startswith(f'{catalog} · ') for catalog in catalogs):
        return 'catalogo', text, {}
    return 'admin', text, {}


def cite(ref, text, actor=None, *, kind=None, detail=None, catalogs=None):
    """Upsert the ReferenceSource a reference_source text names (or kind and citation=text when kind is given) and merge its detail.
    Returns (source, created), or (None, False) for a blank text."""
    _, ReferenceSource = _models()
    found = (kind, (text or '').strip()[:500], {}) if kind else source_of(text, catalogs)
    if found is None:
        return None, False
    kind, citation, extra = found
    if kind not in SOURCE_KINDS:
        raise ReferenceConflict(f'Tipo de fuente desconocido: {kind}.')
    merged = {**extra, **(detail or {})}
    source, created = ReferenceSource.objects.get_or_create(reference=ref, kind=kind, citation=citation,
                                                            defaults={'detail': merged, 'created_by': actor if getattr(actor, 'pk', None) else None})
    missing = {key: value for key, value in merged.items() if key not in source.detail}
    if not created and missing:  # another sighting adds what the source did not say yet, and never overwrites it
        source.detail = {**source.detail, **missing}
        source.save(update_fields=['detail'])
    return source, created
