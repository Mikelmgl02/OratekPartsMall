# Written by hand as the tighten migration of inventory refactor stage S3 (plan §1.2.4, §3 S3), applied from the worktree after the
# backfill (python -m mall.refactor.s3_variants --apply, before landing and again after) and before its commit lands. Its RunPython
# first repairs, idempotently, the drift that has one right answer (on the historical models, since a migration imports no app code):
# an item with no line holds no Reference and waits pending; an item whose line changed since the backfill, or that has none of its
# line's References (a queryset or the code before S3 wrote its part), hangs again from its line's Reference for its code, by
# mall.variants' rules (the same key first, the supplier's interno code before a public one, the main before the others; a brandless
# item joins the branded Reference of its clave, a branded one the brandless Reference), or from a new interno Reference of its
# supplier when the line has none (declarado, like the S3 code makes it). It stops, with counts, only for what needs a person: an item
# on a unificada line (I3: Agrupar SKU or Separar SKU decides where it goes), or one whose supplier already uses its code as an interno
# code on another line. The repair ends with SET CONSTRAINTS ALL IMMEDIATE: each item it re-attaches leaves a deferred foreign key check
# pending (Django's foreign keys are DEFERRABLE INITIALLY DEFERRED), and PostgreSQL refuses ALTER TABLE mall_supplieritem in the same
# transaction while one is pending ("pending trigger events"), as 0076 does for the mains. Then:
# - the composite foreign key variant_line_follows_reference: (reference_id, part_id) -> mall_partcode (id, part_id), ON UPDATE CASCADE
#   (moving a Reference to another line moves its items in the same UPDATE, rule 9), DEFERRABLE INITIALLY DEFERRED (only the check
#   waits for the commit; the cascade runs at once). Django cannot describe a composite key, so it is RunSQL with its reverse;
# - CHECK (reference_id IS NULL) = (part_id IS NULL), until S10;
# - CHECK reference_id IS NOT NULL OR matching_status = 'pending', for good (OD21: an item no code places yet waits without one).
# On an empty database it changes nothing.
#
# Rolling S3 back, in this order (the code before S3 writes a line without a Reference, which these CHECKs refuse): under the freeze,
# reverse this migration (python manage.py migrate mall 0077_variant_reference, from the worktree), run python -m
# mall.refactor.s3_variants --rollback (it refuses while these constraints exist), revert the code, then reverse 0077 from the worktree.

import re

from django.db import migrations, models

from mall.refactor.migration_ops import with_lock_timeout

LIVE = ['verificado', 'declarado', 'inferido', 'disputa']
EXCLUSIVE = ['verificado', 'declarado', 'inferido']
ESTADO_RANK = {'verificado': 0, 'declarado': 1, 'inferido': 2, 'disputa': 3}
SPACES = re.compile(r'[ \t\n\r\f\v]+')
COMPOSITE = ('ALTER TABLE mall_supplieritem ADD CONSTRAINT variant_line_follows_reference FOREIGN KEY (reference_id, part_id) '
             'REFERENCES mall_partcode (id, part_id) ON UPDATE CASCADE DEFERRABLE INITIALLY DEFERRED')
DROP_COMPOSITE = 'ALTER TABLE mall_supplieritem DROP CONSTRAINT IF EXISTS variant_line_follows_reference'


def upper(text):
    return ''.join(char if len(char.upper()) != 1 else char.upper() for char in text)


def clave(code):
    """mall.references.clave, as of S3: the compact code when OEM-like (8+ [A-Z0-9], 3+ digits), else the printed form."""
    code = code or ''
    compact = re.sub(r'[^A-Z0-9]', '', upper(code))
    if len(compact) >= 8 and len(re.sub(r'[^0-9]', '', code)) >= 3:
        return compact
    return SPACES.sub(' ', upper(code.strip(' ')))


def brand_of(text):
    return ' '.join((text or '').upper().split())


def choose(rows, item):
    """mall.variants.choose on the line's live References, without the public creations only the backfill makes."""
    key, brand = clave((item.codigo or '').strip().upper()), brand_of(item.brand)
    usable = [row for row in rows if row.clave == key and row.supplier_id in (None, item.supplier_id)]
    rank = lambda row: (row.supplier_id is None, not row.is_main, ESTADO_RANK.get(row.estado, 9), row.pk)
    exact = sorted((row for row in usable if brand_of(row.brand) == brand), key=rank)
    if exact:
        return exact[0]
    if not brand and usable:
        return sorted(usable, key=rank)[0]
    blank = sorted((row for row in usable if not row.brand), key=rank)
    return blank[0] if blank else None


def repair_variants(apps, schema_editor):
    Part = apps.get_model('mall', 'Part')
    Reference = apps.get_model('mall', 'Reference')
    SupplierItem = apps.get_model('mall', 'SupplierItem')
    SupplierItem.objects.filter(part__isnull=True, reference__isnull=False).update(reference=None)
    SupplierItem.objects.filter(part__isnull=True).exclude(matching_status='pending').update(matching_status='pending')
    merged = Part.objects.filter(models.Q(merged_into__isnull=False) | models.Q(estado='unificada'))
    on_merged = SupplierItem.objects.filter(part__in=merged).count()
    if on_merged:
        raise RuntimeError(f'0078_variant_reference_checks no puede poner las restricciones: {on_merged} artículos de proveedor están en '
                           'líneas unificadas (I3). Muévelos con Agrupar SKU o Separar SKU y vuelve a migrar.')
    stale = (SupplierItem.objects.filter(part__isnull=False)
             .filter(models.Q(reference__isnull=True) | models.Q(reference__estado='retirada') | ~models.Q(reference__part=models.F('part')))
             .order_by('pk'))
    clashes = []
    for item in stale.iterator():
        rows = list(Reference.objects.filter(part_id=item.part_id, estado__in=LIVE).order_by('pk'))
        target = choose(rows, item)
        if target is None:
            code, brand = (item.codigo or '').strip().upper(), (item.brand or '').strip().upper()
            held = Reference.objects.filter(clave=clave(code), brand=brand, supplier_id=item.supplier_id, estado__in=EXCLUSIVE).first()
            if held is not None:
                clashes.append(f'{item.codigo} ({item.supplier_invent_id})')
                continue
            target = Reference.objects.create(part_id=item.part_id, code=code, brand=brand, tipo='interno', ref_type='unknown',
                                              supplier_id=item.supplier_id, estado='declarado',
                                              reference_source=f'Declarado por artículo {item.supplier_invent_id}'[:500],
                                              detail={'s3': {'como': 'creada', 'articulo': str(item.pk), 'por': '0078'}})  # --rollback finds it
        SupplierItem.objects.filter(pk=item.pk).update(reference=target)
    if clashes:
        raise RuntimeError('0078_variant_reference_checks no puede poner las restricciones: el proveedor ya usa estos códigos como código '
                           f'interno en otra línea: {", ".join(clashes[:20])}. Vincúlalos desde Inventario y vuelve a migrar.')
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')  # the deferred checks of the writes above, now: the ALTER TABLEs follow


class Migration(migrations.Migration):

    dependencies = [
        ('mall', '0077_variant_reference'),
    ]

    operations = with_lock_timeout([
        migrations.RunPython(repair_variants, migrations.RunPython.noop),
        migrations.RunSQL(COMPOSITE, DROP_COMPOSITE),
        migrations.AddConstraint(
            model_name='supplieritem',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('part__isnull', True), ('reference__isnull', True)),
                                                                 models.Q(('part__isnull', False), ('reference__isnull', False)), _connector='OR'),
                                              name='variant_reference_iff_line'),
        ),
        migrations.AddConstraint(
            model_name='supplieritem',
            constraint=models.CheckConstraint(condition=models.Q(('reference__isnull', False), ('matching_status', 'pending'), _connector='OR'),
                                              name='variant_unattached_is_pending'),
        ),
    ])
