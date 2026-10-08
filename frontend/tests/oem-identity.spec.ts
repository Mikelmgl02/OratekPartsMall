import {test, expect} from '@playwright/test';

for (const width of [1440, 390]) {
 test(`OEM research and reviewed MAIN selection at ${width}px`, async ({page}) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'Disposable superuser token required.');
  await page.context().addCookies([{name:'partsmall_session',value:process.env.E2E_ADMIN_TOKEN!,url:'http://localhost:8080',httpOnly:true,sameSite:'Strict'}]);
  await page.setViewportSize({width,height:1000});
  const part={id:'02000000-0000-4000-8000-000000000009',sku:'0110-GSU45A48-FEB',name:'PUNTA FLECHA',description:'PUNTA FLECHA EXT TOY RAV4',active:true,stock_record_count:1,codes:[{code:'0110-GSU45A48',brand:'FEBEST',ref_type:'company',reference_source:''}], identity:{ref_type:'company',status:'needs_oem',brand:'FEBEST'}};
  let payload:any;
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/,route=>route.fulfill({json:{count:1,results:[part]}}));
  await page.route('**/api/management/catalog/import/issues?**',route=>route.fulfill({json:{count:0,pending_count:0,results:[]}}));
  await page.route(`**/api/management/catalog/${part.id}/oem-equivalents`,route=>route.fulfill({json:{numbers:[],cross_references:[]}}));
  await page.route(`**/api/management/catalog/${part.id}/oem-lookup`,route=>route.fulfill({json:{cached:true,metrics:{total_tokens:0},note:'REVISAR FUENTES',sources:[{url:'https://example.com/catalog',title:'CATÁLOGO'}],search_suggestions:'',references:[{code:'43460-TEST',brand:'TOYOTA',ref_type:'oem',relationship:'equivalent',source_url:'https://example.com/catalog',reference_source:'https://example.com/catalog',reason:'PIEZA INDIVIDUAL'},{code:'43410-ASSEMBLY',brand:'TOYOTA',ref_type:'oem',relationship:'component',source_url:'https://example.com/assembly',reference_source:'https://example.com/assembly',reason:'CONJUNTO COMPLETO'}]}}));
  await page.route(`**/api/management/catalog/${part.id}`,route=>{payload=route.request().postDataJSON();return route.fulfill({json:{...part,...payload,sku:'43460-TEST'}});});
  await page.goto('/administracion?seccion=inventario');
  await expect(page.getByText('MAIN INTERNO · FEBEST · OEM PENDIENTE',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:`Editar SKU ${part.sku}`,exact:true}).click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByLabel('Tipo de referencia 1',{exact:true})).toHaveValue('company');
  await dialog.getByText('Buscar referencias OEM con IA',{exact:true}).click();
  await dialog.getByRole('button',{name:'Buscar OEM',exact:true}).click();
  await expect(dialog.getByText('BÚSQUEDA GUARDADA · SIN NUEVA LLAMADA A IA')).toBeVisible();
  await expect(dialog.getByRole('checkbox',{name:'43410-ASSEMBLY · TOYOTA',exact:true})).toBeDisabled();
  const add=dialog.getByRole('button',{name:/Agregar .* referencias revisadas al formulario/});
  await expect(add).toBeDisabled();
  await dialog.getByRole('checkbox',{name:'43460-TEST · TOYOTA',exact:true}).check();
  await expect(add).toBeDisabled();
  await dialog.getByRole('checkbox',{name:/Verifiqué en las fuentes/}).check();
  await add.click();
  await expect(dialog.getByLabel('Código 2',{exact:true})).toHaveValue('43460-TEST');
  await dialog.getByLabel('OEM para el MAIN',{exact:true}).selectOption({label:'43460-TEST · TOYOTA'});
  expect(await dialog.evaluate(el=>el.scrollWidth>el.clientWidth)).toBe(false);
  await page.screenshot({path:`/tmp/motionpartes-oem-${width}.png`});
  await dialog.getByRole('button',{name:'Guardar SKU',exact:true}).click();
  await expect(page.getByText('SKU y alternos guardados.',{exact:true})).toBeVisible();
  expect(payload.main_reference).toEqual({code:'43460-TEST',brand:'TOYOTA'});
  expect(payload.codes).toContainEqual({code:'0110-GSU45A48',brand:'FEBEST',ref_type:'company',reference_source:''});
  expect(payload.codes).not.toEqual(expect.arrayContaining([expect.objectContaining({code:'43410-ASSEMBLY'})]));
 });
}

for (const width of [1440, 390]) {
 test(`OEM flag persists on reopen and catalog can filter it at ${width}px`, async ({page}) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'Disposable superuser token required.');
  await page.context().addCookies([{name:'partsmall_session',value:process.env.E2E_ADMIN_TOKEN!,url:'http://localhost:8080',httpOnly:true,sameSite:'Strict'}]);
  await page.setViewportSize({width,height:1000});
  let part={id:'02000000-0000-4000-8000-000000000019',sku:'58411-1R000',is_OEM:false,name:'',description:'TAMBOR DE FRENO',active:true,stock_record_count:0,codes:[]};
  let filter=''; let payload:any;
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/,route=>{
    filter=new URL(route.request().url()).searchParams.get('is_OEM')||'';
    const show=!filter||filter===String(part.is_OEM);
    return route.fulfill({json:{count:show?1:0,results:show?[part]:[]}});
  });
  await page.route('**/api/management/catalog/import/issues?**',route=>route.fulfill({json:{count:0,pending_count:0,results:[]}}));
  await page.route(`**/api/management/catalog/${part.id}/oem-equivalents`,route=>route.fulfill({json:{numbers:[],cross_references:[]}}));
  await page.route(`**/api/management/catalog/${part.id}`,route=>{
    payload=route.request().postDataJSON();part={...part,...payload};return route.fulfill({json:part});
  });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button',{name:`Editar SKU ${part.sku}`,exact:true}).click();
  let dialog=page.getByRole('dialog');
  await expect(dialog.getByRole('checkbox',{name:'EL SKU PRINCIPAL ES OEM',exact:true})).not.toBeChecked();
  await dialog.getByRole('checkbox',{name:'EL SKU PRINCIPAL ES OEM',exact:true}).check();
  await dialog.getByRole('button',{name:'Guardar SKU',exact:true}).click();
  await expect(page.getByText('MAIN OEM',{exact:true})).toBeVisible();
  expect(payload.is_OEM).toBe(true);
  await page.getByRole('combobox',{name:'Tipo de SKU',exact:true}).selectOption('true');
  await expect.poll(()=>filter).toBe('true');
  await page.getByRole('button',{name:`Editar SKU ${part.sku}`,exact:true}).click();
  dialog=page.getByRole('dialog');
  await expect(dialog.getByRole('checkbox',{name:'EL SKU PRINCIPAL ES OEM',exact:true})).toBeChecked();
  expect(await dialog.evaluate(el=>el.scrollWidth>el.clientWidth)).toBe(false);
  await page.screenshot({path:`/tmp/motionpartes-oem-flag-${width}.png`});
 });
}
