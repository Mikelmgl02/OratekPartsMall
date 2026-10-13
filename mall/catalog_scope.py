"""Which SKUs a member browses on the storefront. A client account sees the whole catalog; a supplier-only account sees only the SKUs
its own matched items reached, with its own stock alone and no other supplier's name, count or code; an account without a role sees
nothing. The limit is per user (a member of any client account sees everything, since nobody can give themselves a role); ?account=
narrows it to one of the user's accounts, so a member of both kinds can look at the catalog as one of its suppliers sees it."""
from dataclasses import dataclass
from uuid import UUID

from django.http import Http404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import Account, SupplierItem

UNCONFIGURED = 'Tu administrador debe configurar tu cuenta.'
ACCOUNT_PARAMETER = OpenApiParameter('account', OpenApiTypes.UUID, description='Una cuenta del usuario: el catálogo como lo ve esa cuenta. '
                                     'Una cuenta solo proveedora ve únicamente sus artículos.')


@dataclass(frozen=True)
class Scope:
    own: tuple | None = None  # the supplier accounts whose items make the scope; None for the whole catalog

    @property
    def full(self):
        return self.own is None

    def own_part_ids(self):
        return SupplierItem.objects.filter(supplier_id__in=self.own, matching_status='matched').values('part_id')

    def parts(self, query):
        """The Part queryset narrowed to the scope."""
        return query if self.full else query.filter(pk__in=self.own_part_ids())


FULL = Scope()


def catalog_scope(user, account_id=''):
    accounts = Account.objects.filter(active=True, memberships__user=user)
    if account_id:
        try:
            UUID(str(account_id))
        except ValueError:
            raise ValidationError({'account': 'La cuenta no es válida.'})
        accounts = accounts.filter(pk=account_id)
        if not accounts.exists():
            raise Http404
    if accounts.filter(roles__capability='client').exists():
        return FULL
    suppliers = tuple(accounts.filter(roles__capability='supplier').order_by('pk').values_list('pk', flat=True).distinct())
    if not suppliers:
        raise PermissionDenied(UNCONFIGURED)
    return Scope(suppliers)


def request_scope(request):
    return catalog_scope(request.user, request.query_params.get('account', '').strip())
