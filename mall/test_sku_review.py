import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from .category_suggestions import taxonomy
from .catalog_classification import ClassificationProviderError
from .matching_engine import item_row, save_case
from .models import Account, Part, PartCode, Role, SupplierItem, User
from .oem_reference_models import OEMReference
from .quote_assistant_models import AIUsageRecord
from .sku_proposal_models import SkuProposal
from .sku_proposals import approve, edit, propose
from .sku_review import DOUBTFUL_OEM, guarded, typo_fix
from .sku_review_models import SkuReviewRun, SkuReviewVerdict

ENABLED = dict(GEMINI_API_KEY='test-key-not-real', SKU_REVIEW_MONTHLY_USD='10', AI_MONTHLY_BUDGET_USD='200', CATALOG_EXTRA_MAKES=['KAIYI'])
USAGE = {'input_tokens': 1200, 'output_tokens': 300, 'thinking_tokens': 0, 'total_tokens': 1500}


def answer(i, **values):
    return {'i': i, 'v': 'good', 'motivo': '', 't': -1, 'ct': 0, 'd': '', 'cd': 0, 'oem': True, 'co': 90, 'dup': -1, 'c': 90,
            'razones': ['Repuesto con datos completos.'], 'nota': '', **values}


class FakeAI:
    """Answers by SKU, whatever order the batch sends them in; records what it was sent."""

    def __init__(self, by_sku=None, change=None):
        self.by_sku, self.change, self.calls = by_sku or {}, change, []

    def __call__(self, payload, count, **options):
        data = json.loads(payload['contents'][0]['parts'][0]['text'])
        self.calls.append(data)
        if self.change:
            self.change()
        reviews = [answer(row['i'], **self.by_sku.get(row['sku'], {})) for row in data['articulos']]
        return {'reviews': reviews}, USAGE


def type_index(category, name):
    return taxonomy().index((category, name))


@override_settings(**ENABLED)
class SkuReviewTests(APITestCase):
    def setUp(self):
        self.root = User.objects.create_superuser('root', 'root@example.com', 'SecureRoot938!')
        self.dealer = self.supplier('Motion Motors')
        self.other = self.supplier('Repuestos Chinos')
        self.client.force_authenticate(self.root)

    def supplier(self, name):
        account = Account.objects.create(name=name)
        account.roles.add(Role.objects.get(code='supplier_wholesale'))
        return account

    def item(self, account, invent_id, codigo, brand, description='', case='unmatched', **fields):
        item = SupplierItem.objects.create(supplier=account, supplier_invent_id=invent_id, codigo=codigo, brand=brand, description=description,
                                           source='apiag', **fields)
        if case:
            save_case(key=f'supplier:{item.pk}', kind='supplier', sources=[item_row(item)], candidates=[], item=item, status=case)
        return item

    def proposal(self, sku):
        return SkuProposal.objects.get(sku=sku)

    def start(self, **data):
        response = self.client.post('/api/v1/management/catalog/proposals/review/', {'id': str(uuid.uuid4()), **data}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def batch(self, run, index=0, expect=200, **params):
        response = self.client.post(f'/api/v1/management/catalog/proposals/review/{run["id"]}/', {'mode': 'review', 'batch_index': index}, format='json')
        self.assertEqual(response.status_code, expect, response.content)
        return response.json()

    def verdict(self, sku):
        return SkuReviewVerdict.objects.get(proposal__sku=sku)

    def test_rules_settle_known_codes_no_uso_and_placeholder_data_without_the_ai(self):
        target = Part.objects.create(sku='A-1', name='A-1')
        self.item(self.dealer, '1', 'A-1', 'ACME', 'PIEZA A')                          # the catalog already has it
        self.item(self.dealer, '2', 'B-2', 'ACME', 'NO USO FILTRO DE CABINA')
        self.item(self.dealer, '3', 'FX11-1234567-01', '', 'SIN DESCRIPCION')          # nothing to recover it from
        self.item(self.dealer, '4', 'FX11-6207118-01A', '', 'MAL CREADO')
        self.item(self.other, 'S1', 'F02-6207118-01AA', 'KAIYI', 'MOLDURA KAIYI', case=None)  # a sibling does not rescue a mistyped code
        twin = Part.objects.create(sku='TWIN-A', name='TWIN-A')
        PartCode.objects.create(part=twin, code='TW-1', brand='ACME')
        Part.objects.create(sku='TW1', name='TW1')                                  # the same code, spelled as another SKU
        self.item(self.dealer, '5', 'TW-1', 'ACME', 'PIEZA EN DOS SKU')
        propose()
        with patch('mall.sku_review.generate_json') as ai:
            report = self.batch(self.start())
        ai.assert_not_called()
        self.assertEqual((report['status'], report['counts']['bad'], report['counts']['link'], report['counts']['reject']), ('completed', 5, 1, 3))
        known, unused, filler = self.verdict('A-1'), self.verdict('B-2'), self.verdict('FX11-1234567-01')
        self.assertEqual((known.method, known.verdict, known.reason, known.link_part), ('rule', 'bad', 'duplicate', target))
        self.assertEqual((unused.verdict, unused.reason), ('bad', 'other'))
        self.assertEqual((filler.verdict, filler.reason), ('bad', 'bad_data'))
        self.assertEqual((self.verdict('FX11-6207118-01A').verdict, self.verdict('FX11-6207118-01A').reason), ('bad', 'bad_data'))
        twins = self.verdict('TW-1')
        self.assertEqual((twins.verdict, twins.reason, twins.link_part), ('bad', 'duplicate', None))   # a person chooses which SKU
        self.assertIn('TW1, TWIN-A', twins.reasons[0])
        self.assertEqual(self.proposal('A-1').version, known.proposal_version)  # rules write nothing

    def test_the_ai_reviews_and_fills_only_what_it_may(self):
        lights = type_index('CARROCERÍA', 'FAROS')
        self.item(self.dealer, '1', 'F02-4121010-01AA', 'KAIYI', 'FARO DELANTERO IZQUIERDO KAIYI X3')
        self.item(self.dealer, '2', 'KYJP000484', 'KAIYI', 'GORRA KAIYI')
        self.item(self.dealer, '3', 'VP-100', 'ACME', 'VIDIRO PUERTA DELANTERA')
        self.item(self.dealer, '4', 'LOW-1', 'ACME', 'PIEZA RARA')
        self.item(self.dealer, '5', 'FAR-1', 'ACME', 'BOMBA DE AGUA')
        self.item(self.dealer, '6', 'FX11-3501116-01AA', '', 'SIN DESCRIPCION')        # placeholder, but a sibling knows the number
        self.item(self.other, 'S1', 'F02-3501116-01AA', 'KAIYI', 'TACOS DE FRENO DELANTERO KAIYI FX12', case=None)
        self.item(self.dealer, '7', 'ADM-1', 'ACME', 'DESCRIPCION MAL ESCRTA')
        for index in range(3):  # our own descriptions: the vocabulary a typo fix must land on
            Part.objects.create(sku=f'SEED-{index}', description='VIDRIO PUERTA DELANTERA BOMBA AGUA')
        self.item(self.dealer, '8', 'DEF-1', 'ACME', 'DEFELCTOR RADIADOR LH')
        self.item(self.dealer, '11', 'COV-1', 'ACME', 'COVER INTERNO L/H COMPUERTA DFKS')
        self.item(self.dealer, '9', 'KYJP000999', 'KAIYI', 'CAMARA DE REVERSA KAIYI')
        self.item(self.dealer, '10', 'URT-22', 'MOTION MOTORS', 'ESCOBILLA 22 URTTEX')
        propose()
        admin = self.proposal('ADM-1')
        edit(admin.pk, self.root, admin.version, {'description': 'BOMBA AGAU'})   # the administrator's own typo
        before = {proposal.sku: (proposal.version, proposal.updated_at) for proposal in SkuProposal.objects.all()}
        summary_before = self.client.get('/api/v1/management/catalog/proposals/summary/').json()['pending_changed_at']
        fake = FakeAI({
            'F02-4121010-01AA': {'t': lights, 'ct': 85, 'nota': 'FARO en la descripción'},
            'KYJP000484': {'oem': False, 'razones': ['Mercancía de la marca: se vende, pero su código no es un número OEM.']},
            'VP-100': {'d': 'VIDRIO PUERTA DELANTERA', 'cd': 90, 'c': 85, 'nota': 'VIDIRO es VIDRIO'},
            'LOW-1': {'c': 60},
            'FAR-1': {'d': 'BOMBA DE AGUA TOYOTA HILUX 2015 ORIGINAL', 'cd': 95},          # a rewrite, not a typo fix
            'FX11-3501116-01AA': {'d': 'TACOS DE FRENO DELANTERO KAIYI FX11', 'cd': 90, 't': type_index('FRENOS', 'PASTILLAS DE FRENO'), 'ct': 50},
            'ADM-1': {'d': 'BOMBA AGUA', 'cd': 95},                                          # a real fix, but the field is the administrator's
            'DEF-1': {'d': 'DEFLECTOR RADIADOR RH', 'cd': 95},                               # fixes the typo but swaps the side: dropped
            'COV-1': {'d': 'COVER EXTERNO R/H COMPUERTA DFSK', 'cd': 95},                    # INTERNO → EXTERNO and L/H → R/H: dropped
            'KYJP000999': {'oem': False, 'co': 60},                                            # not sure enough to take its OEM number away
        })
        with patch('mall.sku_review.generate_json', side_effect=fake):
            report = self.batch(self.start())
        self.assertEqual(len(fake.calls), 1)
        sent = {row['sku']: row for row in fake.calls[0]['articulos']}
        self.assertTrue(sent['KYJP000484']['es_marca_de_vehiculo'])
        self.assertEqual(sent['FX11-3501116-01AA']['hermanos'][0]['codigo'], 'F02-3501116-01AA')
        self.assertIn('descripcion_de_relleno', sent['FX11-3501116-01AA']['senales'])
        self.assertEqual(fake.calls[0]['tipos'][lights], [lights, 'CARROCERÍA', 'FAROS'])

        lamp = self.proposal('F02-4121010-01AA')
        self.assertEqual((lamp.part_type.category, lamp.part_type.name, lamp.version), ('CARROCERÍA', 'FAROS', before[lamp.sku][0] + 1))
        self.assertEqual({k: lamp.field_sources['part_type'][k] for k in ('source', 'confidence', 'note')}, {'source': 'ai', 'confidence': 0.85, 'note': 'FARO en la descripción'})
        self.assertEqual(lamp.updated_at, before[lamp.sku][1])        # the storefront reminder does not move for the AI's fills
        self.assertEqual(self.verdict(lamp.sku).proposal_version, lamp.version)
        self.assertEqual(self.verdict(lamp.sku).verdict, 'good')
        cap = self.proposal('KYJP000484')
        self.assertEqual((cap.internal_code, cap.oem_code, cap.field_sources['internal_code']['source'], cap.field_sources['oem']['source']), (True, '', 'ai', 'ai'))
        glass = self.proposal('VP-100')
        self.assertEqual((glass.description, glass.field_sources['description']['source']), ('VIDRIO PUERTA DELANTERA', 'ai'))
        self.assertEqual(self.verdict('LOW-1').verdict, 'unsure')     # a good verdict needs 80
        far = self.proposal('FAR-1')
        self.assertEqual((far.description, far.version, self.verdict('FAR-1').suggestions.get('description')), ('BOMBA DE AGUA', before['FAR-1'][0], None))
        drafted = self.proposal('FX11-3501116-01AA')
        verdict = self.verdict(drafted.sku)
        self.assertEqual((verdict.verdict, drafted.description, drafted.part_type), ('unsure', 'SIN DESCRIPCION', None))  # shown, never filled
        self.assertEqual((verdict.suggestions['description']['value'], verdict.suggestions['description']['drafted'], verdict.suggestions['part_type']['confidence']),
                         ('TACOS DE FRENO DELANTERO KAIYI FX11', True, 0.5))
        self.assertEqual((self.proposal('DEF-1').description, self.verdict('DEF-1').suggestions.get('description')), ('DEFELCTOR RADIADOR LH', None))
        self.assertEqual(self.proposal('COV-1').description, 'COVER INTERNO L/H COMPUERTA DFKS')
        camera = self.proposal('KYJP000999')
        self.assertEqual((camera.internal_code, camera.oem_code, self.verdict('KYJP000999').suggestions['internal_code']), (False, 'KYJP000999', {'confidence': 0.6}))
        self.assertEqual(self.verdict('KYJP000999').verdict, 'unsure')   # approving would file a doubtful code as the make's OEM number
        self.assertIn('marca_es_el_proveedor', sent['URT-22']['senales'])   # the AI weighs it; the owner approves the supplier's own brand
        self.assertEqual(cap.field_sources['internal_code']['note'], 'Mercancía de la marca: se vende, pero su código no es un número OEM.')
        kept = self.proposal('ADM-1')
        self.assertEqual((kept.description, self.verdict('ADM-1').applied['kept']), ('BOMBA AGAU', ['description']))
        self.assertEqual(self.client.get('/api/v1/management/catalog/proposals/summary/').json()['pending_changed_at'], summary_before)
        self.assertEqual((report['counts']['good'], report['counts']['unsure']), (8, 3))
        self.assertEqual((report['metrics']['ai_items'], report['metrics']['input_tokens'], report['metrics']['ai_calls']), (11, 1200, 1))
        # The supplier rewrites the description the AI corrected: the supplier's new text replaces the fix.
        item = SupplierItem.objects.get(codigo='VP-100')
        SupplierItem.objects.filter(pk=item.pk).update(description='NO USO VIDIRO PUERTA', updated_at=timezone.now() + timedelta(seconds=1))
        propose()
        self.assertEqual((self.proposal('VP-100').description, self.proposal('VP-100').field_sources['description']['source']), ('NO USO VIDIRO PUERTA', 'supplier'))

    def test_one_catalog_owner_and_a_different_earlier_sku_leave_the_choice_to_a_person(self):
        owner = Part.objects.create(sku='OWN-1', name='OWN-1')
        earlier = Part.objects.create(sku='EARLY-1', name='EARLY-1')
        PartCode.objects.create(part=owner, code='ZZ-100', brand='ACME')
        self.item(self.dealer, '1', 'ZZ-100', 'ACME', 'PIEZA')
        propose()
        proposal = self.proposal('ZZ-100')
        SkuProposal.objects.create(key=proposal.key, status='linked', sku='ZZ-100', part=earlier, decided_at=timezone.now(), fingerprint='x')
        with patch('mall.sku_review.generate_json') as ai:
            self.batch(self.start())
        ai.assert_not_called()
        verdict = self.verdict('ZZ-100') if SkuReviewVerdict.objects.filter(proposal=proposal).exists() else None
        self.assertEqual((verdict.verdict, verdict.link_part), ('bad', None))
        self.assertIn('EARLY-1', verdict.reasons[0])

    def test_a_verdict_saved_stale_does_not_supersede_the_current_one(self):
        self.item(self.dealer, '1', 'P-1', 'ACME', 'BOMBA')
        propose()
        with patch('mall.sku_review.generate_json', side_effect=FakeAI()):
            first = self.batch(self.start())
        proposal = self.proposal('P-1')
        other = SkuReviewRun.objects.create(id=uuid.uuid4(), created_by=self.root, proposal_ids=[str(proposal.pk)])
        SkuReviewVerdict.objects.create(run=other, proposal=proposal, position=0, proposal_version=proposal.version - 1, method='ai', verdict='bad',
                                        reason='other', applied={'skipped': 'changed'})
        self.assertEqual(self.client.get(f'/api/v1/management/catalog/proposals/review/{first["id"]}/').json()['counts']['good'], 1)
        listed = self.client.get('/api/v1/management/catalog/proposals/').json()['results'][0]
        self.assertEqual((listed['ai_review']['verdict'], listed['ai_review']['stale']), ('good', False))

    def test_the_ai_never_links_and_a_bad_verdict_fills_nothing(self):
        self.item(self.dealer, '1', '6107111-FS01', 'DFSK', 'MOLDURA PUERTA')
        self.item(self.dealer, '2', '6107111-FS01-01', 'DFSK', 'MOLDURA PUERTA')
        self.item(self.dealer, '3', 'JUNK-1', 'ACME', 'XXXXX ?? 123')
        self.item(self.dealer, '4', 'MEH-1', 'ACME', 'PIEZA RARA')
        propose()
        fake = FakeAI({'6107111-FS01-01': {'dup': 0, 'v': 'bad', 'motivo': 'duplicate'}, 'MEH-1': {'v': 'bad', 'motivo': 'other', 'c': 50},
                       'JUNK-1': {'v': 'bad', 'motivo': 'bad_data', 'd': 'XXXXX 123', 'cd': 99, 't': 0, 'ct': 99}})
        with patch('mall.sku_review.generate_json', side_effect=fake):
            self.batch(self.start())
        variant = self.verdict('6107111-FS01-01')
        self.assertEqual((variant.verdict, variant.reason, variant.link_part, variant.suggestions['duplicate_of']['sku']), ('unsure', '', None, '6107111-FS01'))
        junk = self.verdict('JUNK-1')
        self.assertEqual((junk.verdict, junk.reason, junk.applied), ('bad', 'bad_data', {}))
        self.assertEqual((self.proposal('JUNK-1').description, self.proposal('JUNK-1').part_type), ('XXXXX ?? 123', None))
        self.assertEqual((self.verdict('MEH-1').verdict, self.verdict('MEH-1').reason), ('unsure', ''))

    def test_an_invalid_answer_saves_nothing_and_frees_the_batch(self):
        self.item(self.dealer, '1', 'A-1', 'ACME', 'PIEZA A')
        propose()
        run = self.start()
        for broken in [{'reviews': []}, {'reviews': [answer(0, v='great')]}, {'reviews': [answer(0, v='bad')]},
                       {'reviews': [answer(0, razones=[])]}, {'reviews': [answer(0), answer(0)]}, {'other': []}]:
            with patch('mall.sku_review.generate_json', return_value=(broken, USAGE)):
                self.batch(run, expect=502)
            saved = SkuReviewRun.objects.get(pk=run['id'])
            self.assertEqual((saved.completed_batches, saved.claim_id, saved.estimated_cost_micro_usd, saved.verdicts.count()), (0, None, 0, 0))
        with patch('mall.sku_review.generate_json', side_effect=ClassificationProviderError('La IA tardó demasiado en responder.')):
            self.batch(run, expect=502)
        with patch('mall.sku_review.generate_json', side_effect=FakeAI()):
            self.assertEqual(self.batch(run)['counts']['good'], 1)

    def test_one_unreadable_row_becomes_a_dudosa_and_the_rest_is_saved(self):
        for index in range(3):
            self.item(self.dealer, str(index), f'P-{index}', 'ACME', f'BOMBA {index}')
        propose()
        run = self.start()

        def answers(payload, count, **options):
            rows = json.loads(payload['contents'][0]['parts'][0]['text'])['articulos']
            odd = next(row['i'] for row in rows if row['sku'] == 'P-1')
            return {'reviews': [answer(row['i'], t=10_000) if row['sku'] == 'P-0' else answer(row['i'], v='maybe') if row['i'] == odd else answer(row['i'])
                                for row in rows]}, USAGE

        with patch('mall.sku_review.generate_json', side_effect=answers):
            report = self.batch(run)
        self.assertEqual((report['counts']['good'], report['counts']['unsure']), (2, 1))
        self.assertEqual(self.verdict('P-0').suggestions.get('part_type'), None)               # an index out of range is no type
        self.assertIn('no dio una respuesta válida', self.verdict('P-1').reasons[0])

    def test_a_newer_run_supersedes_an_older_ones_verdicts(self):
        for index in range(2):
            self.item(self.dealer, str(index), f'P-{index}', 'ACME', f'BOMBA {index}')
        propose()
        with patch('mall.sku_review.BATCH_SIZE', 1):
            first = self.start()
        with patch('mall.sku_review.generate_json', side_effect=FakeAI()):
            self.batch(first, 0)
        with patch('mall.sku_review.generate_json', side_effect=FakeAI({'P-0': {'v': 'unsure', 'c': 40}, 'P-1': {'v': 'unsure', 'c': 40}})):
            second = self.batch(self.start(only_unreviewed=False))
        self.assertEqual(second['counts']['unsure'], 2)
        with patch('mall.sku_review.generate_json') as ai:
            resumed = self.batch(first, 1)                          # P-1 was reviewed by the newer run since: not again
        ai.assert_not_called()
        self.assertEqual((resumed['counts']['good'], resumed['counts']['stale'], SkuReviewVerdict.objects.filter(run_id=first['id']).count()), (0, 1, 1))
        url = f'/api/v1/management/catalog/proposals/review/{first["id"]}/?mode=actionable&verdict=good'
        self.assertEqual(self.client.get(url).json()['count'], 0)

    def test_a_proposal_that_changes_while_the_ai_reads_it_gets_a_stale_verdict_and_no_fill(self):
        item = self.item(self.dealer, '1', 'VP-100', 'ACME', 'VIDIRO PUERTA')
        propose()

        def supplier_changes():
            SupplierItem.objects.filter(pk=item.pk).update(description='VIDRIO PUERTA TRASERA', updated_at=timezone.now() + timedelta(seconds=1))
            propose()

        with patch('mall.sku_review.generate_json', side_effect=FakeAI({'VP-100': {'d': 'VIDRIO PUERTA', 'cd': 95}}, change=supplier_changes)):
            report = self.batch(self.start())
        proposal, verdict = self.proposal('VP-100'), self.verdict('VP-100')
        self.assertEqual((proposal.description, verdict.applied, report['counts']['stale']), ('VIDRIO PUERTA TRASERA', {'skipped': 'changed'}, 1))
        self.assertNotEqual(verdict.proposal_version, proposal.version)
        self.assertEqual(self.client.get(f'/api/v1/management/catalog/proposals/review/{report["id"]}/?mode=actionable&verdict=good').json()['count'], 0)

    def test_the_monthly_cap_refuses_before_calling_and_spend_is_recorded(self):
        self.item(self.dealer, '1', 'A-1', 'ACME', 'PIEZA A')
        propose()
        run = self.start()
        with override_settings(SKU_REVIEW_MONTHLY_USD='0'), patch('mall.sku_review.generate_json') as ai:
            self.assertIn('desactivada', self.batch(run, expect=403)['detail'])
        ai.assert_not_called()
        AIUsageRecord.objects.create(account=None, feature='sku_review', day=timezone.localdate(), cost_micro_usd=9_999_000)
        with patch('mall.sku_review.generate_json') as ai:
            self.assertIn('límite', self.batch(run, expect=429)['detail'])
        ai.assert_not_called()
        self.assertIsNone(SkuReviewRun.objects.get(pk=run['id']).claim_id)
        AIUsageRecord.objects.all().delete()
        provider = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': json.dumps({'reviews': [answer(0)]})}]}}],
                    'usageMetadata': {'promptTokenCount': 1000, 'candidatesTokenCount': 200}}

        class Answer:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return json.dumps(provider).encode()

        with patch('mall.catalog_classification.urlopen', return_value=Answer()):
            self.batch(run)
        usage = AIUsageRecord.objects.get(feature='sku_review')
        self.assertEqual((usage.calls, usage.input_tokens, usage.output_tokens), (1, 1000, 200))

    def test_runs_go_batch_by_batch_and_are_idempotent(self):
        for index in range(3):
            self.item(self.dealer, str(index), f'P-{index}', 'ACME', f'PIEZA {index}')
        propose()
        with patch('mall.sku_review.BATCH_SIZE', 2):
            run = self.start()
        self.assertEqual((run['total_batches'], run['status']), (2, 'ready'))
        self.batch(run, 1, expect=400)
        with patch('mall.sku_review.generate_json', side_effect=FakeAI()):
            self.batch(run, 0)
            self.batch(run, 0)                                  # a replay of a saved batch reviews nothing again
            done = self.batch(run, 1)
        self.assertEqual((done['status'], SkuReviewVerdict.objects.count()), ('completed', 3))
        SkuReviewRun.objects.filter(pk=run['id']).update(completed_batches=1, claim_id=uuid.uuid4(), claimed_at=timezone.now())
        self.assertEqual(self.batch(run, 1, expect=409)['detail'], 'Un lote de esta revisión está en curso. Espera a que termine para reanudar.')
        again = self.client.post('/api/v1/management/catalog/proposals/review/', {'id': run['id']}, format='json')
        self.assertEqual((again.status_code, again.json()['id']), (200, run['id']))
        clash = self.client.post('/api/v1/management/catalog/proposals/review/', {'id': run['id'], 'search': 'OTRA'}, format='json')
        self.assertEqual(clash.status_code, 409)
        nothing = self.client.post('/api/v1/management/catalog/proposals/review/', {'id': str(uuid.uuid4())}, format='json')
        self.assertEqual(nothing.status_code, 400)                 # every pending proposal has a verdict that still holds
        overview = self.client.get('/api/v1/management/catalog/proposals/review/').json()
        self.assertEqual((overview['eligible_count'], overview['unreviewed_count'], overview['configured'], overview['cap_micro_usd']), (3, 0, True, 10_000_000))
        member = User.objects.create_user('member', 'member@example.com', 'SecureMember938!')
        self.client.force_authenticate(member)
        self.assertEqual(self.client.get('/api/v1/management/catalog/proposals/review/').status_code, 403)
        self.assertEqual(self.client.get(f'/api/v1/management/catalog/proposals/review/{run["id"]}/').status_code, 403)

    def test_the_report_feeds_batch_approve_reject_and_link(self):
        target = Part.objects.create(sku='OLD-1', name='OLD-1')
        self.item(self.dealer, '1', 'OLD-1', 'ACME', 'PIEZA CONOCIDA')
        self.item(self.dealer, '2', 'GOOD-1', 'ACME', 'BOMBA DE AGUA')
        self.item(self.dealer, '3', 'GOOD-2', 'KAIYI', 'LLAVERO KAIYI')
        self.item(self.dealer, '4', 'BAD-1', 'ACME', 'ESTO NO ES NADA')
        self.item(self.dealer, '5', 'MAYBE-1', 'ACME', 'PIEZA DUDOSA')
        propose()
        fake = FakeAI({'GOOD-2': {'oem': False}, 'BAD-1': {'v': 'bad', 'motivo': 'not_a_part', 'razones': ['No describe un producto.']},
                       'MAYBE-1': {'v': 'unsure', 'c': 40}})
        with patch('mall.sku_review.generate_json', side_effect=fake):
            run = self.batch(self.start())
        self.assertEqual(run['counts'], {'good': 2, 'bad': 2, 'unsure': 1, 'stale': 0, 'decided': 0, 'link': 1, 'reject': 1})
        page = self.client.get(f'/api/v1/management/catalog/proposals/review/{run["id"]}/?verdict=bad').json()
        self.assertEqual(sorted(row['current']['sku'] for row in page['results']), ['BAD-1', 'OLD-1'])
        url = f'/api/v1/management/catalog/proposals/review/{run["id"]}/?mode=actionable&verdict='
        good, bad, links = (self.client.get(url + wanted).json()['results'] for wanted in ('good', 'bad', 'link'))
        self.assertEqual(sorted(row['sku'] for row in good), ['GOOD-1', 'GOOD-2'])
        self.assertEqual({row['sku']: (row['internal_code'], row['oem']) for row in good}, {'GOOD-1': (False, None), 'GOOD-2': (True, None)})
        self.assertEqual([(row['sku'], row['reason']) for row in bad], [('BAD-1', 'not_a_part')])
        self.assertEqual([(row['sku'], row['part']) for row in links], [('OLD-1', str(target.pk))])
        bulk = '/api/v1/management/catalog/proposals/bulk/'
        approved = self.client.post(bulk, {'action': 'approve', 'proposals': [{'id': row['id'], 'version': row['version']} for row in good]}, format='json').json()
        self.assertEqual((approved['done'], approved['approved']), (2, 2))
        keyring = Part.objects.get(sku='GOOD-2')
        self.assertFalse(keyring.is_OEM)                                    # an internal code is no OEM number
        self.assertEqual(list(PartCode.objects.filter(part=keyring).values_list('code', 'brand', 'ref_type')), [('GOOD-2', 'KAIYI', 'company')])
        self.assertFalse(OEMReference.objects.filter(code='GOOD2').exists())
        rejected = self.client.post(bulk, {'action': 'reject', 'proposals': [{'id': row['id'], 'version': row['version'], 'reason': row['reason'], 'note': row['note']} for row in bad]
                                           + [{'id': str(uuid.uuid4()), 'version': 1, 'reason': 'other'}, {'id': 'nope', 'version': 1}, 'x']}, format='json').json()
        self.assertEqual([result['ok'] for result in rejected['results']], [True, False, False, False])
        refused = self.proposal('BAD-1')
        self.assertEqual((refused.status, refused.reject_reason, refused.reject_note), ('rejected', 'not_a_part', 'Revisión IA: No describe un producto.'))
        linked = self.client.post(bulk, {'action': 'link', 'proposals': [{'id': row['id'], 'version': row['version'], 'part': row['part']} for row in links]}, format='json').json()
        self.assertEqual((linked['done'], self.proposal('OLD-1').status, self.proposal('OLD-1').part), (1, 'linked', target))
        after = self.client.get(f'/api/v1/management/catalog/proposals/review/{run["id"]}/?verdict=decided').json()
        self.assertEqual((after['counts']['decided'], after['counts']['unsure']), (4, 1))
        self.assertEqual(self.client.post(bulk, {'action': 'delete', 'proposals': [{'id': row['id'], 'version': 1} for row in good]}, format='json').status_code, 400)

    def test_the_grid_shows_and_filters_the_latest_verdict(self):
        self.item(self.dealer, '1', 'GOOD-1', 'ACME', 'BOMBA DE AGUA')
        self.item(self.dealer, '2', 'BAD-1', 'ACME', 'NADA')
        self.item(self.dealer, '3', 'NEW-1', 'ACME', 'PIEZA NUEVA')
        propose()
        with patch('mall.sku_review.generate_json', side_effect=FakeAI({'BAD-1': {'v': 'bad', 'motivo': 'bad_data'}})):
            self.batch(self.start())
        self.item(self.dealer, '4', 'LATE-1', 'ACME', 'PIEZA TARDIA')
        propose()
        url = '/api/v1/management/catalog/proposals/?ai='
        self.assertEqual(sorted(row['sku'] for row in self.client.get(url + 'good').json()['results']), ['GOOD-1', 'NEW-1'])
        self.assertEqual(sorted(row['sku'] for row in self.client.get(url + 'none').json()['results']), ['LATE-1'])
        listed = {row['sku']: row for row in self.client.get('/api/v1/management/catalog/proposals/').json()['results']}
        self.assertEqual((listed['BAD-1']['ai_review']['verdict'], listed['BAD-1']['ai_review']['stale'], listed['LATE-1']['ai_review']), ('bad', False, None))
        good = self.proposal('GOOD-1')
        edit(good.pk, self.root, good.version, {'description': 'BOMBA DE AGUA TOYOTA'})
        self.assertEqual([row['sku'] for row in self.client.get(url + 'stale').json()['results']], ['GOOD-1'])
        self.assertEqual([row['sku'] for row in self.client.get(url + 'good').json()['results']], ['NEW-1'])
        self.assertEqual(self.client.get(url + 'maybe').status_code, 400)

    def test_an_internal_code_survives_the_suppliers_refresh_and_an_administrator_can_set_it(self):
        item = self.item(self.dealer, '1', 'PELU-KY', 'KAIYI', 'OSITO DE PELUCHE KAIYI')
        propose()
        proposal = self.proposal('PELU-KY')
        self.assertEqual(proposal.oem_code, 'PELU-KY')
        edit(proposal.pk, self.root, proposal.version, {'internal_code': True})
        proposal = self.proposal('PELU-KY')
        self.assertEqual((proposal.internal_code, proposal.oem_code, proposal.field_sources['internal_code']['source']), (True, '', 'admin'))
        SupplierItem.objects.filter(pk=item.pk).update(description='OSITO PELUCHE KAIYI GRANDE', updated_at=timezone.now() + timedelta(seconds=1))
        propose()
        proposal = self.proposal('PELU-KY')
        self.assertEqual((proposal.internal_code, proposal.oem_code, proposal.description), (True, '', 'OSITO PELUCHE KAIYI GRANDE'))
        with self.assertRaises(Exception):
            edit(proposal.pk, self.root, proposal.version, {'internal_code': 'yes'})
        edit(proposal.pk, self.root, proposal.version, {'internal_code': False})
        restored = self.proposal('PELU-KY')
        self.assertEqual((restored.internal_code, restored.oem_code, restored.oem_brand, restored.field_sources['oem']), (False, 'PELU-KY', 'KAIYI', {'source': 'supplier'}))
        edit(restored.pk, self.root, restored.version, {'internal_code': True})
        proposal = self.proposal('PELU-KY')
        approve(proposal.pk, self.root, proposal.version)
        part = self.proposal('PELU-KY').part
        reference = OEMReference.objects.create(manufacturer='TOYOTA', code='1610039315', printed_forms=['16100-39315'])
        from .oem_reference_models import OEMCrossReference
        OEMCrossReference.objects.create(reference=reference, brand='GMB', code='GWT-41A', number='GWT41A')
        self.item(self.dealer, '3', 'GWT-41A', 'GMB', 'BOMBA DE AGUA')
        propose()
        pump = self.proposal('GWT-41A')
        self.assertEqual((pump.oem_code, pump.field_sources['oem']['source']), ('16100-39315', 'oem_library'))
        response = self.client.patch(f'/api/v1/management/catalog/proposals/{pump.pk}/', {'expected_version': pump.version, 'internal_code': True, 'oem_code': 'X-1', 'oem_brand': 'GMB'}, format='json')
        self.assertEqual(response.status_code, 400)
        edit(pump.pk, self.root, pump.version, {'internal_code': True, 'oem_code': '', 'oem_brand': ''})   # as the drawer sends it
        pump = self.proposal('GWT-41A')
        self.assertEqual((pump.internal_code, pump.oem_code), (True, ''))
        edit(pump.pk, self.root, pump.version, {'internal_code': False})
        pump = self.proposal('GWT-41A')
        self.assertEqual((pump.oem_code, pump.oem_brand, pump.field_sources['oem']['source']), ('16100-39315', 'TOYOTA', 'oem_library'))
        self.assertEqual((part.is_OEM, list(part.codes.values_list('ref_type', flat=True))), (False, ['company']))
        regular = self.item(self.dealer, '2', 'F02-1234567-01AA', 'KAIYI', 'PARASOL KAIYI')
        propose()
        normal = self.proposal('F02-1234567-01AA')
        approve(normal.pk, self.root, normal.version)
        self.assertTrue(self.proposal('F02-1234567-01AA').part.is_OEM)        # a real dealer code stays the make's OEM number
        self.assertEqual(regular.pk, self.proposal('F02-1234567-01AA').items.get().item_id)

    def test_typo_fixes_only_turn_a_misspelling_into_a_real_word(self):
        known = {word: 5 for word in ['VIDRIO', 'PUERTA', 'DEFLECTOR', 'RADIADOR', 'SELLADOR', 'VALVULA', 'INTERIORES', 'EXTERIORES', 'SUPERIORES',
                                      'INFERIORES', 'INTERNO', 'EXTERNO', 'FILTRO', 'ACEITE', 'AIRE', 'BOMBA', 'AGUA', 'LH', 'RH', 'DE', 'FARO', 'ESPEJO']}
        for old, new in [('VIDIRO PUERTA', 'VIDRIO PUERTA'), ('DEFELCTOR RADIADOR LH', 'DEFLECTOR RADIADOR LH'), ('SELLAD DE VALVULA', 'SELLADOR DE VALVULA')]:
            self.assertTrue(typo_fix(old, new, known), (old, new))
        for old, new in [('DEFLECTOR LH', 'DEFLECTOR RH'), ('FARO L/H', 'FARO R/H'), ('VIDIRO PUERTA-LH', 'VIDRIO PUERTA-RH'),
                         ('MOLDURA DELANTERO/IZQ', 'MOLDURA DELANTERO/DER'), ('GUARDAPOLVO INTERNO', 'GUARDAPOLVO EXTERNO'),
                         ('BUJES SUPERIORES', 'BUJES INFERIORES'), ('AMORTIGUADORES INTERIORES', 'AMORTIGUADORES EXTERIORES'),
                         ('ESPEJO LHS', 'ESPEJO RHS'), ('PUERTALH', 'PUERTARH'), ('RODAMIENTO 6203', 'RODAMIENTO 6205'), ('FILTRO ACEITE', 'FILTRO AIRE'),
                         ('BOMBA AGUA', 'BOMBA DE AGUA'), ('PUERTA-LH', 'PUERTA LH'), ('VIDIRIOOO PUERTA', 'VIDRIO PUERTA')]:
            self.assertFalse(typo_fix(old, new, known), (old, new))

    def test_a_doubtful_internal_code_keeps_its_reason_and_an_administrators_oem_field_stops_a_good_verdict(self):
        row = {'senales': [], 'candidatos': [], 'hermanos': [], 'descripcion': 'CAMARA', 'es_marca_de_vehiculo': True, '_largo': 6}
        result = guarded(answer(0, oem=False, co=60, razones=['a', 'b', 'c']), row, taxonomy(), {})
        self.assertEqual((result['verdict'], result['reasons']), ('unsure', ['a', 'b', DOUBTFUL_OEM]))
        self.item(self.dealer, '1', 'KYJP0001', 'KAIYI', 'GORRA KAIYI')
        propose()
        cap = self.proposal('KYJP0001')
        edit(cap.pk, self.root, cap.version, {'oem_code': '', 'oem_brand': ''})     # the administrator owns the OEM field now
        with patch('mall.sku_review.generate_json', side_effect=FakeAI({'KYJP0001': {'oem': False, 'co': 95}})):
            self.batch(self.start())
        verdict = self.verdict('KYJP0001')
        self.assertEqual((verdict.verdict, verdict.applied['kept'], self.proposal('KYJP0001').internal_code), ('unsure', ['internal_code'], False))

    def test_an_administrators_oem_comes_back_as_theirs_and_a_second_fix_keeps_the_suppliers_text(self):
        self.item(self.dealer, '1', 'ACM-77', 'ACME', 'BOMBA DE AGUA')
        propose()
        pump = self.proposal('ACM-77')
        edit(pump.pk, self.root, pump.version, {'oem_code': '16100-39315', 'oem_brand': 'TOYOTA'})
        pump = self.proposal('ACM-77')
        edit(pump.pk, self.root, pump.version, {'internal_code': True})
        pump = self.proposal('ACM-77')
        edit(pump.pk, self.root, pump.version, {'internal_code': False})
        pump = self.proposal('ACM-77')
        self.assertEqual((pump.oem_code, pump.field_sources['oem']['source'], pump.field_sources['oem']['by']), ('16100-39315', 'admin', 'root'))
        from .sku_proposals import ai_fill
        self.item(self.dealer, '2', 'VP-1', 'ACME', 'VIDIRO PUERTA DELANTRA')
        propose()
        glass = self.proposal('VP-1')
        ai_fill(glass, {'description': ('VIDRIO PUERTA DELANTRA', 0.9)}, note='', run_id=uuid.uuid4(), model='m')
        ai_fill(glass, {'description': ('VIDRIO PUERTA DELANTERA', 0.9)}, note='', run_id=uuid.uuid4(), model='m')
        self.assertEqual(self.proposal('VP-1').field_sources['description']['from'], 'VIDIRO PUERTA DELANTRA')
        item = SupplierItem.objects.get(codigo='VP-1')
        SupplierItem.objects.filter(pk=item.pk).update(references=[{'code': 'X-1', 'brand': 'ACME'}], updated_at=timezone.now() + timedelta(seconds=1))
        propose()                                                 # the supplier changed something else: the fix stays
        self.assertEqual(self.proposal('VP-1').description, 'VIDRIO PUERTA DELANTERA')

