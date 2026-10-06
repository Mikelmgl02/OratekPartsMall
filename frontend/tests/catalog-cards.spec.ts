import { expect, test } from '@playwright/test';

const parts = ['low', 'high', 'sold_out', 'unknown', 'medium'].map((status, index) => ({
  id: `part-${index}`, sku: index === 0 ? '58411-1R000' : `SKU-${index}`, name: '',
  description: index === 0 ? 'DISCO DE FRENO VENTILADO DELANTERO PARA HYUNDAI ACCENT' : '',
  category: index === 0 ? 'FRENOS' : '', subcategory: index === 0 ? 'DISCOS DELANTEROS' : '',
  codes: index === 0 ? [{code: '58411-1R000-G', brand: 'MARCA A'}, {code: '58-1R0', brand: 'MARCA B'}, {code: 'D-HYU-1R', brand: ''}] : [],
  availability: {status, supplier_count: status === 'low' ? 2 : status === 'high' || status === 'medium' ? 1 : 0, updated_at: status === 'unknown' ? null : '2026-10-02T16:30:00Z'},
}));

for (const viewport of [{name:'desktop', width:1440, height:1000}, {name:'mobile', width:390, height:844}]) {
  test(`${viewport.name}: catalog cards show stock, metadata, alternates and read-only details`, async ({page}) => {
    await page.setViewportSize(viewport);
    await page.route('**/api/session', route => route.fulfill({json:{authenticated:true,
      user:{id:1, username:'CLIENTE', is_superuser:false}, accounts:{count:1, next:null, previous:null,
      results:[{id:'client-a', name:'CLIENTE', roles:['client_business'], capabilities:['client']}]}}}));
    await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({json:{count:parts.length, next:null, previous:null, results:parts}}));
    await page.route('**/api/market/catalog/part-0/suppliers', route => route.fulfill({json:[]}));
    await page.goto('/');
    const card = page.getByRole('article', {name:'Repuesto 58411-1R000', exact:true});
    await expect(card).toBeVisible();
    await expect(card).toContainText('BAJAS EXISTENCIAS');
    await expect(card).toContainText('DISCO DE FRENO VENTILADO DELANTERO PARA HYUNDAI ACCENT');
    await expect(card).toContainText('GRUPO');
    await expect(card).toContainText('FRENOS');
    await expect(card).toContainText('DISCOS DELANTEROS');
    await expect(card).toContainText('2 PROVEEDORES CON STOCK');
    await expect(card).toContainText('STOCK ACTUALIZADO');
    await expect(card.getByText('58411-1R000-G', {exact:true})).toBeVisible();
    await expect(card.getByRole('button', {name:'Ver los 3 alternos de 58411-1R000'})).toHaveText('+1');
    await expect(page.getByRole('article', {name:'Repuesto SKU-1', exact:true})).toContainText('ALTAS EXISTENCIAS');
    await expect(page.getByRole('article', {name:'Repuesto SKU-4', exact:true})).toContainText('EXISTENCIAS MEDIAS');
    await expect(page.getByRole('article', {name:'Repuesto SKU-2', exact:true})).toContainText('AGOTADO');
    const unknown = page.getByRole('article', {name:'Repuesto SKU-3', exact:true});
    await expect(unknown).toContainText('SIN EXISTENCIAS REPORTADAS');
    await expect(unknown).toContainText('DESCRIPCIÓN PENDIENTE');
    await expect(unknown).toContainText('SIN CLASIFICAR');
    await expect(unknown).not.toContainText('AGOTADO');
    await page.getByRole('button', {name:'Vista de lista', exact:true}).click();
    await expect(card).toContainText('BAJAS EXISTENCIAS');
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await card.getByRole('button', {name:'Ver los 3 alternos de 58411-1R000'}).click();
    const detail = page.getByRole('dialog');
    await expect(detail).toContainText('BAJAS EXISTENCIAS');
    await expect(detail).toContainText('DISCOS DELANTEROS');
    await expect(detail).toContainText('D-HYU-1R');
    await expect(detail).toContainText('Aún no hay proveedores con existencias disponibles.');
    await expect(detail.getByRole('button', {name:/Agregar/})).toHaveCount(0);
  });
}
