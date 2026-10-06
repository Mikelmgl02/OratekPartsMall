import json
import uuid
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from .category_suggestions import classify_categories, local_category, category_provider, taxonomy
from .catalog_assistant_models import CatalogAssistantJob, CategorySuggestionCache
from .catalog_classification import ClassificationProviderError
from .models import Part, PartType, User, CatalogMerge


def row(sku='TEST', description='TACO HYU ACCENT 12-16', **extra):
    return dict(sku=sku, description=description, name=sku, category='', subcategory='', **extra)


@override_settings(GEMINI_API_KEY='test-key')
class CategoryEngineTests(TestCase):
    def test_anchored_rules_distinguish_accessories_services_and_non_brake_pastillas(self):
        for description, expected in [
            ('TACO HYU ACCENT 06-10', 'PASTILLAS DE FRENO'),
            ('PASTILLA DE FRENO TOY', 'PASTILLAS DE FRENO'),
            ('CLIP TACO HYU ACCENT', 'ACCESORIOS DE PASTILLAS'),
            ('DISCO FRENO HYU', 'DISCOS DE FRENO'),
            ('DISCO CLUTCH TOY', 'DISCOS DE EMBRAGUE'),
            ('FILTRO DIESEL', 'FILTROS DE COMBUSTIBLE'),
            ('BASE AMORT HYU', 'BASES DE AMORTIGUADOR'),
            ('AMORT HYU', 'AMORTIGUADORES'),
            ('BIELA MIT 4G63', 'BIELAS'),
            ('CASQ BIELA MIT 4G63', 'COJINETES DE BIELA'),
            ('RETENEDORA RDA TRAS INT TOY', 'RETENES DE RUEDA'),
            ('BALINERA TRAS TOY', 'RODAMIENTOS DE RUEDA'),
            ('PASTILLAS TOY 2L 3L 5L 1KZ 2.9MM', None),
            ('PASTILLA DE AVION DESODORANTE', None),
            ('TACO DE MOTOR', None), ('TACO GOMA', None),
            ('CAMBIO DE TACO 4X4', None), ('KIT REP BOMBA AGUA', None),
            ('SOPORTE FILTRO AIRE', None), ('BASE TOY', None),
        ]:
            with self.subTest(description=description):
                value=local_category(row(description=description))
                self.assertEqual(value[1] if value else None,expected)

    def test_local_category_never_calls_provider_or_merges(self):
        with patch('mall.category_suggestions.category_provider') as provider:
            results, metrics=classify_categories([row(),row(sku='OTHER')])
        provider.assert_not_called()
        self.assertEqual(metrics['rule_items'],2)
        self.assertEqual(metrics['ai_calls'],0)
        self.assertEqual([r['target_sku'] for r in results],['TEST','OTHER'])

    def provider(self, rows, choices, instructions):
        return {i:{'category':'MOTOR','subcategory':'BASES DE MOTOR','confidence':.94} for i in range(len(rows))},dict(total_tokens=123,input_tokens=100,output_tokens=23)

    def test_deduplicates_full_descriptions_and_reuses_durable_results(self):
        rows=[row('A','SOPORTE DE MOTOR TOY COROLLA LH'),row('B','SOPORTE DE MOTOR TOY COROLLA LH'),row('C','SOPORTE DE MOTOR TOY COROLLA RH')]
        with patch('mall.category_suggestions.category_provider',side_effect=self.provider) as provider:
            results, metrics=classify_categories(rows)
        self.assertEqual(len(provider.call_args.args[0]),2)
        self.assertEqual(metrics['deduplicated_items'],1)
        self.assertEqual(metrics['total_tokens'],123)
        self.assertEqual(CategorySuggestionCache.objects.count(),2)
        with patch('mall.category_suggestions.category_provider') as provider:
            repeated, metrics=classify_categories(rows)
        provider.assert_not_called()
        self.assertEqual(metrics['cached_items'],3)
        self.assertEqual(repeated[0]['category'],results[0]['category'])

    def test_model_instructions_and_taxonomy_invalidate_cache_and_custom_instructions_bypass_rules(self):
        with patch('mall.category_suggestions.category_provider',side_effect=self.provider) as provider:
            classify_categories([row()],instructions='MI CLASIFICACION')
            self.assertEqual(provider.call_count,1)
            classify_categories([row()],instructions='MI CLASIFICACION')
            self.assertEqual(provider.call_count,1)
            classify_categories([row()],instructions='OTRA CLASIFICACION')
            self.assertEqual(provider.call_count,2)
            with override_settings(GEMINI_MODEL='different-model'):
                classify_categories([row()],instructions='MI CLASIFICACION')
            self.assertEqual(provider.call_count,3)
            PartType.objects.create(category='OTRO',name='CUSTOM')
            classify_categories([row()],instructions='MI CLASIFICACION')
            self.assertEqual(provider.call_count,4)

    def test_no_description_and_no_key_leave_unknowns_for_review(self):
        with override_settings(GEMINI_API_KEY=''),patch('mall.category_suggestions.category_provider') as provider:
            results,metrics=classify_categories([row(),row('B',''),row('C','UNKNOWN')])
        provider.assert_not_called()
        self.assertEqual(metrics['rule_items'],1)
        self.assertEqual(metrics['review_items'],2)
        self.assertEqual(results[1]['subcategory'],'')

    def test_strict_compact_provider_validation(self):
        choices=[('MOTOR','BASES DE MOTOR')]
        for output in [{'assignments':[]},{'assignments':[{'i':0,'t':30,'c':99}]},
                       {'assignments':[{'i':0,'t':0,'c':True}]},
                       {'assignments':[{'i':0,'t':0,'c':90},{'i':0,'t':0,'c':90}]}]:
            with self.subTest(output=output),patch('mall.category_suggestions.generate_json',return_value=(output,{})):
                with self.assertRaises(ClassificationProviderError):
                    category_provider([[0,'BASE TOY','','']],choices,'')
        with patch('mall.category_suggestions.generate_json',return_value=({'assignments':[{'i':0,'t':-1,'c':99}]},{})) as transport:
            results,_=category_provider([[0,'UNKNOWN','','']],choices,'')
        self.assertEqual(results[0]['confidence'],0)
        self.assertEqual(transport.call_args.kwargs['feature'],'category_suggestions')
        sent=json.loads(transport.call_args.args[0]['contents'][0]['parts'][0]['text'])
        self.assertNotIn('candidates',sent)
        self.assertEqual(sent['articulos'],[[0,'UNKNOWN','','']])

    def test_uncertain_results_are_not_cached_and_conflicting_manual_category_is_not_overwritten_by_rule(self):
        with patch('mall.category_suggestions.category_provider',return_value=({0:{'category':'MOTOR','subcategory':'BASES DE MOTOR','confidence':.5}},{})):
            classify_categories([row('A','BASE TOY')])
        self.assertFalse(CategorySuggestionCache.objects.exists())
        value=row();value['category']='MANUAL'
        self.assertIsNone(local_category(value))

    def test_opaque_abbreviations_cannot_enter_bulk_approval_even_with_high_model_confidence(self):
        with patch('mall.category_suggestions.category_provider',side_effect=self.provider):
            results,_=classify_categories([row('A','BASE MIT 4M40'),row('B','BUJE V NIS B12 INF')])
        self.assertTrue(all(r['confidence'] < .85 for r in results))
        self.assertFalse(CategorySuggestionCache.objects.exists())


@override_settings(GEMINI_API_KEY='')
class FastCategoryApiTests(APITestCase):
    url='/api/v1/management/catalog/assistant/'

    def setUp(self):
        self.root=User.objects.create_superuser('categories-root','categories@example.invalid','Test12345!')
        self.client.force_authenticate(self.root)
        Part.objects.bulk_create([Part(sku=f'PAD-{i:04}',description='TACO HYU ACCENT 06-10') for i in range(205)])

    def start(self):
        result=self.client.post(self.url,{'id':str(uuid.uuid4()),'task':'categories','scope':'unclassified'},format='json')
        self.assertEqual(result.status_code,201,result.data)
        return result.data

    def action(self,job,**data):
        return self.client.post(f'{self.url}{job["id"]}/',data,format='json')

    def test_bounded_reads_fast_batches_replay_bulk_approval_and_part_types(self):
        job=self.start()
        self.assertEqual((job['batch_size'],job['total_batches']),(200,2))
        with CaptureQueriesContext(connection) as queries,patch('mall.catalog_assistant.candidate_context') as candidates:
            first=self.action(job,mode='classify',batch_index=0)
        candidates.assert_not_called()
        self.assertEqual(first.status_code,200,first.data)
        part_reads=[q['sql'] for q in queries.captured_queries if 'FROM "mall_part"' in q['sql']]
        self.assertTrue(part_reads)
        self.assertTrue(all(' IN ' in sql for sql in part_reads),part_reads)
        self.assertEqual(first.data['count'],200)
        self.assertEqual(len(first.data['results']),50)
        self.assertEqual(first.data['metrics']['rule_items'],200)
        self.assertEqual(self.action(job,mode='classify',batch_index=0).data['count'],200)
        last=self.action(job,mode='classify',batch_index=1)
        self.assertEqual(last.data['offset'],200)
        self.assertEqual(last.data['count'],205)
        self.assertFalse(Part.objects.exclude(category='').exists())
        for expected in [200,5,0]:
            with CaptureQueriesContext(connection) as queries:
                applied=self.action(job,mode='apply_category',category='FRENOS',subcategory='PASTILLAS DE FRENO')
            self.assertEqual(applied.status_code,200,applied.data)
            self.assertEqual(applied.data['applied_batch'],expected)
            self.assertLess(len(queries),40)  # Not N queries per SKU.
        self.assertEqual(Part.objects.filter(part_type__name='PASTILLAS DE FRENO').count(),205)
        self.assertFalse(CatalogMerge.objects.exists())
        self.assertEqual(PartType.objects.count(),1)

    def test_stale_source_blocks_group_write_atomically(self):
        job=self.start()
        self.action(job,mode='classify',batch_index=0)
        Part.objects.filter(sku='PAD-0002').update(description='EDITED SINCE ANALYSIS')
        response=self.action(job,mode='apply_category',category='FRENOS',subcategory='PASTILLAS DE FRENO')
        self.assertEqual(response.status_code,409)
        self.assertFalse(Part.objects.exclude(category='').exists())

    def test_low_confidence_excluded_and_failed_provider_releases_claim(self):
        job=self.start()
        with patch('mall.catalog_assistant.classify_categories',side_effect=ClassificationProviderError()):
            response=self.action(job,mode='classify',batch_index=0)
        self.assertEqual(response.status_code,502)
        saved=CatalogAssistantJob.objects.get(pk=job['id'])
        self.assertIsNone(saved.claim_id)
        self.assertEqual(saved.completed_batches,0)
        response=self.action(job,mode='classify',batch_index=0)
        item=saved.suggestions.first();item.proposal['confidence']=.5;item.save()
        applied=self.action(job,mode='apply_category',category='FRENOS',subcategory='PASTILLAS DE FRENO')
        self.assertEqual(applied.data['applied_batch'],199)
        item.refresh_from_db();self.assertEqual(item.status,'pending')

    def test_dismiss_invalidates_cached_result(self):
        job=self.start();self.action(job,mode='classify',batch_index=0)
        saved=CatalogAssistantJob.objects.get(pk=job['id']);item=saved.suggestions.first()
        CategorySuggestionCache.objects.create(pk='a'*64,result={})
        item.proposal['cache_key']='a'*64;item.save()
        self.assertEqual(self.action(job,mode='dismiss',ids=[str(item.pk)]).status_code,200)
        self.assertFalse(CategorySuggestionCache.objects.exists())

    def test_owner_and_superuser_restrictions_for_bulk_review(self):
        job=self.start();self.action(job,mode='classify',batch_index=0)
        other=User.objects.create_superuser('another-root','other@example.invalid','Test12345!')
        self.client.force_authenticate(other)
        self.assertEqual(self.action(job,mode='apply_category',category='FRENOS',subcategory='PASTILLAS DE FRENO').status_code,404)
        other.is_superuser=False;other.save();self.client.force_authenticate(other)
        self.assertEqual(self.action(job,mode='apply_category',category='FRENOS',subcategory='PASTILLAS DE FRENO').status_code,403)
