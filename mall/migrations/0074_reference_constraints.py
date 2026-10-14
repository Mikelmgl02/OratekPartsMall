# Written by hand as the tighten migration of inventory refactor stage S2a (plan §1.2.4, §3 S2a), applied from the worktree before its
# commit lands. It first repairs, idempotently, the drift that has one right answer: tipo from ref_type (the code before S2a wrote
# ref_type alone, so its rows kept 0073's tipo otro; references.TIPO_OF repeated here on the historical model, since a migration imports
# no app code) and a Reference named as its own replacement. It stops, with counts, on what needs a person: an unknown tipo, estado or
# ref_type, an interno code without its supplier (or a supplier on another tipo), a blank code, a dispute without its note, and a key
# two live rows hold (mall.refactor.s2a_references retires a line's spelling duplicates and lists the keys of two lines). Then
# unique_brand_code gives way to reference_one_live_line (one exclusive Reference per clave, brand and supplier: a code retired or
# disputed holds nothing), the CHECKs, among them the transition rule tipo = ref_type until S10, and the clave index. On an empty
# database it changes nothing.
#
# Rolling S2a back: mall.refactor.s2a_references has the whole order. Reverse this migration before reverting the code (python manage.py
# migrate mall 0073_reference, from the worktree): the code before S2a writes ref_type without tipo, which these CHECKs refuse. Its
# reverse re-adds unique_brand_code and refuses, with counts, while one printed brand and code is held twice (a code retired on one
# line and added on another after landing): run python -m mall.refactor.s2a_references --rollback --purge-retired first.

from django.db import migrations, models
from django.db.models import Count, F, Q

from mall.refactor.migration_ops import with_lock_timeout

TIPOS = ['oem', 'aftermarket', 'interno', 'otro']
ESTADOS = ['verificado', 'declarado', 'inferido', 'disputa', 'retirada']
EXCLUSIVE = ['verificado', 'declarado', 'inferido']
TIPO_OF = {'oem': 'oem', 'company': 'aftermarket', 'unknown': 'otro'}


def repair_references(apps, schema_editor):
    Reference = apps.get_model('mall', 'Reference')
    for ref_type, tipo in TIPO_OF.items():
        rows = Reference.objects.filter(ref_type=ref_type).exclude(tipo=tipo)
        (rows.exclude(tipo='interno') if ref_type == 'unknown' else rows).update(tipo=tipo)
    Reference.objects.filter(reemplazado_por=F('id')).update(reemplazado_por=None)
    keys = (Reference.objects.filter(estado__in=EXCLUSIVE).values('clave', 'brand', 'supplier').annotate(rows=Count('id'))
            .filter(rows__gt=1).order_by('clave', 'brand'))
    problems = {
        'referencias con un tipo desconocido': Reference.objects.exclude(tipo__in=TIPOS).count(),
        'referencias con un estado desconocido': Reference.objects.exclude(estado__in=ESTADOS).count(),
        'referencias con un ref_type desconocido': Reference.objects.exclude(ref_type__in=list(TIPO_OF)).count(),
        'códigos internos sin proveedor o proveedores en otro tipo': Reference.objects.filter(
            Q(tipo='interno', supplier__isnull=True) | (~Q(tipo='interno') & Q(supplier__isnull=False))).count(),
        'referencias sin código': Reference.objects.filter(code='').count(),
        'referencias en disputa sin motivo': Reference.objects.filter(estado='disputa', dispute_note='').count(),
        'claves (clave, marca, proveedor) que tienen dos referencias vivas': keys.count(),
    }
    problems = {label: count for label, count in problems.items() if count}
    if problems:
        examples = ', '.join(f"{key['clave']} {key['brand']}".strip() for key in keys[:10])
        raise RuntimeError('0074_reference_constraints no puede poner las restricciones: '
                           + '; '.join(f'{count} {label}' for label, count in problems.items())
                           + (f' ({examples})' if examples else '')
                           + '. Necesitan una persona (python -m mall.refactor.s2a_references --dry-run las lista): corrígelas con '
                             'mall.references y vuelve a migrar.')


def refuse_printed_duplicates(apps, schema_editor):
    """Reverse only: unique_brand_code comes back, so one printed (brand, code) may be held once, retired rows included."""
    Reference = apps.get_model('mall', 'Reference')
    held = Reference.objects.values('brand', 'code').annotate(rows=Count('id')).filter(rows__gt=1).order_by('brand', 'code')
    if held.exists():
        examples = ', '.join(f"{row['brand']} {row['code']}".strip() for row in held[:10])
        raise RuntimeError(f'No se puede volver a unique_brand_code: {held.count()} marcas y códigos están dos veces ({examples}). '
                           'Ejecuta python -m mall.refactor.s2a_references --rollback --purge-retired y vuelve a revertir.')


class Migration(migrations.Migration):

    dependencies = [
        ('mall', '0073_reference'),
    ]

    operations = with_lock_timeout([
        migrations.RunPython(repair_references, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name='reference',
            name='unique_brand_code',
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_printed_duplicates),
        migrations.AddConstraint(
            model_name='reference',
            constraint=models.UniqueConstraint(condition=models.Q(('estado__in', ['verificado', 'declarado', 'inferido'])), fields=('clave', 'brand', 'supplier'), name='reference_one_live_line', nulls_distinct=False),
        ),
        migrations.AddConstraint(
            model_name='reference',
            constraint=models.CheckConstraint(condition=models.Q(('tipo__in', ['oem', 'aftermarket', 'interno', 'otro'])), name='reference_valid_tipo'),
        ),
        migrations.AddConstraint(
            model_name='reference',
            constraint=models.CheckConstraint(condition=models.Q(('estado__in', ['verificado', 'declarado', 'inferido', 'disputa', 'retirada'])), name='reference_valid_estado'),
        ),
        migrations.AddConstraint(
            model_name='reference',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('ref_type', 'oem'), ('tipo', 'oem')), models.Q(('ref_type', 'company'), ('tipo', 'aftermarket')), models.Q(('ref_type', 'unknown'), ('tipo__in', ['interno', 'otro'])), _connector='OR'), name='reference_tipo_matches_ref_type'),
        ),
        migrations.AddConstraint(
            model_name='reference',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('supplier__isnull', False), ('tipo', 'interno')), models.Q(models.Q(('tipo', 'interno'), _negated=True), ('supplier__isnull', True)), _connector='OR'), name='reference_interno_has_supplier'),
        ),
        migrations.AddConstraint(
            model_name='reference',
            constraint=models.CheckConstraint(condition=models.Q(('code', ''), _negated=True), name='reference_code_set'),
        ),
        migrations.AddConstraint(
            model_name='reference',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('estado', 'disputa'), _negated=True), models.Q(('dispute_note', ''), _negated=True), _connector='OR'), name='reference_dispute_note'),
        ),
        migrations.AddConstraint(
            model_name='reference',
            constraint=models.CheckConstraint(condition=models.Q(('reemplazado_por', models.F('id')), _negated=True), name='reference_not_own_replacement'),
        ),
        migrations.AddIndex(
            model_name='reference',
            index=models.Index(fields=['clave'], name='reference_clave_idx'),
        ),
    ])
