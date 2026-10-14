"""python -m mall.refactor.s3_variants: every item on a line hangs from a Reference of that line by the plan's rules, the References a
line lacks are made (public or interno), the codes learned since S2b become sources, the manifest and the rollback."""
import io
import json
import os
import tempfile
from unittest.mock import patch

from django.test import TestCase

from . import references
from .brand_models import Brand
from .catalog_suffix_models import CatalogCodeSuffix
from .matching_models import SupplierCodeMapping
from .models import Part, Reference, ReferenceSource, SupplierItem
from .refactor import s3_variants
from .test_factories import Fresh, make_line, make_reference, make_supplier, make_user, unhook_variants
from .test_variants import broken_variants


class S3BackfillTests(Fresh, TestCase):
    """The items as the code before S3 left them: on their lines, with no Reference."""

    def setUp(self):
        super().setUp()
        self.root = make_user('s3-root', superuser=True)
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)
        patcher = patch.dict(os.environ, {'REFACTOR_OUTPUT': self.output.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        Brand.objects.create(nombre='TOYOTA', tipo='vehiculo')
        Brand.objects.create(nombre='ENDO', tipo='repuesto')
        CatalogCodeSuffix.objects.create(token='ZQX', cls='TAG', tag_kind='brand', attribution='brand: GMB', confidence='high',
                                         status='owner_confirmed', owner_confirmed=True)
        self.grp, self.motion = make_supplier('S3 GRP'), make_supplier('S3 MOTION')
        self.plain = make_line('04111-22152', description='JUEGO EMPAQUES')
        self.branded = make_line('43512-60150', description='DISCO FRENO', is_OEM=True)
        references.retype_main(self.branded, 'oem', brand='TOYOTA')
        self.alias = make_reference(self.plain, '04111-22152-AS')
        self.spaced = make_line('AL-X7-5P', description='ALARMA')
        self.spaced_ref = make_reference(self.spaced, 'AL-X7-5P', brand='MOTION MOTORS', tipo='aftermarket')
        self.retired = make_line('RET-0001', active=False)
        rows = [  # (supplier, id, code, brand, line)
            (self.grp, 'A', '04111-22152', '', self.plain),               # its main
            (self.grp, 'B', '43512-60150', '', self.branded),             # brandless onto the TOYOTA main (rule 2)
            (self.grp, 'C', '04111-22152-AS', '', self.plain),            # another Reference of its key
            (self.grp, 'D', '04111-22152', 'ENDO', self.plain),           # branded onto the brandless main
            (self.grp, 'E', '43512-60150-ZQX', '', self.branded),         # rule 3: a public Reference
            (self.motion, 'F', '43512-60150', 'ENDO', self.branded),      # the line's own clave under another brand: public
            (self.motion, 'G', 'DFSK-0716', 'DFSK', self.branded),        # a different code: interno for Motion
            (self.motion, 'H', 'AL-X7-5P', 'MOTION  MOTORS', self.spaced),  # spaces do not count
            (self.grp, 'I', 'RET-0001', '', self.retired),                # a retired line's item attaches the same way
            (self.grp, 'J', 'SIN-LINEA', '', None),                       # no line: stays pending
        ]
        SupplierItem.objects.bulk_create([  # no save(): the shim would attach them
            SupplierItem(supplier=supplier, supplier_invent_id=invent, codigo=code, brand=brand, part=line, source='upload',
                         matching_status='matched' if line else 'pending', reported_quantity=1)
            for supplier, invent, code, brand, line in rows])
        unhook_variants()
        self.items = {item.supplier_invent_id: item for item in SupplierItem.objects.all()}

    def run_cli(self, **options):
        out = io.StringIO()
        return s3_variants.run(stdout=out, **options), out.getvalue()

    def test_the_dry_run_plans_every_rule_and_writes_nothing(self):
        before = (Reference.all_objects.count(), list(SupplierItem.objects.values_list('reference_id', flat=True)))
        result, out = self.run_cli()
        final = result['result']
        self.assertEqual({key: final.get(key, 0) for key in ('variantes', 'principal', 'marca_de_la_linea', 'exacta', 'sin_marca', 'nuevas',
                                                            'nuevas_internas', 'en_retiradas', 'sin_linea')},
                         {'variantes': 9, 'principal': 2, 'marca_de_la_linea': 1, 'exacta': 2, 'sin_marca': 1, 'nuevas': 3,
                          'nuevas_internas': 1, 'en_retiradas': 1, 'sin_linea': 1})
        self.assertEqual(sorted((row['code'], row['brand'], row['tipo']) for row in result['new']),
                         [('43512-60150', 'ENDO', 'aftermarket'), ('43512-60150-ZQX', '', 'otro'), ('DFSK-0716', 'DFSK', 'interno')])
        self.assertIsNone(result['simulated'])
        self.assertIn('Simulación · S3', out)
        self.assertEqual((Reference.all_objects.count(), list(SupplierItem.objects.values_list('reference_id', flat=True))), before)

    def test_apply_attaches_every_item_and_a_rerun_changes_nothing(self):
        result, out = self.run_cli(apply=True, username=self.root.username)
        self.assertEqual(result['written'], {'referencias': 3, 'variantes': 9, 'fuentes': 0, 'internas_aprendidas': 0})
        hung = {item.supplier_invent_id: item for item in SupplierItem.objects.select_related('reference')}
        main = references.main_of
        self.assertEqual(hung['A'].reference_id, main(self.plain).pk)
        self.assertEqual((hung['B'].reference_id, hung['B'].reference.brand), (main(self.branded).pk, 'TOYOTA'))
        self.assertEqual((hung['C'].reference_id, hung['D'].reference_id, hung['H'].reference_id), (self.alias.pk, main(self.plain).pk, self.spaced_ref.pk))
        self.assertEqual((hung['E'].reference.code, hung['E'].reference.supplier_id, hung['E'].part_id), ('43512-60150-ZQX', None, self.branded.pk))
        self.assertEqual((hung['F'].reference.brand, hung['F'].reference.tipo), ('ENDO', 'aftermarket'))
        self.assertEqual((hung['G'].reference.tipo, hung['G'].reference.supplier_id), ('interno', self.motion.pk))
        self.assertEqual((hung['I'].reference_id, hung['J'].reference_id, hung['J'].part_id), (main(self.retired).pk, None, None))
        self.assertTrue(ReferenceSource.objects.filter(reference=hung['E'].reference, kind='proveedor').exists())
        self.assertEqual(broken_variants(), {})
        lines = [json.loads(line) for line in open(s3_variants.manifest_path(), encoding='utf-8')]
        self.assertEqual(sum(1 for line in lines if line.get('model') == 'mall.SupplierItem'), 9)
        self.assertEqual(sum(1 for line in lines if line.get('model') == 'mall.Reference'), 3)
        again, _ = self.run_cli(apply=True, username=self.root.username)
        self.assertEqual((again['result'].get('variantes', 0), again['result']['ya_tenia'], again['written']['referencias']), (0, 9, 0))

    def test_codes_learned_since_s2b_become_sources_once(self):
        mapping = SupplierCodeMapping.objects.create(supplier=self.grp, code='0411122152AS', brand='', part=self.plain)
        cited = SupplierCodeMapping.objects.create(supplier=self.motion, code='0411122152', brand='', part=self.plain)
        ReferenceSource.objects.create(reference=references.main_of(self.plain), kind='proveedor', citation='Código aprendido · S3 MOTION · ayer',
                                       detail={'mapping': cited.pk})
        result, _ = self.run_cli(apply=True, username=self.root.username)
        self.assertEqual((result['result']['aprendidos'], result['written']['fuentes']), (1, 1))
        source = ReferenceSource.objects.get(detail__mapping=mapping.pk)
        self.assertEqual((source.reference_id, source.kind, source.citation.startswith('Código aprendido · S3 GRP')), (self.alias.pk, 'proveedor', True))
        again, _ = self.run_cli(apply=True, username=self.root.username)
        self.assertEqual(again['result']['aprendidos'], 0)

    def test_rollback_takes_the_references_off_again(self):
        self.run_cli(apply=True, username=self.root.username)
        created = list(Reference.all_objects.filter(variants__supplier_invent_id__in=['E', 'F', 'G']).values_list('pk', flat=True))
        with patch.object(s3_variants, 'tightened', return_value=False):
            result, out = self.run_cli(rollback=True)
        self.assertEqual((result['restored'], result['changed']), (9, []))
        self.assertFalse(SupplierItem.objects.filter(reference__isnull=False).exists())
        self.assertFalse(Reference.all_objects.filter(pk__in=created).exists())
        self.assertEqual(Reference.all_objects.filter(pk=self.alias.pk).count(), 1)

    def test_rollback_undoes_what_the_s3_code_wrote_since(self):
        """After landing, Vincular makes an interno Reference and a load leaves a code waiting; the rollback takes both back, since the
        code before S3 shows every interno code to everyone and 0077 reversed drops the waiting code."""
        from . import variants
        self.run_cli(apply=True, username=self.root.username)
        loose = SupplierItem.objects.get(supplier_invent_id='J')
        variants.vincular(loose, self.plain, actor=self.root)
        interno = Reference.all_objects.get(pk=SupplierItem.objects.get(pk=loose.pk).reference_id)
        self.assertEqual((interno.tipo, interno.detail['s3']['como']), ('interno', 'creada'))
        named = make_reference(self.branded, 'VIEJO-0001')
        references.retire(named, replaced_by=variants.vincular(SupplierItem.objects.get(supplier_invent_id='E'), self.branded,
                                                               actor=self.root).reference)
        SupplierItem.objects.filter(supplier_invent_id='C').update(codigo_pendiente='NUEVO-0001', matching_status='review')
        with patch.object(s3_variants, 'tightened', return_value=False):
            result, out = self.run_cli(rollback=True)
        self.assertEqual((result['restored'], result['pending_applied']), (10, 1))
        self.assertFalse(SupplierItem.objects.filter(reference__isnull=False).exists())
        self.assertFalse(Reference.all_objects.filter(pk=interno.pk).exists())
        replaced = Reference.all_objects.get(code='43512-60150-ZQX')
        self.assertEqual(replaced.estado, 'retirada')  # named as a replacement: retired, not deleted
        self.assertTrue(any('nombra como reemplazo' in line for line in result['changed']))
        changed = SupplierItem.objects.get(supplier_invent_id='C')
        self.assertEqual((changed.codigo, changed.codigo_pendiente, changed.matching_status), ('NUEVO-0001', None, 'review'))
        lines = [json.loads(line) for line in open(s3_variants.manifest_path(), encoding='utf-8')]
        self.assertTrue(any(line.get('role') == 'pendiente' and line['before']['codigo_pendiente'] == 'NUEVO-0001' for line in lines))

    def test_an_item_on_a_unificada_line_is_listed_and_left(self):
        Part.objects.filter(pk=self.spaced.pk).update(merged_into=self.plain, estado='unificada', active=False)
        result, out = self.run_cli()
        self.assertEqual(result['result']['en_unificadas'], 1)
        self.assertIn('AL-X7-5P (H) en AL-X7-5P', out)

    def test_apply_needs_its_user_and_rollback_waits_for_0078_reversed(self):
        self.assertIsNone(self.run_cli(apply=True)[0])
        with patch.object(s3_variants, 'tightened', return_value=True):
            result, out = self.run_cli(rollback=True)
        self.assertIsNone(result)
        self.assertIn('0078_variant_reference_checks', out)

    def test_without_s2b_mains_the_dry_run_simulates_them(self):
        unhook_variants()
        references.purge(Reference.all_objects.filter(is_main=True).values_list('pk', flat=True))
        result, out = self.run_cli()
        self.assertTrue(result['simulated']['mains_simulated'])
        self.assertEqual(result['result']['variantes'], 9)
        self.assertIn('se simulan con el plan de s2b_main_refs', out)
