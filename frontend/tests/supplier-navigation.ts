import { Page } from '@playwright/test';

export async function openSupplierNavigation(page: Page) {
  const toggle = page.getByRole('button', { name: 'Menú de proveedor', exact: false });
  if (await toggle.isVisible() && await toggle.getAttribute('aria-expanded') === 'false') await toggle.click();
}
