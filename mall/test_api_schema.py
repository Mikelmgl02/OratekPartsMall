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
        self.assertIn('requestBody', operations['/api/v1/management/catalog/{id}/oem-lookup/', 'post'])
        self.assertEqual(operations['/api/v1/management/matching/{id}/', 'post']['responses']['200']['content']['application/json']['schema'],
                         {'$ref': '#/components/schemas/MatchingCaseResponse'})
