"""Storefront catalog search on the Search Document (mall.search_document): one row per line on sale, every word one LIKE on a
trigram-indexed column. Every word of the search has to match somewhere on the line, in any order; accents and case are ignored; a word
with a digit is a code, compared without separators (51750J9000 finds 51750-J9000); DEL is no filler; every word also goes through
mall.brands.resolve, so "filtro aceite toyota" finds the rows the catalog wrote as TOY. A supplier's interno codes count only in its own
catalog (mall.catalog_scope's own), and disputa codes only when they are the line's main (B15). A code search ranks the lines holding the
code first (0), then the ones holding a code that contains it (1), then the rest (2).

The legacy search on the catalog tables (CATALOG_SEARCH_SOURCE=legacy) went with inventory refactor S10: the owner flipped the setting to
document once S7's counts verified."""
import operator
import unicodedata
from functools import reduce

from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.lookups import Contains

from .oem_links import MIN_SEARCH
from .oem_reference_models import compact

# Words that join a search but name nothing (filtro de aceite para Toyota). Not DEL: the catalog writes it for delantero.
FILLER = {'DE', 'LA', 'LAS', 'EL', 'LOS', 'PARA', 'CON', 'Y', 'EN', 'POR', 'UN', 'UNA'}
MAX_WORDS = 8
ACCENTED = 'ÁÀÂÄÉÈÊËÍÌÎÏÓÒÔÖÚÙÛÜÑáàâäéèêëíìîïóòôöúùûüñ'
PLAIN = 'AAAAEEEEIIIIOOOOUUUUNaaaaeeeeiiiioooouuuun'
UNACCENT = str.maketrans(ACCENTED, PLAIN)


def fold(text):
    """Bujía -> BUJIA, piñón -> PINON: how a search word and a Search Document are folded."""
    return unicodedata.normalize('NFC', text).translate(UNACCENT).upper()


def search_words(search):
    """The words that must each match: folded, without bare punctuation, the filler left out unless nothing else is left."""
    words = [word for word in fold(search).split() if compact(word)][:MAX_WORDS]
    return [word for word in words if word not in FILLER] or words


def code_key(search):
    """The whole search without separators when it reads as a code (a digit, MIN_SEARCH characters or more); '' otherwise."""
    key = compact(fold(search))
    return key if len(key) >= MIN_SEARCH and any(char.isdigit() for char in key) else ''


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
    word with a digit, an own interno code contains it."""
    key = compact(word)
    match = Q(text__contains=word) | Q(codes__contains=word)
    brand = (resolved or {}).get(word)
    if brand:
        match |= Q(text__contains=brand)
    if any(char.isdigit() for char in key):
        match |= Q(compact__contains=key)
        if own:
            match |= internal_q(key, own)
    if own:
        match |= internal_q(word, own)
    return match


def document_code_q(key, exact=False, own=None):
    """A document holds a code equal to key (exact) or containing it: one of its codes without separators, an own interno code."""
    needle = f' {key} ' if exact else key
    match = Q(compact__contains=needle)
    return match | internal_q(key, own, exact=exact) if own else match


def documents_matching(match):
    from .search_document_models import SearchDocument
    return SearchDocument.objects.filter(match).values('line_id')


def search_parts(query, search, own=None):
    """The lines of `query` whose document every word matches; a code split by spaces (MR 968365) also matches whole. With own suppliers
    (mall.catalog_scope), only their codes count, so another supplier's code finds nothing. Only lines on sale have a document."""
    words = search_words(search)
    if not words:
        return query
    resolved = resolved_words(words)
    match = reduce(operator.and_, [document_word_q(word, own, resolved) for word in words])
    key = code_key(search)
    if key and len(words) > 1:
        match |= document_code_q(key, own=own)
    return query.filter(pk__in=documents_matching(match))


def search_rank(search, own=None):
    """0 for a line holding the code searched, 1 for one holding a code that contains it, 2 for the rest; None when the search is no
    code."""
    key = code_key(search)
    if not key:
        return None
    return Case(When(pk__in=documents_matching(document_code_q(key, exact=True, own=own)), then=Value(0)),
                When(pk__in=documents_matching(document_code_q(key, own=own)), then=Value(1)), default=Value(2),
                output_field=IntegerField())
