"""Storefront catalog search. Every word of the search has to match somewhere on the SKU, in any order; accents and case are ignored;
a word with a digit is a code, compared without separators (51750J9000 finds 51750-J9000); and a code search ranks the SKUs whose
own code, an alterno, a supplier code or an OEM number reached equals it first, then the ones containing it, then the rest."""
import operator
import unicodedata
from functools import reduce

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


def matched_items():
    return SupplierItem.objects.filter(matching_status='matched', supplier__active=True)


def word_q(word):
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
             | Q(pk__in=matched_items().filter(items).values('part_id')))
    library = library_q(key) if any(char.isdigit() for char in key) else None
    return match | library if library is not None else match


def code_key(search):
    """The whole search without separators when it reads as a code (a digit, MIN_SEARCH characters or more); '' otherwise."""
    key = compact(fold(search))
    return key if len(key) >= MIN_SEARCH and any(char.isdigit() for char in key) else ''


def code_q(key, exact=False):
    return (compact_has('sku', key, exact)
            | Q(pk__in=PartCode.objects.filter(compact_has('code', key, exact)).values('part_id'))
            | Q(pk__in=matched_items().filter(compact_has('codigo', key, exact)).values('part_id'))
            | library_q(key, exact=exact))


def search_parts(query, search):
    """The parts every word matches; a code split by spaces (MR 968365) also matches whole."""
    words = search_words(search)
    if not words:
        return query
    match = reduce(operator.and_, map(word_q, words))
    key = code_key(search)
    return query.filter(match | code_q(key) if key and len(words) > 1 else match)


def search_rank(search):
    """0 for a SKU, alterno, supplier code or OEM number equal to the code searched, 1 for one containing it, 2 for the rest; None
    when the search is no code."""
    key = code_key(search)
    if not key:
        return None
    return Case(When(code_q(key, exact=True), then=Value(0)), When(code_q(key), then=Value(1)), default=Value(2),
                output_field=IntegerField())
