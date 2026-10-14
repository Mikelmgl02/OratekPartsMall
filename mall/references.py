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
    brandless_q(clave, brand)    the rows a (clave, brand) is reserved against; live_q() what readers see, through a relation
    clave(code)                  the database's generated mall_partcode.clave, in Python
    cite(ref, text)              upsert the ReferenceSource a reference_source text names (A3)
    find(code, brand, supplier)  the live Reference a (code, brand) belongs to, as rules 1 and 2 look it up (S3)

The main Reference (S2b). Every live line (any estado but unificada) has exactly one main Reference, not retired, whose code is the
line's codigo (Part.sku) and whose tipo the line's tipo mirrors (oem → OEM, aftermarket → AFTERMARKET, interno and otro → UNKNOWN;
is_OEM = tipo == 'OEM' until S10). This module is the only writer of is_main and of a live line's codigo:

    set_main(line, ref, actor, reason)  make a Reference of the line its main: the codigo follows it while the line is live (OD3,
                                 CatalogIdentityChange when it changes), the line's tipo too; refused (MainConflict) when another line
                                 has that codigo, optionally opening a merge case
    ensure_main(line, ...)       the main of a line that has none, or whose codigo moved: a live Reference of the line with the codigo's
                                 key (respelled to the codigo) or a new one (disputa when another line holds the key)
    ensure_mains()               ensure_main on the live lines without a main on their codigo (the matching worker, every pass, to S10)
    retype_main(line, tipo)      the main's tipo (and brand, when its key is free), the line's tipo following it
    move(refs, line)             References to another line, never as its main (merges)
    main_of(line), mains_of(ids) the live main of a line, of several
    bulk_mains(lines)            built mains for lines inserted in bulk (mall.lines.bulk_create_lines saves them)

Variants (S3). A supplier's item hangs from one Reference (SupplierItem.reference, written by mall.variants only) and sits on its line:
a Reference that moves to another line (update(part=...), move) takes its variants along (mall.variants.follow, and from 0078 the
composite foreign key), and retire() refuses a Reference variants still hang from (ReferenceHasVariants, 409: move or separate them
first), so no removal path ends in a ProtectedError. A Reference made for one supplier's code that is not the line's code is interno,
scoped to that supplier: readers never see it (Reference.objects, a line's codes and live_q leave it out, unless it is a line's main),
this module and mall.variants read it through Reference.all_objects, and only that supplier's items match it (own_interno_q).

Until S10 Part.save() keeps the main in step for the code that writes sku, is_OEM or merged_into directly (follow_line): a line
inserted without a main gets one, a renamed line's main moves to its new codigo (the old main stays as one of its References), a line
brought back from a merge takes its main back, and a tipo written on the line retypes its main. Readers leave the main out of a line's
code lists (the codigo already shows it) and fall back to the codigo while a line has no main yet (before mall.refactor.s2b_main_refs).

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


class ReferenceHasVariants(ReferenceConflict):
    """retire() refused: supplier items hang from the Reference (S3). 409, so every removal path shows the message."""
    status_code = 409
    default_code = 'conflict'


VARIANTS_HANG = 'tiene artículos de proveedor: muévelos o sepáralos antes de quitarla.'


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
    """The References readers see (Reference.objects: not retired, and not a supplier's interno code unless it is a line's main),
    through a relation when prefix names one ('codes__', 'part__codes__'): a join reaches every row, unlike Reference.objects and a
    line's codes."""
    return Q(**{f'{prefix}estado__in': LIVE}) & ~Q(**{f'{prefix}tipo': 'interno', f'{prefix}is_main': False})


def own_interno_q(supplier, prefix=''):
    """A supplier's own interno codes (S3), which only that supplier's items match, through a relation when prefix names one."""
    return Q(**{f'{prefix}estado__in': LIVE, f'{prefix}tipo': 'interno', f'{prefix}supplier_id': getattr(supplier, 'pk', supplier)})


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
    rows = Reference.all_objects.filter(clave=key, estado__in=EXCLUSIVE)  # interno codes too: they clash with their own supplier's
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


def find(code, brand='', *, supplier=None):
    """The live Reference a (code, brand) belongs to, as rules 1 and 2 look it up (S3, for a supplier's code change; S6's resolve
    builds on it): the supplier's own interno code first (its brand, then none), then the exclusive public (clave, brand), then the
    brandless (clave, ''); a brandless code joins the one line its clave sits on. None when nothing holds it, or a brandless code
    whose clave sits on two or more lines (a person decides). A Reference in disputa never takes a variant."""
    Reference, _ = _models()
    key, brand = clave(code_key(code)), brand_key(brand)
    supplier_id = getattr(supplier, 'pk', supplier)
    rows = list(Reference.all_objects.filter(clave=key, estado__in=EXCLUSIVE).order_by('-is_main', 'pk'))

    def same(a):  # spaces collapsed, as mall.variants.same_brand compares a variant's brand
        return ' '.join(a.split()) == ' '.join(brand.split())
    if supplier_id is not None:
        own = [row for row in rows if row.supplier_id == supplier_id]
        found = next((row for row in own if same(row.brand)), None) or next((row for row in own if not row.brand), None)
        if found is not None:
            return found
    public = [row for row in rows if row.supplier_id is None]
    found = next((row for row in public if same(row.brand)), None)
    if found is not None or brand:
        return found or next((row for row in public if not row.brand), None)
    return public[0] if len({row.part_id for row in public}) == 1 else None


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
    if len(code_key(code)) > 120:  # Reference.code holds 120: a bulk path (a line's main from a long SKU) is refused here, not by the database
        raise ReferenceConflict(f'El código {code_key(code)[:40]}… supera 120 caracteres.')
    check(tipo, estado, supplier, dispute_note)
    fields = {'part' if hasattr(line, 'pk') else 'part_id': line, 'supplier' if hasattr(supplier, 'pk') else 'supplier_id': supplier}
    return Reference(code=code_key(code), brand=brand_key(brand), tipo=tipo, ref_type=REF_TYPE_OF[tipo], estado=estado,
                     reference_source=(source or '').strip()[:500], created_by=actor if getattr(actor, 'pk', None) else None,
                     detail=dict(detail or {}), notes=notes or '', printed_forms=list(printed_forms or []),
                     dispute_note=(dispute_note or '').strip(), **fields)


def bulk_create(references, *, batch_size=500, actor=None, serialized=False):
    """Insert built References in bulk (no signals, as before S2a's catalog import), then their sources. The caller has planned the
    keys (the catalog import's plan); here they are checked again under each clave's advisory lock (plan §1.6.5), so a writer adding
    the same key elsewhere meanwhile waits and one of the two is refused (ReferenceConflict), and every brand becomes a brand (I12).
    The database refuses a duplicate key from 0074. serialized: the caller already keeps every other writer out (a stage CLI under the
    catalog freeze and the matching lock), so the keys are checked without taking one advisory lock each: S2b's backfill inserts some
    31,000 mains in one transaction, more locks than PostgreSQL's lock table holds."""
    Reference, _ = _models()
    references = list(references)
    if not references:
        return references
    keys = sorted({clave(ref.code) for ref in references})
    with transaction.atomic():
        if not serialized:
            lock(*keys)
        held = {}
        for start in range(0, len(keys), 2000):
            for row in Reference.all_objects.filter(clave__in=keys[start:start + 2000], estado__in=EXCLUSIVE).select_related('part').order_by('pk'):
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
                ref._checked = True  # validated above: Reference.save's shim (adopt_insert) has nothing to do
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


def adopt_insert(ref):
    """Reference.save's S2b shim for an insert by code outside this module (old code, tests: add() checks its keys first). Its
    key (clave, brand, supplier) held by its line's main makes it an update of the main: the main's id, uid, code (the codigo; another
    spelling joins printed_forms) and is_main stay, the insert's tipo wins unless it is otro (an alias never downgrades the main), and
    its source, estado and notes win when given; the line's tipo follows the main after the save. Its key held by another line's main
    makes it disputa with a note, as the S2b backfill leaves a code two lines hold (a key another line holds as any other Reference
    is refused by the database, as before S2b). Returns whether it became an update of the main."""
    Reference, _ = _models()
    if not ref.part_id or ref.is_main or ref.estado not in EXCLUSIVE:
        return False
    key = clave(ref.code)
    holders = list(Reference.all_objects.filter(clave=key, brand=ref.brand, supplier_id=ref.supplier_id, estado__in=EXCLUSIVE)
                   .select_related('part').order_by('pk')[:2])
    main = next((row for row in holders if row.part_id == ref.part_id and row.is_main), None)
    if main is None:
        # Only another line's main: a key another line holds as any other Reference was refused before S2b too (0074).
        other = next((row for row in holders if row.part_id != ref.part_id), None)
        if other is not None and other.is_main:
            ref.estado, ref.dispute_note = 'disputa', ref.dispute_note or dispute_note_for(ref.code, other)
        return False
    spelling = ref.code
    for name in ('id', 'uid', 'code', 'is_main', 'created_at', 'created_by_id', 'version'):
        setattr(ref, name, getattr(main, name))
    ref.printed_forms = list(dict.fromkeys([*main.printed_forms, *([spelling] if spelling != main.code else []), *ref.printed_forms]))[:MAX_PRINTED]
    if ref.tipo == 'otro' and main.tipo != 'otro':
        ref.tipo, ref.ref_type = main.tipo, main.ref_type
    ref.reference_source = ref.reference_source or main.reference_source
    ref.notes = ref.notes or main.notes
    ref.detail = {**(main.detail or {}), **(ref.detail or {})}
    ref.oem_reference_id = ref.oem_reference_id or main.oem_reference_id
    ref._state.adding = False
    ref._tipo_pair = (main.tipo, main.ref_type)
    ref._adopted_main = True
    return True


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
    if ref.is_main and ref.estado != 'retirada' and ({'part_id', 'supplier_id'} & set(changed) or
                                                     ('code' in changed and changed['code'] != code_key(ref.part.sku))):
        raise ReferenceConflict(f'{ref.code} es el código principal del SKU {ref.part.sku}: cambia el SKU para cambiarlo.', ref)
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
        if 'part_id' in changed:  # its variants go with it (S3; the composite foreign key does it too from 0078)
            from .variants import follow
            follow([ref.pk])
        if 'reference_source' in changed and ref.reference_source:
            cite(ref, ref.reference_source, actor)
        if ref.is_main and {'tipo', 'ref_type'} & set(changed):  # the line's tipo mirrors its main (I11)
            sync_line_tipo(ref.part, ref)
    return ref


def retire(refs, *, actor=None, replaced_by=None, reason='', allow_main=False, allow_variants=False):
    """Take References off their line: estado retirada (readers skip them), reemplazado_por when another Reference replaces it, who
    and why in detail['retired']. One save() each, so the matching queue and the OEM library mirror follow (the mirror drops the
    source an OEM alterno backed, and keeps the number). Accepts a Reference, its id, a list or a queryset (of any manager). Returns
    how many were retired; retired ones are left as they are. The main of a live line is refused (ReferenceConflict: give the line
    another main first, set_main), unless allow_main, for a caller that gives it one in the same transaction. A Reference supplier
    items hang from is refused (ReferenceHasVariants, 409; S3), unless allow_variants (a line merged by a direct write)."""
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
        rows = list(Reference.all_objects.select_for_update().filter(pk__in=ids).exclude(estado='retirada').select_related('part').order_by('pk'))
        held = next((row for row in rows if row.is_main and is_live(row.part)), None) if not allow_main else None
        if held is not None:
            raise ReferenceConflict(f'{held.code} es el código principal del SKU {held.part.sku}: dale otro código principal antes de '
                                    'retirarlo.', held)
        if not allow_variants and rows:
            from .variants import hanging
            hung = hanging(rows).values_list('reference_id', flat=True).first()
            if hung is not None:
                row = next(row for row in rows if row.pk == hung)
                raise ReferenceHasVariants(f'{row} {VARIANTS_HANG}', row)
        for row in rows:
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


# --- the main Reference (S2b) --------------------------------------------------------------------------------------------------------

LINE_TIPO = {'oem': 'OEM', 'aftermarket': 'AFTERMARKET', 'interno': 'UNKNOWN', 'otro': 'UNKNOWN'}  # main Reference tipo -> line tipo
MAIN_TIPO = {'OEM': 'oem', 'AFTERMARKET': 'aftermarket', 'UNKNOWN': 'otro'}                          # line tipo -> main Reference tipo
LINE_TIPOS = tuple(MAIN_TIPO)
ESTADO_RANK = {'verificado': 0, 'declarado': 1, 'inferido': 2, 'disputa': 3}


class MainConflict(ReferenceConflict):
    """set_main refused: another line already has that codigo (UNIQUE(sku) until S4). `line` is that line and `case` the merge case
    opened for it (open_case=True), or None."""
    def __init__(self, detail, reference=None, line=None, case=None):
        super().__init__(detail, reference)
        self.line, self.case = line, case


def line_tipo(tipo):
    """The line tipo a main Reference of this tipo gives its line."""
    return LINE_TIPO.get(tipo, 'UNKNOWN')


def main_tipo(line_tipo_value, current=None):
    """The Reference tipo a line of this tipo asks of its main: an interno main stays interno on an UNKNOWN line."""
    if current == 'interno' and line_tipo_value == 'UNKNOWN':
        return 'interno'
    return MAIN_TIPO.get(line_tipo_value, 'otro')


def is_live(line):
    """Whether a line holds a main: any estado but unificada, and not merged."""
    return not line.merged_into_id and line.estado != 'unificada'


def main_of(line, *, lock=False):
    """The live main Reference of a line (a Part or its id), or None (a unificada line, or one the S2b backfill has not reached)."""
    Reference, _ = _models()
    rows = Reference.objects.filter(part_id=getattr(line, 'pk', line), is_main=True)
    return (rows.select_for_update() if lock else rows).order_by('pk').first()


def mains_of(lines):
    """{line id: its live main Reference} for lines (Parts or ids); a line without one is left out."""
    Reference, _ = _models()
    ids = {getattr(line, 'pk', line) for line in lines}
    found = {}
    for ref in Reference.objects.filter(part_id__in=ids, is_main=True).order_by('pk'):
        found.setdefault(ref.part_id, ref)
    return found


def main_in(codes):
    """The main among a line's References already loaded (a prefetched line.codes.all()), or None."""
    return next((ref for ref in codes if ref.is_main), None)


def not_main(codes):
    """A line's References already loaded, without its main: what a code list shows (the codigo shows the main)."""
    return [ref for ref in codes if not ref.is_main]


def pair_line_tipo(line, before=None):
    """Put an unsaved change of the line's tipo or is_OEM in step before it is written (Part.save): a tipo changed since the line was
    loaded (or since its defaults, for a new line) wins, otherwise is_OEM decides (True is OEM; False leaves AFTERMARKET and UNKNOWN
    as they are and turns OEM into UNKNOWN). Legacy writers only know is_OEM."""
    values = line.__dict__
    if 'tipo' not in values and 'is_OEM' not in values:
        return line
    if line._state.adding:
        loaded_tipo, loaded_oem = 'UNKNOWN', False
    else:
        loaded_tipo, loaded_oem = (before or {}).get('tipo'), (before or {}).get('is_OEM')
    tipo, oem = line.tipo, bool(line.is_OEM)
    if tipo not in LINE_TIPOS:
        raise ReferenceConflict(f'Tipo de línea desconocido: {tipo}.')
    tipo_changed = loaded_tipo is not None and tipo != loaded_tipo
    if tipo_changed or (loaded_tipo is None and loaded_oem is not None and oem == loaded_oem):
        line.is_OEM = tipo == 'OEM'
    elif oem != (tipo == 'OEM'):
        line.tipo = 'OEM' if oem else ('UNKNOWN' if tipo == 'OEM' else tipo)
    return line


def follow_line(line, before, *, adding=False, fields=None, using=None):
    """Part.save's S2b shim (after the write): a live line inserted without a main, renamed, brought back from a merge or given
    another tipo by code that writes those columns directly gets its main in step (ensure_main). Writers of this module and
    mall.lines.create_line set line._main_followed, so their own saves are left alone."""
    values = line.__dict__  # read without loading a deferred field: most saves trigger nothing
    if values.pop('_main_followed', False) or using not in (None, 'default'):
        return None
    before = before or {}
    if (fields is None or {'merged_into', 'estado'} & set(fields)) and before and not before.get('merged_into_id') \
            and values.get('merged_into_id'):
        return retire_main_of_merged(line)
    renamed = (fields is None or 'sku' in fields) and before.get('sku') not in (None, values.get('sku'))
    back = (fields is None or {'merged_into', 'estado', 'active'} & set(fields)) and (
        before.get('merged_into_id') is not None or before.get('estado') == 'unificada')
    retyped = (fields is None or {'tipo', 'is_OEM'} & set(fields)) and before.get('tipo') not in (None, values.get('tipo'))
    if not (adding or renamed or back or retyped) or not is_live(line):
        return None
    if retyped and not (adding or renamed or back) and main_of(line) is None:
        # Only the tipo moved on a line the S2b backfill has not reached (0075 left every line UNKNOWN, and pair_line_tipo puts an OEM
        # line's tipo back in step with is_OEM on a full save): the backfill makes its main by the plan's rules, not this shim.
        return None
    return ensure_main(line, line_wins=bool(retyped and not renamed))


def retire_main_of_merged(line):
    """A line merged by code that writes merged_into directly (old code, tests): its main is retired, as a merge does (I3), so the
    line it went into can hold its codigo."""
    Reference, _ = _models()
    rows = Reference.objects.filter(part_id=line.pk, is_main=True)
    return retire(rows, reason='Línea unificada en otra', allow_main=True, allow_variants=True) if rows.exists() else 0


def _bump(rows, **values):
    """A queryset update of References that keeps their version and updated_at (no signals: is_main and tipo mean nothing to the OEM
    library mirror)."""
    from django.db.models import F
    return rows.update(**values, version=F('version') + 1, updated_at=timezone.now())


def _mark_main(line, ref, current):
    """is_main on ref and off every other main of the line (current: the line's mains, locked). Returns ref."""
    Reference, _ = _models()
    others = [row.pk for row in current if row.pk != ref.pk]
    if others:
        _bump(Reference.all_objects.filter(pk__in=others), is_main=False)
    if not ref.is_main:
        _bump(Reference.all_objects.filter(pk=ref.pk), is_main=True)
        ref.is_main = True
    return ref


def _retype(ref, tipo):
    Reference, _ = _models()
    if ref.tipo != tipo:
        _bump(Reference.all_objects.filter(pk=ref.pk), tipo=tipo, ref_type=REF_TYPE_OF[tipo])
        ref.tipo, ref.ref_type = tipo, REF_TYPE_OF[tipo]
    return ref


def sync_line_tipo(line, ref):
    """The line's tipo (and is_OEM) as its main says, written without signals when it differs."""
    from .models import Part
    tipo = line_tipo(ref.tipo)
    if line.__dict__.get('tipo') != tipo or line.__dict__.get('is_OEM') != (tipo == 'OEM'):
        Part.objects.filter(pk=line.pk).update(tipo=tipo, is_OEM=tipo == 'OEM')
        line.tipo, line.is_OEM = tipo, tipo == 'OEM'
        if hasattr(line, '_identity'):
            line._identity.update(tipo=tipo, is_OEM=tipo == 'OEM')
    return line


def dispute_note_for(code, other):
    """The note of a main that cannot hold its key: another line holds it (a person decides, through a merge case)."""
    sku = getattr(getattr(other, 'part', None), 'sku', '') or ''
    return (f'El código {code} ya es de otro SKU ({sku}): caso de unificación.' if sku else
            f'El código {code} ya es de otro SKU: caso de unificación.')


def ensure_main(line, *, brand='', tipo=None, source='', actor=None, strict=False, line_wins=False, estado=None):
    """The main of a live line on its codigo (returns it; None for a unificada line). A main already on the codigo stays. Otherwise a
    live Reference of the line with the codigo's key becomes the main (the exact spelling first, then the brand asked, then the tipo
    asked; another spelling is respelled to the codigo and kept in printed_forms), or a new one is added with `brand`, `tipo` (the
    line's when None) and `source`. A key another line holds makes the new main disputa with a note (strict: ReferenceConflict
    instead). Then the line's tipo follows the main, or with line_wins the main's tipo follows the line (a tipo written on the line by
    code that only knows is_OEM). The caller locks the line when it matters."""
    Reference, _ = _models()
    if not is_live(line):
        return None
    code = code_key(line.sku)
    key = clave(code)
    with transaction.atomic():
        lock(key)
        current = list(Reference.all_objects.select_for_update().filter(part_id=line.pk, is_main=True).order_by('pk'))
        target = next((row for row in current if row.code == code and row.estado != 'retirada'), None)
        wanted = tipo or main_tipo(line.tipo)
        if target is None:
            rows = list(Reference.objects.select_for_update().filter(part_id=line.pk, clave=key, supplier__isnull=True).order_by('pk'))
            rows.sort(key=lambda row: (row.code != code, row.brand != brand_key(brand), (row.tipo == 'oem') != (wanted == 'oem'),
                                       ESTADO_RANK.get(row.estado, 9), row.pk))
            target = rows[0] if rows else None
            if target is not None and target.code != code:
                target = update(target, code=code, actor=actor)
            if target is None:  # a main this line had before (a merge retired it): it comes back, brand and tipo as they were
                former = (Reference.all_objects.filter(part_id=line.pk, clave=key, estado='retirada', supplier__isnull=True)
                          .order_by(exact_first(code), '-pk').first())
                if former is not None:
                    brand, tipo, wanted = former.brand, tipo or former.tipo, tipo or former.tipo
            if target is None:
                try:
                    target, _ = add(line, code, brand, tipo=wanted, source=source, actor=actor, estado=estado)
                except ReferenceConflict as error:
                    if strict or error.reference is None or error.reference.part_id == line.pk:
                        raise
                    target, _ = add(line, code, brand, tipo=wanted, source=source, actor=actor, estado='disputa',
                                    dispute_note=dispute_note_for(code, error.reference))
            elif tipo and target.tipo != tipo:
                _retype(target, tipo)
        if line_wins and not tipo and line.tipo == 'OEM' and target.tipo != 'oem':
            # Marked OEM by code that only knows is_OEM: a branded OEM Reference of the codigo becomes the main rather than a second
            # OEM row made of the brandless one.
            branded = (Reference.objects.filter(part_id=line.pk, code=code, tipo='oem', supplier__isnull=True).exclude(brand='')
                       .order_by('brand', 'pk').first())
            target = branded or target
        _mark_main(line, target, current)
        if line_wins and not tipo:
            _retype(target, main_tipo(line.tipo, target.tipo))
        sync_line_tipo(line, target)
    return target


def exact_first(code):
    """An ordering that puts the rows spelled exactly `code` first."""
    from django.db.models import Case, IntegerField, Value, When
    return Case(When(code=code, then=Value(0)), default=Value(1), output_field=IntegerField())


def codigo_holder(line, code):
    """Another line whose codigo is `code` (any estado: UNIQUE(sku) covers every line until S4), or None."""
    from .lines import all_lines
    return all_lines().filter(sku__iexact=code_key(code)).exclude(pk=line.pk).order_by('pk').first()


MAIN_CASE_METHOD = 'main_codigo'


def covering_case(line_ids):
    """A legacy catalog case (any status) whose lines include all of these (compact_duplicate, shared_oem_number, main_codigo...), or
    None: such a pair already waits for a person, or one decided it."""
    from .matching_models import MatchingCase
    rows = MatchingCase.objects.filter(kind='catalog')
    for pk in {str(getattr(line, 'pk', line)) for line in line_ids}:
        rows = rows.filter(Q(sources__contains=[{'id': pk}]) | Q(candidates__contains=[{'id': pk}]))
    return rows.order_by('pk').first()


def open_main_case(line, holder, ref, reason=''):
    """A legacy catalog case (method main_codigo) asking whether a line whose main (or the one it wants) has a key holder holds is
    holder's part: today's Agrupación inteligente approves it as a merge into holder, and dismissing it keeps the two apart
    (mall.line_merge.KEEP_APART_METHODS). A case this pair already has, decided or not, is returned as it is (a person's decision is
    never reopened here), and so is nothing (None) for lines kept apart."""
    from .line_merge import apart
    from .matching_engine import part_row, save_case
    from .matching_models import MatchingCase
    from .models import Part
    key = f'catalog:main:{line.pk}:{holder.pk}'
    existing = MatchingCase.objects.filter(key=key).first() or covering_case([line, holder])
    if existing is not None:
        return existing
    if apart(line.pk, holder.pk):
        return None
    source, target = (Part.objects.prefetch_related('codes').get(pk=pk) for pk in (line.pk, holder.pk))
    what = f'que es el SKU de {holder.sku}' if code_key(holder.sku) == ref.code else f'cuya clave ya es de {holder.sku}'
    return save_case(key=key, kind='catalog', sources=[part_row(source)], candidates=[part_row(target)], target=holder.sku,
                     method=MAIN_CASE_METHOD,
                     reason=(f'{reason}: ' if reason else '') + f'{source.sku} quiere como código principal {ref.code}, {what}. Si son el '
                            'mismo repuesto, agrúpalos; si no, mantenlos separados.')


def set_main(line, ref, actor=None, reason='', *, open_case=False):
    """Make `ref` (a live Reference of the line, or its id) the line's main, and return it. While the line is live its codigo follows
    the main (OD3): sku and, when it was the old codigo, name take the main's code and a CatalogIdentityChange records the change;
    the line's tipo and is_OEM follow the main's tipo either way. A former main stays one of the line's References (a brandless one
    keeps reserving its code for every brand next to a branded main of the same code). Refuses (MainConflict) when another line has
    that codigo, opening a merge case with open_case. The caller holds the line (Account → Part) when it matters; this locks the line and
    its mains."""
    Reference, _ = _models()
    from .models import CatalogIdentityChange, Part
    holder = None
    with transaction.atomic():
        list(Part.objects.select_for_update().filter(pk=line.pk).values_list('pk', flat=True))
        ref = Reference.all_objects.select_for_update().get(pk=getattr(ref, 'pk', ref))
        if ref.part_id != line.pk:
            raise ReferenceConflict('La referencia principal tiene que ser de este SKU.')
        if ref.estado == 'retirada':
            raise ReferenceConflict('Una referencia retirada no puede ser la principal del SKU.')
        live, old_sku = is_live(line), line.sku
        if live and ref.code != old_sku:
            holder = codigo_holder(line, ref.code)
        if holder is None:
            _switch_main(line, ref, live, old_sku, actor, reason)
    if holder is not None:  # outside the atomic block, so the case outlives the refusal unless the caller rolls back too
        case = open_main_case(line, holder, ref, reason) if open_case else None
        raise MainConflict(f'El código {ref.code} ya es el SKU {holder.sku}. Si son el mismo repuesto, usa Agrupar SKU.', ref,
                           line=holder, case=case)
    return ref


def _switch_main(line, ref, live, old_sku, actor, reason):
    """set_main's write, inside its transaction: is_main moves to ref, the codigo and tipo follow, the change is recorded."""
    Reference, _ = _models()
    from .models import CatalogIdentityChange
    current = list(Reference.all_objects.select_for_update().filter(part_id=line.pk, is_main=True).order_by('pk'))
    if live and not current and ref.code != old_sku:  # no main yet (before the backfill): the old codigo stays one of its References
        current = [ensure_main(line, actor=actor)]
    _mark_main(line, ref, current)
    fields, tipo = [], line_tipo(ref.tipo)
    if live and line.sku != ref.code:
        line.sku = ref.code
        fields.append('sku')
        if line.name == old_sku:
            line.name = ref.code
            fields.append('name')
    if line.tipo != tipo or line.is_OEM != (tipo == 'OEM'):
        line.tipo, line.is_OEM = tipo, tipo == 'OEM'
        fields += ['tipo', 'is_OEM']
    if fields:
        line._main_followed = True
        line.save(update_fields=fields)
    if live and old_sku != line.sku:
        previous = next((row for row in current if row.pk != ref.pk), None)
        CatalogIdentityChange.objects.create(part=line, previous_sku=old_sku, sku=line.sku, actor=actor if getattr(actor, 'pk', None) else None,
                                             reference={'code': ref.code, 'brand': ref.brand, 'ref_type': ref.ref_type, 'tipo': ref.tipo,
                                                        'reference_source': ref.reference_source, 'action': 'set_main', 'reason': reason,
                                                        'main': ref.pk, 'previous_main': previous.pk if previous else None})
    return ref


def retype_main(line, tipo, *, brand=None, actor=None, reason=''):
    """Give the line's main this tipo (oem, aftermarket, interno or otro) and, when brand is given and its key is free (no other row of
    the line under it, and validate() lets it), that brand; the line's tipo and is_OEM follow. A line without a main gets one first
    (ensure_main). Written without signals: a mark is no evidence for the OEM library. Returns the main, or None for a unificada
    line."""
    Reference, _ = _models()
    if tipo not in REF_TYPE_OF or tipo == 'interno':
        raise ReferenceConflict(f'Tipo de referencia principal no válido aquí: {tipo}.')
    with transaction.atomic():
        main = main_of(line, lock=True) or ensure_main(line, actor=actor)
        if main is None:
            return None
        values = {}
        if brand is not None and brand_key(brand) != main.brand:
            wanted = brand_key(brand)
            if not Reference.objects.filter(part_id=line.pk, clave=main.clave, brand=wanted, supplier__isnull=True).exclude(pk=main.pk).exists():
                try:  # as an exclusive key, even for a main in disputa: the brand is taken only when nobody else holds it
                    validate(main.code, wanted, line=line.pk, estado='declarado', exclude=main.pk)
                    values['brand'] = wanted
                except ReferenceConflict:
                    pass
        if main.tipo != tipo:
            values.update(tipo=tipo, ref_type=REF_TYPE_OF[tipo])
        if values.get('brand'):
            ensure_brand(values['brand'], tipo)  # every brand a Reference carries is a Brand nombre or alias (I12), as add() does
        if values:
            _bump(Reference.all_objects.filter(pk=main.pk), **values)
            for name, value in values.items():
                setattr(main, name, value)
        if line.tipo != line_tipo(main.tipo) or line.is_OEM != (line_tipo(main.tipo) == 'OEM'):
            line.tipo = line_tipo(main.tipo)
            line.is_OEM = line.tipo == 'OEM'
            line._main_followed = True
            line.save(update_fields=['tipo', 'is_OEM'])
    return main


def absorb_into(ref, line, *, actor=None, reason='', tipo=None):
    """A merge's absorb (S2b): `ref` (a source line's main, say) stays on its line as retirada, replaced by the Reference of the same
    key on `line` (found, or added with ref's brand and tipo, or `tipo`), which Separar can bring back. Returns (that Reference,
    added)."""
    Reference, _ = _models()
    with transaction.atomic():
        ref = Reference.all_objects.select_for_update().get(pk=getattr(ref, 'pk', ref))
        retire(ref, actor=actor, reason=reason or 'Absorbida por otra línea', allow_main=True, allow_variants=True)
        kept, added = add(line, ref.code, ref.brand, tipo=tipo or ref.tipo, actor=actor)
        if kept.pk != ref.pk:
            _bump(Reference.all_objects.filter(pk=ref.pk), reemplazado_por=kept.pk)
            from .variants import rehang
            rehang([ref.pk], kept)  # its variants hang from the Reference that replaces it (S3)
    return kept, added


def move(refs, line, *, actor=None):
    """Move References (ids, rows or a queryset) to another line, never as its main: a merge carries a source's codes, its former main
    among them, onto the line it joins. One UPDATE (no signals, like the merge before S2b); their variants go with them (S3). Returns
    how many moved."""
    Reference, _ = _models()
    ids = list(refs.values_list('pk', flat=True)) if hasattr(refs, 'values_list') else [getattr(ref, 'pk', ref) for ref in refs]
    if not ids:
        return 0
    moved = _bump(Reference.all_objects.filter(pk__in=ids), part_id=getattr(line, 'pk', line), is_main=False)
    from .variants import follow
    follow(ids)  # their variants go with them (S3; the composite foreign key does it too from 0078)
    return moved


def bulk_mains(lines, *, actor=None):
    """Built (unsaved) mains for live lines just inserted in bulk (mall.lines.bulk_create_lines): brandless, tipo from the line, the
    codigo as code; disputa with a note when another line (or an earlier line of the batch) holds the key, since the bulk path cannot
    stop a whole import for it."""
    Reference, _ = _models()
    lines = [line for line in lines if is_live(line)]
    keys = {clave(code_key(line.sku)) for line in lines}
    held = {}
    for row in (Reference.objects.filter(clave__in=keys, estado__in=EXCLUSIVE, supplier__isnull=True).select_related('part').order_by('pk')):
        held.setdefault(row.clave, row)
    built = []
    for line in lines:
        code = code_key(line.sku)
        key = clave(code)
        other = held.get(key)
        if other is not None and other.part_id != line.pk:
            ref = build(line, code, '', tipo=main_tipo(line.tipo), estado='disputa', dispute_note=dispute_note_for(code, other), actor=actor)
        else:
            ref = build(line, code, '', tipo=main_tipo(line.tipo), actor=actor)
            held[key] = ref
        ref.is_main = True
        built.append(ref)
    return built


BACKFILL_PENDING_LINES = 1000  # more live lines than this without a main on their codigo: mall.refactor.s2b_main_refs has not run
FAILED_MAINS = set()             # lines ensure_main refused in this process (a person fixes them): left out of later passes


def ensure_mains(*, limit=500, actor=None):
    """ensure_main on up to `limit` live lines without a main on their codigo (lines inserted by a path that skipped it, or renamed by
    a queryset update), then a merge case (open_main_case) for each live line whose main waits in disputa with no case about it yet:
    the matching worker runs it on every pass until S10. While more than BACKFILL_PENDING_LINES live lines have no main on their
    codigo (before mall.refactor.s2b_main_refs, or after its --rollback with this code still deployed) it does nothing (status
    'backfill_pending'), so the backfill's brand rules and its manifest decide every line. A line ensure_main refuses (a key it cannot
    hold, a code too long) is logged, counted in 'failed' and left out of this process's later passes, so one line never stops
    matching. Returns {'status', 'lines', 'failed', 'cases'}."""
    import logging
    from django.db import DatabaseError
    from django.db.models import Exists, OuterRef
    from .lines import LIVE, catalog_lines
    from .models import Part
    Reference, _ = _models()
    on_codigo = Reference.objects.filter(part_id=OuterRef('pk'), is_main=True, code=OuterRef('sku'))
    missing = catalog_lines(LIVE).annotate(main_ok=Exists(on_codigo)).filter(main_ok=False)
    if missing[:BACKFILL_PENDING_LINES + 1].count() > BACKFILL_PENDING_LINES:
        logging.getLogger(__name__).info('ensure_mains: más de %s líneas sin principal; espera a mall.refactor.s2b_main_refs.',
                                         BACKFILL_PENDING_LINES)
        return {'status': 'backfill_pending', 'lines': 0, 'failed': 0, 'cases': 0}
    ids = list(missing.exclude(pk__in=FAILED_MAINS).order_by('pk').values_list('pk', flat=True)[:limit])
    done = failed = cases = 0
    for pk in ids:
        try:
            with transaction.atomic():
                line = Part.objects.select_for_update().filter(pk=pk).first()
                if line is not None and ensure_main(line, actor=actor) is not None:
                    done += 1
        except (ReferenceConflict, DatabaseError) as error:
            FAILED_MAINS.add(pk)
            failed += 1
            logging.getLogger(__name__).warning('ensure_mains: la línea %s no puede tener su principal: %s', pk,
                                                getattr(error, 'message', None) or error)
    disputed = (Reference.objects.filter(is_main=True, estado='disputa', supplier__isnull=True, part__in=catalog_lines(LIVE))
                .exclude(part_id__in=FAILED_MAINS).select_related('part').order_by('pk')[:limit])
    for ref in disputed:
        holder = (conflicts(ref.code, ref.brand, line=ref.part_id, estado='declarado').exclude(part_id=ref.part_id)
                  .select_related('part').order_by('pk').first())
        if holder is None or covering_case([ref.part_id, holder.part_id]) is not None:
            continue
        try:
            with transaction.atomic():
                cases += int(open_main_case(ref.part, holder.part, ref, 'Código en disputa') is not None)
        except DatabaseError as error:
            FAILED_MAINS.add(ref.part_id)
            logging.getLogger(__name__).warning('ensure_mains: no se pudo abrir el caso de %s: %s', ref.part.sku, error)
    return {'status': 'ok', 'lines': done, 'failed': failed, 'cases': cases}
