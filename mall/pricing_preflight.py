"""Run with python -m mall.pricing_preflight [--check] before suppliers tighten pricing permissions.

Lists active supplier accounts with no active owner or manager, and accounts whose pricing settings require
a permission no active member holds, so Oratek can promote a member first. Reads memberships and the two
permission settings only; never prices, lists, profiles or rules. --check exits 1 when anything is listed.
"""
import argparse
import os
import sys


def preflight():
    from django.db.models import Count, Q
    from .models import Account
    from .pricing_models import PERMISSION_CHOICES, SupplierPricingSettings
    from .views import PERMISSION_RANK
    labels = {value: label.lower() for value, label in PERMISSION_CHOICES}
    active = Q(memberships__user__is_active=True)
    suppliers = Account.objects.filter(roles__capability='supplier').values('pk')
    accounts = Account.objects.filter(active=True, pk__in=suppliers).annotate(
        members=Count('memberships', filter=active, distinct=True),
        managers=Count('memberships', filter=active & Q(memberships__permission='manager'), distinct=True),
        owners=Count('memberships', filter=active & Q(memberships__permission='owner'), distinct=True)).order_by('name', 'id')
    required = {row['supplier_id']: max(row['config_min_permission'], row['publish_min_permission'], key=PERMISSION_RANK.get)
                for row in SupplierPricingSettings.objects.values('supplier_id', 'config_min_permission', 'publish_min_permission')}
    rows = []
    for account in accounts:
        best = 'owner' if account.owners else 'manager' if account.managers else 'staff' if account.members else None
        needed = required.get(account.pk, 'staff')
        issues = [] if account.owners or account.managers else ['sin propietario ni administrador']
        if best is None or PERMISSION_RANK[best] < PERMISSION_RANK[needed]:
            issues.append(f'la configuración de precios exige el permiso {labels[needed]} y ningún miembro activo lo tiene')
        if issues:
            rows.append({'id': str(account.pk), 'name': account.name, 'members': account.members, 'issues': issues})
    return rows


def render(rows):
    if not rows:
        return 'Todas las cuentas proveedoras activas tienen un propietario o administrador.'
    return '\n'.join([f'Cuentas proveedoras por revisar: {len(rows)}'] + [
        f"- {row['name']} ({row['id']}) · {row['members']} {'miembro activo' if row['members'] == 1 else 'miembros activos'} · {'; '.join(row['issues'])}"
        for row in rows])


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Cuentas proveedoras sin propietario ni administrador.')
    parser.add_argument('--check', action='store_true', help='Termina con código 1 si alguna cuenta necesita revisión.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    rows = preflight()
    print(render(rows), file=stdout or sys.stdout)
    return 1 if options.check and rows else 0


if __name__ == '__main__':
    sys.exit(main())
