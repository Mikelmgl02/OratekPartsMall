"""Storefront catalog search. Every word of the search has to match somewhere on the SKU, in any order; accents and case are ignored;
a word with a digit is a code, compared without separators (51750J9000 finds 51750-J9000); and a code search ranks the SKUs whose
own code, an alterno, a supplier code or an OEM number reached equals it first, then the ones containing it, then the rest.

Two ways to run it (inventory refactor S7), chosen by settings.CATALOG_SEARCH_SOURCE in backend(), the one place that decides:
    legacy    the catalog tables, as before S7: the line's columns, its References, the matched supplier codes and the OEM library
              (legacy_search_parts, legacy_search_rank); the default, kept behind backend() until S10
    document  the Search Document (mall.search_document): one row per line on sale, every word one LIKE on a trigram-indexed column
              (document_search_parts, document_search_rank). The same rules (3c41049): words in any order, accents ignored, codes
              without separators, DEL is no filler; and every word also through mall.brands.resolve, so "filtro aceite toyota" finds the
              rows the catalog wrote as TOY. A supplier's interno codes count only in its own catalog (mall.catalog_scope's own), and
              disputa codes only when they are the line's main (B15). Its results are the legacy ones or more.
search_parts() and search_rank() keep their signatures and the 0/1/2 rank contract on both."""
import operator
import unicodedata
from functools import reduce

from django.conf import settings
from django.db import connection
from django.db.models import BooleanField, Case, CharField, F, Func, IntegerField, Q, Value, When
from django.db.models.functions import Concat, Length, Replace, Upper
from django.db.models.lookups import Contains, Exact, GreaterThan

from .models import PartCode, SupplierItem
from .oem_links import MIN_SEARCH, search_q as library_q
from .oem_reference import compact_sql
from .oem_reference_models import compact

# Words that join a search but name nothing (filtro de aceite para Toyota). Not DEL: the catalog writes it for delantero.
FILLER = {'DE', 'LA', 'LAS', 'EL', 'LOS', 'PARA', 'CON', 'Y', 'EN', 'POR', 'UN', 'UNA'}
MAX_WORDS = 8
ACCENTED = 'ÁÀÂÄÉÈÊËÍÌÎÏÓÒÔÖÚÙÛÜÑáàâäéèêëíìîïóòôöúùûüñ'
PLAIN = 'AAAAEEEEIIIIOOOOUUUUNaaaaeeeeiiiioooouuuun'
UNACCENT = str.maketrans(ACCENTED, PLAIN)


def fold(text):
    """Bujía -> BUJIA, piñón -> PINON: the form folded_sql gives a column."""
    return unicodedata.normalize('NFC', text).translate(UNACCENT).upper()


def folded_sql(expression):
    """The column without accents, upper case: TRANSLATE on PostgreSQL (no extension needed), one REPLACE per letter elsewhere."""
    if connection.vendor == 'postgresql':
        return Upper(Func(expression, Value(ACCENTED), Value(PLAIN), function='TRANSLATE', output_field=CharField()))
    return Upper(reduce(lambda sql, pair: Replace(sql, Value(pair[0]), Value(pair[1])), zip(ACCENTED, PLAIN), expression))


def contains(expression, word):
    """The column contains the word, ignoring case and accents. TRANSLATE costs ten times UPPER on PostgreSQL, so only the rows with
    an accent (more bytes than characters: about 220 descriptions) pay for it."""
    if connection.vendor != 'postgresql':
        return Q(Contains(folded_sql(expression), word))
    accented = GreaterThan(Func(expression, function='OCTET_LENGTH', output_field=IntegerField()), Length(expression))
    return Q(Contains(Upper(expression), word)) | (Q(accented) & Q(Contains(folded_sql(expression), word)))


class Like(Func):
    """expression LIKE pattern, the pattern's wildcards kept (Contains escapes them)."""
    arg_joiner = ' LIKE '
    template = '(%(expressions)s)'
    output_field = BooleanField()


def compact_has(field, key, exact=False):
    """The column without separators contains (or equals) the key. The regex behind compact_sql is the costly part of a code search,
    so a LIKE on the key's characters in order (5%1%7%5%0%…) first rules out the rows that cannot match: four times faster."""
    in_order = Like(Upper(field), Value('%' + '%'.join(key) + '%'))
    return Q(in_order) & Q((Exact if exact else Contains)(compact_sql(field), key))


def joined(*fields):
    """The fields in one text, a space apart, so one LIKE covers them (a word has no spaces, so it never spans two)."""
    parts = [part for field in fields for part in (field, Value(' '))][:-1]
    return Concat(*parts, output_field=CharField())


def search_words(search):
    """The words that must each match: folded, without bare punctuation, the filler left out unless nothing else is left."""
    words = [word for word in fold(search).split() if compact(word)][:MAX_WORDS]
    return [word for word in words if word not in FILLER] or words


def matched_items(own=None):
    """The supplier codes a search reads: every active supplier's, or only the own ones in a supplier's view of the catalog."""
    items = SupplierItem.objects.filter(matching_status='matched', supplier__active=True)
    return items if own is None else items.filter(supplier_id__in=own)


def word_q(word, own=None):
    """The SKU, name, description, department or subgroup contain the word, or an alterno (code or brand), a matched supplier code or,
    for a code, an OEM number the SKU reaches or an aftermarket code filed under one. Uncorrelated subqueries, no joins."""
    key = compact(word)
    text = contains(joined('sku', 'name', 'description', 'category', 'subcategory'), word)
    codes = contains(joined('code', 'brand'), word)
    items = contains(F('codigo'), word)
    if any(char.isdigit() for char in key):
        text |= compact_has('sku', key)
        codes |= compact_has('code', key)
        items |= compact_has('codigo', key)
    match = (text | Q(pk__in=PartCode.objects.filter(codes).values('part_id'))
             | Q(pk__in=matched_items(own).filter(items).values('part_id')))
    library = library_q(key) if any(char.isdigit() for char in key) else None
    return match | library if library is not None else match


def code_key(search):
    """The whole search without separators when it reads as a code (a digit, MIN_SEARCH characters or more); '' otherwise."""
    key = compact(fold(search))
    return key if len(key) >= MIN_SEARCH and any(char.isdigit() for char in key) else ''


def code_q(key, exact=False, own=None):
    return (compact_has('sku', key, exact)
            | Q(pk__in=PartCode.objects.filter(compact_has('code', key, exact)).values('part_id'))
            | Q(pk__in=matched_items(own).filter(compact_has('codigo', key, exact)).values('part_id'))
            | library_q(key, exact=exact))


def legacy_search_parts(query, search, own=None):
    """The parts every word matches; a code split by spaces (MR 968365) also matches whole. With own suppliers (mall.catalog_scope),
    only their codes count, so another supplier's code finds nothing."""
    words = search_words(search)
    if not words:
        return query
    match = reduce(operator.and_, [word_q(word, own) for word in words])
    key = code_key(search)
    return query.filter(match | code_q(key, own=own) if key and len(words) > 1 else match)


def legacy_search_rank(search, own=None):
    """0 for a SKU, alterno, supplier code or OEM number equal to the code searched, 1 for one containing it, 2 for the rest; None
    when the search is no code."""
    key = code_key(search)
    if not key:
        return None
    return Case(When(code_q(key, exact=True, own=own), then=Value(0)), When(code_q(key, own=own), then=Value(1)), default=Value(2),
                output_field=IntegerField())


# --- the Search Document (S7) --------------------------------------------------------------------------------------------------------

SOURCES = ('legacy', 'document')


def search_source():
    """legacy or document: what settings.CATALOG_SEARCH_SOURCE asks for (legacy for anything else)."""
    value = str(getattr(settings, 'CATALOG_SEARCH_SOURCE', 'legacy') or '').strip().lower()
    return value if value in SOURCES else 'legacy'


def resolved_words(words):
    """{word: the canonical brand nombre it names} for the words that are another spelling of a brand (TOY → TOYOTA): mall.brands.resolve
    on the search's words, the aliases and nombres read in one query."""
    from .brand_models import BrandAlias
    wanted = {word for word in words if word}
    if not wanted:
        return {}
    found = {}
    for alias, nombre in BrandAlias.objects.values_list('alias', 'brand__nombre'):  # a few dozen rows, folded as the documents fold them
        alias, nombre = ' '.join(fold(alias).split()), ' '.join(fold(nombre).split())
        if alias in wanted and nombre != alias:
            found[alias] = nombre
    return found


def internal_q(value, own, exact=False):
    """The documents holding one of the own suppliers' interno codes that contain (or are) value: their codes in internal[supplier]."""
    from django.db.models.fields.json import KT
    if not own:
        return Q(pk__in=[])
    needle = f'"{value}"' if exact else value
    return reduce(operator.or_, [Q(Contains(KT(f'internal__{supplier}'), needle)) for supplier in own])


def document_word_q(word, own=None, resolved=None):
    """A document matches a word: its text or a printed code contains it (or the brand it names), a code without separators contains a
    word with a digit, a number the line reaches contains one of MIN_SEARCH characters or more, an own interno code contains it."""
    key = compact(word)
    match = Q(text__contains=word) | Q(codes__contains=word)
    brand = (resolved or {}).get(word)
    if brand:
        match |= Q(text__contains=brand)
    if any(char.isdigit() for char in key):
        match |= Q(compact__contains=key)
        if len(key) >= MIN_SEARCH:
            match |= Q(reach__contains=key)
        if own:
            match |= internal_q(key, own)
    if own:
        match |= internal_q(word, own)
    return match


def document_code_q(key, exact=False, own=None):
    """A document holds a code equal to key (exact) or containing it: one of its codes without separators, a number it reaches, an own
    interno code."""
    needle = f' {key} ' if exact else key
    match = Q(compact__contains=needle) | Q(reach__contains=needle)
    return match | internal_q(key, own, exact=exact) if own else match


def documents_matching(match):
    from .search_document_models import SearchDocument
    return SearchDocument.objects.filter(match).values('line_id')


def document_search_parts(query, search, own=None):
    """search_parts on the Search Document: the lines of `query` whose document every word matches; a code split by spaces also matches
    whole. Only lines on sale have a document."""
    words = search_words(search)
    if not words:
        return query
    resolved = resolved_words(words)
    match = reduce(operator.and_, [document_word_q(word, own, resolved) for word in words])
    key = code_key(search)
    if key and len(words) > 1:
        match |= document_code_q(key, own=own)
    return query.filter(pk__in=documents_matching(match))


def document_search_rank(search, own=None):
    """search_rank on the Search Document: 0 for a line holding the code searched, 1 for one holding a code that contains it, 2 for the
    rest; None when the search is no code."""
    key = code_key(search)
    if not key:
        return None
    return Case(When(pk__in=documents_matching(document_code_q(key, exact=True, own=own)), then=Value(0)),
                When(pk__in=documents_matching(document_code_q(key, own=own)), then=Value(1)), default=Value(2),
                output_field=IntegerField())


def backend():
    """(search_parts, search_rank) of the search the storefront runs now: the Search Document's with CATALOG_SEARCH_SOURCE=document, else
    the legacy ones. The only place that chooses, so flipping the setting back is S7's rollback and S10 removes the legacy pair here."""
    if search_source() == 'document':
        return document_search_parts, document_search_rank
    return legacy_search_parts, legacy_search_rank


def search_parts(query, search, own=None):
    """The parts every word matches (backend()); a code split by spaces (MR 968365) also matches whole. With own suppliers
    (mall.catalog_scope), only their codes count, so another supplier's code finds nothing."""
    return backend()[0](query, search, own)


def search_rank(search, own=None):
    """0 for a SKU, alterno, supplier code or OEM number equal to the code searched, 1 for one containing it, 2 for the rest; None
    when the search is no code (backend())."""
    return backend()[1](search, own)
