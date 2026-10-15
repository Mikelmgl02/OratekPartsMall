from rest_framework import filters


class StableOrdering(filters.OrderingFilter):
    """Grid sorting (?ordering=-part_count,name) on the view's ordering_fields only, always ending in the primary key: equal values
    keep one order, so a grid's page-sized blocks never repeat or skip a row while it scrolls. A view's ordering_aliases ({'category':
    'part_type__grupo__nombre'}) keep a grid's column name working once the column it named is gone (inventory refactor S10): the name
    stays in ordering_fields and is sorted by what the alias says."""

    def get_ordering(self, request, queryset, view):
        ordering = super().get_ordering(request, queryset, view)
        aliases = getattr(view, 'ordering_aliases', None) or {}
        if ordering and aliases:
            ordering = [('-' if term.startswith('-') else '') + aliases.get(term.lstrip('-'), term.lstrip('-')) for term in ordering]
        if ordering and not {'id', '-id', 'pk', '-pk'} & set(ordering):
            ordering = [*ordering, 'id']
        return ordering
