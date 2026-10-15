import contextlib
import io

from django.test import SimpleTestCase
from drf_spectacular.drainage import GENERATOR_STATS
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.validation import validate_schema


class APISchemaTests(SimpleTestCase):
    def test_schema_generates_without_warnings_or_errors(self):
        GENERATOR_STATS.reset()
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            schema = SchemaGenerator().get_schema(request=None, public=True)
        self.assertFalse(GENERATOR_STATS, 'drf-spectacular reported problems:\n' + output.getvalue())
        validate_schema(schema)
        operations = {(path, method): operation for path, item in schema['paths'].items() for method, operation in item.items()}
        ids = [operation['operationId'] for operation in operations.values()]
        self.assertEqual(len(ids), len(set(ids)))
        for path, method in [('/api/v1/management/matching/', 'get'), ('/api/v1/management/matching/', 'post'),
                             ('/api/v1/management/matching/{id}/', 'post'), ('/api/v1/management/catalog/{id}/oem-lookup/', 'post'),
                             ('/api/v1/management/oem-finder/pending/', 'get'), ('/api/v1/management/oem-finder/pending/preview/', 'post'),
                             ('/api/v1/management/oem-finder/pending/apply/', 'post'), ('/api/v1/management/oem-finder/pending/send-to-review/', 'post')]:
            self.assertIn((path, method), operations)
        # The inventory refactor's S7 routes (S8's contract).
        for path, method in [('/api/v1/management/lines/', 'get'), ('/api/v1/management/lines/{id}/', 'get'), ('/api/v1/management/lines/{id}/', 'patch'),
                             ('/api/v1/management/lines/{id}/transition/', 'post'), ('/api/v1/management/lines/{id}/references/', 'get'),
                             ('/api/v1/management/lines/{id}/references/', 'post'), ('/api/v1/management/lines/{id}/main/', 'get'),
                             ('/api/v1/management/lines/{id}/main/', 'post'), ('/api/v1/management/references/{id}/', 'patch'),
                             ('/api/v1/management/references/{id}/', 'delete'), ('/api/v1/management/cases/', 'get'),
                             ('/api/v1/management/cases/{id}/', 'get'), ('/api/v1/management/cases/{id}/', 'post'),
                             ('/api/v1/management/cases/batches/{key}/', 'get'), ('/api/v1/management/cases/batches/{key}/', 'post'),
                             ('/api/v1/management/merges/', 'get'), ('/api/v1/management/brands/', 'get'), ('/api/v1/management/brands/', 'post'),
                             ('/api/v1/management/brands/{id}/', 'get'), ('/api/v1/management/brands/{id}/', 'patch'),
                             ('/api/v1/management/brands/{id}/aliases/', 'post'), ('/api/v1/management/brands/{id}/aliases/{alias_id}/', 'delete'),
                             ('/api/v1/management/grupos/', 'get'), ('/api/v1/management/grupos/', 'post'), ('/api/v1/management/grupos/{id}/', 'get'),
                             ('/api/v1/management/grupos/{id}/', 'patch'), ('/api/v1/management/inventory/{id}/separate/', 'post')]:
            self.assertIn((path, method), operations)
        self.assertEqual(operations['/api/v1/management/cases/{id}/', 'post']['responses']['200']['content']['application/json']['schema'],
                         {'$ref': '#/components/schemas/MatchingCaseResponse'})
        part = schema['components']['schemas']['Part']['properties']
        self.assertTrue({'brand', 'tipo', 'estado'} <= set(part))
        self.assertEqual(part['sku']['maxLength'], 120)
        self.assertTrue({'calidad', 'condicion', 'garantia', 'unidades_por_empaque', 'reference'}
                        <= set(schema['components']['schemas']['OfferItem']['properties']))
        self.assertIn('requestBody', operations['/api/v1/management/catalog/{id}/oem-lookup/', 'post'])
        self.assertEqual(operations['/api/v1/management/matching/{id}/', 'post']['responses']['200']['content']['application/json']['schema'],
                         {'$ref': '#/components/schemas/MatchingCaseResponse'})
