import { test, expect, Page } from '@playwright/test';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: 'Cuenta y listas', exact: true }).click();
  await page.getByRole('button', { name: 'Iniciar sesión', exact: true }).first().click();
  await page.getByLabel('Usuario', { exact: true }).fill(process.env.E2E_ADMIN_USERNAME!);
  await page.getByLabel('Contraseña', { exact: true }).fill(process.env.E2E_ADMIN_PASSWORD!);
  await page.getByRole('dialog').getByRole('button', { name: 'Iniciar sesión', exact: true }).last().click();
  await expect(page.getByText('Cuenta conectada', { exact: true })).toBeVisible();
}

test('a reviewed catalog family merges into one searchable SKU without requiring AI', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_USERNAME || !process.env.E2E_ADMIN_PASSWORD, 'A disposable superuser fixture is required.');
  await signIn(page);
  const origin = new URL(page.url()).origin;
  const sku = `${process.env.E2E_ADMIN_USERNAME}-58411-1R000`.toUpperCase();
  const sourceSkus = [`${sku}-KSM`, `${sku}-RAZ-NP`];
  const createdParts = [];
  for (const code of [sku, ...sourceSkus]) {
    const response = await page.request.post('/api/management/catalog', { headers: { Origin: origin }, data: { sku: code, description: 'TAMBOR DE FRENO HYUNDAI ACCENT', codes: code === sku ? [] : [{ code: `${code}-CODIGO` }] } });
    expect(response.status()).toBe(201); createdParts.push(await response.json());
  }
  let aiCalls = 0;
  await page.route('**/api/management/catalog/grouping/classify', route => { aiCalls++; return route.fulfill({ status: 500, json: { detail: 'No AI call expected.' } }); });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Agrupar SKU', exact: true }).click();
  await page.getByLabel('Buscar familias de SKU', { exact: true }).fill(sku.toLowerCase());
  await expect(page.getByLabel('Buscar familias de SKU', { exact: true })).toHaveValue(sku);
  await expect(page.getByLabel('Buscar familias de SKU', { exact: true })).toHaveAttribute('autocomplete', 'off');
  await page.getByRole('button', { name: `Revisar familia ${sku}`, exact: true }).click();
  const action = page.getByRole('button', { name: `Agrupar 0 alternos bajo ${sku}`, exact: true });
  await expect(action).toBeDisabled();
  await expect(page.getByLabel(`Agrupar ${sourceSkus[0]} bajo ${sku}`, { exact: true })).not.toBeChecked();
  for (const code of sourceSkus) await page.getByLabel(`Agrupar ${code} bajo ${sku}`, { exact: true }).check();
  const reviewedAction = page.getByRole('button', { name: `Agrupar 2 alternos bajo ${sku}`, exact: true });
  await expect(reviewedAction).toBeDisabled();
  await page.getByLabel('Confirmo que los SKU seleccionados son el mismo repuesto.', { exact: true }).check();
  await page.screenshot({ path: '/tmp/motionpartes-grouping-review-desktop.png' });
  await reviewedAction.click();
  await expect(page.getByRole('dialog').getByText(`2 SKU agrupados bajo ${sku}. Sus códigos ya son alternos del SKU interno.`, { exact: true })).toBeVisible();
  expect(aiCalls).toBe(0);
  const catalog = await (await page.request.get(`/api/market/catalog?search=${encodeURIComponent(sku)}`)).json();
  expect(catalog.count).toBe(1); expect(catalog.results[0].id).toBe(createdParts[0].id);
  for (const code of sourceSkus) {
    const byAlternate = await (await page.request.get(`/api/market/catalog?search=${encodeURIComponent(code)}`)).json();
    expect(byAlternate.count).toBe(1); expect(byAlternate.results[0].sku).toBe(sku);
    expect(catalog.results[0].codes.some((item: { code: string }) => item.code === code)).toBe(true);
  }
  await page.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  await page.getByLabel('Buscar repuestos', { exact: true }).fill(sku);
  await expect(page.getByRole('button', { name: `Editar SKU ${sku}`, exact: true })).toBeVisible();
});

test('selected-family AI advice stays separate from reviewed merge choices and fits mobile', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_USERNAME || !process.env.E2E_ADMIN_PASSWORD, 'A disposable superuser fixture is required.');
  await signIn(page);
  const sku = '58411-1R000';
  const sourceSkus = [`${sku}-KSM`, `${sku}-RAZ-NP`];
  const part = (code: string, index: number) => ({ id: `00000000-0000-0000-0000-00000000000${index}`, sku: code, name: code, description: 'TAMBOR DE FRENO HYUNDAI ACCENT', category: '', subcategory: '', codes: [] });
  const group = { target_sku: sku, target: part(sku, 1), sources: sourceSkus.map((code, index) => part(code, index + 2)), source_skus: sourceSkus, source_count: 2, reason: 'Código base y descripción coinciden.', warnings: ['58411-1R000-G pertenece a 1034-LED-FIJO; revisa este alterno.'], needs_review: true };
  let submitted: unknown = null;
  let merged = false;
  const groupingUrl = /\/api\/management\/catalog\/grouping(?:\?.*)?$/;
  await page.route(groupingUrl, route => route.fulfill({ json: { count: merged ? 0 : 1, next: null, previous: null, results: merged ? [] : [group] } }));
  await page.route('**/api/management/catalog/grouping/classify', async route => {
    expect(route.request().postDataJSON()).toEqual({ target_sku: sku, source_skus: sourceSkus });
    await route.fulfill({ json: { target_sku: sku, source_skus: [sourceSkus[1]], category: 'FRENOS', subcategory: 'TAMBORES', reason: 'Una referencia verificada y otra por confirmar.', confidence: 0.9, suggestions: sourceSkus.map((code, index) => ({ source_sku: code, target_sku: index ? sku : code, category: 'FRENOS', subcategory: 'TAMBORES', reason: index ? 'La referencia coincide.' : 'Falta confirmar la aplicación.', confidence: index ? 0.95 : 0.5, source_reference: index ? sku : '', target_reference: index ? sku : '' })) } });
  });
  await page.route('**/api/management/catalog/grouping/merge', async route => { submitted = route.request().postDataJSON(); merged = true; await route.fulfill({ json: { target_sku: sku } }); });
  try {
    await page.goto('/administracion?seccion=inventario');
    await page.getByRole('button', { name: 'Agrupar SKU', exact: true }).click();
    await page.getByRole('button', { name: `Revisar familia ${sku}`, exact: true }).click();
    await expect(page.getByText('58411-1R000-G pertenece a 1034-LED-FIJO; revisa este alterno.', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Revisar familia con IA', exact: true }).click();
    await expect(page.getByText('IA: conservar separado', { exact: true })).toBeVisible();
    await expect(page.getByText(`IA: agrupar bajo ${sku}`, { exact: true })).toBeVisible();
    for (const code of sourceSkus) await expect(page.getByLabel(`Agrupar ${code} bajo ${sku}`, { exact: true })).not.toBeChecked();
    expect(submitted).toBeNull();
    await page.getByLabel(`Agrupar ${sourceSkus[1]} bajo ${sku}`, { exact: true }).check();
    await page.getByLabel('Categoría de agrupación', { exact: true }).fill('frenos');
    await expect(page.getByLabel('Categoría de agrupación', { exact: true })).toHaveValue('FRENOS');
    await expect(page.getByLabel('Categoría de agrupación', { exact: true })).toHaveAttribute('autocomplete', 'off');
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.getByRole('dialog').evaluate(dialog => dialog.scrollWidth > dialog.clientWidth)).toBe(false);
    await page.screenshot({ path: '/tmp/motionpartes-grouping-review-mobile.png' });
    await page.getByLabel('Confirmo que los SKU seleccionados son el mismo repuesto.', { exact: true }).check();
    await page.getByRole('button', { name: `Agrupar 1 alterno bajo ${sku}`, exact: true }).click();
    expect(submitted).toEqual({ target_sku: sku, source_skus: [sourceSkus[1]], create_target: false, category: 'FRENOS', subcategory: 'TAMBORES' });
  } finally { await page.unroute(groupingUrl); await page.unroute('**/api/management/catalog/grouping/classify'); await page.unroute('**/api/management/catalog/grouping/merge'); }
});


test('a missing parent shows four children and is created only after reviewed confirmation', async ({page}) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required.');
  await page.context().addCookies([{name:'partsmall_session',value:process.env.E2E_ADMIN_TOKEN!,url:'http://localhost:8080',httpOnly:true,sameSite:'Strict'}]);
  const sku = '58411-07000';
  const sourceSkus = ['K','KSM-NP','NP','RAZ'].map(suffix=>`${sku}-${suffix}`);
  const part = (code:string) => ({id:code,sku:code,name:code,description:'TAMBOR KIA PICANTO 04-11',category:'',subcategory:'',codes:[]});
  const group = {target_sku:sku,target:{...part(sku),id:'',proposed_parent:true},sources:sourceSkus.map(part),source_skus:sourceSkus,source_count:4,reason:'Crear SKU padre a partir del código base común.',warnings:['Los años de aplicación difieren entre los códigos. Confirma la equivalencia antes de agrupar.'],needs_review:true};
  let submitted:unknown;
  await page.route(/\/api\/management\/catalog\/grouping(?:\?.*)?$/,route=>route.fulfill({json:{count:submitted?0:1,next:null,previous:null,results:submitted?[]:[group]}}));
  await page.route('**/api/management/catalog/grouping/merge',async route=>{submitted=route.request().postDataJSON();await route.fulfill({json:{target_sku:sku}});});
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button',{name:'Agrupar SKU',exact:true}).click();
  await page.getByRole('button',{name:`Revisar familia ${sku}`,exact:true}).click();
  await expect(page.getByText(`Se creará el SKU padre ${sku} al confirmar la agrupación. Los códigos seleccionados se conservarán como alternos.`,{exact:true})).toBeVisible();
  await expect(page.getByText(group.warnings[0],{exact:true})).toBeVisible();
  // Switching to an existing child must never offer the virtual parent as a source.
  await page.getByLabel('SKU interno de destino',{exact:true}).selectOption(sourceSkus[0]);
  await expect(page.getByLabel(`Agrupar ${sku} bajo ${sourceSkus[0]}`,{exact:true})).toHaveCount(0);
  await page.getByLabel('SKU interno de destino',{exact:true}).selectOption(sku);
  for(const code of sourceSkus) await page.getByLabel(`Agrupar ${code} bajo ${sku}`,{exact:true}).check();
  const action=page.getByRole('button',{name:`Agrupar 4 alternos bajo ${sku}`,exact:true});
  await expect(action).toBeDisabled();
  expect(submitted).toBeUndefined();
  await page.getByLabel('Confirmo que los SKU seleccionados son el mismo repuesto.',{exact:true}).check();
  await action.click();
  expect(submitted).toEqual({target_sku:sku,source_skus:sourceSkus,create_target:true});
});
