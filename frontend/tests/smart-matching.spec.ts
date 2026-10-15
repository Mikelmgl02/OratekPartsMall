import {test,expect} from '@playwright/test';

// Agrupación inteligente (S8): the matching pass's state, its last run and what it left to decide, by kind, with the way to Casos de
// coincidencia, which decides the cases (tests/matching-cases.spec.ts covers its cards). The backend is mocked in the browser.
test('the analysis lists what waits by kind and opens Casos de coincidencia', async ({page}) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'Disposable superuser token required.');
  await page.context().addCookies([{name:'partsmall_session',value:process.env.E2E_ADMIN_TOKEN!,url:'http://localhost:8080',httpOnly:true,sameSite:'Strict'}]);
  await page.route(/\/api\/management\/(catalog|roles|catalog\/import\/issues|lines)(?:\?.*)?$/,route=>route.fulfill({json:route.request().url().includes('/roles')?[]:{count:0,results:[],pending_count:0}}));
  await page.route(/\/api\/management\/matching(?:\?.*)?$/,route=>route.fulfill({json:{state:'completed',running:false,queued:false,stage:'completed',last_run:'2026-10-15T01:47:37Z',
    summary:{matched_items:12,placed_items:3,ai_analyzed:0,ai_status:'no_pending_cases'},error:'',counts:{review:455,applied:1575,dismissed:4191},kinds:{merge:313,reference:139,line:1,variant:1,supplier:1},count:455,next_offset:null,results:[]}}));
  await page.route(/\/api\/management\/cases(?:\?.*)?$/,route=>route.fulfill({json:{count:0,next:null,previous:null,kinds:{},results:[]}}));
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button',{name:'Agrupación inteligente',exact:true}).click();
  const dialog=page.getByRole('dialog');
  const pending=dialog.getByRole('region',{name:'Casos por decidir'});
  await expect(pending).toContainText('CASOS POR DECIDIR · 455');
  await expect(pending.getByRole('listitem').first()).toHaveText(/REFERENCIA DECLARADA\s*139/);  // in the queue's kind order
  await expect(pending).toContainText('UNIFICAR LÍNEAS313');
  await expect(pending).toContainText('APLICADOS · 1,575 · DESCARTADOS · 4,191');
  await expect(dialog.getByRole('article')).toHaveCount(0);  // the cards live in the queue now
  await pending.getByRole('link',{name:'Abrir Casos de coincidencia'}).click();
  await expect(page).toHaveURL(/seccion=casos/);
  await expect(page.getByRole('region',{name:'Casos de coincidencia'})).toBeVisible();
});

for (const width of [1440, 390]) {
  test(`analysis shows queue, running stage and completed no-op at ${width}px`, async ({page}) => {
    test.skip(!process.env.E2E_ADMIN_TOKEN, 'Disposable superuser token required.');
    await page.context().addCookies([{name:'partsmall_session',value:process.env.E2E_ADMIN_TOKEN!,url:'http://localhost:8080',httpOnly:true,sameSite:'Strict'}]);
    await page.setViewportSize({width,height:1000});
    let state='idle';
    const done = {catalog_count:31637,supplier_total:23305,supplier_already_matched:23305,supplier_examined:0,matched_items:0,placed_items:0,grouped_skus:0,created_parents:0,promoted_oems:0,marked_exact_oems:0,ai_analyzed:0,ai_status:'no_pending_cases',oem_skus:0,non_oem_skus:31637};
    await page.route(/\/api\/management\/(catalog|roles|catalog\/import\/issues)(?:\?.*)?$/,route=>route.fulfill({json:route.request().url().includes('/roles')?[]:{count:0,results:[],pending_count:0}}));
    await page.route(/\/api\/management\/matching(?:\?.*)?$/,route=>{
      if (route.request().method()==='POST') {state='queued';return route.fulfill({status:202,json:{queued:true}});}
      return route.fulfill({json:{state,running:state==='running',queued:['queued','running'].includes(state),stage:state==='running'?'suppliers':'completed',last_run:state==='completed'?'2026-10-05T23:47:37Z':null,summary:state==='completed'?done:{},error:'',counts:{applied:1440},count:0,next_offset:null,results:[]}});
    });
    await page.goto('/administracion?seccion=inventario');
    await page.getByRole('button',{name:'Agrupación inteligente',exact:true}).click();
    const dialog=page.getByRole('dialog');
    await dialog.getByRole('button',{name:'Analizar catálogo y proveedores',exact:true}).click();
    await expect(dialog.getByRole('status').filter({hasText:'Análisis en cola…'})).toBeVisible();
    state='running';
    await dialog.getByRole('button',{name:'Actualizar',exact:true}).click();
    await expect(dialog.getByRole('status').filter({hasText:'Vinculando artículos de proveedores por sus referencias…'})).toBeVisible();
    state='completed';
    // Completion arrives through the normal poll, without a reload or click.
    await expect(dialog.getByText('ANÁLISIS COMPLETADO',{exact:true})).toBeVisible({timeout:10000});
    await expect(dialog.getByText(/IA no utilizada: no hay casos ambiguos/)).toBeVisible();
    await expect(dialog.getByText(/Sin cambios en el catálogo ni en los vínculos/)).toBeVisible();
    await expect(dialog.getByText('LLEVADOS A PRODUCTOS NUEVOS',{exact:true})).toBeVisible();
    // S6 creates no parents and renames no MAIN: those tiles are gone; the list marks only when OEM_EXACT_MARK=1 marked some.
    for (const gone of ['PADRES CREADOS','MAIN CAMBIADOS A OEM','SKU AGRUPADOS','SKU MARCADOS OEM POR LISTA']) await expect(dialog.getByText(gone,{exact:true})).toHaveCount(0);
    await expect(dialog.getByText(/Análisis en cola|Análisis programado/)).toHaveCount(0);
    await expect(dialog.getByRole('button',{name:'Analizar catálogo y proveedores',exact:true})).toBeEnabled();
    await expect(dialog.getByText(/Este botón no busca OEM en internet ni clasifica categorías/)).toBeVisible();
    await expect(dialog.getByText('APLICADOS · 1,440')).toBeVisible();
    expect(await dialog.evaluate(el=>el.scrollWidth>el.clientWidth)).toBe(false);
    await page.screenshot({path:`/tmp/motionpartes-matching-progress-${width}.png`});
  });
}

test('a pass from before S6 that marked SKUs OEM by the OEM list still says so, without its parents or MAIN tiles', async ({page}) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'Disposable superuser token required.');
  await page.context().addCookies([{name:'partsmall_session',value:process.env.E2E_ADMIN_TOKEN!,url:'http://localhost:8080',httpOnly:true,sameSite:'Strict'}]);
  const done = {catalog_count:31405,matched_items:0,grouped_skus:0,created_parents:5,promoted_oems:2,marked_exact_oems:240,ai_analyzed:0,ai_status:'no_pending_cases'};
  await page.route(/\/api\/management\/(catalog|roles|catalog\/import\/issues)(?:\?.*)?$/,route=>route.fulfill({json:route.request().url().includes('/roles')?[]:{count:0,results:[],pending_count:0}}));
  await page.route(/\/api\/management\/matching(?:\?.*)?$/,route=>route.fulfill({json:{state:'completed',running:false,queued:false,stage:'completed',last_run:'2026-10-12T23:47:37Z',summary:done,error:'',counts:{},count:0,next_offset:null,results:[]}}));
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button',{name:'Agrupación inteligente',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByText('SKU MARCADOS OEM POR LISTA',{exact:true})).toBeVisible();
  await expect(dialog.locator('dd',{hasText:'240'})).toBeVisible();
  await expect(dialog.getByText(/Sin cambios en el catálogo/)).toHaveCount(0);
  for (const gone of ['PADRES CREADOS','MAIN CAMBIADOS A OEM']) await expect(dialog.getByText(gone,{exact:true})).toHaveCount(0);
});
