import {test,expect,type Page} from '@playwright/test';

// Agrupación inteligente on matching on References (S6): one card per case kind, decided on matching/<id> with the case's fingerprint and,
// where a line is chosen, the line's id. The backend is mocked in the browser (the proxy's allowlist is admin-proxy-routes' job).
type Code = {brand:string;code:string};
type Row = {id:string;sku:string;description:string;name?:string;brand?:string;estado?:string;supplier_invent_id?:string;references?:Code[];codes?:Code[];codigo_pendiente?:string|null;brand_pendiente?:string|null};
type Case = {id:string;kind:string;sources:Row[];candidates:Row[];target_sku:string;reason:string;status:string;fingerprint:string;supplier_name:string;ai:Record<string,unknown>;
  lines?:Row[];new_code?:string;new_brand?:string;reference?:Record<string,unknown>|null;guard?:string;route?:string;batch_key?:string;evidence?:Record<string,unknown>};

const empty = {count:0,next:null,previous:null,results:[],pending_count:0};
const uuid = (group:number,n:number) => `0${group}000000-0000-4000-8000-${String(n).padStart(12,'0')}`;
const fp = (n:number) => String(n).repeat(64).slice(0,64);
const item = (n:number,sku:string,brand:string,description:string,extra:Partial<Row>={}):Row => ({id:uuid(1,n),sku,brand,description,supplier_invent_id:`SUP-${n}`,references:[],...extra});
const line = (n:number,sku:string,description:string,extra:Partial<Row>={}):Row => ({id:uuid(2,n),sku,name:sku,description,codes:[],estado:'publicada',...extra});
const base = (n:number,kind:string,extra:Partial<Case>):Case => ({id:uuid(3,n),kind,sources:[],candidates:[],target_sku:'',reason:'',status:'review',fingerprint:fp(n%10),supplier_name:'REPUESTOS DEL ISTMO',ai:{},...extra});

const drum = 'TAMBOR HYUNDAI ACCENT 11-16';
const L = {
  accent:line(1,'58411-1R000',drum,{codes:[{code:'58-1R0',brand:''}]}), accentNew:line(2,'58411-1R100','TAMBOR HYUNDAI ACCENT 17-20'), gmb:line(3,'D-HYU-1R',drum),
  disc:line(4,'43512-60150','DISCO FRENO DEL TOY LAND CRUISER 300MM'), clutchNis:line(5,'EC-01','KIT EMBRAGUE NISSAN FRONTIER D22'),
  clutchHyu:line(6,'EC01','KIT EMBRAGUE HYUNDAI H100',{estado:'preparacion'}), sealAs:line(7,'04111-22152-AS','JUEGO EMPAQUES MOTOR TOY 4AFE'),
  seal:line(8,'04111-22152','JUEGO EMPAQUES MOTOR TOY 4AFE'), lampL:line(9,'81560-0K010','FARO TRASERO TOY HILUX IZQ'), lampR:line(10,'81550-0K010','FARO TRASERO TOY HILUX DER'),
  picked:line(11,'EC-01-NIS','KIT EMBRAGUE NISSAN FRONTIER D22 4X4'),
};
const cases = {
  codeChange:base(1,'code_change',{sources:[item(1,'58411-1R000','HYUNDAI',drum,{codigo_pendiente:'58411-1R100',brand_pendiente:null})],lines:[L.accentNew],
    new_code:'58411-1R100',new_brand:'HYUNDAI',reference:{id:501,code:'58411-1R000',brand:'HYUNDAI',tipo:'oem',line:L.accent.id,line_sku:'58411-1R000'},
    reason:'El proveedor cambió el código de este artículo y el código nuevo está en otra línea OEM.'}),
  reference:base(2,'reference',{sources:[item(2,'D-HYU-1R','GMB',drum,{references:[{code:'58411-1R000',brand:'HYUNDAI'}]})],lines:[L.gmb,L.accent],
    new_code:'58411-1R000',new_brand:'HYUNDAI',reference:{id:502,code:'D-HYU-1R',brand:'GMB',tipo:'aftermarket',line:L.gmb.id,line_sku:'D-HYU-1R'},
    reason:'El proveedor declaró una referencia que no está en la línea OEM del artículo.'}),
  variant:base(3,'variant',{sources:[item(3,'43512-60150','TOYOTA','DISCO FRENO DEL TOY LAND CRUISER 280MM')],lines:[L.disc],target_sku:L.disc.sku,guard:'spec_conflict',
    reason:'El diámetro del artículo (280 MM) no coincide con el de la línea OEM (300 MM).'}),
  line:base(4,'line',{sources:[item(4,'EC-01','','KIT EMBRAGUE NISSAN FRONTIER')],lines:[L.clutchNis,L.clutchHyu],reason:'El código sin marca está en 2 líneas OEM.',
    ai:{target_sku:'EC-01',reason:'La descripción del artículo nombra NISSAN FRONTIER.',confidence:.7}}),
  merge:base(5,'merge',{supplier_name:'',lines:[L.sealAs,L.seal],target_sku:L.seal.sku,route:'lote',batch_key:'legacy-auto-merge',
    evidence:{numbers:['04111-22152'],codes:[{brand:'AS',code:'04111-22152-AS'}]},reason:'Unificación automática anterior que las reglas nuevas no harían.'}),
  // A merge case written before the lines field: its lines come from sources and candidates.
  keep:base(6,'merge',{supplier_name:'',sources:[L.lampL],candidates:[L.lampR],target_sku:L.lampR.sku,guard:'side',reason:'Comparten el número 81550-0K010 en un catálogo.'}),
};

// The overview of each status from the cases' state; a decision moves its case to applied (dismissed for Rechazar / Mantener separadas).
async function mockMatching(page:Page,list:Case[],{refuse=false,overview={}}:{refuse?:boolean;overview?:Record<string,unknown>}={}) {
  test.skip(!process.env.E2E_ADMIN_TOKEN,'Disposable superuser token required.');
  await page.context().addCookies([{name:'partsmall_session',value:process.env.E2E_ADMIN_TOKEN!,url:'http://localhost:8080',httpOnly:true,sameSite:'Strict'}]);
  const state = new Map(list.map(row=>[row.id,{...row}]));
  const payloads:Record<string,unknown[]> = {}, queries:string[] = [];
  await page.route(/\/api\/management\/(catalog|roles|catalog\/import\/issues)(?:\?.*)?$/,route=>{
    const url=new URL(route.request().url()), search=(url.searchParams.get('search')||'').toUpperCase();
    if (url.pathname.endsWith('/roles')) return route.fulfill({json:[]});
    if (url.pathname.endsWith('/catalog') && search) {  // the line search of "Buscar otra línea OEM" (PartPicker)
      const results=[L.picked].filter(row=>row.sku.includes(search)).map(row=>({...row,active:true,is_OEM:false,category:'',subcategory:'',part_type:null,stock_record_count:0,images:[]}));
      return route.fulfill({json:{count:results.length,next:null,previous:null,results}});
    }
    return route.fulfill({json:empty});
  });
  await page.route(/\/api\/management\/matching(?:\?.*)?$/,route=>{
    const params=new URL(route.request().url()).searchParams, status=params.get('status')||'review', kind=params.get('kind')||'';
    queries.push(params.toString());
    const rows=[...state.values()], shown=rows.filter(row=>row.status===status&&(!kind||row.kind===kind));
    const counts:Record<string,number>={};
    for (const row of rows) counts[row.status]=(counts[row.status]??0)+1;
    return route.fulfill({json:{running:false,queued:false,last_run:null,summary:{},error:'',counts,count:shown.length,next_offset:null,results:shown,...overview}});
  });
  await page.route(/\/api\/management\/matching\/[0-9a-f-]{36}$/,route=>{
    const id=route.request().url().split('/').pop()!, body=route.request().postDataJSON() as {action:string};
    (payloads[id]??=[]).push(body);
    if (refuse) return route.fulfill({status:409,json:{detail:'Los datos cambiaron. Actualiza el análisis antes de continuar.'}});
    const row=state.get(id)!;
    row.status=['dismiss','keep_apart'].includes(body.action)?'dismissed':'applied';
    return route.fulfill({json:row});
  });
  return {payloads,queries};
}
async function openMatching(page:Page) {
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button',{name:'Agrupación inteligente',exact:true}).click();
  return page.getByRole('dialog');
}

for(const viewport of [{width:1440,height:1000},{width:390,height:844}]) {
  test(`each case kind has its card and decides by line id at ${viewport.width}px`,async({page})=>{
    const mock=await mockMatching(page,Object.values(cases));
    await page.setViewportSize(viewport);
    const dialog=await openMatching(page);
    const card=(name:string)=>dialog.getByRole('article',{name,exact:true});
    await expect(card('CAMBIO DE CÓDIGO 58411-1R000')).toBeVisible();
    expect(await dialog.evaluate(el=>el.scrollWidth>el.clientWidth)).toBe(false);
    await page.screenshot({path:`/tmp/motionpartes-smart-matching-cases-${viewport.width}.png`,fullPage:true});

    // code_change: the code the item sells under and the one waiting, out of stock until decided.
    const change=card('CAMBIO DE CÓDIGO 58411-1R000');
    await expect(change.getByText('CÓDIGO ACTUAL',{exact:true})).toBeVisible();
    await expect(change.getByRole('group',{name:'Cambio de código'}).getByText('58411-1R100',{exact:true})).toBeVisible();
    await expect(change.getByText('EL CÓDIGO NUEVO YA ESTÁ EN: 58411-1R100')).toBeVisible();
    await expect(change.getByText('LÍNEA OEM DEL ARTÍCULO: 58411-1R000')).toBeVisible();
    await expect(change.getByText('Mientras se decide, este artículo no cuenta en las existencias.')).toBeVisible();
    await change.getByRole('button',{name:'Aprobar',exact:true}).click();
    await expect(dialog.getByRole('status').filter({hasText:'Cambio de código aprobado.'})).toBeVisible();
    expect(mock.payloads[cases.codeChange.id]).toEqual([{action:'approve',fingerprint:cases.codeChange.fingerprint}]);
    await expect(change).toHaveCount(0);  // decided: it leaves POR REVISAR

    // reference: Elegir línea shows the lines involved; the declared code is approved on the one chosen.
    const reference=card('REFERENCIA DECLARADA D-HYU-1R');
    await expect(reference.locator('p.matching-code')).toHaveText(/^REFERENCIA DECLARADA\s*58411-1R000\s*HYUNDAI$/);
    await expect(reference.getByRole('radio')).toHaveCount(0);
    await reference.getByRole('button',{name:'Elegir línea',exact:true}).click();
    await expect(reference.getByRole('button',{name:'Aprobar en la línea elegida',exact:true})).toBeDisabled();
    await reference.getByRole('radio',{name:/^58411-1R000/}).check();
    await reference.getByRole('button',{name:'Aprobar en la línea elegida',exact:true}).click();
    await expect(dialog.getByRole('status').filter({hasText:'Referencia aprobada en la línea OEM.'})).toBeVisible();
    expect(mock.payloads[cases.reference.id]).toEqual([{action:'approve',fingerprint:cases.reference.fingerprint,line:L.accent.id}]);

    // variant: the line it attached to is proposed; Separar referencia keeps its code as the supplier's own.
    const variant=card('VARIANTE EN REVISIÓN 43512-60150');
    await expect(variant.getByText('Las especificaciones no coinciden con las de la línea OEM.')).toBeVisible();
    await expect(variant.getByRole('radio',{name:/^43512-60150/})).toBeChecked();
    await variant.getByRole('button',{name:'Separar referencia',exact:true}).click();
    await expect(dialog.getByRole('status').filter({hasText:'Referencia separada'})).toBeVisible();
    expect(mock.payloads[cases.variant.id]).toEqual([{action:'separar_referencia',fingerprint:cases.variant.fingerprint}]);

    // line: nothing is chosen for the admin; the AI's line and a line still in preparación are marked.
    const choose=card('ELEGIR LÍNEA OEM EC-01');
    await expect(choose.getByRole('button',{name:'Vincular',exact:true})).toBeDisabled();
    await expect(choose.getByRole('radio',{name:/^EC-01\s*PROPUESTA IA/})).not.toBeChecked();
    await expect(choose.getByRole('radio',{name:/^EC01\s*EN PREPARACIÓN/})).toBeVisible();
    await choose.getByRole('radio',{name:/^EC01/}).check();
    await choose.getByRole('button',{name:'Vincular',exact:true}).click();
    await expect(dialog.getByRole('status').filter({hasText:'Artículo vinculado a la línea OEM elegida.'})).toBeVisible();
    expect(mock.payloads[cases.line.id]).toEqual([{action:'approve',fingerprint:cases.line.fingerprint,line:L.clutchHyu.id}]);

    // merge (the legacy-auto-merge batch): the proposed survivor is preselected and another may be kept instead.
    const merge=card('UNIFICAR LÍNEAS 04111-22152-AS + 04111-22152');
    await expect(merge.getByText('LOTE · UNIFICACIONES AUTOMÁTICAS ANTERIORES',{exact:true})).toBeVisible();
    await expect(merge.getByText('NÚMEROS OEM: 04111-22152',{exact:true})).toBeVisible();
    await expect(merge.getByText('CÓDIGOS: AS: 04111-22152-AS',{exact:true})).toBeVisible();
    await expect(merge.getByRole('radio',{name:/^04111-22152\s*JUEGO/})).toBeChecked();
    await merge.getByRole('radio',{name:/^04111-22152-AS/}).check();
    await merge.getByRole('button',{name:'Unificar',exact:true}).click();
    await expect(dialog.getByRole('status').filter({hasText:'Líneas unificadas.'})).toBeVisible();
    expect(mock.payloads[cases.merge.id]).toEqual([{action:'merge',fingerprint:cases.merge.fingerprint,line:L.sealAs.id}]);

    // merge, Mantener separadas (its lines read from sources and candidates).
    const keep=card('UNIFICAR LÍNEAS 81560-0K010 + 81550-0K010');
    await expect(keep.getByText('El lado no coincide (izquierdo / derecho, delantero / trasero).')).toBeVisible();
    await keep.getByRole('button',{name:'Mantener separadas',exact:true}).click();
    await expect(dialog.getByRole('status').filter({hasText:'Las líneas se mantendrán separadas'})).toBeVisible();
    expect(mock.payloads[cases.keep.id]).toEqual([{action:'keep_apart',fingerprint:cases.keep.fingerprint}]);

    await expect(dialog.getByText('No hay coincidencias en este estado.',{exact:true})).toBeVisible();
    await expect(dialog.getByRole('button',{name:'APLICADAS · 5',exact:true})).toBeVisible();
    await expect(dialog.getByRole('button',{name:'DESCARTADAS · 1',exact:true})).toBeVisible();
    await expect(dialog.getByRole('button',{name:/SIN COINCIDENCIA/})).toHaveCount(0);  // the history before S6, empty here
  });
}

test('a code change and a declared reference can be rejected, and a reference approved on the item\'s own line', async ({page}) => {
  const second={...cases.reference,id:uuid(3,7),fingerprint:fp(7),sources:[item(7,'D-HYU-2R','GMB',drum,{references:[{code:'58411-2R000',brand:''}]})],new_code:'58411-2R000',new_brand:''};
  const mock=await mockMatching(page,[cases.codeChange,cases.reference,second]);
  const dialog=await openMatching(page);
  await dialog.getByRole('article',{name:'CAMBIO DE CÓDIGO 58411-1R000'}).getByRole('button',{name:'Rechazar',exact:true}).click();
  await expect(dialog.getByRole('status').filter({hasText:'Cambio de código rechazado. El artículo conserva su código actual.'})).toBeVisible();
  expect(mock.payloads[cases.codeChange.id]).toEqual([{action:'dismiss',fingerprint:cases.codeChange.fingerprint}]);
  await dialog.getByRole('article',{name:'REFERENCIA DECLARADA D-HYU-1R'}).getByRole('button',{name:'Aprobar',exact:true}).click();
  await expect(dialog.getByRole('status').filter({hasText:'Referencia aprobada en la línea OEM.'})).toBeVisible();
  expect(mock.payloads[cases.reference.id]).toEqual([{action:'approve',fingerprint:cases.reference.fingerprint}]);  // no line: the item's own
  const other=dialog.getByRole('article',{name:'REFERENCIA DECLARADA D-HYU-2R'});
  await expect(other.getByText('TODAS LAS MARCAS',{exact:true})).toBeVisible();  // a brandless declared code
  await other.getByRole('button',{name:'Elegir línea',exact:true}).click();
  await other.getByRole('button',{name:'Cancelar elección',exact:true}).click();
  await other.getByRole('button',{name:'Rechazar',exact:true}).click();
  await expect(dialog.getByRole('status').filter({hasText:'Referencia rechazada.'})).toBeVisible();
  expect(mock.payloads[second.id]).toEqual([{action:'dismiss',fingerprint:second.fingerprint}]);
  await expect(dialog.getByRole('button',{name:'DESCARTADAS · 2',exact:true})).toBeVisible();
});

test('Buscar otra línea OEM finds a line by search and Vincular sends its id; a stale case says so', async ({page}) => {
  const mock=await mockMatching(page,[cases.line],{refuse:true});
  const dialog=await openMatching(page);
  const card=dialog.getByRole('article',{name:'ELEGIR LÍNEA OEM EC-01'});
  await card.getByRole('button',{name:'Buscar otra línea OEM',exact:true}).click();
  await card.getByLabel('Buscar línea OEM').fill('EC-01-NIS');
  await expect(card.getByRole('option',{name:/^EC-01-NIS/})).toBeAttached();
  await card.getByRole('combobox',{name:'línea OEM',exact:true}).selectOption(L.picked.id);
  await expect(card.getByRole('radio',{checked:true})).toHaveCount(0);
  await card.getByRole('button',{name:'Vincular',exact:true}).click();
  expect(mock.payloads[cases.line.id]).toEqual([{action:'approve',fingerprint:cases.line.fingerprint,line:L.picked.id}]);
  await expect(dialog.getByRole('alert').filter({hasText:'Los datos cambiaron. Actualiza el análisis antes de continuar.'})).toBeVisible();
  await expect(card).toBeVisible();  // refused: still waiting
});

test('the history keeps the cases before S6 read-only, and the kind filter asks the backend for one kind', async ({page}) => {
  const legacy=base(8,'supplier',{status:'applied',sources:[item(8,'D-HYU-1R','GMB',drum,{references:[{code:'58411-1R000',brand:'HYUNDAI'}]})],candidates:[L.accent],
    target_sku:L.accent.sku,reason:'Coincidencia semántica: requiere confirmar.',ai:{target_sku:L.accent.sku,reason:'Revisar aplicación antes de agrupar.',confidence:.8}});
  const applied={...cases.line,status:'applied',target_sku:L.clutchNis.sku};
  const waiting=base(9,'supplier',{status:'unmatched',sources:[item(9,'X-1','','PIEZA SIN DATOS')],reason:'Sin coincidencia.'});
  const mock=await mockMatching(page,[legacy,applied,waiting],{overview:{kinds:{line:1,supplier:1,merge:0}}});
  const dialog=await openMatching(page);
  await expect(dialog.getByRole('button',{name:'SIN COINCIDENCIA · 1',exact:true})).toBeVisible();  // shown while it still holds cases
  await dialog.getByRole('button',{name:'APLICADAS · 2',exact:true}).click();
  const old=dialog.getByRole('article',{name:'ARTÍCULO DE PROVEEDOR D-HYU-1R',exact:true});
  await expect(old.getByText('REFERENCIAS DECLARADAS: HYUNDAI: 58411-1R000')).toBeVisible();
  await expect(old.getByText('Revisar aplicación antes de agrupar.',{exact:true})).toBeVisible();
  await expect(old.getByRole('button')).toHaveCount(0);
  const done=dialog.getByRole('article',{name:'ELEGIR LÍNEA OEM EC-01'});
  await expect(done.getByRole('group',{name:'Líneas OEM propuestas'})).toBeVisible();
  await expect(done.getByRole('radio',{name:/^EC-01\s*PROPUESTA IA/})).toBeChecked();  // the line it was applied to
  await expect(done.getByRole('radio',{name:/^EC-01\s*PROPUESTA IA/})).toBeDisabled();
  await expect(done.getByRole('button')).toHaveCount(0);
  const kinds=dialog.getByRole('group',{name:'Tipo de caso'});
  await expect(kinds.getByRole('button',{name:'UNIFICAR LÍNEAS · 0'})).toHaveCount(0);
  await kinds.getByRole('button',{name:'ELEGIR LÍNEA OEM · 1',exact:true}).click();
  await expect(old).toHaveCount(0);
  await expect(done).toBeVisible();
  expect(mock.queries.at(-1)).toBe('status=applied&offset=0&kind=line');
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
    await expect(dialog.getByRole('button',{name:'APLICADAS · 1440',exact:true})).toBeVisible();
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
