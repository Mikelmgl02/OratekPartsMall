# The tighten migration of inventory refactor stage S5 (plan §1.2.4, §3 S5), applied from the worktree before its commit lands. It first
# repairs, idempotently, the drift that has one right answer: a subgrupo with no grupo takes the grupo its text names (created when
# missing, matched ignoring case as UNIQUE Lower(nombre) does), and a subgrupo whose text spells its own grupo otherwise takes the grupo's
# spelling (mall.technical_data's rule, repeated here on the historical models because a migration imports no app code). It stops, with
# counts, on what needs a person: a subgrupo with no grupo and no text, one whose text names another grupo than its FK, and two
# subgrupos of one grupo with the same name ignoring case. Then PartType.grupo becomes NOT NULL and UNIQUE (grupo, Lower(name)) is added;
# the old unique_part_type_group_name stays until S10. On an empty database it changes nothing.
#
# Rolling S5 back: reverse this migration first, from the worktree (python manage.py migrate mall 0081_grupo), and only then revert the
# code. The code before S5 creates subgrupos with their text alone (part_type_for, bind_part_types, the part-types API), which the NOT
# NULL refuses; S5's code runs fine without it. Never revert the code while this migration is applied (mall.refactor.s5_grupos).

import uuid
from collections import Counter

import django.db.models.deletion
import django.db.models.functions.text
from django.db import migrations, models

from mall.refactor.migration_ops import with_lock_timeout


def normalized(value):
    return ' '.join((value or '').strip().upper().split())


def repair_grupos(apps, schema_editor):
    Grupo = apps.get_model('mall', 'Grupo')
    PartType = apps.get_model('mall', 'PartType')
    grupos = {grupo.nombre.lower(): grupo for grupo in Grupo.objects.all()}
    names_by_id = {grupo.pk: grupo.nombre for grupo in grupos.values()}
    rows = list(PartType.objects.values('pk', 'category', 'name', 'grupo_id'))  # no join: one would leave out the subgrupos without a grupo
    keys, blank, moved = {}, 0, 0
    for row in rows:
        text = normalized(row['category'])
        if row['grupo_id']:
            keys[row['pk']] = names_by_id[row['grupo_id']].lower()
            moved += text.lower() != keys[row['pk']]
        elif text:
            keys[row['pk']] = text.lower()
        else:
            blank += 1
    names = Counter((keys[row['pk']], row['name'].lower()) for row in rows if row['pk'] in keys)
    problems = {
        'subgrupos sin grupo y sin texto de grupo': blank,
        'subgrupos cuyo texto nombra otro grupo que el suyo': moved,
        'subgrupos repetidos en un grupo (mismo nombre sin distinguir mayúsculas)': sum(count for count in names.values() if count > 1),
    }
    problems = {label: count for label, count in problems.items() if count}
    if problems:
        raise RuntimeError('0082_grupo_checks no puede exigir el grupo: ' + '; '.join(f'{count} {label}' for label, count in problems.items())
                           + '. Necesitan una persona: corrígelos (python -m mall.refactor.s5_grupos --dry-run los lista) y vuelve a migrar.')
    touched = False
    for row in rows:
        key = keys[row['pk']]
        if key not in grupos:
            grupos[key] = Grupo.objects.create(id=uuid.uuid4(), nombre=normalized(row['category']))
        grupo = grupos[key]
        if row['grupo_id'] != grupo.pk or row['category'] != grupo.nombre:
            PartType.objects.filter(pk=row['pk']).update(grupo=grupo, category=grupo.nombre)
            touched = True
    if touched and hasattr(schema_editor, 'execute'):
        # The deferred foreign key checks of those updates fire now: PostgreSQL refuses to ALTER a table with pending trigger events.
        schema_editor.execute('SET CONSTRAINTS ALL IMMEDIATE')


class Migration(migrations.Migration):

    dependencies = [
        ('mall', '0081_grupo'),
    ]

    operations = with_lock_timeout([
        migrations.RunPython(repair_grupos, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='parttype',
            name='grupo',
            field=models.ForeignKey(blank=True, on_delete=django.db.models.deletion.PROTECT, related_name='subgrupos', to='mall.grupo', verbose_name='grupo'),
        ),
        migrations.AddConstraint(
            model_name='parttype',
            constraint=models.UniqueConstraint(models.F('grupo'), django.db.models.functions.text.Lower('name'), name='subgrupo_unique_in_grupo'),
        ),
    ])
