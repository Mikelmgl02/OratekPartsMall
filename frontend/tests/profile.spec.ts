import { expect, Page, test } from '@playwright/test';

// Mi perfil (/perfil): names, email (with the current password), password, notices and accounts; reached from the account menu.
const empty = { count: 0, next: null, previous: null, results: [] };
const user = { id: 7, username: 'frank', first_name: 'Francisco', last_name: 'Tang', email: 'frank@example.com', is_superuser: false };
const account = { id: '11111111-1111-4111-8111-111111111111', name: 'Frank Motors', roles: ['client_business'], capabilities: ['client'], permission: 'staff' };

async function mock(page: Page, authenticated = true) {
  const writes: { url: string; body: Record<string, unknown> }[] = [];
  let email = user.email;  // the server's, as the profile changes it
  await page.route('**/api/**', route => route.fulfill({ json: empty }));
  await page.route('**/api/session', route => route.fulfill({ json: authenticated ? { authenticated: true, user, accounts: { ...empty, count: 1, results: [account] } } : { authenticated: false } }));
  await page.route('**/api/market/inbox', route => route.fulfill({ json: { unread_messages: 0, unread_conversations: 0, unseen_notifications: 0, latest_message: null, latest_notification: null } }));
  await page.route('**/api/market/inbox/push', route => route.fulfill({ json: { enabled: false, public_key: '' } }));
  await page.route('**/api/market/inbox/email', route => route.fulfill({ json: { enabled: true, address: email, messages: true, updates: true } }));
  await page.route('**/api/market/inbox/whatsapp', route => route.fulfill({ json: { enabled: true, number: '', opted_in: false, messages: true, updates: true } }));
  await page.route('**/api/session/profile', route => {
    const body = route.request().postDataJSON(); writes.push({ url: 'profile', body });
    if (body.email && body.current_password !== 'ClaveActual938!') return route.fulfill({ status: 400, json: { current_password: ['Escribe tu contraseña actual para cambiar tu correo.'] } });
    if (body.email) email = body.email;
    return route.fulfill({ json: { ...user, ...body, current_password: undefined } });
  });
  await page.route('**/api/session/password', route => { writes.push({ url: 'password', body: route.request().postDataJSON() }); return route.fulfill({ json: { changed: true } }); });
  return writes;
}

test('the account menu leads to Mi perfil, where a person edits their names and, with their password, their email', async ({ page }) => {
  const writes = await mock(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Cuenta y listas', exact: true }).click();
  await expect(page.getByRole('link', { name: 'Mi perfil', exact: true })).toHaveAttribute('href', '/perfil');
  await page.goto('/perfil');
  const profile = page.getByRole('main', { name: 'Mi perfil', exact: true });
  await expect(profile.getByRole('heading', { name: 'Francisco Tang', exact: true })).toBeVisible();
  await expect(profile.getByLabel('Usuario', { exact: true })).toHaveValue('frank');  // read only
  const save = profile.getByRole('button', { name: 'Guardar cambios', exact: true });
  await expect(save).toBeDisabled();  // nothing changed yet
  await profile.getByLabel('Nombre', { exact: true }).fill('Francisco José');
  await save.click();
  await expect(profile.getByRole('status')).toContainText('Tus datos se guardaron.');
  expect(writes.at(-1)).toEqual({ url: 'profile', body: { first_name: 'Francisco José', last_name: 'Tang' } });  // no email, no password
  await expect(profile.getByRole('heading', { name: 'Francisco José Tang', exact: true })).toBeVisible();

  await profile.getByLabel('Correo electrónico', { exact: true }).fill('francisco@example.com');
  await expect(save).toBeDisabled();  // the current password first
  await profile.getByLabel('Contraseña actual', { exact: true }).first().fill('otra');
  await save.click();
  await expect(profile.getByRole('alert')).toContainText('Escribe tu contraseña actual para cambiar tu correo.');
  await profile.getByLabel('Contraseña actual', { exact: true }).first().fill('ClaveActual938!');
  await save.click();
  await expect(profile.getByRole('status')).toContainText('Tus datos se guardaron.');
  expect(writes.at(-1)?.body).toMatchObject({ email: 'francisco@example.com', current_password: 'ClaveActual938!' });
  await expect(profile.getByText('Avisos por correo a', { exact: false })).toContainText('francisco@example.com');  // notices follow the new email
});

test('a new password needs the current one and a matching repeat; notices and accounts are on the same page', async ({ page }) => {
  const writes = await mock(page);
  await page.goto('/perfil');
  const profile = page.getByRole('main', { name: 'Mi perfil', exact: true });
  const password = profile.getByRole('region', { name: 'Contraseña', exact: true });
  const change = password.getByRole('button', { name: 'Cambiar contraseña', exact: true });
  await password.getByLabel('Contraseña actual', { exact: true }).fill('ClaveActual938!');
  await password.getByLabel('Nueva contraseña', { exact: true }).fill('NuevaClave938!');
  await password.getByLabel('Repite la nueva contraseña', { exact: true }).fill('NuevaClave939!');
  await expect(password).toContainText('Las dos contraseñas nuevas no coinciden.');
  await expect(change).toBeDisabled();
  await password.getByLabel('Repite la nueva contraseña', { exact: true }).fill('NuevaClave938!');
  await change.click();
  await expect(password.getByRole('status')).toContainText('Las sesiones de tus otros dispositivos se cerraron.');
  expect(writes.at(-1)).toEqual({ url: 'password', body: { current_password: 'ClaveActual938!', new_password: 'NuevaClave938!' } });
  await expect(password.getByLabel('Nueva contraseña', { exact: true })).toHaveValue('');  // cleared

  const notices = profile.getByRole('region', { name: 'Avisos', exact: true });
  await expect(notices.getByLabel('Tu número de WhatsApp', { exact: true })).toBeVisible();
  await expect(notices.getByLabel('Cambios en tus órdenes').first()).toBeChecked();
  const accounts = profile.getByRole('region', { name: 'Tus cuentas', exact: true });
  await expect(accounts.getByRole('listitem')).toHaveText([/Frank Motors.*Empleado/]);
});

test('signed out, Mi perfil offers to sign in', async ({ page }) => {
  await mock(page, false);
  await page.goto('/perfil');
  await expect(page.getByRole('heading', { name: 'Inicia sesión para ver tu perfil.', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Iniciar sesión', exact: true })).toBeVisible();
});
