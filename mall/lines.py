"""The OEM line's lifecycle (inventory refactor S1): the only writer of Part.estado, origen and the decision fields, and the querysets
the readers take lines from, each with an explicit estado decision. From S5 it also writes a line's subgrupo (set_subgrupo: part_type
and, until S10, the grupo and subgrupo text that follow it).

    referencia   a number of the OEM library with no product yet (S4)
    preparacion  a new product being prepared from supplier items (Productos nuevos, S6)
    publicada    on sale: the storefront shows it
    rechazada    turned down, with a reason (decision_reason) and who decided
    retirada     taken off the catalog; it keeps its codes reserved
    unificada    merged into another line (merged_into); its codigo is frozen

"Live" means any estado but unificada. Until S10 `active` mirrors estado == 'publicada': Part.save() keeps the pair in step (via
pair_lifecycle), queryset writers set both, and migration 0072 adds the CHECK. Old code that only knows `active` keeps working: a
line it retires becomes retirada, one it reactivates publicada.

Readers: storefront_lines() for what customers see, catalog_lines() for the admin grid (publicada and retirada by default),
lines_by_codigo() for a SKU string, all_lines() where every estado counts (a writer addressing the exact row, a code another line
holds), detail_line() for a detail endpoint (any estado; a write refuses a unificada line), and same_line()/canonical() wherever two lines are compared (order lines, cesta, quote drafts): a merged line is the line it was
merged into. mall.test_refactor_guards keeps every Part.objects filter on active, merged_into or sku outside this module going through
them, unless its line says why not (# estado: <reason>).
"""
import uuid

from django.db.models import Q
from django.db.models.functions import Lower
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException

ESTADOS = ('referencia', 'preparacion', 'publicada', 'rechazada', 'retirada', 'unificada')
ORIGENES = ('catalogo', 'biblioteca', 'proveedor')
REASONS = ('duplicate', 'not_a_part', 'bad_data', 'other')  # Part.REASONS (SkuProposal.REJECT_REASONS)
STOREFRONT = ('publicada',)
CATALOG = ('publicada', 'retirada')
LIVE = tuple(estado for estado in ESTADOS if estado != 'unificada')
PHRASES = {'referencia': 'en referencia', 'preparacion': 'en preparación', 'publicada': 'publicada', 'rechazada': 'rechazada',
           'retirada': 'retirada', 'unificada': 'unificada en otra'}
PAIRED = {'active', 'estado', 'merged_into'}  # a write of any of these writes estado and active together
MAX_CHAIN = 10  # merged_into is depth 1 (I6); a longer chain is followed this far at most


class LineStateError(ValueError):
    """A transition the lifecycle does not allow; the message is for the person who asked for it."""


class LineMerged(APIException):
    """A write to a unificada line: its codigo is frozen and its edits belong to the line it was merged into."""
    status_code = 409
    default_detail = 'Este SKU se agrupó en otro: edita el SKU en el que se agrupó.'


def _lines(queryset=None):
    from .models import Part
    return Part.objects.all() if queryset is None else queryset


def phrase(estado):
    """How a message says a line is in an estado: 'Esta línea está en preparación'."""
    return PHRASES.get(estado, estado)


def codigo_key(codigo):
    """A SKU string as lines store it: trimmed and upper case (Part.save)."""
    return (codigo or '').strip().upper()


# --- readers --------------------------------------------------------------------------------------------------------------------------

def storefront_lines(queryset=None):
    """The lines customers see: publicada. Until S10 it also asks for active and no merge, which the 0072 CHECKs make the same thing,
    so a database not yet backfilled (estado still at its default) shows nothing it did not show before."""
    return _lines(queryset).filter(storefront_q())


def storefront_q(prefix=''):
    """storefront_lines as a Q, through a relation when prefix names one ('part__')."""
    return Q(**{f'{prefix}estado': 'publicada', f'{prefix}active': True, f'{prefix}merged_into__isnull': True})


def catalog_lines(estados=CATALOG, queryset=None):
    """The lines in these estados (the admin catalog: publicada and retirada). merged_into decides unificada too, for the same reason
    as storefront_lines: unless it is asked for, merged lines are left out, and when it is, a merged line counts whatever its estado."""
    return _lines(queryset).filter(catalog_q(estados))


def catalog_q(estados=CATALOG, prefix=''):
    """catalog_lines as a Q, through a relation when prefix names one ('part__', 'parts__'): for counts and joins that reach lines
    from another model."""
    estados = tuple(estados)
    unknown = sorted(set(estados) - set(ESTADOS))
    if unknown:
        raise ValueError(f'Estados de línea desconocidos: {", ".join(unknown)}')
    if 'unificada' in estados:
        return Q(**{f'{prefix}estado__in': estados}) | Q(**{f'{prefix}merged_into__isnull': False})
    return Q(**{f'{prefix}estado__in': estados, f'{prefix}merged_into__isnull': True})


def all_lines(queryset=None):
    """Every line, in every estado, unificada included: where any line counts, such as a code another line already holds or a writer
    that addresses the exact row it loaded. From S4 a codigo is unique among the catalog's lines only (line_codigo_unique_catalog):
    lines in referencia may share one across makes, so a SKU string goes through lines_by_codigo(s). A reader that wants the line
    a SKU names uses lines_by_codigos; one that lists lines uses storefront_lines or catalog_lines."""
    return _lines(queryset).all()


def on_storefront(line):
    """Whether a loaded line is on sale (storefront_lines, in Python): publicada, and until S10 active and not merged as well."""
    return line is not None and line.estado == 'publicada' and line.active and not line.merged_into_id


def refuse_unificada(line):
    """For a detail endpoint's write: a unificada line refuses (LineMerged, 409) naming the line it went into. Returns the line."""
    if line.merged_into_id or line.estado == 'unificada':
        target = canonical(line) if line.merged_into_id else None
        raise LineMerged(f'El SKU {line.sku} se agrupó en {target.sku}: edita ese SKU.' if target and target.pk != line.pk else None)
    return line


def detail_line(pk, *, queryset=None, write=False):
    """A line by id for a detail endpoint (the admin's catalog detail, technical sheet and images): any estado, so a line in
    preparación keeps its sheet and images editable (MatchEditor), and 404 when no line has the id. write=True refuses a unificada
    line (refuse_unificada)."""
    line = get_object_or_404(all_lines(queryset), pk=pk)
    return refuse_unificada(line) if write else line


def lines_by_codigos(codigos, *, include=(), queryset=None):
    """{codigo (trimmed, upper case): [lines]} for SKU strings, best first: the publicada line, then retirada, then the line a
    unificada line with that codigo was merged into (its canonical line, once), then lines in the estados of `include` (referencia,
    preparacion, rechazada: several makes may share one codigo there, so the caller chooses). Codigos naming no line are left out."""
    keys = {codigo_key(codigo) for codigo in codigos} - {''}
    include = tuple(include)
    if set(include) - set(ESTADOS) or 'unificada' in include:
        raise ValueError('include solo admite referencia, preparacion, publicada, rechazada o retirada.')
    wanted = set(CATALOG) | {'unificada'} | set(include)
    found = {}
    if not keys:
        return found
    rows = list(_lines(queryset).annotate(codigo_lower=Lower('sku')).filter(codigo_lower__in=[key.lower() for key in keys])
                .select_related('merged_into'))
    targets = canonical_lines([row for row in rows if row.merged_into_id])
    extra = [estado for estado in include if estado not in CATALOG]
    rank = {'publicada': 0, 'retirada': 1, 'unificada': 2, **{estado: 3 + i for i, estado in enumerate(extra)}}
    rows = [(effective_estado(row), row) for row in rows]
    for estado, row in sorted(((e, r) for e, r in rows if e in wanted), key=lambda pair: (rank[pair[0]], pair[1].sku, str(pair[1].pk))):
        line = targets.get(row.pk) if estado == 'unificada' else row
        if line is None or (estado == 'unificada' and effective_estado(line) not in (*CATALOG, *include)):
            continue
        bucket = found.setdefault(codigo_key(row.sku), [])
        if all(line.pk != other.pk for other in bucket):
            bucket.append(line)
    return found


def lines_by_codigo(codigo, *, include=(), queryset=None):
    """The lines a SKU string names, best first (lines_by_codigos)."""
    return lines_by_codigos([codigo], include=include, queryset=queryset).get(codigo_key(codigo), [])


def is_uuid(value):
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def canonical_ids(ids):
    """{id: the id of its canonical line} for line ids (UUIDs or their text): a merged line maps to the line it was merged into,
    any other to itself. Ids that name no line are left out."""
    from .models import Part
    ids = {str(pk) for pk in ids if pk and is_uuid(pk)}
    parent = {str(pk): (str(target) if target else None) for pk, target in Part.objects.filter(pk__in=ids).values_list('pk', 'merged_into_id')}
    for _ in range(MAX_CHAIN):
        missing = {target for target in parent.values() if target and target not in parent}
        if not missing:
            break
        parent.update({str(pk): (str(target) if target else None)
                       for pk, target in Part.objects.filter(pk__in=missing).values_list('pk', 'merged_into_id')})
    out = {}
    for pk in ids & set(parent):
        current = pk
        for _ in range(MAX_CHAIN):
            if not parent.get(current):
                break
            current = parent[current]
        out[pk] = current
    return out


def canonical_id(line):
    """The id (text) of a line's canonical line; line is a Part or an id. None for an id that names no line."""
    pk = str(getattr(line, 'pk', line) or '')
    if not pk:
        return None
    if getattr(line, 'pk', None) is not None and not line.merged_into_id:
        return pk
    return canonical_ids([pk]).get(pk)


def canonical(line):
    """The line itself, or the line it was merged into (a Part, or None for an id that names no line)."""
    from .models import Part
    if getattr(line, 'pk', None) is not None and not line.merged_into_id:
        return line
    pk = canonical_id(line)
    return Part.objects.filter(pk=pk).first() if pk else None


def canonical_lines(lines):
    """{line id: its canonical Part} for Parts, in at most a few queries (merged lines' targets are read together)."""
    from .models import Part
    lines = list(lines)
    mapping = canonical_ids([line.pk for line in lines if line.merged_into_id])
    targets = Part.objects.in_bulk(set(mapping.values()))
    targets = {str(pk): part for pk, part in targets.items()}
    return {line.pk: (targets.get(mapping.get(str(line.pk))) if line.merged_into_id else line) for line in lines}


def same_line(a, b):
    """Whether two lines (Parts or ids) are the same once merges are followed. The same id is the same line without a query."""
    first, second = str(getattr(a, 'pk', a) or ''), str(getattr(b, 'pk', b) or '')
    if not first or not second:
        return False
    if first == second:
        return True
    first, second = canonical_id(a), canonical_id(b)
    return first is not None and first == second


def effective_estado(line):
    """A line's estado, read through its pair while the database may still be out of step (a merged line is unificada, an inactive
    publicada line retirada): what estado becomes once mall.refactor.s1_lifecycle or 0072 has run."""
    repaired = repaired_estado(line.active, line.merged_into_id, line.estado)
    return repaired[0] if repaired else line.estado


# --- writers --------------------------------------------------------------------------------------------------------------------------

def repaired_estado(active, merged, estado):
    """The (estado, active) a stored row belongs at once its pair is put in step, or None when that needs a person (an active line in
    referencia, preparacion or rechazada, or an estado the lifecycle does not know). Old writers only know `active` and
    `merged_into`, so those win: a merged line is unificada, a line they retire is retirada, one they reactivate publicada.
    The tighten migration 0072 repeats this rule on historical models, and mall.refactor.s1_lifecycle backfills with it."""
    if estado not in ESTADOS:
        return None
    if merged:
        return 'unificada', False
    if estado == 'unificada':
        return ('publicada' if active else 'retirada'), bool(active)
    if active:
        return ('publicada', True) if estado in CATALOG else None
    return ('retirada' if estado == 'publicada' else estado), False


def pair_lifecycle(line):
    """Put an unsaved change of estado, active or merged_into in step before it is written (Part.save and the bulk writers call it).
    merged_into forces unificada; a line no longer merged follows active; otherwise whichever of the two changed since the line was
    loaded (or since its defaults, for a new line) wins, estado when both did. A field deferred at load and assigned since counts as
    changed. A stored pair out of step with neither changed follows active, as repaired_estado does. active alone never moves a line
    in referencia, preparacion or rechazada (LineStateError): only set_estado does, as only a person can say which estado it takes."""
    if line.merged_into_id is not None:
        line.estado, line.active = 'unificada', False
        return line
    if hasattr(line, '_lifecycle'):
        (loaded_active, loaded_estado), tracked = line._lifecycle, True
    elif line._state.adding:
        (loaded_active, loaded_estado), tracked = (True, 'publicada'), True
    else:
        (loaded_active, loaded_estado), tracked = (None, None), False
    values = line.__dict__  # read before line.estado or line.active may load a deferred field

    def changed(name, loaded):
        if loaded is None:  # deferred at load: changed when it has been assigned since without being read
            return tracked and name in values
        return values.get(name, loaded) != loaded

    estado_changed, active_changed = changed('estado', loaded_estado), changed('active', loaded_active)
    if line.estado == 'unificada':
        line.estado = 'publicada' if line.active else 'retirada'
    elif not estado_changed and (active_changed or line.active != (line.estado == 'publicada')):
        if line.estado in CATALOG:
            line.estado = 'publicada' if line.active else 'retirada'
        elif line.active:
            raise LineStateError(f'Esta línea está {phrase(line.estado)}: cambia su estado (mall.lines.set_estado), no «activo».')
    line.active = line.estado == 'publicada'
    return line


def loaded_pair(line):
    """(active, estado) as the line holds them, without loading a deferred one (None then): what pair_lifecycle compares against."""
    return line.__dict__.get('active'), line.__dict__.get('estado')


def validate_codes(estado, origen):
    if estado not in ESTADOS:
        raise LineStateError(f'Estado de línea desconocido: {estado}.')
    if origen not in ORIGENES:
        raise LineStateError(f'Origen de línea desconocido: {origen}.')


def create_line(sku, *, estado='publicada', origen='catalogo', decided_by=None, decided_at=None, main_brand='', main_tipo=None,
                main_source='', actor=None, strict=False, promote=True, library_brand='', **fields):
    """A new line (Part.objects.create) in this estado and from this origen, with its main Reference (S2b): the codigo as code,
    main_brand ('' any brand), main_tipo (the line's when None: oem for is_OEM=True, otro otherwise) and main_source
    (mall.references.ensure_main). A key another line holds refuses the line with strict (ReferenceConflict, so its transaction
    rolls back), else the main waits in disputa for a merge case. Other Part fields pass through. A unificada line is only ever made
    by merging an existing one. Every writer that creates lines goes through here or bulk_create_lines.
    From S4 a catalog line (publicada or retirada) whose codigo a line in referencia has (a library number no product carried yet)
    promotes that line instead (promotable, promote): the same id, its References and origen kept, the texts given written over the
    library's. When several makes share the codigo, library_brand (or main_brand) names the one (LineStateError otherwise)."""
    from django.db import transaction
    from . import references
    from .models import Part
    validate_codes(estado, origen)
    if estado == 'unificada' or fields.get('merged_into') or fields.get('merged_into_id'):
        raise LineStateError('Una línea nueva no nace unificada: agrupa una existente.')
    if promote and estado in CATALOG:
        target = promotable(sku, library_brand or main_brand)
        if target is not None:
            given = {name: value for name, value in fields.items() if name in PROMOTED_FIELDS and value not in (None, '')}
            with transaction.atomic():
                target = Part.objects.select_for_update().get(pk=target.pk)
                return promote_line(target, estado=estado, user=decided_by or (actor if getattr(actor, 'pk', None) else None), **given)
    line = Part(sku=sku, estado=estado, origen=origen, decided_by=decided_by, decided_at=decided_at, **fields)
    line._main_followed = True  # the main is made below, with what the caller asked for
    with transaction.atomic():
        line.save(force_insert=True)
        references.ensure_main(line, brand=main_brand, tipo=main_tipo, source=main_source, actor=actor, strict=strict)
    return line


def bulk_create_lines(lines, *, batch_size=500, actor=None, mains=True):
    """Insert unsaved Parts in bulk (no signals, as Part.objects.bulk_create): each codigo trimmed and upper case like Part.save,
    estado and active in step, tipo and is_OEM in step, origen and estado checked, created_at stamped; then each live line's main
    Reference in bulk (mall.references.bulk_mains: brandless, its tipo from the line, disputa when another line holds the key), unless
    mains=False: a caller that inserts each line's main itself right after (S4's referencia lines, whose main is their library number).
    Returns the lines."""
    from . import references
    from .models import Part
    lines, now = list(lines), timezone.now()
    for line in lines:
        validate_codes(line.estado, line.origen)
        line.sku = codigo_key(line.sku)
        pair_lifecycle(line)
        references.pair_line_tipo(line)
        line.created_at = line.created_at or now
    Part.objects.bulk_create(lines, batch_size=batch_size)
    if mains:
        references.bulk_create(references.bulk_mains(lines, actor=actor), batch_size=batch_size, actor=actor)
    for line in lines:
        line._lifecycle = loaded_pair(line)
        line._identity = {name: line.__dict__.get(name) for name in Part.IDENTITY}
    return lines


def bulk_update_lines(lines, fields, *, batch_size=500):
    """Part.objects.bulk_update for loaded lines: when the fields include active, estado or merged_into, each line's pair is put in
    step first and both columns are written; likewise tipo and is_OEM. updated_at is stamped. A codigo or tipo written here moves the
    line's main after it (mall.references.ensure_main), as Part.save does. Returns the number of rows matched."""
    from . import references
    from .models import Part
    lines, fields, now = list(lines), list(dict.fromkeys(fields)), timezone.now()
    if PAIRED & set(fields):
        for line in lines:
            pair_lifecycle(line)
            validate_codes(line.estado, line.origen)
        fields = list(dict.fromkeys([*fields, 'active', 'estado']))
    if {'tipo', 'is_OEM'} & set(fields):
        for line in lines:
            references.pair_line_tipo(line, getattr(line, '_identity', None))
        fields = list(dict.fromkeys([*fields, 'tipo', 'is_OEM']))
    for line in lines:
        line.updated_at = now
    count = Part.objects.bulk_update(lines, [*fields, 'updated_at'], batch_size=batch_size) if lines else 0
    identity = {'sku', 'tipo', 'is_OEM', 'merged_into'} & set(fields)
    for line in lines:
        line._lifecycle = loaded_pair(line)
        if identity:
            references.ensure_main(line, line_wins='sku' not in fields and bool({'tipo', 'is_OEM'} & set(fields)))
        line._identity = {name: line.__dict__.get(name) for name in Part.IDENTITY}
    return count


PROMOTED_FIELDS = ('name', 'description', 'category', 'subcategory', 'part_type', 'part_type_id', 'aplicaciones_texto')


def referencia_lines(codigo):
    """The lines in referencia with this codigo (library numbers no product carries yet: one per make, so several may share it)."""
    key = codigo_key(codigo)
    if not key:
        return []
    return list(_lines().filter(estado='referencia', merged_into__isnull=True).annotate(codigo_lower=Lower('sku'))
                .filter(codigo_lower=key.lower()).order_by('pk'))


def promotable(codigo, brand=''):
    """The line in referencia that a new catalog line with this codigo becomes (S4: promote instead of refusing), or None when no line
    in referencia has it. When several makes share the codigo (44310-STX-305 for ACURA and HONDA), the one whose main is under `brand`
    (that brand first, then its familia); LineStateError naming the makes when no brand picks one."""
    found = referencia_lines(codigo)
    if len(found) <= 1:
        return found[0] if found else None
    from . import brands, references
    mains = references.mains_of(found)
    makers = {line.pk: (mains[line.pk].brand if line.pk in mains else '') for line in found}
    if brand:
        wanted = brands.normalize(brand)
        exact = [line for line in found if makers[line.pk] == wanted]
        familia = exact or [line for line in found if makers[line.pk] and brands.same_familia(makers[line.pk], wanted)]
        if len(familia) == 1:
            return familia[0]
    names = ' y '.join(sorted({maker for maker in makers.values() if maker})) or 'varias marcas'
    raise LineStateError(f'El código {codigo_key(codigo)} está en la Biblioteca para {names}: indica la marca.')


def promote_line(line, *, estado='publicada', user=None, **fields):
    """A line in referencia (already locked by the caller) becomes a catalog line: the same id and References, origen kept, estado
    publicada (or retirada), the fields given written over the texts it had from the library, changed_at stamped. Returns the line."""
    if line.estado != 'referencia':
        raise LineStateError(f'Esta línea está {phrase(line.estado)}: solo una línea en referencia se publica así.')
    if estado not in CATALOG:
        raise LineStateError('Una línea de la Biblioteca se publica o se retira.')
    for name, value in fields.items():
        setattr(line, name, value)
    names = [name.removesuffix('_id') for name in fields]
    names += stage_estado(line, estado, user=user)
    line.save(update_fields=list(dict.fromkeys(names)))
    return line


def stage_estado(line, estado, *, user=None, reason='', note='', at=None):
    """Move a loaded line to another estado without saving it, and return the fields to write: active in step (on save), version + 1,
    changed_at stamped, and the decision recorded when someone made it (user) or when it is a rejection, which needs one of REASONS.
    Merging (unificada) and Separar SKU (leaving it) go through their own tools. set_estado saves it; a writer that saves the line
    itself (the part API) stages the move and saves the fields with its own."""
    validate_codes(estado, line.origen)
    if estado == 'unificada':
        raise LineStateError('Para unificar una línea, agrúpala con otra (Agrupar SKU).')
    if line.estado == 'unificada' or line.merged_into_id:
        raise LineStateError('Esta línea está unificada en otra: usa Separar SKU para devolverla.')
    if estado == 'rechazada' and reason not in REASONS:
        raise LineStateError('Elige un motivo para rechazar la línea.')
    at = at or timezone.now()
    fields = ['estado', 'version', 'changed_at']
    if estado != line.estado and (estado == 'rechazada' or line.estado == 'rechazada'):
        line.decision_reason, line.decision_note = (reason, (note or '').strip()) if estado == 'rechazada' else ('', '')
        fields += ['decision_reason', 'decision_note']
    elif note:
        line.decision_note = note.strip()
        fields.append('decision_note')
    if user is not None or estado == 'rechazada':
        line.decided_by, line.decided_at = user, at
        fields += ['decided_by', 'decided_at']
    line.estado, line.version, line.changed_at = estado, (line.version or 0) + 1, at
    return fields


def set_estado(line, estado, *, user=None, reason='', note='', at=None):
    """Move a line (already locked by the caller when it matters) to another estado and save it (stage_estado). Returns the line."""
    line.save(update_fields=stage_estado(line, estado, user=user, reason=reason, note=note, at=at))
    return line


SUBGRUPO_FIELDS = ['part_type', 'category', 'subcategory']


def set_subgrupo(lines, part_type, *, grupo='', subgrupo=''):
    """Give loaded lines (a Part or several) a subgrupo without saving them, and return the fields to write. Until S10 the text columns
    category and subcategory are the subgrupo's: its grupo's nombre and its name (mall.technical_data). With no subgrupo (None) they hold
    the partial classification given, normalized: a grupo typed without a subgrupo yet, blank by default. A writer that resolves text
    goes through mall.technical_data.bind_part_types, which calls this."""
    from .models import Part
    from .technical_data import grupo_nombre, normalized_label
    lines = [lines] if isinstance(lines, Part) else list(lines)
    text = (grupo_nombre(part_type), part_type.name) if part_type is not None else (normalized_label(grupo), normalized_label(subgrupo))
    for line in lines:
        line.part_type = part_type
        line.category, line.subcategory = text
    return list(SUBGRUPO_FIELDS)


def project_subgrupos(part_types):
    """After a subgrupo moved to another grupo or was renamed, or its grupo was renamed (mall.technical_data.project_grupo): its lines'
    text follows it (one UPDATE per subgrupo). Returns how many lines changed."""
    from .models import Part
    from .technical_data import grupo_nombre
    count = 0
    for part_type in part_types:
        count += (Part.objects.filter(part_type=part_type).exclude(category=grupo_nombre(part_type), subcategory=part_type.name)
                  .update(category=grupo_nombre(part_type), subcategory=part_type.name))
    return count


def touch_visible(lines, at=None):
    """Stamp changed_at on lines whose change a person sees (description, codes, images, estado), never on an AI fill or a background
    recount: the Productos nuevos notice reads it. lines: a Part, an id, or several. Returns the stamp."""
    from .models import Part
    at = at or timezone.now()
    lines = [lines] if isinstance(lines, (Part, str)) or not hasattr(lines, '__iter__') else list(lines)
    Part.objects.filter(pk__in=[getattr(line, 'pk', line) for line in lines]).update(changed_at=at)
    for line in lines:
        if isinstance(line, Part):
            line.changed_at = at
    return at
