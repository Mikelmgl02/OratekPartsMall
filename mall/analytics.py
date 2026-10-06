"""Internal demand and service metrics. No third-party trackers or IP storage."""
import hashlib
import json
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Avg, Count, F, Max, Min, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from .analytics_models import UsageEvent, UsageVisit
from .catalog_availability import catalog_availability
from .management import IsSuperuser
from .models import Part, SupplierItem, User
from .request_models import ClientRequestSubmission, SupplierRequest, SupplierRequestLine
from .views import account_for, catalog_parts


class UsageThrottle(UserRateThrottle):
    rate = '180/min'


class UsageInput(serializers.Serializer):
    event_id = serializers.UUIDField()
    visit_id = serializers.UUIDField()
    account_id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=['visit', 'search', 'part_view', 'basket_add'])
    term = serializers.CharField(max_length=200, required=False, allow_blank=True)
    part_id = serializers.UUIDField(required=False)
    supplier_item_id = serializers.UUIDField(required=False)
    quantity = serializers.IntegerField(min_value=1, max_value=9999, required=False)

    def validate(self, data):
        kind = data['kind']
        allowed = {'visit': set(), 'search': {'term'}, 'part_view': {'part_id'},
                   'basket_add': {'part_id', 'supplier_item_id', 'quantity'}}[kind]
        provided = set(data) - {'event_id', 'visit_id', 'account_id', 'kind'}
        if provided != allowed:
            raise serializers.ValidationError('Los datos no corresponden al tipo de estadística.')
        if kind == 'search':
            data['term'] = data['term'].strip().upper()
            if not data['term']:
                raise serializers.ValidationError('La búsqueda no puede estar vacía.')
        return data


class UsageEvents(APIView):
    throttle_classes = [UsageThrottle]

    @extend_schema(request=UsageInput, responses=OpenApiTypes.OBJECT)
    @transaction.atomic
    def post(self, request):
        serializer = UsageInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if request.user.is_superuser:
            return Response({'recorded': False}, status=202)
        account = account_for(request.user, data['account_id'])
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
        # Serializing this user's writes also protects concurrent retry deduplication.
        User.objects.select_for_update().get(pk=request.user.pk)
        existing = UsageEvent.objects.filter(pk=data['event_id']).select_related('visit').first()
        if existing:
            if existing.visit.user_id != request.user.pk or existing.fingerprint != fingerprint:
                raise ValidationError('El identificador de esta estadística ya fue utilizado.')
            return Response({'recorded': True}, status=202)
        part = None
        item = None
        result_count = None
        if data['kind'] == 'search':
            result_count = catalog_parts(data['term']).count()
        if 'part_id' in data:
            part = get_object_or_404(Part, pk=data['part_id'], active=True, merged_into__isnull=True)
        if data['kind'] == 'basket_add':
            account_for(request.user, account.pk, 'client')
            item = get_object_or_404(SupplierItem.objects.filter(part=part, matching_status='matched',
                                     supplier__active=True, supplier__roles__capability='supplier').distinct(),
                                     pk=data['supplier_item_id'])
        now = timezone.now()
        # Expire stale browser visit keys on the server too; event retries still
        # resolve above against their original immutable event identifier.
        UsageVisit.objects.filter(user=request.user, key=data['visit_id'],
                                  last_seen_at__lt=now-timedelta(minutes=30)).update(key=uuid.uuid4())
        visit, _ = UsageVisit.objects.get_or_create(user=request.user, key=data['visit_id'], defaults={'last_seen_at': now})
        UsageVisit.objects.filter(pk=visit.pk).update(last_seen_at=now)
        if data['kind'] != 'visit':
            UsageEvent.objects.create(id=data['event_id'], visit=visit, account=account, kind=data['kind'],
                                      fingerprint=fingerprint, term=data.get('term', ''), result_count=result_count,
                                      part=part, sku=part.sku if part else '', supplier_item=item,
                                      quantity=data.get('quantity', 0))
        return Response({'recorded': True}, status=202)


def seconds(value):
    return round(value.total_seconds()) if value is not None else None


class AnalyticsDashboard(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        try:
            days = int(request.query_params.get('days', '30'))
        except (ValueError, TypeError):
            raise ValidationError('Selecciona un período válido.')
        if days not in (7, 30, 90):
            raise ValidationError('Selecciona 7, 30 o 90 días.')
        now = timezone.now()
        today = timezone.localdate(now)
        start_day = today - timedelta(days=days - 1)
        start = timezone.make_aware(timezone.datetime.combine(start_day, timezone.datetime.min.time()))
        visits = UsageVisit.objects.filter(started_at__gte=start, started_at__lte=now, user__is_superuser=False)
        events = UsageEvent.objects.filter(occurred_at__gte=start, occurred_at__lte=now, visit__user__is_superuser=False)
        searches = events.filter(kind='search')
        submissions = ClientRequestSubmission.objects.filter(created_at__gte=start, created_at__lte=now)
        requests = SupplierRequest.objects.filter(created_at__gte=start, created_at__lte=now)
        lines = SupplierRequestLine.objects.filter(request__in=requests)
        visit_totals = visits.aggregate(visits=Count('pk'), active_users=Count('user_id', distinct=True))
        active_ids = visits.values('user_id')
        returning = UsageVisit.objects.filter(user_id__in=active_ids, started_at__lte=now).values('user_id').annotate(n=Count('pk')).filter(n__gte=2).count()
        event_totals = events.aggregate(searches=Count('pk', filter=Q(kind='search')),
                                       zero_result_searches=Count('pk', filter=Q(kind='search', result_count=0)),
                                       part_views=Count('pk', filter=Q(kind='part_view')),
                                       basket_additions=Count('pk', filter=Q(kind='basket_add')))
        reviewed = Q(reviewed_at__isnull=False, reviewed_at__gte=F('created_at'))
        request_totals = requests.aggregate(supplier_requests=Count('pk'), reviewed_requests=Count('pk', filter=reviewed),
                                           pending_requests=Count('pk', filter=Q(status='pending')),
                                           pending_over_24h=Count('pk', filter=Q(status='pending', created_at__lt=now-timedelta(hours=24))),
                                           average_review_time=Avg(F('reviewed_at')-F('created_at'), filter=reviewed))
        request_totals['average_review_seconds'] = seconds(request_totals.pop('average_review_time'))
        request_totals['submissions'] = submissions.count()
        request_totals['requested_units'] = lines.aggregate(n=Sum('quantity'))['n'] or 0
        top_searches = list(searches.values('term').annotate(searches=Count('pk'),
                         users=Count('visit__user_id', distinct=True),
                         zero_results=Count('pk', filter=Q(result_count=0))).order_by('-searches', 'term')[:12])
        missing_searches = list(searches.filter(result_count=0).values('term').annotate(
                               searches=Count('pk'), users=Count('visit__user_id', distinct=True)).order_by('-searches', 'term')[:12])

        # Aggregate events and transactional quantities independently to avoid join multiplication.
        activity = {row['canonical']: row for row in events.filter(part__isnull=False).annotate(
                    canonical=Coalesce('part__merged_into_id', 'part_id')).values('canonical').annotate(
                    views=Count('pk', filter=Q(kind='part_view')), basket_additions=Count('pk', filter=Q(kind='basket_add')),
                    basket_units=Coalesce(Sum('quantity', filter=Q(kind='basket_add')), 0))}
        for row in lines.annotate(canonical=Coalesce('part__merged_into_id', 'part_id')).values('canonical').annotate(
                requested_units=Sum('quantity'), requests=Count('request_id', distinct=True)):
            activity.setdefault(row['canonical'], {'views': 0, 'basket_additions': 0, 'basket_units': 0}).update(row)
        ranked = sorted(activity, key=lambda pk: (-activity[pk]['views'], -activity[pk].get('requested_units', 0), str(pk)))[:12]
        parts = {part.pk: part for part in Part.objects.filter(pk__in=ranked)}
        availability = catalog_availability(list(parts.values()))
        top_parts = [{'id': str(pk), 'sku': parts[pk].sku, 'description': parts[pk].description,
                      'category': parts[pk].category, 'subcategory': parts[pk].subcategory,
                      'views': activity[pk]['views'], 'basket_additions': activity[pk]['basket_additions'],
                      'basket_units': activity[pk]['basket_units'], 'requested_units': activity[pk].get('requested_units', 0),
                      'requests': activity[pk].get('requests', 0), 'availability': availability.get(pk)}
                     for pk in ranked if pk in parts]
        suppliers = []
        for row in requests.values('supplier_id', 'supplier__name').annotate(
                received=Count('pk'), reviewed=Count('pk', filter=reviewed), pending=Count('pk', filter=Q(status='pending')),
                pending_over_24h=Count('pk', filter=Q(status='pending', created_at__lt=now-timedelta(hours=24))),
                oldest_pending=Min('created_at', filter=Q(status='pending')),
                average_review=Avg(F('reviewed_at')-F('created_at'), filter=reviewed)).order_by('-received', 'supplier__name')[:12]:
            suppliers.append({'id': str(row['supplier_id']), 'name': row['supplier__name'],
                              **{key: row[key] for key in ('received', 'reviewed', 'pending', 'pending_over_24h')},
                              'average_review_seconds': seconds(row['average_review']),
                              'oldest_pending_seconds': seconds(now-row['oldest_pending']) if row['oldest_pending'] else None})
        visitors = [{'username': row['user__username'], 'visits': row['visits'], 'searches': row['searches'], 'views': row['views'],
                     'last_seen_at': row['last_seen'].isoformat()} for row in visits.values('user_id', 'user__username').annotate(
                     visits=Count('pk', distinct=True), searches=Count('events', filter=Q(events__kind='search'), distinct=True),
                     views=Count('events', filter=Q(events__kind='part_view'), distinct=True), last_seen=Max('last_seen_at')).order_by('-visits', 'user__username')[:12]]
        daily = {start_day+timedelta(days=i): {'visits': 0, 'searches': 0, 'views': 0, 'submissions': 0} for i in range(days)}
        for queryset, date_field, key in [(visits, 'started_at', 'visits'), (searches, 'occurred_at', 'searches'),
                                         (events.filter(kind='part_view'), 'occurred_at', 'views'), (submissions, 'created_at', 'submissions')]:
            for row in queryset.annotate(day=TruncDate(date_field)).values('day').annotate(n=Count('pk')).order_by('day'):
                if row['day'] in daily:
                    daily[row['day']][key] = row['n']
        first = UsageVisit.objects.filter(user__is_superuser=False).aggregate(first=Min('started_at'))['first']
        return Response({'period': {'days': days, 'from': start.isoformat(), 'to': now.isoformat()},
                         'usage_started_at': first.isoformat() if first else None,
                         'summary': {**visit_totals, 'returning_users': returning, **event_totals, **request_totals},
                         'daily': [{'date': day.isoformat(), **counts} for day, counts in daily.items()],
                         'top_searches': top_searches, 'missing_searches': missing_searches,
                         'top_parts': top_parts, 'suppliers': suppliers, 'visitors': visitors})
