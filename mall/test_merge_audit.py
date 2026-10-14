import csv
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.db import DatabaseError, connection
from django.test import TestCase

from . import catalog_suffixes as cs
from .catalog_suffix_models import CatalogCodeSuffix, CatalogCodeSuffixChange
from .matching_models import MatchingDecision
from .models import CatalogMerge, Part, User
from .refactor import s0_merge_audit as merge_audit
from .test_catalog_suffixes import relabel_night


class MergeAuditTests(TestCase):
    """S0.8: the matching worker's automatic merges, judged again by rule 3 (after D4) and the 3c guards, read-only."""

    def setUp(self):
        cs.invalidate()
        self.addCleanup(cs.invalidate)
        self.service = User.objects.create_user(merge_audit.SERVICE_ACCOUNT, 'matching@example.invalid', 'Example938!')
        self.owner = User.objects.create_superuser('miguel', 'miguel@example.invalid', 'Example938!')

    def merge(self, target, description, sources, actor=None, created=True, evidence=True):
        """A merge of sources [(sku, description)] into target as merge_catalog_parts records it, with the worker's decision."""
        part = Part.objects.create(sku=target, name=target, description=description)
        rows = [Part.objects.create(sku=sku, name=sku, description=text, active=False, merged_into=part) for sku, text in sources]
        merge = CatalogMerge.objects.create(actor=actor or self.service, target=part, summary={'target_created': created},
                                            sources=[{'id': str(row.pk), 'sku': row.sku} for row in rows])
        if evidence:
            MatchingDecision.objects.create(actor=actor or self.service, action='automatic', evidence={
                'method': 'base_and_spec', 'merge_id': str(merge.pk), 'target': {'sku': target, 'description': description},
                'sources': [{'id': str(row.pk), 'sku': row.sku, 'description': row.description} for row in rows]})
        return merge

    def catalog(self):
        relabel_night(self.owner)  # AS and M are brand TAGs in the database until D4 applies
        self.merge('04111-22152', 'EMP OVER TOY 1ZZ-FE COROLLA 00-', [('04111-22152-AS', 'EMP OVER TOY 1ZZ-FE COROLLA 00-'),
                                                                     ('04111-22152-M', 'EMP OVER TOY 1ZZ-FE COROLLA 00-')])
        self.merge('11270-4M400', 'BOMBA AGUA NIS SENTRA', [('11270-4M400-ANULADO', 'BOMBA AGUA NIS SENTRA')], created=False)
        self.merge('13503-62030', 'TENSOR CADENA TOY HILUX', [('13503-62030-KOY', 'TENSOR CADENA TOY HILUX')])
        self.merge('48510-09L10', 'AMORT TOY HILUX DEL', [('48510-09L10-KIT', 'AMORT TOY HILUX DEL'), ('48510-09L10-NPW', 'AMORT TOY HILUX DEL')])
        self.merge('22450-74Y25', 'BOBINA NIS SENTRA', [('22450-74Y25-AKESAI', 'BOBINA NIS SENTRA')])
        self.merge('DAC47880055-KIT-FEB', 'BALINERA RUEDA DEL', [('DAC47880055-GMB', 'BALINERA RUEDA DEL')], created=False)
        self.merge('48654-0K030', 'BASE AMORT TOY HILUX', [('48654-0K030-G', 'TOPE AMORT TOY HILUX')], created=False)
        self.merge('04465-0K240', 'PASTILLA FRENO TOY HILUX DEL', [('04465-0K240-NPW', 'PASTILLA FRENO TOY HILUX TRAS')], created=False)
        self.merge('12361-0L020', 'BASE MOTOR TOY HILUX RH/LH', [('12361-0L020-G', 'BASE MOTOR TOY HILUX RH/LH'),
                                                               ('12361-0L020-NPW', 'BASE MOTOR TOY HILUX RH/LH')])
        self.merge('16100-79445', 'BOMBA AGUA TOY HIACE', [('16100-79445-NAK', 'BOMBA AGUA TOY HIACE')], evidence=False)
        self.merge('90919-02240', 'COIL TOY COROLLA', [('90919-02240-L', 'COIL TOY COROLLA')], actor=self.owner)  # a person's merge

    def test_rule_3_and_the_guards_name_the_merges_the_new_rules_would_not_make(self):
        self.catalog()
        result = merge_audit.audit()
        self.assertEqual(result['pending_d4'], 45)
        rows = {r['merge'].target.sku: r for r in result['rows']}
        self.assertNotIn('90919-02240', rows)
        reasons = {s['sku']: s['reason'] for r in result['rows'] for s in r['sources']}
        self.assertEqual(reasons, {
            '04111-22152-AS': 'reverted', '04111-22152-M': 'reverted', '11270-4M400-ANULADO': 'lifecycle', '13503-62030-KOY': 'unknown',
            '48510-09L10-KIT': 'variant', '48510-09L10-NPW': '', '22450-74Y25-AKESAI': 'tag', 'DAC47880055-GMB': 'base',
            '48654-0K030-G': 'head', '04465-0K240-NPW': 'spec', '12361-0L020-G': '', '12361-0L020-NPW': '', '16100-79445-NAK': ''})
        self.assertTrue(rows['04111-22152']['different_parts'])
        self.assertEqual([s['reverted'] for s in rows['04111-22152']['sources']], [['AS'], ['M']])
        # A KIT source beside a bare-tag one: two different parts; one lifecycle copy is never a different part.
        self.assertEqual({sku for sku, r in rows.items() if r['different_parts']}, {'04111-22152', '48510-09L10'})
        summary = merge_audit.summarize(result)
        self.assertEqual({k: summary[k] for k in ('merges', 'sources', 'failing_merges', 'failing_sources', 'different_parts')},
                         {'merges': 10, 'sources': 13, 'failing_merges': 8, 'failing_sources': 9, 'different_parts': 2})
        self.assertEqual(summary['reasons'], {'lifecycle': 1, 'unknown': 1, 'reverted': 2, 'variant': 1, 'tag': 1, 'base': 1, 'head': 1, 'spec': 1})
        self.assertEqual(summary['merges_by_reason']['reverted'], 1)
        # Today's spec_conflict on one identical LH/RH text is set aside (S0.2 fixes it), and counted apart.
        self.assertEqual(summary['same_text_spec_conflicts'], 2)
        self.assertEqual(summary['state_today'], {'unificado': 8})

    def test_once_d4_is_applied_the_audit_reads_the_table_as_it_is(self):
        self.catalog()
        from .refactor import s0_suffix_revert as revert
        with tempfile.TemporaryDirectory() as folder, patch.object(revert, 'OUTPUT', Path(folder)):
            revert.run(apply=True, username='miguel', stdout=io.StringIO())
        result = merge_audit.audit()
        self.assertEqual(result['pending_d4'], 0)
        self.assertEqual({s['sku']: s['reason'] for r in result['rows'] for s in r['sources'] if s['sku'].startswith('04111')},
                         {'04111-22152-AS': 'reverted', '04111-22152-M': 'reverted'})

    def test_a_separated_source_shows_as_separated(self):
        self.catalog()
        Part.objects.filter(sku='04111-22152-M').update(active=True, merged_into=None)
        rows = {r['merge'].target.sku: r for r in merge_audit.audit()['rows']}
        self.assertEqual([s['state'] for s in rows['04111-22152']['sources']], ['unificado', 'separado'])
        self.assertEqual(merge_audit.merge_state(rows['04111-22152']), 'mixto')

    def test_the_report_is_written_and_nothing_in_the_database_changes(self):
        self.catalog()
        before = (CatalogCodeSuffix.objects.order_by('-version').first().version, CatalogCodeSuffixChange.objects.count(),
                  CatalogMerge.objects.count(), MatchingDecision.objects.count(), Part.objects.filter(active=True).count())
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        directory = Path(folder.name) / 'merge-audit'
        out = io.StringIO()
        self.assertEqual(merge_audit.main(['--report', str(directory), '--json'], stdout=out), 0)
        text = out.getvalue()
        self.assertIn('Las reglas nuevas no harían 8 uniones (plan ≈217): 9 fuentes', text)
        self.assertIn('D4 aplicada en memoria: faltan 45 sufijos por revertir en la base', text)
        self.assertEqual(json.loads(text.splitlines()[-1])['different_parts'], 2)
        self.assertEqual(sorted(p.name for p in directory.iterdir()),
                         ['different-parts.csv', 'merges.csv', 'resumen.txt', 'sources.csv', 'summary.json'])
        merges = list(csv.DictReader(open(directory / 'merges.csv', encoding='utf-8')))
        self.assertEqual(len(merges), 8)
        first = next(m for m in merges if m['sku_destino'] == '04111-22152')
        self.assertEqual((first['motivos'], first['piezas_distintas'], first['estado_hoy'], first['destino_creado']), ('reverted', 'sí', 'unificado', 'sí'))
        sources = list(csv.DictReader(open(directory / 'sources.csv', encoding='utf-8')))
        self.assertEqual(len(sources), 9)
        self.assertEqual({s['sku_fuente']: s['palabras_d4'] for s in sources if s['motivo'] == 'reverted'}, {'04111-22152-AS': 'AS', '04111-22152-M': 'M'})
        parts = list(csv.DictReader(open(directory / 'different-parts.csv', encoding='utf-8')))
        self.assertEqual({p['sku_destino'] for p in parts}, {'04111-22152', '48510-09L10'})
        self.assertEqual(json.load(open(directory / 'summary.json'))['expected']['different_parts'], 34)
        after = (CatalogCodeSuffix.objects.order_by('-version').first().version, CatalogCodeSuffixChange.objects.count(),
                 CatalogMerge.objects.count(), MatchingDecision.objects.count(), Part.objects.filter(active=True).count())
        self.assertEqual(after, before)

    def test_the_audit_runs_in_a_read_only_transaction(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL only')

        def write():
            Part.objects.create(sku='ESCRIBE', name='ESCRIBE')
        with patch.object(merge_audit, 'audit', side_effect=write), self.assertRaises(DatabaseError):
            merge_audit.run(stdout=io.StringIO())
        self.assertFalse(Part.objects.filter(sku='ESCRIBE').exists())
