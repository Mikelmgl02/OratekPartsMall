import { expect, test } from '@playwright/test';
import { forwardedQuery } from '../lib/market-proxy';
import { catalogQuery, emptyFilters } from '../lib/catalog-filters';

// What the market proxy passes on to Django. The browser specs mock /api/market before the proxy, so a key the proxy drops (as Mis
// repuestos' mine once was) passes them all while the backend never sees it: these check the proxy itself.
const account = '11111111-1111-4111-8111-111111111111';

test('technical conditions reach catalog and suggestions with the account scope', () => {
  const params = new URLSearchParams(catalogQuery('', 1, { ...emptyFilters, technical: { part_type: account, label: 'DISCOS', rules: [
    { key: 'diameter', operator: 'between', value: '260', value_to: '280', label: 'DIÁMETRO', unit: 'mm' },
  ] } }, account));
  for (const route of ['catalog', 'catalog/suggest']) {
    const forwarded = forwardedQuery(route, params);
    expect(JSON.parse(forwarded.get('technical')!)).toEqual({ part_type: account, rules: [{ key: 'diameter', operator: 'between', value: '260', value_to: '280' }] });
    expect(forwarded.get('account')).toBe(account);
  }
  expect(forwardedQuery('catalog/technical-filters', params).get('account')).toBe(account);
  expect(forwardedQuery('inbox', params).has('technical')).toBe(false);
});

test('the catalog passes Mis repuestos and the active account on, and nothing it does not read', () => {
  const query = forwardedQuery('catalog', new URLSearchParams(
    `search=disco&page=2&mine=cesta&mine=ordenes&mine=favoritos&mine=cesta&mine=otro&account=${account}&category=FRENOS&include_facets=1&unknown=1`));
  expect(query.getAll('mine')).toEqual(['cesta', 'ordenes', 'favoritos']);  // each once; anything else stays out
  expect(query.get('account')).toBe(account);
  expect(Object.fromEntries([...query].filter(([key]) => key !== 'mine'))).toEqual({ search: 'disco', page: '2', category: 'FRENOS', include_facets: '1', account });
  expect(forwardedQuery('catalog/suggest', new URLSearchParams(`search=a&mine=favoritos&account=${account}`)).getAll('mine')).toEqual(['favoritos']);
  // Other routes never take them, and the account must be a uuid.
  expect(forwardedQuery(`accounts/${account}/open-units`, new URLSearchParams(`mine=cesta&account=${account}`)).toString()).toBe('');
  expect(forwardedQuery('catalog', new URLSearchParams('account=not-a-uuid')).has('account')).toBe(false);
});

test('the catalog passes Marca and Calidad on (S7), each value checked', () => {
  const query = forwardedQuery('catalog', new URLSearchParams('brand=KYB&brand=KYB&brand=TOY&brand=%20&calidad=oes&calidad=nope&calidad=original'));
  expect(query.getAll('brand')).toEqual(['KYB', 'TOY']);
  expect(query.getAll('calidad')).toEqual(['oes', 'original']);
  expect(forwardedQuery('catalog/suggest', new URLSearchParams('calidad=alternativo')).getAll('calidad')).toEqual(['alternativo']);
  expect(forwardedQuery(`accounts/${account}/inventory`, new URLSearchParams('brand=KYB&calidad=oes')).toString()).toBe('');
});

test('open-units and one SKU\'s open orders are readable market routes', async ({ request }) => {
  const units = `/api/market/accounts/${account}/open-units`;
  expect((await request.get(units)).status()).toBe(401);  // a session first
  // With a session cookie the proxy passes it on (Django then answers for the token); an unknown route stops at the proxy.
  const foreign = { Cookie: 'partsmall_session=invalid-test-token' };
  expect(await (await request.get(units, { headers: foreign })).text()).not.toContain('La ruta solicitada no existe');
  expect(await (await request.get(`${units}/${account}`, { headers: foreign })).text()).not.toContain('La ruta solicitada no existe');  // one SKU's orders
  expect((await request.get(`${units}/${account}`)).status()).toBe(401);
  expect((await request.get(`${units}/extra`, { headers: foreign })).status()).toBe(404);
  expect((await request.post(units, { headers: { ...foreign, Origin: 'http://localhost:8080' }, data: {} })).status()).toBe(404);  // read only
});
