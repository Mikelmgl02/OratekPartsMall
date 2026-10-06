import uuid
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from .analytics_models import UsageEvent, UsageVisit
from .models import Account, Membership, Part, PartCode, Role, StockEntry, SupplierItem, User
from .request_models import ClientRequestSubmission, SupplierRequest, SupplierRequestLine


class AnalyticsTests(APITestCase):
    def setUp(self):
        self.root = User.objects.create_superuser('analytics-root', 'analytics-root@example.invalid', 'Test938!')
        self.buyer = User.objects.create_user('analytics-buyer', 'analytics-buyer@example.invalid', 'Test938!')
        self.other = User.objects.create_user('analytics-other', 'analytics-other@example.invalid', 'Test938!')
        self.account = Account.objects.create(name='ANALYTICS CLIENT')
        self.account.roles.add(Role.objects.get(code='client_business'))
        Membership.objects.create(user=self.buyer, account=self.account)
        self.other_account = Account.objects.create(name='OTHER CLIENT')
        Membership.objects.create(user=self.other, account=self.other_account)
        self.supplier = Account.objects.create(name='ANALYTICS SUPPLIER')
        self.supplier.roles.add(*Role.objects.filter(capability='supplier'))
        self.part = Part.objects.create(sku='58411-1R000', description='TAMBOR', category='FRENOS', subcategory='TAMBORES')
        PartCode.objects.create(part=self.part, code='58411-1R000-G')
        self.item = SupplierItem.objects.create(supplier=self.supplier, part=self.part, supplier_invent_id='STABLE-1',
                                                codigo='58411-1R000-G', brand='TEST', source='upload',
                                                matching_status='matched', reported_quantity=10)
        self.visit_id = uuid.uuid4()
        self.client.force_authenticate(self.buyer)

    def payload(self, kind, **values):
        return {'event_id': str(uuid.uuid4()), 'visit_id': str(self.visit_id), 'account_id': str(self.account.pk), 'kind': kind, **values}

    def post(self, kind, **values):
        return self.client.post('/api/v1/analytics/events/', self.payload(kind, **values), format='json')

    def dashboard(self, days=30):
        self.client.force_authenticate(self.root)
        return self.client.get(f'/api/v1/management/analytics/?days={days}')

    def test_authenticated_membership_and_superuser_reporting_are_enforced(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.post('visit').status_code, 401)
        self.assertEqual(self.client.get('/api/v1/management/analytics/').status_code, 401)
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.client.get('/api/v1/management/analytics/').status_code, 403)
        self.assertEqual(self.post('visit', account_id=str(self.other_account.pk)).status_code, 404)
        self.account.active = False
        self.account.save()
        self.assertEqual(self.post('visit').status_code, 404)
        self.assertEqual(UsageVisit.objects.count(), 0)

    def test_retries_and_heartbeat_do_not_inflate_visits_or_searches(self):
        self.assertEqual(self.post('visit').status_code, 202)
        payload = self.payload('search', term='  58411-1r000-g  ', result_count=0, user_id=self.other.pk)
        response = self.client.post('/api/v1/analytics/events/', payload, format='json')
        self.assertEqual(response.status_code, 202)
        event = UsageEvent.objects.get()
        self.assertEqual(event.term, '58411-1R000-G')
        self.assertEqual(event.result_count, 1)
        self.assertEqual(event.visit.user_id, self.buyer.pk)
        self.assertEqual(self.client.post('/api/v1/analytics/events/', payload, format='json').status_code, 202)
        self.assertEqual(self.post('visit').status_code, 202)
        self.assertEqual(UsageVisit.objects.count(), 1)
        self.assertEqual(UsageEvent.objects.count(), 1)
        event.refresh_from_db()
        self.assertEqual(self.dashboard().data['summary']['searches'], 1)

    def test_reusing_an_event_identifier_for_another_payload_or_user_is_rejected(self):
        payload = self.payload('search', term='58411')
        self.assertEqual(self.client.post('/api/v1/analytics/events/', payload, format='json').status_code, 202)
        self.assertEqual(self.client.post('/api/v1/analytics/events/', {**payload, 'term': 'OTHER'}, format='json').status_code, 400)
        self.client.force_authenticate(self.other)
        payload['account_id'] = str(self.other_account.pk)
        self.assertEqual(self.client.post('/api/v1/analytics/events/', payload, format='json').status_code, 400)
        self.assertEqual(UsageEvent.objects.count(), 1)

    def test_real_catalog_results_are_counted_and_invalid_events_leave_no_records(self):
        PartCode.objects.create(part=self.part, code='58411-1R000-KSM')
        Part.objects.create(sku='INACTIVE', description='58411', active=False)
        self.assertEqual(self.post('search', term='58411', result_count=999).status_code, 202)
        self.assertEqual(UsageEvent.objects.get().result_count, 1)
        self.assertEqual(self.post('search', term='NOT-IN-CATALOG').status_code, 202)
        before = (UsageVisit.objects.count(), UsageEvent.objects.count())
        for kind, values in [('search', {'term': ''}), ('part_view', {}), ('basket_add', {'part_id': str(self.part.pk), 'supplier_item_id': str(self.item.pk), 'quantity': 0}), ('visit', {'term': 'INVALID'})]:
            self.assertEqual(self.post(kind, **values).status_code, 400)
        self.assertEqual(self.post('basket_add', part_id=str(self.part.pk), supplier_item_id=str(uuid.uuid4()), quantity=2).status_code, 404)
        self.assertEqual((UsageVisit.objects.count(), UsageEvent.objects.count()), before)
        result = self.dashboard().data
        self.assertEqual(result['summary']['zero_result_searches'], 1)
        self.assertEqual(result['missing_searches'][0]['term'], 'NOT-IN-CATALOG')

    def test_views_and_basket_units_do_not_modify_inventory_and_keep_account_context(self):
        before = list(SupplierItem.objects.values())
        self.assertEqual(self.post('part_view', part_id=str(self.part.pk)).status_code, 202)
        # A supplier may hold both supplier roles; the join must not duplicate its item.
        self.assertEqual(self.post('basket_add', part_id=str(self.part.pk), supplier_item_id=str(self.item.pk), quantity=3).status_code, 202)
        self.assertEqual(list(SupplierItem.objects.values()), before)
        self.assertEqual(StockEntry.objects.count(), 0)
        self.assertEqual(UsageEvent.objects.filter(account=self.account).count(), 2)
        row = self.dashboard().data['top_parts'][0]
        self.assertEqual((row['views'], row['basket_additions'], row['basket_units']), (1, 1, 3))
        self.assertEqual(row['availability']['supplier_count'], 1)

    def test_admin_visits_are_excluded_and_inactivity_starts_a_return_visit(self):
        self.post('visit')
        visit = UsageVisit.objects.get()
        UsageVisit.objects.filter(pk=visit.pk).update(last_seen_at=timezone.now()-timedelta(minutes=31))
        self.post('visit')
        self.assertEqual(UsageVisit.objects.count(), 2)
        self.client.force_authenticate(self.root)
        self.assertEqual(self.post('visit').data, {'recorded': False})
        self.assertEqual(UsageVisit.objects.count(), 2)
        summary = self.dashboard().data['summary']
        self.assertEqual((summary['visits'], summary['active_users'], summary['returning_users']), (2, 1, 1))

    def test_transaction_metrics_use_request_timestamps_and_do_not_multiply_lines_by_views(self):
        for _ in range(2):
            self.post('part_view', part_id=str(self.part.pk))
        submission = ClientRequestSubmission.objects.create(client=self.account, submission_id=uuid.uuid4(), actor=self.buyer, payload_hash='a')
        reviewed = SupplierRequest.objects.create(submission=submission, client=self.account, supplier=self.supplier,
                   client_name=self.account.name, supplier_name=self.supplier.name, reference='AN-1', status='reviewed')
        now = timezone.now()
        SupplierRequest.objects.filter(pk=reviewed.pk).update(created_at=now-timedelta(hours=3), reviewed_at=now-timedelta(hours=1))
        SupplierRequestLine.objects.create(request=reviewed, supplier_item=self.item, part=self.part, supplier_invent_id='STABLE-1',
                                          sku=self.part.sku, codigo=self.item.codigo, quantity=4)
        pending_submission = ClientRequestSubmission.objects.create(client=self.account, submission_id=uuid.uuid4(), actor=self.buyer, payload_hash='b')
        pending = SupplierRequest.objects.create(submission=pending_submission, client=self.account, supplier=self.supplier,
                   client_name=self.account.name, supplier_name=self.supplier.name, reference='AN-2')
        SupplierRequest.objects.filter(pk=pending.pk).update(created_at=now-timedelta(hours=25))
        result = self.dashboard().data
        s = result['summary']
        self.assertEqual((s['submissions'], s['supplier_requests'], s['reviewed_requests'], s['pending_over_24h']), (2, 2, 1, 1))
        self.assertEqual(s['average_review_seconds'], 7200)
        self.assertEqual(s['requested_units'], 4)
        self.assertEqual(result['top_parts'][0]['requested_units'], 4)
        self.assertEqual(result['top_parts'][0]['views'], 2)
        self.assertEqual(result['suppliers'][0]['average_review_seconds'], 7200)
        self.assertGreaterEqual(result['suppliers'][0]['oldest_pending_seconds'], 25*3600)

    def test_reviewed_sku_merges_preserve_usage_and_date_filters_exclude_older_visits(self):
        self.post('part_view', part_id=str(self.part.pk))
        target = Part.objects.create(sku='CANONICAL-TAMBOR')
        Part.objects.filter(pk=self.part.pk).update(active=False, merged_into=target)
        self.post('part_view', part_id=str(target.pk))
        old = UsageVisit.objects.create(user=self.buyer, key=uuid.uuid4(), last_seen_at=timezone.now()-timedelta(days=100))
        UsageVisit.objects.filter(pk=old.pk).update(started_at=timezone.now()-timedelta(days=100))
        response = self.dashboard(7)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['daily']), 7)
        self.assertEqual(response.data['summary']['visits'], 1)
        self.assertEqual(response.data['summary']['returning_users'], 1)
        self.assertEqual(response.data['top_parts'][0]['sku'], target.sku)
        self.assertEqual(response.data['top_parts'][0]['views'], 2)
        self.assertEqual(self.dashboard(10).status_code, 400)
        self.assertEqual(self.dashboard('bad').status_code, 400)
