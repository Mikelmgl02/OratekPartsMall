"""Brands (inventory refactor S1, mall.brands): spellings and aliases, the familia rule, and rename/merge as the only writers of
Brand.nombre, which re-key the codes that carry it and open a merge case on a collision."""
from django.test import TestCase

from . import brands
from .brand_models import Brand, BrandAlias
from .matching_models import MatchingCase
from .models import PartCode
from .test_factories import make_line, make_reference, make_user


class BrandSpellingTests(TestCase):
    def setUp(self):
        self.toyota = Brand.objects.create(nombre='TOYOTA', tipo='vehiculo')
        BrandAlias.objects.create(alias='TOY', brand=self.toyota)
        self.motion = Brand.objects.create(nombre='MOTION MOTORS')
        BrandAlias.objects.create(alias='MOTION  MOTORS', brand=self.motion)  # as the supplier columns write it

    def test_normalize_upper_cases_and_collapses_spaces(self):
        self.assertEqual(brands.normalize('  century  / hkt '), 'CENTURY / HKT')
        self.assertEqual(brands.normalize(None), '')
        self.assertEqual(Brand.objects.create(nombre=' febi   bilstein ').nombre, 'FEBI BILSTEIN')

    def test_a_spelling_names_its_brand_by_nombre_or_alias(self):
        self.assertEqual(brands.lookup('toyota'), self.toyota)
        self.assertEqual(brands.lookup(' toy '), self.toyota)
        self.assertEqual(brands.lookup('motion  motors'), self.motion)
        self.assertEqual(brands.lookup('MOTION MOTORS'), self.motion)
        self.assertIsNone(brands.lookup('ACME'))
        self.assertIsNone(brands.lookup(''))

    def test_get_or_create_reuses_aliases_and_fills_a_blank_tipo(self):
        self.assertEqual(brands.get_or_create('Toy'), (self.toyota, False))
        brand, created = brands.get_or_create('  gmb ', tipo='repuesto')
        self.assertTrue(created)
        self.assertEqual((brand.nombre, brand.tipo), ('GMB', 'repuesto'))
        self.assertEqual(brands.get_or_create('MOTION MOTORS', tipo='repuesto')[0].tipo, 'repuesto')
        self.assertEqual(brands.get_or_create('   '), (None, False))
        with self.assertRaises(brands.BrandConflict):
            brands.get_or_create('OTRA', tipo='camion')


class BrandFamiliaTests(TestCase):
    def setUp(self):
        self.chery = Brand.objects.create(nombre='CHERY', tipo='vehiculo')
        self.kaiyi = Brand.objects.create(nombre='KAIYI', tipo='vehiculo')
        self.toyota = Brand.objects.create(nombre='TOYOTA', tipo='vehiculo')
        self.lexus = Brand.objects.create(nombre='LEXUS', tipo='vehiculo')
        BrandAlias.objects.create(alias='TOY', brand=self.toyota)
        brands.set_familia(self.kaiyi, self.chery)
        brands.set_familia(self.lexus, self.toyota)

    def test_a_familia_is_one_part_across_its_makes_never_a_blank_brand(self):
        self.assertEqual(brands.familia_root('KAIYI'), 'CHERY')
        self.assertEqual(brands.familia_root('chery'), 'CHERY')
        self.assertEqual(brands.familia_root('ACME'), 'ACME')
        self.assertTrue(brands.same_familia('KAIYI', 'CHERY'))
        self.assertTrue(brands.same_familia('LEXUS', 'TOY'))
        self.assertFalse(brands.same_familia('KAIYI', 'TOYOTA'))
        self.assertFalse(brands.same_familia('', ''))
        self.assertEqual(brands.familia_map(['KAIYI', 'TOY', 'NADA']), {'KAIYI': 'CHERY', 'TOY': 'TOYOTA'})

    def test_a_familia_has_one_level(self):
        dfsk = Brand.objects.create(nombre='DFSK')
        with self.assertRaises(brands.BrandConflict):
            brands.set_familia(dfsk, self.kaiyi)       # KAIYI is a member: CHERY heads the familia
        with self.assertRaises(brands.BrandConflict):
            brands.set_familia(self.chery, self.toyota)  # CHERY heads one
        with self.assertRaises(brands.BrandConflict):
            brands.set_familia(dfsk, dfsk)
        brands.set_familia(self.kaiyi, None)
        self.assertFalse(brands.same_familia('KAIYI', 'CHERY'))


class BrandRenameMergeTests(TestCase):
    def setUp(self):
        self.febi = Brand.objects.create(nombre='FEBI', tipo='repuesto')
        self.bilstein = Brand.objects.create(nombre='FEBI BILSTEIN', tipo='repuesto', notas='Catálogo 2024')
        self.pump = make_line('16100-39315', description='BOMBA DE AGUA')
        self.other = make_line('16100-39315-FB', description='BOMBA DE AGUA')
        make_reference(self.pump, '01234', brand='FEBI', tipo='aftermarket')
        make_reference(self.pump, '05678', brand='FEBI', tipo='aftermarket')

    def brand_codes(self, brand):
        return sorted(PartCode.objects.filter(brand=brand).values_list('code', flat=True))

    def test_rename_rekeys_the_codes_and_keeps_the_old_spelling_as_an_alias(self):
        result = brands.rename(self.febi, 'febi  gmbh')
        self.febi.refresh_from_db()
        self.assertEqual((self.febi.nombre, result['moved'], result['cases']), ('FEBI GMBH', 2, []))
        self.assertEqual(self.brand_codes('FEBI GMBH'), ['01234', '05678'])
        self.assertEqual(self.brand_codes('FEBI'), [])
        self.assertEqual(BrandAlias.objects.get(alias='FEBI').brand, self.febi)
        self.assertEqual(brands.lookup('FEBI'), self.febi)
        brands.rename(self.febi, 'FEBI')  # back to an own alias: the alias becomes the nombre again
        self.febi.refresh_from_db()
        self.assertEqual(self.febi.nombre, 'FEBI')
        self.assertFalse(BrandAlias.objects.filter(alias='FEBI').exists())

    def test_rename_onto_another_brand_or_its_alias_is_refused(self):
        BrandAlias.objects.create(alias='FB', brand=self.bilstein)
        for name in ('febi bilstein', 'FB', ' '):
            with self.subTest(name=name), self.assertRaises(brands.BrandConflict):
                brands.rename(self.febi, name)
        self.assertEqual(self.brand_codes('FEBI'), ['01234', '05678'])

    def test_merge_moves_codes_aliases_and_familia_and_a_collision_opens_a_merge_case(self):
        BrandAlias.objects.create(alias='FEB', brand=self.febi)
        make_reference(self.other, '01234', brand='FEBI BILSTEIN', tipo='aftermarket')  # the same code under the other spelling
        member = Brand.objects.create(nombre='FEBI ASIA', familia=self.febi)
        result = brands.merge(self.febi, self.bilstein)
        self.assertFalse(Brand.objects.filter(nombre='FEBI').exists())
        self.assertEqual(result['moved'], 1)
        self.assertEqual(self.brand_codes('FEBI BILSTEIN'), ['01234', '05678'])
        self.assertEqual(self.brand_codes('FEBI'), ['01234'])  # kept until the case is decided: FEBI now names FEBI BILSTEIN
        self.assertEqual(set(BrandAlias.objects.filter(brand=self.bilstein).values_list('alias', flat=True)), {'FEB', 'FEBI'})
        member.refresh_from_db()
        self.assertEqual(member.familia, self.bilstein)
        case = MatchingCase.objects.get(pk=result['cases'][0])  # S10: a merge case over the two lines (a catalog_reference case before)
        self.assertEqual((case.kind, case.status, case.route), ('merge', 'review', 'caso'))
        self.assertEqual(set(case.lines.values_list('sku', flat=True)), {self.pump.sku, self.other.sku})
        self.assertIn('01234', case.reason)
        self.assertEqual(brands.merge(Brand.objects.create(nombre='FEBI2'), self.bilstein)['cases'], [])

    def test_merging_into_a_member_of_the_source_familia_makes_it_the_head(self):
        member = Brand.objects.create(nombre='FEBI ASIA', familia=self.febi)
        other = Brand.objects.create(nombre='FEBI EUROPA', familia=self.febi)
        brands.merge(self.febi, member)
        member.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNone(member.familia)
        self.assertEqual(other.familia, member)

    def test_an_oem_alterno_is_the_library_number_and_follows_the_rename(self):
        from . import references
        toyota = Brand.objects.create(nombre='TOYOTA MOTOR', tipo='vehiculo')
        code = make_reference(self.pump, '16100-39315', brand='TOYOTA MOTOR', tipo='oem', source='Declarado por GMB')
        library = PartCode.objects.filter(references.library_q())  # S4: the library's rows are these References
        self.assertEqual(list(library.values_list('brand', flat=True)), ['TOYOTA MOTOR'])
        brands.rename(toyota, 'TOYOTA')
        code.refresh_from_db()
        self.assertEqual((code.brand, list(library.values_list('pk', 'brand'))), ('TOYOTA', [(code.pk, 'TOYOTA')]))


class BrandAdminTests(TestCase):
    """The Django admin keeps the brand rules: a brand is merged, never deleted; an alias is added, never edited or deleted (codes
    may carry its spelling); a familia has one level; a spelling is a nombre or an alias, never both."""

    def setUp(self):
        from django.test import RequestFactory
        self.chery = Brand.objects.create(nombre='CHERY', tipo='vehiculo')
        self.kaiyi = Brand.objects.create(nombre='KAIYI', tipo='vehiculo', familia=self.chery)
        self.toyota = Brand.objects.create(nombre='TOYOTA', tipo='vehiculo')
        self.toy = BrandAlias.objects.create(alias='TOY', brand=self.toyota)
        self.request = RequestFactory().get('/admin/')
        self.request.user = make_user('brand-admin-root', superuser=True)

    def test_brands_are_never_deleted_and_aliases_only_added(self):
        from django.contrib import admin
        from .admin import BrandAdmin, BrandAliasAdmin, BrandAliasForm, BrandAliasInline
        self.assertFalse(BrandAdmin(Brand, admin.site).has_delete_permission(self.request, self.toyota))
        aliases = BrandAliasAdmin(BrandAlias, admin.site)
        self.assertFalse(aliases.has_change_permission(self.request, self.toy))
        self.assertFalse(aliases.has_delete_permission(self.request, self.toy))
        self.assertTrue(aliases.has_add_permission(self.request))
        inline = BrandAliasInline(Brand, admin.site)
        self.assertFalse(inline.has_change_permission(self.request, self.toyota))
        self.assertFalse(inline.has_delete_permission(self.request, self.toyota))
        self.assertTrue(inline.has_add_permission(self.request, self.toyota))
        self.assertFalse(BrandAliasForm(data={'alias': 'chery', 'brand': self.toyota.pk}).is_valid())  # a nombre already
        self.assertTrue(BrandAliasForm(data={'alias': 'toyta', 'brand': self.toyota.pk}).is_valid())

    def test_the_familia_keeps_one_level(self):
        from .admin import BrandAdminForm
        def valid(instance, familia):
            data = {'nombre': instance.nombre, 'tipo': instance.tipo, 'notas': '', 'familia': familia.pk if familia else ''}
            return BrandAdminForm(data=data, instance=instance).is_valid()
        self.assertFalse(valid(self.toyota, self.kaiyi))  # KAIYI is a member of CHERY's familia
        self.assertFalse(valid(self.chery, self.toyota))  # CHERY heads a familia
        self.assertFalse(valid(self.toyota, self.toyota))
        self.assertTrue(valid(self.toyota, self.chery))
        self.assertTrue(valid(self.kaiyi, None))
        self.assertFalse(BrandAdminForm(data={'nombre': 'toy', 'tipo': '', 'notas': ''}).is_valid())  # an alias of TOYOTA
