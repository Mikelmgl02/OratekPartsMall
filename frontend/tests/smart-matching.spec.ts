import {test,expect} from '@playwright/test';

for(const viewport of [{width:1440,height:1000},{width:390,height:844}]) {
  test(`smart matching review persists decisions at ${viewport.width}px`,async({page})=>{
    test.skip(!process.env.E2E_ADMIN_TOKEN,'Disposable superuser token required.');
    await page.context().addCookies([{name:'partsmall_session',value:process.env.E2E_ADMIN_TOKEN!,url:'http://localhost:8080',httpOnly:true,sameSite:'Strict'}]);
    await page.setViewportSize(viewport);
    const source={id:'01000000-0000-4000-8000-000000000001',sku:'D-HYU-1R',description:'TAMBOR HYUNDAI ACCENT 11-16',supplier_invent_id:'SUP-001',references:[{code:'58411-1R000',brand:'HYUNDAI'}]};
    const target={id:'01000000-0000-4000-8000-000000000002',sku:'58411-1R000',description:source.description,codes:[{code:'58-1R0',brand:''}]};
    const id='01000000-0000-4000-8000-000000000003';
    let applied=false;let payload:unknown;
    await page.route(/\/api\/management\/(catalog|roles|catalog\/import\/issues)(?:\?.*)?$/,route=>route.fulfill({json:route.request().url().includes('/roles')?[]:{count:0,results:[],pending_count:0}}));
    await page.route(/\/api\/management\/matching(?:\?.*)?$/,async route=>{
      const status=new URL(route.request().url()).searchParams.get('status')||'review';
      const show=applied?status==='applied':status==='review';
      await route.fulfill({json:{running:false,queued:false,last_run:null,summary:{},error:'',counts:{review:applied?0:1,applied:applied?1:0},count:show?1:0,next_offset:null,results:show?[{id,kind:'supplier',sources:[source],candidates:[target],target_sku:target.sku,reason:'Coincidencia semántica: requiere confirmar.',status:applied?'applied':'review',fingerprint:'a'.repeat(64),supplier_name:'PROVEEDOR DE PRUEBA',ai:{target_sku:target.sku,reason:'Revisar aplicación antes de agrupar.',confidence:.8}}]:[]}});
    });
    await page.route(`**/api/management/matching/${id}`,async route=>{payload=route.request().postDataJSON();applied=true;await route.fulfill({json:{status:'applied'}});});
    await page.goto('/administracion?seccion=inventario');
    await page.getByRole('button',{name:'Agrupación inteligente',exact:true}).click();
    const dialog=page.getByRole('dialog');
    await expect(dialog.getByText('PROVEEDOR DE PRUEBA',{exact:true})).toBeVisible();
    await expect(dialog.getByLabel('Destino para D-HYU-1R')).toHaveAttribute('autocomplete','off');
    await expect(dialog.getByText('Revisar aplicación antes de agrupar.',{exact:true})).toBeVisible();
    await expect(dialog.getByText('REFERENCIAS DECLARADAS: HYUNDAI: 58411-1R000')).toBeVisible();
    await expect(dialog.getByText('REFERENCIAS: 58-1R0')).toBeVisible();
    expect(payload).toBeUndefined();
    expect(await dialog.evaluate(el=>el.scrollWidth>el.clientWidth)).toBe(false);
    await page.screenshot({path:`/tmp/motionpartes-smart-matching-${viewport.width}.png`});
    await dialog.getByRole('button',{name:'Confirmar coincidencia',exact:true}).click();
    await expect(dialog.getByText('Coincidencia aprobada y guardada para próximas cargas.',{exact:true})).toBeVisible();
    expect(payload).toEqual({action:'approve',fingerprint:'a'.repeat(64),target_sku:target.sku});
    await dialog.getByRole('button',{name:'APLICADAS · 1',exact:true}).click();
    await expect(dialog.getByText(source.sku,{exact:true})).toBeVisible();
    await expect(dialog.getByRole('button',{name:'Confirmar coincidencia',exact:true})).toHaveCount(0);
  });
}

for (const width of [1440, 390]) {
  test(`analysis shows queue, running stage and completed no-op at ${width}px`, async ({page}) => {
    test.skip(!process.env.E2E_ADMIN_TOKEN, 'Disposable superuser token required.');
    await page.context().addCookies([{name:'partsmall_session',value:process.env.E2E_ADMIN_TOKEN!,url:'http://localhost:8080',httpOnly:true,sameSite:'Strict'}]);
    await page.setViewportSize({width,height:1000});
    let state='idle';
    const done = {catalog_count:31637,supplier_total:23305,supplier_already_matched:23305,supplier_examined:0,matched_items:0,grouped_skus:0,created_parents:0,promoted_oems:0,ai_analyzed:0,ai_status:'no_pending_cases',oem_skus:0,non_oem_skus:31637};
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
    await expect(dialog.getByRole('status').filter({hasText:'Vinculando artículos de proveedores pendientes…'})).toBeVisible();
    state='completed';
    // Completion arrives through the normal poll, without a reload or click.
    await expect(dialog.getByText('ANÁLISIS COMPLETADO',{exact:true})).toBeVisible({timeout:10000});
    await expect(dialog.getByText(/IA no utilizada: no hay casos ambiguos/)).toBeVisible();
    await expect(dialog.getByText(/Sin cambios en el catálogo ni en los vínculos/)).toBeVisible();
    await expect(dialog.getByText(/Análisis en cola|Análisis programado/)).toHaveCount(0);
    await expect(dialog.getByRole('button',{name:'Analizar catálogo y proveedores',exact:true})).toBeEnabled();
    await expect(dialog.getByText(/Este botón no busca OEM en internet ni clasifica categorías/)).toBeVisible();
    await expect(dialog.getByRole('button',{name:'APLICADAS · 1440',exact:true})).toBeVisible();
    expect(await dialog.evaluate(el=>el.scrollWidth>el.clientWidth)).toBe(false);
    await page.screenshot({path:`/tmp/motionpartes-matching-progress-${width}.png`});
  });
}
