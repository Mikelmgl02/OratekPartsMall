import { expect, type Page } from '@playwright/test';

// The storefront header keeps the active account (and switching it), favorites and requests in the account menu, "Cuenta y listas";
// its button names the active account once signed in.
const trigger = (page: Page) => page.getByRole('button', { name: 'Cuenta y listas', exact: true });
const menu = (page: Page) => page.getByRole('region', { name: 'Cuenta y listas', exact: true });

export async function switchAccount(page: Page, accountId: string) {
  await trigger(page).click();
  await menu(page).getByLabel('Cambiar cuenta desde el menú', { exact: true }).selectOption(accountId);
}

export async function expectAccount(page: Page, accountId: string) {
  await trigger(page).click();
  await expect(menu(page).getByLabel('Cambiar cuenta desde el menú', { exact: true })).toHaveValue(accountId);
  await page.keyboard.press('Escape');
  await expect(menu(page)).toBeHidden();
}

export async function expectSignedIn(page: Page) {
  await expect(trigger(page)).not.toContainText('inicia sesión');
  await expect(trigger(page)).toContainText('Hola,');
}

export async function expectSignedOut(page: Page) {
  await expect(trigger(page)).toContainText('inicia sesión');
}

export async function openFavorites(page: Page) {
  await trigger(page).click();
  await menu(page).getByRole('button', { name: /^Mis favoritos/ }).click();
}
