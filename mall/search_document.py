"""The Search Document (inventory refactor S7): the only writer of mall_searchdocument, one row per line on sale (publicada and not merged:
invariant I8, mall.lines.storefront_lines), holding what the storefront search reads (mall.catalog_search on the document).

    refresh(line_ids)   the documents of these lines follow what they show now: written when the transaction commits (one write per
                        transaction, however many writers asked), at once outside one; a line no longer on sale loses its document
    rebuild()           every document from scratch, writing only what changed and deleting the documents of lines not on sale (the
                        matching worker's stage every REBUILD_HOURS, stage(); python -m mall.refactor.s7_search)
    check()             I8 both ways: lines on sale without a document, documents of lines not on sale (Appendix C, C7)
    documents(ids)      what each document holds, computed, without writing (the CLI's dry run, the tests)

What a document holds (plan §3 S7):
    codes     the line's main (whatever its tipo or estado: it is the SKU the storefront shows, a disputa main included, B15) and its public
              References in an exclusive estado (verificado, declarado, inferido), with their other printed spellings; never a retired one,
              a non-main code in disputa, or a supplier's interno code
    compact   the same codes without separators: a code search compares them whole (' KEY ') or contained ('KEY')
    reach     the aftermarket codes filed under the line's own numbers (OEMCrossReference), compact, which the legacy search
              (mall.oem_links.search_q) reaches too; from S9 a SKU's catalog and name links (PartOEMLink) are no longer read (the merge
              pass put a catalog item's numbers on its SKU's line, and the codes on one line became its References)
    internal  {supplier id: [codes]}: the interno codes of each supplier (not the main), read only within that supplier's own catalog
    text      the description, name, grupo, subgrupo, the main's brand and every public Reference's brand, then the canonical brand of each
              alias word in them (TOY → TOY TOYOTA), so "filtro aceite toyota" finds the rows the catalog wrote as TOY
    brand     the main's brand; grupo, subgrupo: its subgrupo's names
Everything is folded as a search word is (mall.catalog_search.fold: upper case, no accents). Stock and the supplier items' calidades are
never in it: availability and the calidad filter read the items live.

Callers. Part.save and Reference.save (every line or Reference written one by one: mall.lines.set_estado, publish, reject, prepare,
promote_line, set_subgrupo through the line's save; mall.references.add, update, retire, revive), mall.references._bump (set_main, move,
retype_main, absorb_into), references.bulk_create, mall.lines.bulk_create_lines, bulk_update_lines and project_subgrupos (the catalog
import's save_plan, the assistant's bulk taxonomy, a subgrupo or grupo renamed: technical_api, Grupo.save), mall.line_merge.merge_lines
and split_line, mall.brands.rekey, rename and merge; refresh_reach() for what reach reads (the aftermarket codes and SKU links the
catalog import, the catalog replace, the Biblioteca and absorb write: mall.oem_links, mall.supplier_catalog_replace,
mall.oem_reference_views, mall.line_merge); a BrandAlias saved or deleted anywhere (the brand routes, references.ensure_brand, the Django
admin) through its signal (alias_changed, mall.apps). A write that skips all of them is repaired by the next rebuild.

Writes are serialized (LOCK, a transaction-level advisory lock taken before a document is computed): a document is always computed from
what was committed when its writer got the lock, so the rebuild never writes back an older document over a fresher one."""
import logging
import re
import time
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

REBUILD_HOURS = 6         # the matching worker rebuilds every document this often (SEARCH_DOCUMENT_REBUILD_HOURS; 0 turns it off)
RETRY_MINUTES = 15        # after a failed rebuild, the stage tries again this much later instead of waiting REBUILD_HOURS
BATCH = 1000              # documents recomputed and written per transaction by a rebuild (the worker checks for a stop between two)
CHUNK = 2000              # line ids per query
KEEP_RUNS = 100           # rebuild records kept
EXCLUSIVE = ('verificado', 'declarado', 'inferido')
FIELDS = ('codes', 'compact', 'reach', 'internal', 'text', 'brand', 'grupo', 'subgrupo')
LOCK = 724631115          # pg_advisory_xact_lock taken by every document write (724631109-724631114 are taken: plan §1.6.5)
PENDING = '_search_document_pending'  # the connection's set of line ids waiting for the commit
TOKEN = re.compile(r'[A-Z0-9]+')


def _models():
    from .search_document_models import SearchDocument, SearchDocumentRebuild
    return SearchDocument, SearchDocumentRebuild


def fold(text):
    from .catalog_search import fold as fold_search
    return ' '.join(fold_search(str(text or '')).split())


def compact(code):
    return re.sub(r'[^A-Z0-9]', '', fold(code))


def padded(values):
    """' A B C ': each value once, in order, a space before and after each one ('' when there are none)."""
    values = list(dict.fromkeys(value for value in values if value))
    return f' {" ".join(values)} ' if values else ''


def _ids(line_ids):
    if line_ids is None:
        return set()
    if isinstance(line_ids, (str, bytes)) or not hasattr(line_ids, '__iter__'):
        line_ids = [line_ids]
    return {str(getattr(value, 'pk', value)) for value in line_ids if value is not None and getattr(value, 'pk', value) is not None}


def chunks(values, size=CHUNK):
    values = sorted(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


# --- the content ---------------------------------------------------------------------------------------------------------------------

class Aliases:
    """The brand aliases as a document's text expands them: a word (or a run of words) that is an alias adds its brand's nombre."""

    def __init__(self):
        from .brand_models import BrandAlias
        self.single, self.phrases = {}, []
        for alias, nombre in BrandAlias.objects.values_list('alias', 'brand__nombre'):
            words = tuple(TOKEN.findall(fold(alias)))
            if len(words) == 1:
                self.single[words[0]] = fold(nombre)
            elif words:
                self.phrases.append((f' {" ".join(words)} ', fold(nombre)))

    def expand(self, text):
        """The canonical nombres the alias words of a folded text name, not already in it."""
        words = TOKEN.findall(text)
        joined = f' {" ".join(words)} '
        found = [self.single[word] for word in words if word in self.single]
        found += [nombre for phrase, nombre in self.phrases if phrase in joined]
        return [nombre for nombre in dict.fromkeys(found) if f' {" ".join(TOKEN.findall(nombre))} ' not in joined]


def text_of(texts, aliases):
    base = ' '.join(dict.fromkeys(value for value in (fold(text) for text in texts) if value))
    return ' '.join([base, *aliases.expand(base)]).strip()


def _line_rows(line_filter):
    from .lines import storefront_lines
    return storefront_lines().filter(line_filter).values_list(
        'pk', 'sku', 'name', 'description', 'category', 'subcategory', 'part_type_id', 'part_type__name', 'part_type__grupo__nombre')


def _gather(line_filter):
    """{line id: values} for the lines on sale the filter (a Q on Part) names; each kind of row read once."""
    from .models import Reference
    from .oem_reference_models import OEMCrossReference
    lines = {str(row[0]): row for row in _line_rows(line_filter)}
    if not lines:
        return {}
    ids = list(lines)
    refs = defaultdict(list)
    held = defaultdict(set)  # line id -> ids of its live References (the numbers whose aftermarket codes it reaches)
    for chunk in chunks(ids):
        for row in (Reference.all_objects.filter(part_id__in=chunk).exclude(estado='retirada').order_by('-is_main', 'pk')
                    .values_list('part_id', 'pk', 'code', 'brand', 'tipo', 'estado', 'is_main', 'supplier_id', 'printed_forms')):
            refs[str(row[0])].append(row)
            held[str(row[0])].add(row[1])
    numbers = {ref_id for line in held.values() for ref_id in line}
    crossed = defaultdict(list)  # Reference id -> compact aftermarket codes filed under it
    for chunk in chunks(numbers, 5000):
        for ref_id, number in OEMCrossReference.objects.filter(reference_id__in=chunk).values_list('reference_id', 'number').order_by('pk'):
            crossed[ref_id].append(number)
    aliases = Aliases()
    found = {}
    for pk, (_, sku, name, description, category, subcategory, part_type, subgrupo_name, grupo_nombre) in lines.items():
        codes, internal, brands, main_brand = [fold(sku)], defaultdict(list), [], ''
        for _, ref_id, code, brand, tipo, estado, is_main, supplier_id, printed in refs.get(pk, []):
            forms = [fold(form) for form in [code, *(printed or [])] if form]
            if is_main:
                main_brand = main_brand or fold(brand)
            if is_main or (tipo != 'interno' and estado in EXCLUSIVE):
                codes += forms
                brands.append(brand)
            elif tipo == 'interno' and estado in EXCLUSIVE and supplier_id:
                internal[str(supplier_id)] += [*forms, *(compact(form) for form in forms)]
        reach = []
        for ref_id in sorted(held.get(pk, ())):
            reach += crossed.get(ref_id, [])
        compacts = [compact(code) for code in codes]
        held_compact = set(compacts)
        grupo, subgrupo = (grupo_nombre or '', subgrupo_name) if part_type else (category, subcategory)  # the FK (S5), else the text
        found[pk] = {
            'codes': padded(codes), 'compact': padded(compacts),
            'reach': padded(number for number in reach if number not in held_compact),
            'internal': {supplier: list(dict.fromkeys(values)) for supplier, values in sorted(internal.items())},
            'text': text_of([description, name, grupo, subgrupo, main_brand, *brands], aliases),
            'brand': main_brand[:120], 'grupo': fold(grupo)[:120], 'subgrupo': fold(subgrupo)[:120]}
    return found


def documents(line_ids=None):
    """{line id: what its document holds} for the lines on sale among line_ids (every line on sale with None); nothing is written."""
    if line_ids is None:
        return _gather(Q())
    found = {}
    for chunk in chunks(_ids(line_ids)):
        found.update(_gather(Q(pk__in=chunk)))
    return found


# --- writing -------------------------------------------------------------------------------------------------------------------------

def _lock():
    """This transaction's document lock (LOCK): one document writer at a time, each computing what it writes after it holds the lock."""
    if connection.vendor == 'postgresql':
        with transaction.get_connection().cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [LOCK])


def _save(docs):
    """Upsert these documents ({line id: values}) in one statement, in line order."""
    SearchDocument, _ = _models()
    if not docs:
        return 0
    rows = [SearchDocument(line_id=pk, **docs[pk]) for pk in sorted(docs)]
    SearchDocument.objects.bulk_create(rows, batch_size=500, update_conflicts=True, unique_fields=['line'],
                                       update_fields=[*FIELDS, 'updated_at'])
    return len(rows)


def write(line_ids):
    """The documents of these lines, now: upserted for the lines on sale, deleted for the others, each chunk computed and written in one
    transaction under the document lock (so what it writes is what was committed when it got the lock). Returns (written, deleted)."""
    SearchDocument, _ = _models()
    ids = _ids(line_ids)
    written = deleted = 0
    for chunk in chunks(ids):
        with transaction.atomic():
            _lock()
            docs = _gather(Q(pk__in=chunk))
            written += _save(docs)
            gone = sorted(set(chunk) - set(docs))
            if gone:
                deleted += SearchDocument.objects.filter(line_id__in=gone).delete()[0]
    return written, deleted


def _connection():
    return transaction.get_connection()


def _flush():
    pending = getattr(_connection(), PENDING, None)
    if not pending:
        return
    ids = set(pending)
    pending.clear()
    try:
        write(ids)
    except Exception:  # never the writer's failure: the documents stay as they were and the next rebuild writes them
        logger.exception('Search documents not refreshed for %s lines.', len(ids))


def refresh(line_ids):
    """Bring the documents of these lines (Parts or ids) up to date with what they show. Inside a transaction the ids wait for the commit
    (one write for the whole transaction, whatever writers asked, and nothing if it rolls back); outside one they are written at once.
    A failure is logged, never raised: the writer's own change stands and the next rebuild repairs the document. SEARCH_DOCUMENT_SYNC
    (the test settings) writes them at once inside a transaction too: a test's transaction never commits, and the storefront search reads
    nothing else (S10). Returns how many ids."""
    ids = _ids(line_ids)
    if not ids:
        return 0
    current = _connection()
    pending = getattr(current, PENDING, None)
    if pending is None:
        pending = set()
        setattr(current, PENDING, pending)
    pending.update(ids)
    if current.in_atomic_block and not getattr(settings, 'SEARCH_DOCUMENT_SYNC', False):
        # One callback per transaction; a callback a rollback dropped is registered again by the next writer.
        if not any(entry[1] is _flush for entry in current.run_on_commit):
            transaction.on_commit(_flush, robust=True)
    else:
        _flush()
    return len(ids)


def word_pattern(spelling):
    """A PostgreSQL regular expression matching the spelling as whole words of a folded text (MO in 'AMORTIGUADOR MO', not in
    'MOTOR'; NEW ERA across any separator), as Aliases.expand reads the words; None when it has no letter or digit."""
    words = TOKEN.findall(fold(spelling))
    return f'(^|[^A-Z0-9]){"[^A-Z0-9]+".join(words)}([^A-Z0-9]|$)' if words else None


def refresh_text_containing(words):
    """refresh() for the documents whose text holds any of these spellings as whole words (a brand alias or nombre that changed, so its
    expansion did)."""
    SearchDocument, _ = _models()
    query = Q()
    for pattern in {word_pattern(word) for word in words} - {None}:
        query |= Q(text__regex=pattern)
    if query:
        refresh(SearchDocument.objects.filter(query).values_list('line_id', flat=True))


def alias_changed(sender, instance, raw=False, using=None, **kwargs):
    """post_save and post_delete of BrandAlias (mall.apps), wherever the alias is written (the brand routes, references.ensure_brand,
    the Django admin): the documents whose text holds the spelling expand it to its brand now, or no longer."""
    if raw or (using or 'default') != 'default':
        return
    try:
        with transaction.atomic():  # a savepoint: the alias's own write stands whatever happens here
            refresh_text_containing([instance.alias])
    except DatabaseError:  # no document table (before 0085): the S7 rebuild writes every document anyway
        logger.warning('Search documents not refreshed for the alias %s.', instance.alias)


def refresh_reach(ref_ids=(), part_ids=()):
    """refresh() for the lines whose reach follows these References (ids): the lines they sit on (an aftermarket code filed or retired
    under a number, a number's rows moved by absorb), plus part_ids. SKU links are no longer read from S9."""
    from .models import Reference
    lines = {str(getattr(pk, 'pk', pk)) for pk in part_ids if pk}
    ids = {getattr(pk, 'pk', pk) for pk in ref_ids if pk}
    for chunk in chunks(ids):
        lines |= {str(pk) for pk in Reference.all_objects.filter(pk__in=chunk).values_list('part_id', flat=True)}
    return refresh(lines)


# --- the rebuild and its check -------------------------------------------------------------------------------------------------------

def check():
    """I8 both ways (Appendix C, C7): {'missing': lines on sale without a document, 'stale': documents of lines not on sale}, counts."""
    from .lines import storefront_lines
    SearchDocument, _ = _models()
    on_sale = storefront_lines()
    return {'missing': on_sale.filter(search__isnull=True).count(),
            'stale': SearchDocument.objects.exclude(line_id__in=on_sale.values('pk')).count()}


def _differs(doc, values):
    return any(getattr(doc, field) != values[field] for field in FIELDS)


def plan(docs=None):
    """What a rebuild would write: (docs to upsert {id: values}, ids whose document goes, how many are already right)."""
    SearchDocument, _ = _models()
    docs = documents() if docs is None else docs
    upsert, same = {}, 0
    current = SearchDocument.objects.only('line_id', *FIELDS)
    seen = set()
    for doc in current.iterator(chunk_size=5000):
        pk = str(doc.line_id)
        seen.add(pk)
        values = docs.get(pk)
        if values is None:
            continue
        if _differs(doc, values):
            upsert[pk] = values
        else:
            same += 1
    for pk, values in docs.items():
        if pk not in seen:
            upsert[pk] = values
    stale = sorted(seen - set(docs))
    return upsert, stale, same


def rebuild(*, origin='worker', checkpoint=None, log=None):
    """Every document from scratch: computed for every line on sale and compared with the table; the ones that differ (or are missing)
    are computed again and written in batches of BATCH, each batch in one transaction under the document lock (write()), so a document a
    writer refreshed after the first pass is never overwritten with what that pass read; the documents of lines not on sale deleted, then
    I8 checked. checkpoint() runs between two batches (the worker's stop signal). Recorded in SearchDocumentRebuild. Returns its
    summary."""
    _, SearchDocumentRebuild = _models()
    run = SearchDocumentRebuild.objects.create(origin=origin)
    started = time.monotonic()
    try:
        docs = documents()
        computed = time.monotonic() - started
        upsert, stale, same = plan(docs)
        written = deleted = 0
        pending = sorted(upsert)
        for start in range(0, len(pending), BATCH):
            if checkpoint is not None:
                checkpoint()
            batch = write(pending[start:start + BATCH])  # computed again under the lock: a line taken off sale meanwhile loses its document
            written, deleted = written + batch[0], deleted + batch[1]
        deleted += write(stale)[1] if stale else 0  # each one checked again: a line put back on sale meanwhile keeps (gets) its document
        summary = {'lines': len(docs), 'written': written, 'unchanged': same, 'deleted': deleted, 'seconds_computing': round(computed, 1),
                   'seconds': round(time.monotonic() - started, 1), 'check': check()}
    except BaseException as error:
        SearchDocumentRebuild.objects.filter(pk=run.pk).update(finished_at=timezone.now(), error=f'{type(error).__name__}: {error}'[:2000],
                                                                summary={'seconds': round(time.monotonic() - started, 1)})
        raise
    SearchDocumentRebuild.objects.filter(pk=run.pk).update(finished_at=timezone.now(), summary=summary)
    old = list(SearchDocumentRebuild.objects.order_by('-started_at', '-id').values_list('pk', flat=True)[KEEP_RUNS:])
    if old:
        SearchDocumentRebuild.objects.filter(pk__in=old).delete()
    if log is not None:
        log(summary)
    return summary


def rebuild_hours():
    return float(getattr(settings, 'SEARCH_DOCUMENT_REBUILD_HOURS', REBUILD_HOURS) or 0)


def rebuild_due(now=None):
    """Whether the worker's stage rebuilds now: no rebuild started in the last rebuild_hours(), or the last one failed RETRY_MINUTES ago.
    Never with rebuild_hours() 0."""
    _, SearchDocumentRebuild = _models()
    hours = rebuild_hours()
    if hours <= 0:
        return False
    now = now or timezone.now()
    last = SearchDocumentRebuild.objects.order_by('-started_at', '-id').first()
    if last is None:
        return True
    if last.error or (last.finished_at is None and last.started_at <= now - timedelta(hours=1)):  # failed, or killed mid-way
        return last.started_at <= now - timedelta(minutes=RETRY_MINUTES)
    return last.started_at <= now - timedelta(hours=hours)


def stage(*, checkpoint=None):
    """The matching worker's document stage (mall.matching_worker.process_queue, under the matching lock): rebuild() when due, else
    None. A failure of any kind is recorded and logged, never raised, so the matching pass goes on; only the runner's stop
    (WorkerStopping, a BaseException) passes through."""
    try:
        if not rebuild_due():
            return None
    except DatabaseError:  # 0085 not applied yet: nothing to rebuild
        logger.warning('Search documents: no table yet (0085_search_document).')
        return None
    except Exception:
        logger.exception('Search documents: could not tell whether a rebuild is due.')
        return None
    try:
        return rebuild(origin='worker', checkpoint=checkpoint)
    except Exception:
        logger.exception('Search document rebuild failed; the next attempt comes in %s minutes.', RETRY_MINUTES)
        return {'error': 'rebuild failed'}


def in_step(line):
    """Whether a loaded line's document is what it should be now (tests, the CLI's spot checks)."""
    SearchDocument, _ = _models()
    wanted = documents([line]).get(str(getattr(line, 'pk', line)))
    doc = SearchDocument.objects.filter(line_id=getattr(line, 'pk', line)).first()
    return (doc is None) if wanted is None else (doc is not None and not _differs(doc, wanted))


def connection_pending():
    """The line ids waiting for this connection's commit (tests)."""
    return set(getattr(connection, PENDING, None) or ())
