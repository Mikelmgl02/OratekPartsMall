"""S0.10 tools (target data step 1): the own-code audit and the number twins."""
import io
import json
import os
import tempfile

from django.test import TestCase

from .models import Part
from .oem_reference_models import OEMCrossReference, OEMCrossReferenceRetired, OEMReference
from .refactor import s0_own_codes
from .supplier_catalog_import import run as import_catalog
from .test_oem_finder import Fresh
from .test_supplier_catalog_import import extract, item


def write(data):
    handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
    json.dump(data, handle, ensure_ascii=False)
    handle.close()
    return handle.name


class OwnCodeAuditTests(Fresh, TestCase):
    def test_each_item_says_whether_its_code_is_filed_would_be_or_why_not(self):
        joint = lambda code, oem: item(code, 'cv_joint_outer', oem=oem, name_es='JUNTA HOMOCINÉTICA EXTERIOR')
        data = extract([joint('TY-01', [('43460-69115', 'TOYOTA')]),
                        joint('TY-02', [('43460-69125', 'TOYOTA'), ('43460-69135', 'TOYOTA')]), joint('TY-03', [('43460-69135', 'TOYOTA')]),
                        joint('TY-04', [('43410-60010', 'TOYOTA')]),  # a drive-shaft number
                        joint('TY-05', [('43460-69145', 'TOYOTA')]), joint('X', [('43460-69155', 'TOYOTA')])],
                       key='asva_cv', title='ASVA CV', brand='ASVA', product_line='cv_joint')
        path = write(data)
        self.addCleanup(os.unlink, path)
        import_catalog([path], apply=True, stdout=io.StringIO())
        OEMCrossReference.objects.filter(code='TY-05').delete()
        OEMCrossReference.objects.filter(code='TY-02').delete()
        ref = OEMReference.objects.get(code='4346069145')
        OEMCrossReferenceRetired.objects.create(reference=ref, brand='ASVA', number='TY05', code='TY-05')
        with tempfile.TemporaryDirectory() as directory:
            result = s0_own_codes.run([path], report_path=os.path.join(directory, 'own.json'), stdout=io.StringIO())
            with open(os.path.join(directory, 'own.json'), encoding='utf-8') as handle:
                self.assertEqual(json.load(handle), result)
        counts = result['catalogs'][0]['counts']
        self.assertEqual(counts, {'filed': 1, 'refile': 1, 'shared': 1, 'assembly': 1, 'no_key': 1, 'retired': 1})
        self.assertEqual(result['catalogs'][0]['samples'], {'refile': ['TY-02'], 'shared': ['TY-03'], 'assembly': ['TY-04'], 'no_key': ['X'],
                                                            'retired': ['TY-05']})
