import { expect, Page, test } from '@playwright/test';

const jobId = '01000000-0000-4000-8000-000000000001';
const row = (sku:string,id:string) => ({id,sku,name:'',description:'TAMBOR DE FRENO HYUNDAI ACCENT',category:'',subcategory:'',codes:[]});
const source = row('58411-1R000-G','01000000-0000-4000-8000-000000000002');
const target = row('58411-1R000','01000000-0000-4000-8000-000000000003');
const proposalId = '01000000-0000-4000-8000-000000000004';

async function setup(page:Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN,'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{name:'partsmall_session',value:process.env.E2E_ADMIN_TOKEN!,url:'http://localhost:8080',httpOnly:true,sameSite:'Strict'}]);
  await page.route(/\/api\/management\/(catalog|roles|catalog\/import\/issues)(?:\?.*)?$/,route=>route.fulfill({json:route.request().url().includes('/roles') ? [] : {count:0,next:null,previous:null,results:[],pending_count:0}}));
}

for (const size of [{label:'desktop',width:1440,height:1100},{label:'mobile',width:390,height:844}]) {
  test(`${size.label}: reviewed category and grouping proposals remain saved and require confirmation after edits`,async({page})=> {
    await setup(page);await page.setViewportSize(size);
    let classified=false,applied=false,dismissed=false;
    let posted:unknown;
    const summary=()=>({id:jobId,scope:'unclassified',search:'58411',instructions:'USAR FRENOS',model:'MOCK',total_items:1,batch_size:50,completed_batches:classified?1:0,total_batches:1,status:classified?'completed':'ready',count:classified?1:0,pending_count:classified&&!applied&&!dismissed?1:0,applied_count:applied?1:0,dismissed_count:dismissed?1:0,created_at:'2026-10-02T15:00:00Z'});
    const result=()=>({...summary(),configured:true,offset:0,next_offset:null,results:classified?[{id:proposalId,source_sku:source.sku,target_sku:target.sku,category:'FRENOS',subcategory:'TAMBORES',reason:'Código base completo y descripciones idénticas; requiere revisión.',confidence:0.85,source_reference:target.sku,target_reference:target.sku,status:applied?'applied':dismissed?'dismissed':'pending',decision:applied?{target_sku:target.sku,category:'FRENOS',subcategory:'TAMBORES DE FRENO'}:{},source,candidates:[target]}]:[]});
    await page.route(/\/api\/management\/catalog\/assistant(?:\?.*)?$/,async route=> {
      if(route.request().method()==='POST') {
        const input=route.request().postDataJSON();expect(input).toMatchObject({scope:'unclassified',task:'grouping',search:'58411',instructions:'USAR FRENOS'});
        expect(input.id).toMatch(/^[a-f0-9-]{36}$/);await route.fulfill({status:201,json:result()});
      } else await route.fulfill({json:{configured:true,eligible_count:1,jobs:classified?[summary()]:[]}});
    });
    await page.route(new RegExp(`/api/management/catalog/assistant/${jobId}(?:\\?.*)?$`),async route=> {
      if(route.request().method()==='POST') {
        const input=route.request().postDataJSON();
        if(input.mode==='classify') {expect(input).toEqual({mode:'classify',batch_index:0});classified=true;}
        else if(input.mode==='apply') {posted=input;applied=true;}
        else {dismissed=true;}
      }
      await route.fulfill({json:result()});
    });
    await page.goto('/administracion?seccion=inventario');
    await page.getByRole('button',{name:'Asistente IA',exact:true}).click();
    await page.getByLabel('Tipo de análisis',{exact:true}).selectOption('grouping');
    await page.getByLabel('Filtrar SKU o descripción',{exact:true}).fill('58411');
    await page.getByLabel('Indicaciones para la IA (opcional)',{exact:true}).fill('usar frenos');
    await expect(page.getByLabel('Indicaciones para la IA (opcional)',{exact:true})).toHaveValue('USAR FRENOS');
    await expect(page.getByLabel('Filtrar SKU o descripción',{exact:true})).toHaveAttribute('autocomplete','off');
    await page.getByRole('button',{name:'Analizar inventario con IA',exact:true}).click();
    await expect(page.getByText('Análisis terminado. Revisa las propuestas antes de aplicarlas.',{exact:true})).toBeVisible();
    await expect(page.getByLabel(`Revisión confirmada para ${source.sku}`,{exact:true})).not.toBeChecked();
    await expect(page.getByRole('button',{name:'Aplicar 0 propuestas revisadas',exact:true})).toBeDisabled();
    await expect(page.getByText(`El código ${source.sku} será un alterno de ${target.sku}.`,{exact:true})).toBeVisible();
    await page.getByLabel(`Revisión confirmada para ${source.sku}`,{exact:true}).check();
    await page.getByLabel(`Subgrupo para ${source.sku}`,{exact:true}).fill('tambores de freno');
    await expect(page.getByLabel(`Revisión confirmada para ${source.sku}`,{exact:true})).not.toBeChecked();
    await expect(page.getByLabel(`Subgrupo para ${source.sku}`,{exact:true})).toHaveValue('TAMBORES DE FRENO');
    expect(posted).toBeUndefined();
    expect(await page.getByRole('dialog').evaluate(dialog=>dialog.scrollWidth>dialog.clientWidth)).toBe(false);
    await page.getByLabel(`Revisión confirmada para ${source.sku}`,{exact:true}).check();
    await page.getByRole('button',{name:'Aplicar 1 propuestas revisadas',exact:true}).click();
    expect(posted).toEqual({mode:'apply',decisions:[{id:proposalId,target_sku:target.sku,category:'FRENOS',subcategory:'TAMBORES DE FRENO'}]});
    await expect(page.getByText('Propuestas revisadas aplicadas al inventario.',{exact:true})).toBeVisible();
    await page.getByRole('button',{name:'Cerrar ventana',exact:true}).click();
    await page.reload();await page.getByRole('button',{name:'Asistente IA',exact:true}).click();
    await page.getByRole('region',{name:'Análisis guardados',exact:true}).getByRole('button').click();
    await expect(page.getByText('APLICADA',{exact:true})).toBeVisible();
    await expect(page.getByLabel(`Subgrupo para ${source.sku}`,{exact:true})).toHaveValue('TAMBORES DE FRENO');
  });
}

test('pause finishes the current batch and resumes from saved progress after a provider failure',async({page})=> {
  await setup(page);
  let completed=0,failed=false,resolveBatch:()=>void=()=>{};
  const held=new Promise<void>(resolve=>{resolveBatch=resolve;});
  const result=()=>({id:jobId,scope:'all',search:'',instructions:'',model:'MOCK',total_items:101,batch_size:50,completed_batches:completed,total_batches:3,status:'ready',count:0,pending_count:0,applied_count:0,dismissed_count:0,created_at:'2026-10-02T15:00:00Z',configured:true,offset:0,next_offset:null,results:[]});
  await page.route(/\/api\/management\/catalog\/assistant(?:\?.*)?$/,route=>route.fulfill({json:route.request().method()==='POST'?result():{configured:true,eligible_count:101,jobs:[]}}));
  await page.route(`**/api/management/catalog/assistant/${jobId}`,async route=> {
    if(route.request().method()==='POST') {
      const data=route.request().postDataJSON();expect(data.batch_index).toBe(completed);
      if(completed===0) await held;
      if(completed===1&&!failed) {failed=true;await route.fulfill({status:502,json:{detail:'La IA alcanzó su límite. Reanuda el lote.'}});return;}
      completed++;
    }
    await route.fulfill({json:result()});
  });
  await page.goto('/administracion?seccion=inventario');await page.getByRole('button',{name:'Asistente IA',exact:true}).click();
  await page.getByLabel('Artículos para analizar',{exact:true}).selectOption('all');
  await page.getByRole('button',{name:'Analizar inventario con IA',exact:true}).click();
  await page.getByRole('button',{name:'Pausar análisis',exact:true}).click();resolveBatch();
  await expect(page.getByText('Análisis pausado. El progreso y las propuestas quedaron guardados.',{exact:true})).toBeVisible();
  expect(completed).toBe(1);
  await page.getByRole('button',{name:'Reanudar análisis',exact:true}).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('La IA alcanzó su límite. Reanuda el lote.');
  await page.getByRole('button',{name:'Reanudar análisis',exact:true}).click();
  await expect(page.getByText('Análisis terminado. Revisa las propuestas antes de aplicarlas.',{exact:true})).toBeVisible();
  expect(completed).toBe(3);
});

for (const viewport of [{label:'desktop',width:1440,height:1000},{label:'mobile',width:390,height:844}]) {
  test(`${viewport.label}: fast categories show savings and apply a reviewed category in batches`,async({page})=> {
    await setup(page);await page.setViewportSize(viewport);
    let analyzed=false,applied=0;
    const category='FRENOS',subcategory='PASTILLAS DE FRENO';
    const fastSource={...source,description:'TACO HYU ACCENT 06-10'};
    const result=()=>({id:jobId,task:'categories',scope:'unclassified',search:'',instructions:'',model:'MOCK',
      total_items:205,batch_size:200,page_size:50,completed_batches:analyzed?2:0,total_batches:2,
      status:analyzed?'completed':'ready',count:analyzed?205:0,pending_count:analyzed?205-applied:0,
      applied_count:applied,dismissed_count:0,created_at:'2026-10-05T15:00:00Z',configured:true,offset:0,next_offset:null,
      metrics:{rule_items:analyzed?205:0,total_tokens:0},
      category_groups:analyzed&&applied<205?[{category,subcategory,count:205-applied}]:[],
      results:analyzed?[{id:proposalId,source_sku:source.sku,target_sku:source.sku,category,subcategory,
        reason:'Tipo reconocido en la descripción.',confidence:.98,method:'rule',source_reference:'',target_reference:'',
        status:applied?'applied':'pending',decision:{target_sku:source.sku,category,subcategory},source:fastSource,candidates:[]}]:[]});
    await page.route(/\/api\/management\/catalog\/assistant(?:\?.*)?$/,async route=> {
      if(route.request().method()==='POST') {
        expect(route.request().postDataJSON()).toMatchObject({task:'categories',instructions:''});
        await route.fulfill({status:201,json:result()});
      } else await route.fulfill({json:{configured:true,eligible_count:205,jobs:[]}});
    });
    await page.route(new RegExp(`/api/management/catalog/assistant/${jobId}(?:\\?.*)?$`),async route=> {
      let appliedBatch=0;
      if(route.request().method()==='POST') {
        const input=route.request().postDataJSON();
        if(input.mode==='classify') analyzed=true;
        else if(input.mode==='apply_category') {
          expect(input).toEqual({mode:'apply_category',category,subcategory});
          appliedBatch=Math.min(200,205-applied);applied+=appliedBatch;
        }
      }
      await route.fulfill({json:{...result(),applied_batch:appliedBatch}});
    });
    await page.goto('/administracion?seccion=inventario');
    await page.getByRole('button',{name:'Asistente IA',exact:true}).click();
    await expect(page.getByLabel('Tipo de análisis',{exact:true})).toHaveValue('categories');
    await page.getByRole('button',{name:'Analizar inventario con IA',exact:true}).click();
    await expect(page.getByLabel('Uso del análisis')).toContainText('205');
    await expect(page.getByText('REGLA LOCAL',{exact:true})).toBeVisible();
    await expect(page.getByLabel(`Destino para ${source.sku}`,{exact:true})).toHaveCount(0);
    await page.getByLabel('Categoría para revisar',{exact:true}).selectOption(JSON.stringify([category,subcategory]));
    await expect(page.getByRole('button',{name:'Aplicar categoría a 205 SKU',exact:true})).toBeDisabled();
    await page.getByRole('checkbox',{name:'Revisé esta categoría y apruebo sus 205 propuestas.'}).check();
    await page.screenshot({path:`/tmp/motionpartes-fast-categories-${viewport.label}.png`});
    expect(await page.getByRole('dialog').evaluate(el=>el.scrollWidth>el.clientWidth)).toBe(false);
    await page.getByRole('button',{name:'Aplicar categoría a 205 SKU',exact:true}).click();
    await expect(page.getByText('Categoría aplicada. Las propuestas de baja confianza quedan para revisión individual.',{exact:true})).toBeVisible();
    expect(applied).toBe(205);
  });
}
