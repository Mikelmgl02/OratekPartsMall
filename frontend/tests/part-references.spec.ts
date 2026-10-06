import {test, expect} from '@playwright/test';

for (const width of [1440, 390]) {
  test(`master reference editor preserves identity and reference metadata at ${width}px`, async ({page}) => {
    test.skip(!process.env.E2E_ADMIN_TOKEN, 'Disposable superuser token required.');
    await page.context().addCookies([{name:'partsmall_session', value:process.env.E2E_ADMIN_TOKEN!, url:'http://localhost:8080', httpOnly:true, sameSite:'Strict'}]);
    await page.setViewportSize({width, height:1000});
    const part = {id:'02000000-0000-4000-8000-000000000001', sku:'RN11002V', name:'DISCO DELANTERO', description:'DISCO DELANTERO HYUNDAI ACCENT 256 MM', active:true, stock_record_count:2,
      codes:[{code:'51712-1R000', brand:'HYUNDAI', kind:'oem', reference_source:'CATÁLOGO VERIFICADO'}]};
    let payload:Record<string,unknown>|undefined;
    await page.route(/\/api\/management\/catalog(?:\?.*)?$/, route=>route.fulfill({json:{count:1, results:[part]}}));
    await page.route('**/api/management/catalog/import/issues?**', route=>route.fulfill({json:{count:0, pending_count:0, results:[]}}));
    await page.route(`**/api/management/catalog/${part.id}`, route=>{
      payload=route.request().postDataJSON();
      return route.fulfill({json:{...part,...payload}});
    });
    await page.goto('/administracion?seccion=inventario');
    await page.getByRole('button',{name:`Editar SKU ${part.sku}`,exact:true}).click();
    const dialog=page.getByRole('dialog');
    await expect(dialog.getByText('Biblioteca de equivalencias del SKU maestro',{exact:true})).toBeVisible();
    await expect(dialog.getByLabel('Tipo de referencia 1',{exact:true})).toHaveValue('oem');
    await expect(dialog.getByLabel('Fuente de la equivalencia 1',{exact:true})).toHaveValue('CATÁLOGO VERIFICADO');
    await dialog.getByRole('button',{name:'Agregar código',exact:true}).click();
    await dialog.getByLabel('Código 2',{exact:true}).fill('51712-0u000');
    await dialog.getByLabel('Marca del código 2',{exact:true}).fill('kia');
    await dialog.getByLabel('Tipo de referencia 2',{exact:true}).selectOption('oem');
    await dialog.getByLabel('Fuente de la equivalencia 2',{exact:true}).fill('https://example.invalid/Catalog');
    await expect(dialog.getByLabel('Código 2',{exact:true})).toHaveValue('51712-0U000');
    await expect(dialog.getByLabel('Código 2',{exact:true})).toHaveAttribute('autocomplete','off');
    expect(await dialog.evaluate(el=>el.scrollWidth>el.clientWidth)).toBe(false);
    await page.screenshot({path:`/tmp/motionpartes-references-${width}.png`});
    await dialog.getByRole('button',{name:'Guardar SKU',exact:true}).click();
    await expect(page.getByText('SKU y alternos guardados.',{exact:true})).toBeVisible();
    expect(payload?.sku).toBe('RN11002V');
    expect(payload?.codes).toEqual([...part.codes.map(({kind, ...row}) => ({...row, ref_type:kind})),{code:'51712-0U000',brand:'KIA',ref_type:'oem',reference_source:'https://example.invalid/Catalog'}]);
  });
}
