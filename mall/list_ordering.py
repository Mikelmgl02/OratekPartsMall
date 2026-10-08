from rest_framework import filters


class StableOrdering(filters.OrderingFilter):
    """Grid sorting (?ordering=-part_count,name) on the view's ordering_fields only, always ending in the primary key: equal values
    keep one order, so a grid's page-sized blocks never repeat or skip a row while it scrolls."""

    def get_ordering(self, request, queryset, view):
        ordering = super().get_ordering(request, queryset, view)
        if ordering and not {'id', '-id', 'pk', '-pk'} & set(ordering):
            ordering = [*ordering, 'id']
        return ordering
