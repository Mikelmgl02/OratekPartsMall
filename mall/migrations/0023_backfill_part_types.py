from django.db import migrations


def backfill(apps, schema_editor):
    Part = apps.get_model('mall', 'Part')
    PartType = apps.get_model('mall', 'PartType')
    Template = apps.get_model('mall', 'TechnicalTemplate')
    db = schema_editor.connection.alias
    pairs = list(Part.objects.using(db).exclude(category='').exclude(subcategory='').values_list('category', 'subcategory').distinct())
    for category, name in pairs:
        group, subgroup = ' '.join(category.upper().split()), ' '.join(name.upper().split())
        if not group or not subgroup:
            continue
        part_type, _ = PartType.objects.using(db).get_or_create(category=group, name=subgroup)
        Template.objects.using(db).get_or_create(part_type=part_type)
        Part.objects.using(db).filter(category=category, subcategory=name).update(part_type=part_type, category=group, subcategory=subgroup)


class Migration(migrations.Migration):
    dependencies = [('mall', '0022_technical_templates_applications')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
