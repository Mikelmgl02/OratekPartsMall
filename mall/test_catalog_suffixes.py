import io
import json
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from . import catalog_suffixes as cs, oem_finder
from .catalog_identity import company_reference, reconcile_identities
from .catalog_suffix_models import CatalogCodeSuffix, CatalogCodeSuffixChange
from .catalog_suffixes import (LEGACY_COMPANY_SUFFIXES, SuffixTable, chain_summary, classify, classify_chain, company_suffixes,
                               description_head, strip_tags, suffix_table, tokenize_chain)
from .models import Part, User
from .oem_finder_models import OEMFinderRun
from .refactor import s0_suffix_revert as revert

BASE = '/api/v1/management/catalog/suffixes/'
RELABEL_NIGHT = datetime(2026, 10, 7, 2, 50, tzinfo=dt_timezone.utc)  # 21:50 on Oct 6 in Panama


def legacy_company_reference(sku):
    base, sep, suffix = sku.strip().upper().rpartition('-')
    if sep and base and suffix in LEGACY_COMPANY_SUFFIXES:
        return {'code': base, 'brand': LEGACY_COMPANY_SUFFIXES[suffix], 'ref_type': 'company'}
    return None


def relabel_night(user, tokens=revert.TOKENS):
    """The owner's relabel of 2026-10-07 (Sufijos, bulk confirm_tag): every token becomes an owner-confirmed brand TAG, dated that
    night, after the seed confirmations."""
    from .catalog_suffix_views import label, lock_table, locked_row, relabel
    with transaction.atomic():
        lock_table()
        for token in tokens:
            row = locked_row(token)
            relabel(row, label(row, 'confirm_tag', {}), 'confirm_tag', user)
    CatalogCodeSuffixChange.objects.filter(action='seed_confirm').update(created_at=RELABEL_NIGHT - timedelta(hours=1))
    CatalogCodeSuffixChange.objects.filter(action='confirm_tag', token__in=tokens).update(created_at=RELABEL_NIGHT)


@contextmanager
def scratch_checkout():
    """A temporary main checkout with its output/ folder: a test's revert writes its manifest there, never in the live output/."""
    with tempfile.TemporaryDirectory() as folder, override_settings(MAIN_CHECKOUT=folder):
        (Path(folder) / 'output').mkdir()
        yield Path(folder)


class Fresh:
    def setUp(self):
        cs.invalidate()
        super().setUp()

    def tearDown(self):
        cs.invalidate()
        super().tearDown()


class SuffixSeedTests(Fresh, TestCase):
    def test_seed_loads_the_final_table_with_the_owner_confirmations(self):
        rows = CatalogCodeSuffix.objects
        self.assertEqual(rows.count(), 891)
        self.assertEqual({c: rows.filter(cls=c).count() for c in ('TAG', 'VARIANT', 'UNKNOWN')}, {'TAG': 548, 'VARIANT': 120, 'UNKNOWN': 223})
        self.assertEqual({s: rows.filter(status=s).count() for s in ('owner_confirmed', 'seed_confirmed', 'needs_owner_label', 'needs_owner_confirmation')},
                         {'owner_confirmed': 44, 'seed_confirmed': 403, 'needs_owner_label': 444, 'needs_owner_confirmation': 0})
        self.assertEqual(rows.filter(owner_confirmed=True).count(), 44)
        g, kit, jp, deleted = (rows.get(token=t) for t in ('G', 'KIT', 'JP', '_<ID>_DELETED'))
        self.assertEqual((g.cls, g.tag_kind, g.attribution, g.status), ('TAG', 'genuine', 'origin: OEM genuine', 'owner_confirmed'))
        self.assertTrue(g.auto_eligible and g.confirmed_at)
        self.assertEqual((kit.cls, kit.variant_kind, kit.owner_confirmed), ('VARIANT', 'kit', True))
        self.assertEqual(jp.alias_of.token, 'J')
        self.assertEqual(rows.get(token='ANULADA').alias_of.token, 'ANULADO')
        self.assertEqual((deleted.match, deleted.tag_kind, deleted.auto_eligible), ('shape', 'lifecycle', False))
        self.assertEqual(sum(r.auto_eligible for r in rows.all()), 300 + 39)
        self.assertFalse(any('run' in r.stats for r in rows.all()))
        self.assertEqual(rows.get(token='U').stats['count_all'], 56)
        audits = CatalogCodeSuffixChange.objects.filter(action='seed_confirm')
        self.assertEqual(audits.count(), 44)
        self.assertEqual(audits.get(token='G').before, {'status': 'needs_owner_confirmation', 'owner_confirmed': False})

    def test_every_save_takes_the_next_table_version_and_normalizes_the_token(self):
        top = CatalogCodeSuffix.objects.order_by('-version').first().version
        row = CatalogCodeSuffix.objects.get(token='U')
        row.notes = 'REVISAR'
        row.save(update_fields=['notes'])
        self.assertEqual(row.version, top + 1)
        new = CatalogCodeSuffix.objects.create(token='  zz  top ', cls='UNKNOWN')
        self.assertEqual((new.token, new.version), ('ZZ TOP', top + 2))

    def test_class_and_kind_must_agree(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            CatalogCodeSuffix.objects.create(token='QQQ', cls='TAG')
        with self.assertRaises(IntegrityError), transaction.atomic():
            CatalogCodeSuffix.objects.create(token='QQR', cls='UNKNOWN', variant_kind='size')


class SuffixServiceTests(Fresh, TestCase):
    def test_tokenizer_rebuilds_compounds_multiword_tokens_and_decimal_sizes(self):
        table = suffix_table()
        self.assertEqual(table.source, 'db')
        self.assertEqual(tokenize_chain('-NEW_ERA', table), [('-', 'NEW ERA')])
        self.assertEqual(tokenize_chain('-NEW_ERA'), [('-', 'NEW'), ('_', 'ERA')])
        self.assertEqual(tokenize_chain('-.25'), [('-', '0.25')])
        self.assertEqual(tokenize_chain('-SPK/WAP-NP', table), [('-', 'SPK/WAP'), ('-', 'NP')])
        self.assertEqual(tokenize_chain('-STD-TAIH'), [('-', 'STD'), ('-', 'TAIH')])
        self.assertEqual(tokenize_chain('-KSM-NP (1)'), [('-', 'KSM'), ('-', 'NP'), (' (', '1')])
        self.assertEqual(classify('-', '0.25').brief()['kind'], 'size')
        chain = classify_chain('-STD-TAIH', table)
        self.assertEqual([(c['entry'].cls, c['entry'].canon) for c in chain], [('VARIANT', 'STD'), ('TAG', 'TAIHO')])
        self.assertEqual(chain_summary(chain)['chain_class'], 'variant')
        chain = classify_chain('-KSM-NP', table)
        self.assertEqual(chain_summary(chain)['chain_class'], 'tag')
        self.assertTrue(all(c['entry'].auto_eligible for c in chain))
        compound = classify('-', 'SPK/WAP')
        self.assertEqual((compound.cls, compound.derived, compound.auto_eligible), ('TAG', False, True))
        derived = classify('-', 'KYB/NAK')
        self.assertEqual((derived.cls, derived.derived, [c.token for c in derived.components]), ('TAG', True, ['KYB', 'NAK']))

    def test_slash_numeric_tails_expand_into_alternate_codes(self):
        chain = classify_chain('/170-G', base_written='27060-16160')
        self.assertEqual(chain[0]['alt_code'], '27060-16170')
        self.assertEqual((chain[0]['entry'].cls, chain[0]['entry'].kind), ('VARIANT', 'alt_oem_tail'))
        summary = chain_summary(chain)
        self.assertEqual((summary['alt_codes'], summary['tags'], summary['chain_class']), (['27060-16170'], ['G'], 'variant'))
        full = classify_chain('/27060-16170', base_written='27060-16160', full_alternate=lambda rest: rest[:11])
        self.assertEqual([c['alt_code'] for c in full], ['27060-16170'])

    def test_scope_overrides_follow_the_description_head_noun(self):
        self.assertEqual(description_head('CASQUETE BIELA TOY 2KD STD'), ('CASQ', 'BIELA'))
        self.assertEqual(description_head('BUJE BIELA HYU'), ('BUJE', 'BIELA'))
        dai = lambda desc: classify('-', 'DAI', description_head(desc))
        self.assertEqual(dai('CASQUETE BIELA TOY').attribution, 'DAIDO METAL (engine bearings)')
        self.assertEqual(dai('BUJE BIELA HYU').attribution, 'DAIDO METAL (engine bearings)')
        self.assertEqual(dai('BASE MOTOR TOY').attribution, 'DAI (unidentified mount/rubber brand)')
        self.assertEqual(classify('-', 'HD', 'TACO').cls, 'TAG')
        self.assertEqual((classify('-', 'HD', ('AMORT', '')).cls, classify('-', 'HD').kind), ('VARIANT', 'spec'))
        self.assertEqual(classify('-', 'IN', description_head('BATERIA 12V')).attribution, 'INTERSTATE')
        self.assertEqual(classify('-', 'IN', 'BUJE').cls, 'UNKNOWN')
        self.assertEqual(classify('-', 'M', 'FUSIBLE').kind, 'descriptor')
        self.assertEqual(classify('-', 'M', 'EMP').kind, 'material')
        self.assertTrue(classify('-', 'DAI', 'CASQ').auto_eligible)

    def test_aliases_share_the_canonical_attribution(self):
        jp, heml = classify('-', 'JP'), classify('-', 'HEML')
        self.assertEqual((jp.canon, jp.label(), jp.auto_eligible), ('J', 'ORIGIN:JAPAN', True))
        self.assertEqual((heml.canon, heml.brand_name()), ('HELM', 'HELM'))
        self.assertEqual(classify('-', 'MANDO').label(), classify('-', 'MAND').label())

    def test_lifecycle_tokens_hold_the_part_and_are_never_auto_stripped(self):
        for code in ('58411-1R000-ANULADO', '58411-1R000 ANULADA1', '58411-1R000_1234_DELETED', '58411-1R000-AULADO-'):
            base, chain, chain_class = strip_tags(code)
            self.assertEqual((base, chain_class), ('58411-1R000', 'lifecycle'), code)
            self.assertFalse(chain[-1]['entry'].auto_eligible)
        self.assertEqual(classify('-', 'ANULADA').canon, 'ANULADO')

    def test_digit_codes_use_shape_rows(self):
        code = classify('-', 'PA16')
        self.assertEqual((code.token, code.cls, code.derived), ('CODE:-AA99', 'UNKNOWN', False))
        self.assertEqual(classify('', '7').token, 'CODE:(NONE)9')
        self.assertEqual(classify('-', '12AB34CD').token, 'CODE:99AA99AA')
        self.assertTrue(classify('-', '12AB34CD').derived)
        self.assertEqual(classify('-', 'ZQXW').brief(), {'token': 'ZQXW', 'class': 'UNKNOWN', 'kind': 'unseen', 'confidence': 'low',
                                                         'status': 'needs_owner_label', 'auto_eligible': False, 'derived': True})

    def test_strip_tags_returns_the_base_and_the_chain_class(self):
        cases = {'54650-D4000-MANDO': ('54650-D4000', 'tag'), '17100-M79M00-G': ('17100-M79M00', 'tag'),
                 '54500-07160-SPK/WAP': ('54500-07160', 'tag'), 'MD-325048-NEW_ERA': ('MD-325048', 'tag'),
                 '49500-2S610-EJE-WAP': ('49500-2S610', 'unknown'), '13011-75050-STD': ('13011-75050', 'variant'),
                 '58411-1R000': ('58411-1R000', 'bare'), 'mb-699175-raz-np': ('MB-699175', 'tag'), 'KIT': ('KIT', 'bare'),
                 '13041-75050-.25': ('13041-75050-.25', 'bare'), '': ('', 'bare')}
        for code, expected in cases.items():
            base, chain, chain_class = strip_tags(code)
            self.assertEqual((base, chain_class), expected, code)
        self.assertEqual([c['tok'] for c in strip_tags('MB-699175-RAZ-NP')[1]], ['RAZ', 'NP'])
        self.assertEqual(strip_tags('48610-0K040-HD', 'TACO')[2], 'tag')
        self.assertEqual(strip_tags('48610-0K040-HD', 'AMORT')[2], 'variant')

    def test_scenarios_confirm_tags_and_simulate_owner_labels(self):
        table = suffix_table()
        self.assertEqual(table.classify('-', 'U').cls, 'UNKNOWN')
        labeled = table.scenario(as_tag={'U'}).classify('-', 'U')
        self.assertEqual((labeled.cls, labeled.auto_eligible), ('TAG', True))
        low = CatalogCodeSuffix.objects.filter(cls='TAG', status='needs_owner_label').exclude(tag_kind='lifecycle').first()
        self.assertFalse(table.classify('-', low.token).auto_eligible)
        self.assertTrue(table.scenario(confirm={low.token}).classify('-', low.token).auto_eligible)

    def test_company_suffixes_keep_the_legacy_registry(self):
        self.assertEqual(company_suffixes(), LEGACY_COMPANY_SUFFIXES)
        self.assertEqual(SuffixTable.from_fixture().company_suffixes(), LEGACY_COMPANY_SUFFIXES)
        rows = cs.fixture_rows()
        self.assertEqual(SuffixTable(rows).company_suffixes(), LEGACY_COMPANY_SUFFIXES)
        self.assertEqual(SuffixTable([dict(r, creates_company_reference=False) if r['token'] == 'FEB' else r for r in rows]).company_suffixes(),
                         {'FEBEST': 'FEBEST'})

    def test_company_reference_behavior_is_unchanged(self):
        skus = ['0110-GSU45A48-FEB', '0110-gsu45a48-febest ', 'TY-1764-FEB-NP', 'FEB', '-FEB', '43460-TEST', '0229-T31-FEB-KIT',
                'ABC-123-MANDO', '54650-D4000-FEB', 'X-FEBESTO', 'KIT-FEB']
        for sku in skus:
            self.assertEqual(company_reference(sku), legacy_company_reference(sku), sku)
        with override_settings(CATALOG_COMPANY_SUFFIXES={'CUSTOM': 'acme '}):
            self.assertEqual(company_reference('ABC-1-CUSTOM'), {'code': 'ABC-1', 'brand': 'ACME', 'ref_type': 'company'})
            self.assertIsNone(company_reference('ABC-1-FEB'))

    def test_table_cache_is_keyed_by_version_and_refreshed_after_edits(self):
        first = suffix_table()
        self.assertIs(suffix_table(), first)
        row = CatalogCodeSuffix.objects.get(token='FEB')
        row.creates_company_reference = False
        row.save()
        self.assertEqual(company_suffixes(), {'FEBEST': 'FEBEST'})
        self.assertIsNot(suffix_table(), first)
        cached = suffix_table()
        # Another process edits the table: the version check (every CHECK_SECONDS) picks it up.
        CatalogCodeSuffix.objects.filter(token='FEB').update(creates_company_reference=True, version=F('version') + 100)
        self.assertIs(suffix_table(), cached)
        cs._cache['checked'] = float('-inf')
        self.assertEqual(company_suffixes(), LEGACY_COMPANY_SUFFIXES)
        self.assertEqual(suffix_table().version, CatalogCodeSuffix.objects.get(token='FEB').version)

    def test_missing_table_falls_back_to_the_fixture_and_legacy_registry(self):
        with patch.object(CatalogCodeSuffix._meta, 'db_table', 'mall_catalogcodesuffix_not_migrated'):
            cs.invalidate()
            table = suffix_table()
            self.assertEqual((table.source, len(table.rows)), ('fixture', 891))
            self.assertEqual(company_suffixes(), LEGACY_COMPANY_SUFFIXES)
            self.assertEqual(company_reference('0110-GSU45A48-FEB')['brand'], 'FEBEST')
            self.assertEqual(strip_tags('54650-D4000-MANDO')[2], 'tag')
            # The failed read ran in a savepoint: the surrounding transaction is still usable.
            self.assertEqual(Part.objects.count(), 0)
        cs.invalidate()
        self.assertEqual(suffix_table().source, 'db')

    def test_empty_table_also_uses_the_fixture(self):
        CatalogCodeSuffixChange.objects.all().delete()
        CatalogCodeSuffix.objects.update(alias_of=None)
        CatalogCodeSuffix.objects.all().delete()
        self.assertEqual(suffix_table().source, 'fixture')
        self.assertEqual(company_suffixes(), LEGACY_COMPANY_SUFFIXES)

    def test_reconcile_uses_company_codes_from_the_table(self):
        part = Part.objects.create(sku='ABC-123456-ACMECO', name='ABC-123456-ACMECO')
        CatalogCodeSuffix.objects.create(token='ACMECO', cls='TAG', tag_kind='company_code', attribution='brand: ACME CO',
                                         creates_company_reference=True, status='owner_confirmed', owner_confirmed=True)
        reconcile_identities()
        self.assertTrue(part.codes.filter(code='ABC-123456', brand='ACME CO', ref_type='company').exists())


class SuffixAPITests(Fresh, APITestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser('suffix-admin', 'suffix@example.invalid', 'Example938!')
        self.client.force_authenticate(self.admin)

    def row(self, token):
        return CatalogCodeSuffix.objects.get(token=token)

    def patch(self, token, payload, url=None):
        return self.client.patch(url or f'{BASE}{token}/', payload, format='json')

    def test_only_superusers_can_read_or_edit(self):
        staff = User.objects.create_user('suffix-staff', 'staff@example.invalid', 'Example938!')
        for user in (None, staff):
            self.client.force_authenticate(user)
            for response in (self.client.get(BASE), self.client.get(f'{BASE}G/'), self.patch('G', {'action': 'set_unknown', 'version': 1}),
                             self.client.patch(BASE, {}, format='json'), self.client.get('/api/v1/management/catalog/company-suffixes/')):
                self.assertIn(response.status_code, (401, 403))
        self.assertEqual(self.row('G').cls, 'TAG')

    def test_list_filters_counts_and_company_suffixes(self):
        data = self.client.get(BASE).data
        self.assertEqual((data['count'], len(data['results'])), (891, 50))
        self.assertEqual(data['results'][0]['token'], 'G')
        self.assertEqual(data['counts']['cls'], {'TAG': 548, 'VARIANT': 120, 'UNKNOWN': 223})
        self.assertEqual(data['counts']['status']['owner_confirmed'], 44)
        self.assertEqual(data['company_suffixes'], LEGACY_COMPANY_SUFFIXES)
        self.assertEqual(self.client.get(BASE, {'class': 'UNKNOWN'}).data['count'], 223)
        self.assertEqual(self.client.get(BASE, {'status': 'owner_confirmed', 'class': 'VARIANT'}).data['count'], 2)
        self.assertEqual({r['token'] for r in self.client.get(BASE, {'kind': 'company_code'}).data['results']}, {'FEB', 'FEBEST'})
        self.assertIn('HELM', [r['token'] for r in self.client.get(BASE, {'search': 'helm'}).data['results']])
        for bad in ({'class': 'BRAND'}, {'status': 'x'}, {'kind': 'x'}, {'has_unlock': 'si'}):
            self.assertEqual(self.client.get(BASE, bad).status_code, 400)
        self.assertEqual(self.client.get(BASE, {'has_unlock': 'true'}).data['count'], 0)
        for token, unlock in (('U', 24), ('EJE', 19), ('OR', 0)):
            row = self.row(token)
            row.stats = {**row.stats, 'run': {'unknown_unlock_if_labeled_TAG': {'skus_blocked_all': unlock + 5, 'unlock_alone_all': unlock}}}
            row.save()
        unlocked = self.client.get(BASE, {'has_unlock': 'true'}).data
        self.assertEqual([r['token'] for r in unlocked['results']], ['U', 'EJE'])
        self.assertEqual(unlocked['counts']['has_unlock'], 2)
        self.assertEqual(unlocked['results'][0]['stats']['run']['unknown_unlock_if_labeled_TAG']['skus_blocked_all'], 29)
        self.assertEqual(self.client.get(BASE, {'has_unlock': 'false'}).data['count'], 889)

    def test_detail_accepts_tokens_with_slashes_spaces_and_shapes(self):
        for path, token in (('SPK/WAP', 'SPK/WAP'), ('SPK%2FWAP', 'SPK/WAP'), ('NEW%20ERA', 'NEW ERA'), ('CODE%3A-AA99', 'CODE:-AA99'), ('_%3CID%3E_DELETED', '_<ID>_DELETED'),
                            ('CODE:-AA99', 'CODE:-AA99'), ('helm', 'HELM')):
            response = self.client.get(f'{BASE}{path}/')
            self.assertEqual((response.status_code, response.data['token']), (200, token), path)
        helm = self.client.get(f'{BASE}HELM/').data
        self.assertEqual(set(helm['aliases']), {'HELMET', 'HEM', 'HEML'})
        self.assertEqual(self.client.get(f'{BASE}G/').data['changes'][0]['action'], 'seed_confirm')
        self.assertEqual(self.client.get(f'{BASE}NOPE/').status_code, 404)

    def test_confirm_tag_labels_an_unknown_token_with_audit_and_version(self):
        before = self.row('U')
        response = self.patch('U', {'action': 'confirm_tag', 'version': before.version, 'note': 'Union Sangyo'})
        self.assertEqual(response.status_code, 200, response.data)
        row = self.row('U')
        self.assertEqual((row.cls, row.tag_kind, row.attribution, row.creates_company_reference, row.status, row.owner_confirmed),
                         ('TAG', 'brand', 'brand: U', True, 'owner_confirmed', True))
        self.assertEqual((row.confirmed_by, response.data['auto_eligible'], response.data['version']), (self.admin, True, row.version))
        self.assertGreater(row.version, before.version)
        change = row.changes.get()
        self.assertEqual((change.action, change.actor, change.note, change.version), ('confirm_tag', self.admin, 'Union Sangyo', row.version))
        self.assertEqual((change.before['cls'], change.after['cls'], change.after['owner_confirmed']), ('UNKNOWN', 'TAG', True))
        self.assertEqual(response.data['changes'][0]['action'], 'confirm_tag')
        self.assertEqual(classify('-', 'U').cls, 'TAG')
        # A retry with the stale version is a no-op, a different edit is a conflict.
        retry = self.patch('U', {'action': 'confirm_tag', 'version': before.version})
        self.assertEqual((retry.status_code, row.changes.count()), (200, 1))
        self.assertEqual(self.patch('U', {'action': 'set_variant', 'version': before.version}).status_code, 409)
        self.assertEqual(self.row('U').cls, 'TAG')

    def test_variant_unknown_and_kind_attribution_edits(self):
        row = self.row('OR')
        response = self.patch('OR', {'action': 'set_variant', 'version': row.version, 'variant_kind': 'material', 'attribution': 'O-ring'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['cls'], response.data['variant_kind'], response.data['attribution'], response.data['owner_confirmed']),
                         ('VARIANT', 'material', 'O-ring', True))
        response = self.patch('OR', {'action': 'set_unknown', 'version': response.data['version']})
        self.assertEqual((response.data['cls'], response.data['status'], response.data['owner_confirmed'], response.data['confirmed_by']),
                         ('UNKNOWN', 'needs_owner_label', False, None))
        feb = self.row('FEB')
        response = self.patch('FEB', {'action': 'confirm_tag', 'version': feb.version, 'tag_kind': 'brand', 'attribution': 'brand: FEBEST'})
        self.assertEqual((response.data['tag_kind'], response.data['creates_company_reference']), ('brand', True))
        self.assertEqual(company_suffixes(), {'FEBEST': 'FEBEST'})
        self.assertIsNone(company_reference('0110-GSU45A48-FEB'))
        for payload in ({'action': 'set_variant', 'creates_company_reference': True}, {'action': 'confirm_tag', 'tag_kind': 'origin', 'creates_company_reference': True},
                        {'action': 'confirm_tag', 'tag_kind': 'maker'}, {'action': 'rename'}):
            self.assertEqual(self.patch('K', {**payload, 'version': self.row('K').version}).status_code, 400, payload)
        self.assertEqual(self.patch('K', {'action': 'note', 'version': self.row('K').version, 'notes': 'KOREA'}).data['notes'], 'KOREA')
        self.assertEqual(self.row('K').status, 'owner_confirmed')

    def test_add_alias_copies_the_canonical_label(self):
        helm = self.row('HELM')
        response = self.patch('HELM', {'action': 'add_alias', 'version': helm.version, 'alias': 'helmm'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn('HELMM', response.data['aliases'])
        alias = self.row('HELMM')
        self.assertEqual((alias.alias_of, alias.cls, alias.tag_kind, alias.attribution, alias.owner_confirmed), (helm, 'TAG', 'brand', helm.attribution, True))
        self.assertEqual(classify('-', 'HELMM').canon, 'HELM')
        self.assertEqual(alias.changes.get().action, 'set_alias')
        self.assertEqual(helm.changes.get().after, {'alias': 'HELMM'})
        # Adding an alias through an alias attaches it to the canonical row; an existing unknown spelling is relabeled.
        response = self.patch('HEML', {'action': 'add_alias', 'version': self.row('HEML').version, 'alias': 'OR'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((self.row('OR').alias_of, self.row('OR').cls), (helm, 'TAG'))
        retry = self.patch('HELM', {'action': 'add_alias', 'version': helm.version, 'alias': 'HELMM'})
        self.assertEqual((retry.status_code, alias.changes.count()), (200, 1))
        for bad in ('HELM', '', 'CODE:-99', 'A<B', 'J'):
            self.assertEqual(self.patch('HELM', {'action': 'add_alias', 'version': self.row('HELM').version, 'alias': bad}).status_code, 400, bad)
        self.assertEqual(self.patch('CODE:-AA99', {'action': 'add_alias', 'version': self.row('CODE:-AA99').version, 'alias': 'PA'}).status_code, 400)

    def test_a_label_on_the_canonical_row_reaches_its_aliases(self):
        j = self.row('J')
        response = self.patch('J', {'action': 'set_variant', 'version': j.version, 'variant_kind': 'market'})
        self.assertEqual(response.status_code, 200, response.data)
        jp = self.row('JP')
        self.assertEqual((jp.cls, jp.variant_kind, jp.owner_confirmed, jp.alias_of.token), ('VARIANT', 'market', True, 'J'))
        self.assertEqual((jp.changes.first().action, jp.changes.first().note, jp.changes.first().before['cls']), ('set_variant', 'Alias de J', 'TAG'))
        self.assertFalse(classify('-', 'JP').auto_eligible)
        # A bulk label that also lists the alias stays a single audited change per row.
        anulado, anulada = self.row('ANULADO'), self.row('ANULADA')
        response = self.client.patch(BASE, {'action': 'set_unknown', 'rows': [{'token': 'ANULADO', 'version': anulado.version},
                                                                              {'token': 'ANULADA', 'version': anulada.version}]}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((self.row('ANULADA').cls, self.row('ANULADA').changes.filter(action='set_unknown').count()), ('UNKNOWN', 1))
        # Notes and scopes stay per row; an alias edited on its own does not move the canonical row.
        self.patch('JP', {'action': 'confirm_tag', 'version': self.row('JP').version, 'tag_kind': 'origin'})
        self.assertEqual((self.row('J').cls, self.row('JP').cls), ('VARIANT', 'TAG'))

    def test_uncertain_aliases_follow_a_class_change_but_never_a_confirmation(self):
        helm = self.row('HELM')
        self.assertEqual(self.patch('HELM', {'action': 'set_variant', 'version': helm.version}).status_code, 200)
        heml = self.row('HEML')
        self.assertEqual((heml.cls, heml.status, heml.owner_confirmed), ('VARIANT', 'needs_owner_label', False))
        self.assertEqual(self.patch('HELM', {'action': 'confirm_tag', 'version': self.row('HELM').version}).status_code, 200)
        heml = self.row('HEML')
        self.assertEqual((heml.cls, heml.tag_kind, heml.status, heml.owner_confirmed), ('TAG', 'brand', 'needs_owner_label', False))
        self.assertTrue(classify('-', 'HELM').auto_eligible)
        self.assertFalse(classify('-', 'HEML').auto_eligible)
        # An alias the owner adds is exactly as trusted as its root.
        low = CatalogCodeSuffix.objects.filter(cls='TAG', status='needs_owner_label', alias_of=None, match='exact').exclude(tag_kind='lifecycle').order_by('token').first()
        self.assertEqual(self.patch(low.token, {'action': 'add_alias', 'version': low.version, 'alias': 'QQZX'}).status_code, 200)
        alias = self.row('QQZX')
        self.assertEqual((alias.alias_of, alias.status, alias.owner_confirmed), (low, 'needs_owner_label', False))
        self.assertFalse(classify('-', 'QQZX').auto_eligible)

    def test_a_one_click_relabel_restores_the_previous_label_of_that_class(self):
        feb = self.row('FEB')
        self.assertEqual(self.patch('FEB', {'action': 'set_variant', 'version': feb.version}).status_code, 200)
        self.assertEqual(company_suffixes(), {'FEBEST': 'FEBEST'})
        self.assertEqual(self.patch('FEB', {'action': 'confirm_tag', 'version': self.row('FEB').version}).status_code, 200)
        feb = self.row('FEB')
        self.assertEqual((feb.tag_kind, feb.attribution, feb.creates_company_reference), ('company_code', 'brand: FEBEST', True))
        self.assertEqual(company_suffixes(), LEGACY_COMPANY_SUFFIXES)
        self.assertEqual(company_reference('0110-GSU45A48-FEB')['brand'], 'FEBEST')
        kit = self.row('KIT')
        self.assertEqual(self.patch('KIT', {'action': 'confirm_tag', 'version': kit.version}).status_code, 200)
        self.assertEqual(self.patch('KIT', {'action': 'set_variant', 'version': self.row('KIT').version}).status_code, 200)
        self.assertEqual((self.row('KIT').cls, self.row('KIT').variant_kind), ('VARIANT', 'kit'))
        # A company code always names its company: it is the brand of the references catalog_identity writes.
        response = self.patch('FEB', {'action': 'confirm_tag', 'version': self.row('FEB').version, 'tag_kind': 'company_code', 'attribution': ' '})
        self.assertEqual((response.status_code, self.row('FEB').attribution), (400, 'brand: FEBEST'))
        # Without an earlier label of that class the defaults apply.
        self.assertEqual(self.patch('OR', {'action': 'set_variant', 'version': self.row('OR').version}).data['variant_kind'], 'spec')

    def test_aliases_only_take_spellings_the_tokenizer_can_match(self):
        version = lambda token: self.row(token).version
        for bad in ('SPK-WAP', 'NEW_ERA', 'A.B', 'HELM2', '050', 'A B C'):
            response = self.patch('HELM', {'action': 'add_alias', 'version': version('HELM'), 'alias': bad})
            self.assertEqual(response.status_code, 400, bad)
        self.assertFalse(CatalogCodeSuffix.objects.filter(token__in=['SPK-WAP', 'HELM2']).exists())
        self.assertEqual((self.row('050').cls, self.row('050').alias_of), ('VARIANT', None))
        # An existing VARIANT is never silently turned into a TAG alias (it would become strippable).
        self.assertEqual(self.patch('HELM', {'action': 'add_alias', 'version': version('HELM'), 'alias': 'KIT'}).status_code, 400)
        self.assertEqual((self.row('KIT').cls, self.row('KIT').alias_of), ('VARIANT', None))
        for good in ('HELM NDKX', 'HLM/NDK'):
            self.assertEqual(self.patch('HELM', {'action': 'add_alias', 'version': version('HELM'), 'alias': good}).status_code, 200, good)
        self.assertEqual(tokenize_chain('-HELM_NDKX', suffix_table()), [('-', 'HELM NDKX')])
        self.assertEqual(classify('-', 'HELM NDKX').canon, 'HELM')
        self.assertEqual(self.patch('STD', {'action': 'add_alias', 'version': version('STD'), 'alias': '0.10'}).status_code, 200)

    def test_structural_rows_and_notes_are_guarded(self):
        tail = self.row('ALT_TAIL:/99')
        for payload in ({'action': 'confirm_tag'}, {'action': 'set_unknown'}, {'action': 'set_variant', 'variant_kind': 'spec'}):
            self.assertEqual(self.patch(tail.token, {**payload, 'version': tail.version}).status_code, 400, payload)
        self.assertEqual(self.row('ALT_TAIL:/99').cls, 'VARIANT')
        g = self.row('G')
        self.assertEqual(self.patch('G', {'action': 'note', 'version': g.version}).status_code, 400)
        self.assertEqual(self.row('G').notes, g.notes)

    def test_scope_heads_are_stored_as_classify_compares_them(self):
        row = self.row('TE')
        response = self.patch('TE', {'action': 'add_scope_override', 'version': row.version, 'heads': ['BOBINA', 'FILTRO GASOLINA', 'BUJE DE BIELA'],
                                     'cls': 'TAG', 'kind': 'brand', 'attribution': 'brand: TE'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['scope_overrides'][0]['heads'], ['COIL', 'FILTRO GAS', 'BUJE BIELA'])
        self.assertEqual(classify('-', 'TE', description_head('BOBINA ENCENDIDO HYU')).cls, 'TAG')
        self.assertEqual(classify('-', 'TE', description_head('FILTRO COMBUSTIBLE TOY')).cls, 'TAG')
        self.assertEqual(classify('-', 'TE', description_head('FILTRO ACEITE TOY')).cls, 'UNKNOWN')
        version = response.data['version']
        for heads in (['BUJE XYZ'], ['123'], ['FILTRO ACEITE MOTOR'], ['COIL']):
            self.assertEqual(self.patch('TE', {'action': 'add_scope_override', 'version': version, 'heads': heads, 'cls': 'VARIANT', 'kind': 'spec'}).status_code,
                             400, heads)

    def test_scope_overrides_can_be_added_and_removed(self):
        row = self.row('TE')
        self.assertEqual(classify('-', 'TE', 'COIL').cls, 'UNKNOWN')
        response = self.patch('TE', {'action': 'add_scope_override', 'version': row.version, 'heads': ['coil', 'BUJE  BIELA'],
                                     'cls': 'TAG', 'kind': 'brand', 'attribution': 'brand: TE'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['scope_overrides'], [{'heads': ['COIL', 'BUJE BIELA'], 'cls': 'TAG', 'kind': 'brand',
                                                              'attribution': 'brand: TE', 'confidence': 'high'}])
        self.assertEqual((classify('-', 'TE', 'COIL').cls, classify('-', 'TE', description_head('BOBINA HYU')).attribution), ('TAG', 'brand: TE'))
        self.assertEqual(classify('-', 'TE', 'BASE').cls, 'UNKNOWN')
        version = response.data['version']
        for payload in ({'heads': ['COIL'], 'cls': 'VARIANT', 'kind': 'spec'}, {'heads': ['BASE'], 'cls': 'TAG', 'kind': 'spec'},
                        {'heads': ['BASE'], 'cls': 'UNKNOWN', 'kind': 'brand'}, {'heads': [], 'cls': 'TAG', 'kind': 'brand'}):
            self.assertEqual(self.patch('TE', {'action': 'add_scope_override', 'version': version, **payload}).status_code, 400, payload)
        self.assertEqual(self.patch('CODE:-AA99', {'action': 'add_scope_override', 'version': self.row('CODE:-AA99').version,
                                                   'heads': ['RADIADOR'], 'cls': 'VARIANT', 'kind': 'spec'}).status_code, 400)
        self.assertEqual(self.patch('TE', {'action': 'remove_scope_override', 'version': version, 'index': 3}).status_code, 400)
        response = self.patch('TE', {'action': 'remove_scope_override', 'version': version, 'index': 0})
        self.assertEqual((response.status_code, response.data['scope_overrides']), (200, []))
        self.assertEqual(classify('-', 'TE', 'COIL').cls, 'UNKNOWN')
        self.assertEqual(list(self.row('TE').changes.values_list('action', flat=True)), ['remove_scope_override', 'add_scope_override'])

    def test_bulk_labels_are_atomic(self):
        u, eje = self.row('U'), self.row('EJE')
        response = self.client.patch(BASE, {'action': 'set_variant', 'variant_kind': 'descriptor',
                                            'rows': [{'token': 'u', 'version': u.version}, {'token': 'EJE', 'version': eje.version}]}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([(r['token'], r['cls'], r['variant_kind']) for r in response.data['results']],
                         [('EJE', 'VARIANT', 'descriptor'), ('U', 'VARIANT', 'descriptor')])
        self.assertEqual(CatalogCodeSuffixChange.objects.filter(action='set_variant').count(), 2)
        stale = self.client.patch(BASE, {'action': 'confirm_tag', 'rows': [{'token': 'OR', 'version': self.row('OR').version},
                                                                           {'token': 'U', 'version': u.version}]}, format='json')
        self.assertEqual(stale.status_code, 409)
        self.assertEqual((self.row('OR').cls, self.row('U').cls), ('UNKNOWN', 'VARIANT'))
        self.assertEqual(self.client.patch(BASE, {'action': 'confirm_tag', 'rows': [{'token': 'OR', 'version': 1}, {'token': 'or', 'version': 1}]},
                                           format='json').status_code, 400)
        self.assertEqual(self.client.patch(BASE, {'action': 'note', 'rows': [{'token': 'OR', 'version': 1}]}, format='json').status_code, 400)
        self.assertEqual(self.client.patch(BASE, {'action': 'confirm_tag', 'rows': [{'token': f'T{i}', 'version': 1} for i in range(101)]},
                                           format='json').status_code, 400)
        self.assertEqual(self.client.patch(BASE, {'action': 'confirm_tag', 'rows': [{'token': 'NOPE', 'version': 1}]}, format='json').status_code, 404)

    def test_company_suffix_registry_endpoint(self):
        url = '/api/v1/management/catalog/company-suffixes/'
        self.assertEqual(self.client.get(url).data, {'suffixes': LEGACY_COMPANY_SUFFIXES, 'registry': 'table'})
        with override_settings(CATALOG_COMPANY_SUFFIXES={'CUSTOM': 'acme'}):
            self.assertEqual(self.client.get(url).data, {'suffixes': {'CUSTOM': 'ACME'}, 'registry': 'settings'})
        with patch.object(CatalogCodeSuffix._meta, 'db_table', 'mall_catalogcodesuffix_not_migrated'):
            cs.invalidate()
            self.assertEqual(self.client.get(url).data, {'suffixes': LEGACY_COMPANY_SUFFIXES, 'registry': 'fallback'})
            self.assertEqual(self.client.get(BASE).status_code, 503)
            self.assertEqual(self.patch('G', {'action': 'set_unknown', 'version': 1}).status_code, 503)


class SuffixRevertTests(Fresh, APITestCase):
    """D4 (S0.1): the 45 position and size tokens relabelled as brands on 2026-10-07 go back to their labels, audited, once."""
    LABEL = ('cls', 'tag_kind', 'variant_kind', 'attribution', 'creates_company_reference', 'confidence', 'status', 'owner_confirmed',
             'scope_overrides', 'notes')

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser('miguel', 'miguel@example.invalid', 'Example938!')
        self.client.force_authenticate(self.admin)
        self.checkout = self.enterContext(scratch_checkout())

    def row(self, token):
        return CatalogCodeSuffix.objects.get(token=token)

    def run_revert(self, **kwargs):
        out = io.StringIO()
        return revert.run(stdout=out, **kwargs), out.getvalue()

    def manifest(self):
        return [json.loads(line) for line in revert.manifest_path().read_text().splitlines()]

    def add_alias(self, token, alias):
        """An alias added before the relabel night, as Sufijos does it."""
        row = self.row(token)
        response = self.client.patch(f'{BASE}{token}/', {'action': 'add_alias', 'version': row.version, 'alias': alias}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        CatalogCodeSuffixChange.objects.filter(action__in=('add_alias', 'set_alias')).update(created_at=RELABEL_NIGHT - timedelta(minutes=5))

    def test_the_owners_relabel_is_reverted_with_one_audit_row_per_token(self):
        rows = [{'token': t, 'version': self.row(t).version} for t in revert.TOKENS]
        response = self.client.patch(BASE, {'action': 'confirm_tag', 'rows': rows}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        CatalogCodeSuffixChange.objects.filter(action='seed_confirm').update(created_at=RELABEL_NIGHT - timedelta(hours=1))
        CatalogCodeSuffixChange.objects.filter(action='confirm_tag').update(created_at=RELABEL_NIGHT)
        self.assertEqual((self.row('STD').attribution, self.row('STD').creates_company_reference, classify('-', 'L').cls), ('brand: STD', True, 'TAG'))
        self.assertEqual(strip_tags('58411-07000-L')[2], 'tag')
        changes, top = CatalogCodeSuffixChange.objects.count(), CatalogCodeSuffix.objects.order_by('-version').first().version

        result, out = self.run_revert()
        self.assertEqual((result['expected'], result['revert'], result['already'], result['changed'], result['unaudited']), (45, 45, 0, 0, 0))
        self.assertIn('Simulación · D4: 45 sufijos esperados · 45 por revertir', out)
        self.assertIn('L: TAG brand → VARIANT position (seed_confirmed, high)', out)
        self.assertEqual((CatalogCodeSuffixChange.objects.count(), self.row('L').cls), (changes, 'TAG'))

        result, out = self.run_revert(apply=True, username='miguel')
        self.assertEqual((result['revert'], result['rows_written'], result['other_relabels']), (45, 45, []))
        fixture = {r['token']: r for r in cs.fixture_rows()}
        for token in revert.TOKENS:
            row = self.row(token)
            self.assertEqual({k: getattr(row, k) for k in self.LABEL}, {k: fixture[token][k] for k in self.LABEL}, token)
            self.assertIsNone(row.alias_of)
            audit = row.changes.order_by('-created_at', '-id').first()
            self.assertEqual((audit.action, audit.actor, audit.note, audit.version, audit.before['cls'], audit.after['cls']),
                             ('set_variant', self.admin, revert.NOTE, row.version, 'TAG', 'VARIANT'), token)
        self.assertEqual(CatalogCodeSuffixChange.objects.filter(action='set_variant').count(), 45)
        self.assertGreater(CatalogCodeSuffix.objects.order_by('-version').first().version, top)
        # The confirmation stamp comes back too: only M was owner-confirmed before (by the seed, not by a person).
        seeded = self.row('M').changes.get(action='seed_confirm')
        self.assertEqual((self.row('M').confirmed_by, self.row('M').confirmed_at), (None, seeded.created_at))
        self.assertFalse(CatalogCodeSuffix.objects.filter(token__in=revert.TOKENS, confirmed_by__isnull=False).exists())
        # The cache is invalidated in this process: position and size tails are variants again, never strippable brands.
        self.assertEqual((classify('-', 'L').cls, classify('-', 'STD').kind, strip_tags('58411-07000-L')[2]), ('VARIANT', 'size', 'variant'))
        self.assertEqual(company_suffixes(), LEGACY_COMPANY_SUFFIXES)
        # The manifest lives in the main checkout's output/ (here a temporary one), every line naming its database.
        self.assertEqual((revert.manifest_path(), result['manifest']), (self.checkout / 'output' / revert.MANIFEST, str(revert.manifest_path())))
        lines = self.manifest()
        self.assertEqual((len(lines), {line['database'] for line in lines}), (45, {connection.settings_dict['NAME']}))
        self.assertEqual((lines[0]['before']['cls'], lines[0]['after']['cls'], lines[0]['actor'], lines[0]['action']), ('TAG', 'VARIANT', 'miguel', 'update'))
        self.assertIn('Siguiente: python -m mall.link_oem_numbers --apply', out)

        changes, top = CatalogCodeSuffixChange.objects.count(), CatalogCodeSuffix.objects.order_by('-version').first().version
        again, _ = self.run_revert(apply=True, username='miguel')
        self.assertEqual((again['already'], again['revert'], again['rows_written']), (45, 0, 0))
        self.assertEqual((CatalogCodeSuffixChange.objects.count(), CatalogCodeSuffix.objects.order_by('-version').first().version), (changes, top))
        self.assertEqual(len(self.manifest()), 45)

    def test_tokens_edited_after_the_relabel_or_never_relabelled_are_left_alone(self):
        relabel_night(self.admin, [t for t in revert.TOKENS if t != 'XX'])
        l = self.row('L')
        self.assertEqual(self.patch_note('L', l.version).status_code, 200)
        result, out = self.run_revert(apply=True, username='miguel')
        self.assertEqual((result['revert'], result['changed'], result['unaudited']), (43, 1, 1))
        self.assertEqual((result['tokens']['changed'], result['tokens']['unaudited']), (['L'], ['XX']))
        self.assertIn('L: TAG brand · cambiados después del relabel', out)
        self.assertEqual((self.row('L').cls, self.row('L').notes), ('TAG', 'REVISAR'))
        self.assertEqual((self.row('XX').cls, self.row('RH').cls), ('VARIANT', 'VARIANT'))
        self.assertFalse(self.row('XX').changes.exists())

    def test_a_token_confirmed_before_without_an_audited_confirmation_is_left_alone(self):
        # RH was owner-confirmed before the relabel by a write that left no audit row: the relabel's own stamp is all the row has, so
        # the revert cannot give its confirmation back and leaves it for Sufijos.
        CatalogCodeSuffix.objects.filter(token='RH').update(owner_confirmed=True, status='owner_confirmed',
                                                            confirmed_at=RELABEL_NIGHT - timedelta(days=2))
        relabel_night(self.admin)
        result, out = self.run_revert(apply=True, username='miguel')
        self.assertEqual((result['revert'], result['unaudited'], result['tokens']['unaudited']), (44, 1, ['RH']))
        self.assertIn('RH: TAG brand · sin la auditoría del relabel', out)
        rh = self.row('RH')
        self.assertEqual((rh.cls, rh.confirmed_by), ('TAG', self.admin))
        self.assertFalse(rh.changes.filter(action='set_variant').exists())
        self.assertEqual(self.row('LH').cls, 'VARIANT')

    def patch_note(self, token, version):
        return self.client.patch(f'{BASE}{token}/', {'action': 'note', 'version': version, 'notes': 'REVISAR'}, format='json')

    def test_aliases_follow_their_token_back(self):
        self.add_alias('L', 'LFT')
        before = {k: getattr(self.row('LFT'), k) for k in self.LABEL}
        relabel_night(self.admin)
        self.assertEqual((self.row('LFT').cls, self.row('LFT').tag_kind, classify('-', 'LFT').auto_eligible), ('TAG', 'brand', True))
        # A read-only analysis before the apply sees the alias as the apply leaves it.
        memory = {r['token']: r for r in revert.reverted_rows(cs._db_rows())}['LFT']
        result, _ = self.run_revert(apply=True, username='miguel')
        self.assertEqual((result['revert'], result['rows_written']), (45, 46))
        alias = self.row('LFT')
        self.assertEqual(({k: getattr(alias, k) for k in self.LABEL}, alias.alias_of.token), (before, 'L'))
        self.assertEqual({k: memory[k] for k in self.LABEL}, before)
        self.assertEqual((alias.cls, alias.variant_kind), ('VARIANT', 'position'))
        self.assertEqual(alias.changes.order_by('-created_at', '-id').first().note, 'Alias de L')
        self.assertFalse(classify('-', 'LFT').auto_eligible)

    def test_an_apply_on_the_live_database_needs_the_freeze_and_an_author(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL only: the live database is the PostgreSQL one')
        relabel_night(self.admin)
        folder = self.checkout / '.maintenance'
        folder.mkdir()
        with override_settings(LIVE_DATABASE_NAME=connection.settings_dict['NAME'], MAINTENANCE_DIR=str(folder)):
            result, out = self.run_revert(apply=True, username='miguel')
            self.assertIsNone(result)
            self.assertIn('sin el congelamiento del catálogo', out)
            self.assertIsNone(self.run_revert(rollback=True, username='miguel')[0])
            self.assertEqual(self.run_revert()[0]['revert'], 45)  # a dry run never needs it
            (folder / 'freeze').write_text('catalog\n')
            self.assertIsNone(self.run_revert(apply=True)[0])
            self.assertIsNone(self.run_revert(apply=True, username='nadie')[0])
            self.assertFalse(CatalogCodeSuffixChange.objects.filter(action='set_variant').exists())
            with override_settings(MAIN_CHECKOUT=str(self.checkout / 'missing')):
                result, out = self.run_revert(apply=True, username='miguel')
                self.assertIsNone(result)
                self.assertIn('Falta la carpeta', out)
            self.assertEqual(self.run_revert(apply=True, username='miguel')[0]['revert'], 45)
        self.assertEqual(self.row('RH').cls, 'VARIANT')

    def test_the_manifest_is_written_before_the_commit(self):
        relabel_night(self.admin)
        top = CatalogCodeSuffix.objects.order_by('-version').first().version
        with patch.object(revert, 'write_manifest', side_effect=OSError(28, 'No space left on device')), self.assertRaises(OSError):
            self.run_revert(apply=True, username='miguel')
        # A failed write rolls every row back: no revert without its manifest.
        self.assertEqual((self.row('RH').cls, CatalogCodeSuffix.objects.order_by('-version').first().version), ('TAG', top))
        self.assertFalse(CatalogCodeSuffixChange.objects.filter(action='set_variant').exists())
        self.assertFalse(revert.manifest_path().exists())

    def test_a_manifest_with_another_databases_lines_is_refused(self):
        relabel_night(self.admin)
        path = revert.manifest_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'stage': 's0.1', 'database': 'test_partsmall_s0sfx1', 'id': 1, 'token': 'RH', 'action': 'update'}) + '\n')
        result, out = self.run_revert(apply=True, username='miguel')
        self.assertIsNone(result)
        self.assertIn('tiene líneas de otra base (test_partsmall_s0sfx1)', out)
        self.assertIsNone(self.run_revert(rollback=True, username='miguel')[0])
        self.assertIn('Aviso: El manifiesto', self.run_revert()[1])
        self.assertEqual((self.row('RH').cls, len(path.read_text().splitlines())), ('TAG', 1))
        self.assertFalse(CatalogCodeSuffixChange.objects.filter(action='set_variant').exists())
        path.unlink()
        self.assertEqual(self.run_revert(apply=True, username='miguel')[0]['revert'], 45)

    def test_rollback_puts_back_what_the_apply_changed(self):
        self.add_alias('L', 'LFT')
        relabel_night(self.admin)
        tokens = [*revert.TOKENS, 'LFT']
        before = {t: revert.recorded(revert.stamped(self.row(t))) for t in tokens}
        self.assertEqual(self.run_revert(apply=True, username='miguel')[0]['rows_written'], 46)
        self.assertEqual(self.patch_note('XX', self.row('XX').version).status_code, 200)  # edited after the apply: left alone

        result, out = self.run_revert(rollback=True, username='miguel')
        self.assertEqual((result['rows'], result['rollback'], result['already'], result['changed'], result['tokens']['changed']),
                         (46, 45, 0, 1, ['XX']))
        self.assertIn('Rollback · S0.1: 46 filas aplicadas en esta base · 45 por deshacer', out)
        self.assertIn('XX: cambiados después de aplicar, se deja como está', out)
        # Every other row is back as the relabel left it, confirmation stamp included.
        for token in tokens:
            if token != 'XX':
                self.assertEqual(revert.recorded(revert.stamped(self.row(token))), before[token], token)
        self.assertEqual((self.row('XX').cls, self.row('XX').notes), ('VARIANT', 'REVISAR'))
        audit = self.row('RH').changes.order_by('-created_at', '-id').first()
        self.assertEqual((audit.action, audit.note, audit.actor, audit.before['cls'], audit.after['cls']),
                         ('confirm_tag', revert.ROLLBACK_NOTE, self.admin, 'VARIANT', 'TAG'))
        self.assertEqual(CatalogCodeSuffixChange.objects.filter(note=revert.ROLLBACK_NOTE).count(), 45)
        self.assertEqual((classify('-', 'RH').cls, classify('-', 'LFT').auto_eligible), ('TAG', True))
        self.assertEqual([line['action'] for line in self.manifest()].count('rollback'), 45)

        again, _ = self.run_revert(rollback=True, username='miguel')
        self.assertEqual((again['rollback'], again['already'], again['changed'], again['rows_written']), (0, 45, 1, 0))
        # The rollback's own audit rows are not edits: the tokens can be reverted again, the edited one aside.
        result, _ = self.run_revert(apply=True, username='miguel')
        self.assertEqual((result['revert'], result['already'], result['changed'], result['rows_written']), (44, 0, 1, 45))
        self.assertEqual((self.row('RH').cls, self.row('LFT').cls), ('VARIANT', 'VARIANT'))

    def test_command_line(self):
        relabel_night(self.admin, ['RH'])
        Part.objects.create(sku='48654-60040-RH', name='48654-60040-RH', description='BASE AMORT TOY PRADO RH')
        out = io.StringIO()
        with patch.object(oem_finder, 'load_snapshot', wraps=oem_finder.load_snapshot) as loads:
            self.assertEqual(revert.main(['--dry-run', '--finder', '--json'], stdout=out), 0)
        self.assertEqual(loads.call_count, 1)  # both finder passes grade one catalog read
        self.assertIn('1 por revertir', out.getvalue())
        self.assertIn('Buscador OEM (solo lectura), SKU por nivel hoy → con D4:', out.getvalue())
        result = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(result['tokens']['revert'], ['RH'])
        # The finder preview grades the one SKU twice, with RH as a brand and as a position, and stores nothing.
        self.assertEqual([sum(n[i] for n in result['finder'].values()) for i in (0, 1)], [1, 1])
        now, after = ([tier for tier, n in result['finder'].items() if n[i]] for i in (0, 1))
        self.assertNotEqual(now, after)
        self.assertFalse(OEMFinderRun.objects.exists())
        # A read-only analysis sees the table as D4 leaves it before the apply.
        rows = {r['token']: r for r in revert.reverted_rows(cs._db_rows())}
        self.assertEqual((self.row('RH').cls, rows['RH']['cls'], rows['RH']['variant_kind'], rows['RH']['owner_confirmed']), ('TAG', 'VARIANT', 'position', False))
        self.assertEqual(SuffixTable(list(rows.values())).classify('-', 'RH').cls, 'VARIANT')
        self.assertEqual(revert.main(['--apply'], stdout=io.StringIO()), 1)
        self.assertEqual(revert.main(['--apply', '--user', 'miguel'], stdout=io.StringIO()), 0)
        self.assertEqual(self.row('RH').cls, 'VARIANT')
        self.assertEqual(revert.main(['--rollback'], stdout=io.StringIO()), 1)
        self.assertEqual(revert.main(['--rollback', '--user', 'miguel'], stdout=io.StringIO()), 0)
        self.assertEqual(self.row('RH').cls, 'TAG')
