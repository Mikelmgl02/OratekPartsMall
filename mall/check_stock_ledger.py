"""Run with python -m mall.check_stock_ledger [--examples N], daily (see README): proves the stock ledger still explains every count.

1. Balances: replaying an item's entries from zero, in the order they were written, gives each entry's recorded balances (reported_after,
   reserved_after) and ends at the item's reported and reserved counts. An item with units but no entry, or a replay below zero, fails.
2. Reservations: an item reserves exactly what its open reservations hold; every reservation has one reserve entry, and what its
   later entries take out (shortfall, settle, release) leaves exactly what it still holds: its quantity while open, nothing once settled or
   released. A settled reservation has its settle entry, a released one a shortfall or release entry, an open one neither a settle nor a
   release; each entry belongs to its reservation's item.
Read-only, on one consistent snapshot. Prints counts and a few examples; exits 1 when something does not reconcile.
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager

PROBLEMS = {
    'chain': 'Movimientos cuyo saldo registrado no sigue del anterior',
    'final': 'Artículos cuyo saldo no coincide con su historial',
    'unrecorded': 'Artículos con unidades y sin movimientos',
    'negative': 'Movimientos que dejan un saldo negativo',
    'reserved': 'Artículos cuyas unidades reservadas no coinciden con sus reservas abiertas',
    'reservation': 'Reservas sin su movimiento de reserva o de descuento',
}


def movement(entry):
    """(change of the reported count, change of reserved units) an entry records."""
    sign = 1 if entry['direction'] == 'credit' else -1
    reserved = (1 if entry['reserved_direction'] == 'credit' else -1) * entry['reserved_delta']
    return (sign * entry['quantity'], reserved) if entry['balance_type'] == 'stock' else (0, reserved)


@contextmanager
def snapshot():
    """One REPEATABLE READ, read-only transaction, so writes committed while the check runs cannot look like gaps. Inside a caller's
    transaction (tests) it reads what that transaction sees."""
    from django.db import connection, transaction
    if connection.in_atomic_block:
        yield
        return
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        yield


def check(examples=10):
    from .models import StockEntry, SupplierItem
    from .request_models import StockReservation
    found, samples = Counter(), defaultdict(list)

    def problem(kind, text):
        found[kind] += 1
        if len(samples[kind]) < examples:
            samples[kind].append(text)

    with snapshot():
        items = {pk: (label, reported, reserved) for pk, label, reported, reserved in SupplierItem.objects.values_list(
            'pk', 'supplier_invent_id', 'reported_quantity', 'reserved_quantity').iterator(chunk_size=5000)}
        state, linked, entries, out_of_order, last = {}, defaultdict(list), 0, 0, {}
        fields = ['id', 'item_id', 'kind', 'quantity', 'direction', 'balance_type', 'reserved_delta', 'reserved_direction',
                  'reported_after', 'reserved_after', 'reservation_id', 'created_at']
        # Insertion order: every writer holds the item's row lock while it writes, so ids follow the order the changes applied.
        for entry in StockEntry.objects.order_by('item_id', 'id').values(*fields).iterator(chunk_size=5000):
            entries += 1
            reported, reserved = state.get(entry['item_id'], (0, 0))
            change = movement(entry)
            reported, reserved = reported + change[0], reserved + change[1]
            if reported < 0 or reserved < 0:
                problem('negative', f'movimiento {entry["id"]} de {items[entry["item_id"]][0]}: {reported} existencias, {reserved} reservadas')
            if (reported, reserved) != (entry['reported_after'], entry['reserved_after']):
                # Each link is checked from the previous recorded balance: a missing entry breaks one link, an edited balance the two
                # around it, and the item's counts are compared with the last recorded balance.
                problem('chain', f'movimiento {entry["id"]} de {items[entry["item_id"]][0]}: del anterior resulta {reported}/{reserved}, '
                                 f'registra {entry["reported_after"]}/{entry["reserved_after"]}')
                reported, reserved = entry['reported_after'], entry['reserved_after']
            state[entry['item_id']] = (reported, reserved)
            if entry['item_id'] in last and entry['created_at'] < last[entry['item_id']]:
                out_of_order += 1  # the history lists newest first by date: a clock step back would show it out of order
            last[entry['item_id']] = entry['created_at']
            if entry['reservation_id']:
                linked[entry['reservation_id']].append(entry)
        for pk, (label, reported, reserved) in items.items():
            if pk in state:
                if state[pk] != (reported, reserved):
                    problem('final', f'{label}: el historial termina en {state[pk][0]}/{state[pk][1]}, el artículo tiene {reported}/{reserved}')
            elif reported or reserved:
                problem('unrecorded', f'{label}: {reported} existencias, {reserved} reservadas')

        held, reservations = Counter(), 0
        for reservation in StockReservation.objects.select_related('order').iterator(chunk_size=2000):
            reservations += 1
            if reservation.status == 'open':
                held[reservation.item_id] += reservation.quantity
            moves = linked.pop(reservation.pk, [])
            kinds = Counter(entry['kind'] for entry in moves)
            name = f'{reservation.order.reference} en {items[reservation.item_id][0]}'
            left = sum(movement(entry)[1] for entry in moves)  # reserved units in, minus what shortfalls, settles and releases took out
            closing = {'settled': kinds['settle'] == 1, 'released': kinds['settle'] == 0 and (kinds['release'] + kinds['shortfall']) >= 1,
                       'open': kinds['settle'] == 0 and kinds['release'] == 0}[reservation.status]
            if kinds['reserve'] != 1 or not closing or set(kinds) - {'reserve', 'shortfall', 'settle', 'release'}:
                problem('reservation', f'{name} ({reservation.status}): {dict(kinds) or "sin movimientos"}')
            elif any(entry['item_id'] != reservation.item_id for entry in moves) or left != (reservation.quantity if reservation.status == 'open' else 0):
                problem('reservation', f'{name} ({reservation.status}): sus movimientos dejan {left} unidades reservadas, '
                                       f'retiene {reservation.quantity if reservation.status == "open" else 0}')
        for pk, (label, _, reserved) in items.items():
            if reserved != held[pk]:
                problem('reserved', f'{label}: reserva {reserved} unidades, sus reservas abiertas suman {held[pk]}')
    return {'items': len(items), 'entries': entries, 'reservations': reservations, 'out_of_order': out_of_order,
            'problems': {kind: {'count': found[kind], 'examples': samples[kind]} for kind in PROBLEMS if found[kind]}}


def render(result):
    head = (f"Libro de existencias: {result['items']:,} artículos · {result['entries']:,} movimientos · "
            f"{result['reservations']:,} reservas.").replace(',', '.')
    lines = [head + (' Todo cuadra.' if not result['problems'] else '')]
    for kind, detail in result['problems'].items():
        lines.append(f"- {PROBLEMS[kind]}: {detail['count']:,}".replace(',', '.'))
        lines.extend(f'    {text}' for text in detail['examples'])
    if result['out_of_order']:
        lines.append(f"Aviso: {result['out_of_order']} movimientos tienen una fecha anterior al movimiento previo del mismo artículo.")
    return '\n'.join(lines)


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Comprueba que el historial de movimientos explica las existencias y reservas de cada artículo.')
    parser.add_argument('--examples', type=int, default=10, help='Ejemplos por tipo de problema (10 por defecto).')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    result = check(examples=max(0, options.examples))
    print(render(result), file=stdout or sys.stdout)
    return 1 if result['problems'] else 0


if __name__ == '__main__':
    sys.exit(main())
