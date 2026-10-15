"""The storefront search on the Search Document (inventory refactor S7, CATALOG_SEARCH_SOURCE=document): every legacy search test passes
on it (the 3c41049 rules: words in any order, accents ignored, codes without separators, filler words skipped but DEL kept, the exact code
first), plus what only the document does: brand aliases ("filtro aceite toyota" finds TOY), interno codes only in their supplier's own
catalog, disputa codes only as a line's main."""
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import references, search_document
from .brand_models import Brand, BrandAlias
from .catalog_search import backend, document_search_parts, legacy_search_parts, search_source
from .models import Membership, SupplierItem, User
from . import test_catalog_search


class SourceTests(SimpleTestCase):
    def test_legacy_is_the_default_and_anything_else_reads_as_legacy(self):
        self.assertEqual(settings.CATALOG_SEARCH_SOURCE, 'legacy')
        self.assertEqual(backend()[0], legacy_search_parts)
        with override_settings(CATALOG_SEARCH_SOURCE='document'):
            self.assertEqual((search_source(), backend()[0]), ('document', document_search_parts))
        with override_settings(CATALOG_SEARCH_SOURCE='bogus'):
            self.assertEqual(search_source(), 'legacy')


@override_settings(CATALOG_SEARCH_SOURCE='document')
class DocumentCatalogSearchTests(test_catalog_search.CatalogSearchTests):
    """The legacy search's tests, each request reading documents rebuilt from what the test wrote."""

    def setUp(self):
        super().setUp()
        get = self.client.get

        def rebuilt_get(*args, **kwargs):
            search_document.rebuild()
            return get(*args, **kwargs)
        self.client.get = rebuilt_get

    def test_codes_match_without_separators_on_skus_alternos_and_supplier_codes(self):
        # As the legacy test, except a supplier's own code: its interno Reference counts only in that supplier's catalog.
        self.part('51750-J9000', 'CUBO DE RUEDA DEL KIA')
        references.add(self.part('PAD-1', 'PASTILLA'), 'GWT-41A', 'GMB', tipo='aftermarket')
        sensor = self.part('SENSOR-1', 'SENSOR ABS')
        SupplierItem.objects.create(part=sensor, supplier=self.supplier, supplier_invent_id='INV-1', codigo='58411-1R000-G', brand='HYUNDAI',
                                    source='upload', reported_quantity=3, matching_status='matched')
        for search, expected in [('51750J9000', ['51750-J9000']), ('51750 J9000', ['51750-J9000']), ('51750-j9000', ['51750-J9000']),
                                 ('gwt41a', ['PAD-1']), ('GWT 41A', ['PAD-1'])]:
            self.assertEqual(self.skus(search), expected, search)
        sensor_code = references.find('58411-1R000-G', 'HYUNDAI', supplier=self.supplier)
        if sensor_code is not None and sensor_code.tipo == 'interno':
            self.assertEqual(self.skus('584111R000G'), [])  # a client never reads a supplier's own code
            own = User.objects.create_user('search-supplier', 'search-supplier@example.invalid', 'StrongPassword123!')
            Membership.objects.create(user=own, account=self.supplier)
            self.client.force_authenticate(own)
            self.assertEqual(self.skus('584111R000G'), ['SENSOR-1'])
            self.assertEqual(self.skus('58411 1R000 G'), ['SENSOR-1'])
        else:  # a public Reference: everyone finds it
            self.assertEqual(self.skus('584111R000G'), ['SENSOR-1'])

    def test_brand_aliases_find_what_the_catalog_wrote_short(self):
        toyota = Brand.objects.create(nombre='TOYOTA', tipo='vehiculo')
        BrandAlias.objects.create(alias='TOY', brand=toyota)
        self.part('90915-YZZE1', 'FILTRO ACEITE TOY COROLLA')
        self.part('04152-YZZA1', 'FILTRO ACEITE TOYOTA HILUX')
        self.part('15400-PLM-A02', 'FILTRO ACEITE HON CIVIC')
        self.assertEqual(self.skus('filtro aceite toyota'), ['04152-YZZA1', '90915-YZZE1'])
        self.assertEqual(self.skus('filtro aceite toy'), ['04152-YZZA1', '90915-YZZE1'])  # TOY as written, and the brand it names
        with override_settings(CATALOG_SEARCH_SOURCE='legacy'):
            self.assertEqual(self.skus('filtro aceite toyota'), ['04152-YZZA1'])  # what the legacy search could not do

    def test_del_is_a_word_not_filler(self):
        self.part('DISCO-DEL', 'DISCO FRENO DEL')
        self.part('DISCO-TRAS', 'DISCO FRENO TRAS')
        self.assertEqual(self.skus('disco freno del'), ['DISCO-DEL'])
        self.assertEqual(self.skus('disco de freno'), ['DISCO-DEL', 'DISCO-TRAS'])

    def test_a_code_in_dispute_counts_only_as_its_lines_main(self):
        line = self.part('AB-100', 'SOPORTE')
        references.add(line, 'XY-200', '', tipo='otro', estado='disputa', dispute_note='Lo tiene otra línea.')
        other = self.part('XY-201', 'SOPORTE B')
        references.update(references.main_of(other), estado='disputa', dispute_note='Duplicado.')
        self.assertEqual(self.skus('XY-200'), [])
        self.assertEqual(self.skus('XY-201'), ['XY-201'])
        with override_settings(CATALOG_SEARCH_SOURCE='legacy'):
            self.assertEqual(self.skus('XY-200'), ['AB-100'])

    def test_lines_off_the_storefront_are_never_found(self):
        from .lines import set_estado
        line = self.part('RET-1', 'RETEN CIGUENAL')
        self.assertEqual(self.skus('reten'), ['RET-1'])
        set_estado(line, 'retirada')
        self.assertEqual(self.skus('reten'), [])
