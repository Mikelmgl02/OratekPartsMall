import { expect, Page, test } from '@playwright/test';

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');
const origin = process.env.E2E_BASE_URL || 'http://localhost:8080';

async function admin(page: Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: origin, httpOnly: true, sameSite: 'Strict' }]);
}

for (const size of [{ name: 'desktop', width: 1440, height: 1000 }, { name: 'mobile', width: 390, height: 844 }]) {
  test(`${size.name}: upload gallery, change cover, describe, reload and delete through Spaces`, async ({ page }) => {
    test.setTimeout(90000);
    await admin(page);
    await page.setViewportSize(size);
    const sku = `MEDIA-E2E-${process.env.E2E_MEDIA_RUN}-${size.name}`.toUpperCase();
    const created = await page.request.post('/api/management/catalog', { headers: { Origin: origin }, data: { sku, name: 'REPUESTO DE PRUEBA', active: false, codes: [] } });
    expect(created.status()).toBe(201);
    const part = await created.json();
    const base = `/api/management/catalog/${part.id}/images`;
    await page.goto('/administracion?seccion=inventario');
    await page.getByLabel('Buscar repuestos', { exact: true }).fill(sku);
    await page.getByRole('button', { name: `Imágenes de ${sku}`, exact: true }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('Este SKU todavía no tiene imágenes.', { exact: true })).toBeVisible();
    const files = [
      { name: 'front.png', mimeType: 'image/png', buffer: png },
      { name: 'corrupt.png', mimeType: 'image/png', buffer: Buffer.from('invalid image') },
      { name: 'back.png', mimeType: 'image/png', buffer: png },
    ];
    if (size.name === 'desktop') {
      const transfer = await page.evaluateHandle(values => {
        const data = new DataTransfer();
        for (const value of values) data.items.add(new File([new Uint8Array(value.bytes)], value.name, { type: value.mimeType }));
        data.items.add(new File(['not an image'], 'document.pdf', { type: 'application/pdf' }));
        return data;
      }, files.map(file => ({ name: file.name, mimeType: file.mimeType, bytes: [...file.buffer] })));
      await dialog.dispatchEvent('dragenter', { dataTransfer: transfer });
      await expect(dialog).toHaveClass(/catalog-media-dragging/);
      // Crossing child elements must not make the drop highlight flicker.
      await dialog.locator('.modal-header').dispatchEvent('dragenter', { dataTransfer: transfer });
      await dialog.dispatchEvent('dragleave', { dataTransfer: transfer });
      await expect(dialog).toHaveClass(/catalog-media-dragging/);
      await dialog.locator('.modal-header').dispatchEvent('dragleave', { dataTransfer: transfer });
      await expect(dialog).not.toHaveClass(/catalog-media-dragging/);
      let release = () => {};
      const hold = new Promise<void>(resolve => { release = resolve; });
      let uploadRequests = 0;
      await page.route(`**${base}`, async route => {
        if (route.request().method() === 'POST') { uploadRequests++; if (uploadRequests === 1) await hold; }
        await route.continue();
      });
      try {
        await dialog.dispatchEvent('dragenter', { dataTransfer: transfer });
        // The dialog header accepts drops too, beyond just the file picker.
        await dialog.locator('.modal-header').dispatchEvent('drop', { dataTransfer: transfer });
        await expect(dialog.locator('.catalog-media-editor')).toHaveAttribute('aria-busy', 'true');
        await expect(dialog).not.toHaveClass(/catalog-media-dragging/);
        await dialog.dispatchEvent('drop', { dataTransfer: transfer });
      } finally { release(); }
      await expect(dialog.getByRole('status')).toContainText('2 imágenes agregadas.', { timeout: 45000 });
      await expect(dialog.getByRole('alert')).toContainText('document.pdf: usa una imagen JPEG, PNG o WebP.');
      expect(uploadRequests).toBe(3);
      await transfer.dispose();
    } else {
      await dialog.getByLabel('Agregar imágenes al SKU', { exact: true }).setInputFiles(files);
    }
    await expect(dialog.getByRole('status')).toContainText('2 imágenes agregadas.', { timeout: 45000 });
    await expect(dialog.getByRole('alert')).toContainText('corrupt.png');
    await expect(dialog.getByRole('article')).toHaveCount(2);
    const uploaded = await (await page.request.get(base)).json();
    expect(uploaded[0].url).toMatch(/https:\/\/.+\/catalog\//);
    expect(uploaded[0].url).not.toContain('Signature=');
    await expect.poll(() => dialog.locator('img').first().evaluate((image: HTMLImageElement) => image.naturalWidth), { timeout: 20000 }).toBeGreaterThan(0);
    await dialog.getByRole('article', { name: 'Imagen 2', exact: true }).getByRole('button', { name: 'Usar de portada', exact: true }).click();
    await expect(dialog.getByRole('article', { name: 'Imagen 1', exact: true }).locator('img')).toHaveAttribute('src', uploaded[1].thumbnail_url);
    await dialog.getByLabel('Descripción de imagen', { exact: true }).first().fill('vista frontal');
    await dialog.getByRole('button', { name: 'Guardar descripción de imagen 1', exact: true }).click();
    await expect(dialog.getByRole('status')).toContainText('Descripción de imagen guardada.');
    await page.screenshot({ path: `../output/ui/catalog-media-${size.name}.png` });
    expect(await dialog.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
    await page.reload();
    await page.getByLabel('Buscar repuestos', { exact: true }).fill(sku);
    await page.getByRole('button', { name: `Imágenes de ${sku}`, exact: true }).click();
    await expect(dialog.getByLabel('Descripción de imagen', { exact: true }).first()).toHaveValue('VISTA FRONTAL');
    await dialog.getByRole('article', { name: 'Imagen 1', exact: true }).getByRole('button', { name: 'Eliminar imagen', exact: true }).click();
    await dialog.getByRole('button', { name: 'Sí, eliminar', exact: true }).click();
    await expect(dialog.getByRole('article')).toHaveCount(1);
    await expect(dialog.getByRole('article').locator('img')).toHaveAttribute('src', uploaded[0].thumbnail_url);
    await expect(dialog.getByText('PORTADA', { exact: true })).toBeVisible();
    const remaining = await (await page.request.get(base)).json();
    expect(remaining).toHaveLength(1);
    await page.request.delete(`${base}/${remaining[0].id}`, { headers: { Origin: origin } });
  });
}

test('catalog cover and detail thumbnails display the selected photo with fallback for missing images', async ({ page }) => {
  const part = { id: 'media-part', sku: 'MEDIA-SKU', name: 'REPUESTO CON FOTOS', description: 'REPUESTO', codes: [], images: [1, 2].map(id => ({ id: String(id), url: `/test-images/full-${id}.png`, thumbnail_url: `/test-images/thumb-${id}.png`, alt_text: `VISTA ${id}`, position: id - 1, width: 1, height: 1, size_bytes: 20 })) };
  await page.route('**/test-images/*.png', route => route.fulfill({ contentType: 'image/png', body: png }));
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 1, username: 'CLIENTE', is_superuser: false }, accounts: { count: 1, next: null, previous: null, results: [{ id: 'supplier', name: 'PROVEEDOR', roles: ['supplier_retail'], capabilities: ['supplier'] }] } } }));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({ json: { count: 1, next: null, previous: null, results: [part] } }));
  await page.route('**/api/market/wishlist/state', route => route.fulfill({ json: { part_ids: [], count: 0 } }));
  await page.goto('/');
  const card = page.getByRole('article', { name: 'Repuesto MEDIA-SKU', exact: true });
  await expect(card.locator('img')).toHaveAttribute('src', '/test-images/thumb-1.png');
  await card.getByRole('button', { name: 'Ver REPUESTO CON FOTOS', exact: true }).click();
  const detail = page.getByRole('dialog');
  await expect(detail.locator('.detail-art img')).toHaveAttribute('src', '/test-images/full-1.png');
  await detail.getByRole('button', { name: 'Ver imagen 2 de MEDIA-SKU', exact: true }).click();
  await expect(detail.locator('.detail-art img')).toHaveAttribute('src', '/test-images/full-2.png');
  await expect(detail.getByText('2 / 2', { exact: true })).toBeVisible();
  await page.screenshot({ path: '../output/ui/catalog-gallery-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await detail.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
  await detail.locator('.detail-art img').dispatchEvent('error');
  await expect(detail.locator('.detail-art img')).toHaveCount(0);
  await expect(detail.locator('.detail-art svg')).toBeVisible();
});
