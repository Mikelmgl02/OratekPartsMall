# Written by hand as the tighten migration of inventory refactor stage S2b (plan §1.2.4, §3 S2b), applied from the worktree after the
# backfill (python -m mall.refactor.s2b_main_refs --apply) and before its commit lands. Its RunPython first repairs, idempotently, the
# drift that has one right answer (the rules of mall.references.ensure_main, repeated here on the historical models, since a migration
# imports no app code): a retired row is never a main; every live line's main is the Reference of its codigo (the row with its code, else
# a row of its key respelled to it, else a new brandless one, disputa when another line holds its key under any brand: I5); a line keeps one main;
# the line's tipo follows its main and is_OEM follows tipo (a line with no main, unificada, keeps is_OEM); a unificada line holds no main
# (one left there by a direct write is retired, as mall.references.retire_main_of_merged does). It stops, with counts, only for what needs
# a person: a live line still without its main on its codigo (I1, I2), a line whose tipo does not mirror its main (I11), or a unificada
# line that still holds live References or variants (I3: Agrupar SKU or Separar SKU decides where they go). Then one main per line
# (I1's partial unique index), the main never retired, and the transition CHECKs tipo valid and is_OEM = (tipo = 'OEM'). On an empty
# database it changes nothing. I2, I3 and I11 join two tables, so no CHECK can hold them: mall.references (set_main, ensure_mains) and
# mall.line_merge keep them, and Appendix C's queries verify them. The repair ends with SET CONSTRAINTS ALL IMMEDIATE: a main it inserts
# leaves deferred foreign key checks pending, which PostgreSQL would not let the index and constraints after it run with.
#
# Rolling S2b back: reverse this migration first (python manage.py migrate mall 0075_line_main_reference, from the worktree), then
# python -m mall.refactor.s2b_main_refs --rollback (it refuses while these constraints exist), then revert the code. Never revert the
# code alone: the code before S2b moves a source's main onto its merge target and writes is_OEM without tipo, which these refuse.

import re

from django.db import migrations, models
from django.db.models import Count, Exists, OuterRef

from mall.refactor.migration_ops import with_lock_timeout

LIVE = ['verificado', 'declarado', 'inferido', 'disputa']
EXCLUSIVE = ['verificado', 'declarado', 'inferido']
LINE_TIPO = {'oem': 'OEM', 'aftermarket': 'AFTERMARKET', 'interno': 'UNKNOWN', 'otro': 'UNKNOWN'}
REF_TYPE_OF = {'oem': 'oem', 'aftermarket': 'company', 'interno': 'unknown', 'otro': 'unknown'}
SPACES = re.compile(r'[ \t\n\r\f\v]+')
MAX_PRINTED = 20


def upper(text):
    return ''.join(char if len(char.upper()) != 1 else char.upper() for char in text)


def clave(code):
    """mall.references.clave, as of S2b: the compact code when OEM-like (8+ [A-Z0-9], 3+ digits), else the printed form."""
    code = code or ''
    compact = re.sub(r'[^A-Z0-9]', '', upper(code))
    if len(compact) >= 8 and len(re.sub(r'[^0-9]', '', code)) >= 3:
        return compact
    return SPACES.sub(' ', upper(code.strip(' ')))


def repair_mains(apps, schema_editor):
    Part = apps.get_model('mall', 'Part')
    Reference = apps.get_model('mall', 'Reference')
    SupplierItem = apps.get_model('mall', 'SupplierItem')
    Reference.objects.filter(is_main=True, estado='retirada').update(is_main=False)
    merged = Part.objects.filter(models.Q(merged_into__isnull=False) | models.Q(estado='unificada'))
    Reference.objects.filter(is_main=True, part__in=merged).update(is_main=False, estado='retirada')  # I3: a merged line has no main
    live_lines = Part.objects.filter(merged_into__isnull=True).exclude(estado='unificada')
    on_codigo = Reference.objects.filter(part=OuterRef('pk'), is_main=True, code=OuterRef('sku'), estado__in=LIVE)
    for line in live_lines.annotate(ok=Exists(on_codigo)).filter(ok=False).order_by('pk').iterator():
        key = clave(line.sku)
        rows = sorted(Reference.objects.filter(part=line, clave=key, supplier__isnull=True, estado__in=LIVE).order_by('pk'),
                      key=lambda row: (row.code != line.sku, row.pk))
        target = rows[0] if rows else None
        if target is not None and target.code != line.sku:
            forms = [form for form in [target.code, *(target.printed_forms or [])] if form != line.sku][:MAX_PRINTED]
            Reference.objects.filter(pk=target.pk).update(code=line.sku, printed_forms=forms)
        if target is None:
            tipo = 'oem' if line.is_OEM else 'otro'
            # The brandless reservation (I5): a new brandless main clashes with an exclusive row of its key on another line, any brand.
            held = Reference.objects.filter(clave=key, supplier__isnull=True, estado__in=EXCLUSIVE).exclude(part=line).first()
            target = Reference.objects.create(
                part=line, code=line.sku, brand='', tipo=tipo, ref_type=REF_TYPE_OF[tipo], estado='disputa' if held else 'declarado',
                dispute_note=f'El código {line.sku} ya es de otro SKU: caso de unificación.' if held else '')
        Reference.objects.filter(part=line, is_main=True).exclude(pk=target.pk).update(is_main=False)
        Reference.objects.filter(pk=target.pk).update(is_main=True)
    extra = (Reference.objects.filter(is_main=True, estado__in=LIVE).values('part').annotate(mains=Count('pk')).filter(mains__gt=1)
             .values_list('part', flat=True))
    for line in Part.objects.filter(pk__in=list(extra)):
        keep = (Reference.objects.filter(part=line, is_main=True, estado__in=LIVE).order_by(
            models.Case(models.When(code=line.sku, then=0), default=1), 'pk').first())
        Reference.objects.filter(part=line, is_main=True).exclude(pk=keep.pk).update(is_main=False)
    for ref_tipo, line_tipo in LINE_TIPO.items():
        mains = Reference.objects.filter(is_main=True, tipo=ref_tipo, estado__in=LIVE).values('part')
        Part.objects.filter(pk__in=mains).exclude(tipo=line_tipo, is_OEM=line_tipo == 'OEM').update(tipo=line_tipo, is_OEM=line_tipo == 'OEM')
    without = Part.objects.exclude(pk__in=Reference.objects.filter(is_main=True, estado__in=LIVE).values('part'))
    without.filter(is_OEM=True).exclude(tipo='OEM').update(tipo='OEM')
    without.filter(is_OEM=False, tipo='OEM').update(tipo='UNKNOWN')
    Part.objects.exclude(tipo__in=['OEM', 'AFTERMARKET', 'UNKNOWN']).update(tipo=models.Case(models.When(is_OEM=True, then=models.Value('OEM')),
                                                                                             default=models.Value('UNKNOWN')))
    missing = live_lines.annotate(ok=Exists(on_codigo)).filter(ok=False).count()
    if missing:
        raise RuntimeError(f'0076_line_main_reference_checks no puede poner las restricciones: {missing} líneas vivas siguen sin '
                           'referencia principal en su SKU. Revísalas con python -m mall.refactor.s2b_main_refs --dry-run y vuelve a migrar.')
    mains = Reference.objects.filter(is_main=True, estado__in=LIVE, part__in=live_lines)
    mirrored = models.Q()
    for ref_tipo, line_tipo in LINE_TIPO.items():
        mirrored |= models.Q(tipo=ref_tipo, part__tipo=line_tipo)
    broken = {
        'I11 (el tipo de la línea no sigue a su principal)': mains.exclude(mirrored).count(),
        'I3 (referencias vivas en líneas unificadas)': Reference.objects.filter(part__in=merged, estado__in=LIVE).count(),
        'I3 (artículos de proveedor en líneas unificadas)': SupplierItem.objects.filter(part__in=merged).count(),
    }
    broken = {name: count for name, count in broken.items() if count}
    if broken:
        raise RuntimeError('0076_line_main_reference_checks no puede poner las restricciones: '
                           + '; '.join(f'{name}: {count}' for name, count in broken.items())
                           + '. Las unificadas se corrigen con Agrupar SKU o Separar SKU; revisa con las consultas del Apéndice C y vuelve a migrar.')
    # The rows the repair inserted leave deferred foreign key checks pending (Django's are DEFERRABLE INITIALLY DEFERRED), and PostgreSQL
    # refuses the index and constraints below on a table with pending trigger events: check them now, in this migration's transaction.
    if schema_editor.connection.vendor == 'postgresql':
        with schema_editor.connection.cursor() as cursor:
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')


class Migration(migrations.Migration):

    dependencies = [
        ('mall', '0075_line_main_reference'),
    ]

    operations = with_lock_timeout([
        migrations.RunPython(repair_mains, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name='reference',
            index=models.Index(fields=['part', 'is_main'], name='reference_part_main_idx'),
        ),
        migrations.AddConstraint(
            model_name='part',
            constraint=models.CheckConstraint(condition=models.Q(('tipo__in', ['OEM', 'AFTERMARKET', 'UNKNOWN'])), name='line_valid_tipo'),
        ),
        migrations.AddConstraint(
            model_name='part',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('is_OEM', True), ('tipo', 'OEM')), models.Q(('is_OEM', False), models.Q(('tipo', 'OEM'), _negated=True)), _connector='OR'), name='line_oem_iff_tipo'),
        ),
        migrations.AddConstraint(
            model_name='reference',
            constraint=models.UniqueConstraint(condition=models.Q(('is_main', True), models.Q(('estado', 'retirada'), _negated=True)), fields=('part',), name='reference_one_main_per_line'),
        ),
        migrations.AddConstraint(
            model_name='reference',
            constraint=models.CheckConstraint(condition=models.Q(('is_main', True), ('estado', 'retirada'), _negated=True), name='reference_main_not_retired'),
        ),
    ])
