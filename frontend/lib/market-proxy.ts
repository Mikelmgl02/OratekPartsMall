// The query a /api/market/<route> request passes on to Django: only the keys each route reads, each checked (mall's views validate again).
// Kept apart from the route handler so specs can check what reaches the backend (a key left out here is silently dropped).
const uuid = '[a-f0-9-]{36}';
export function forwardedQuery(route: string, params: URLSearchParams) {
  const query = new URLSearchParams();
  for (const key of ['search', 'page', 'status', 'after', 'before', 'solo']) {
    const value = params.get(key);
    if (value) query.set(key, value);
  }
  // The price grid filters by list; the key reaches only the supplier's private pricing routes.
  if (new RegExp(`^accounts/${uuid}/(prices|pricing/history)$`).test(route)) {
    const list = params.get('list');
    if (list && new RegExp(`^${uuid}$`).test(list)) query.set('list', list);
  }
  // The supplier's stock grid filters by matching state and sorts by column (the view accepts only its own ordering fields).
  if (new RegExp(`^accounts/${uuid}/inventory$`).test(route)) {
    const matching = params.get('matching_status'), ordering = params.get('ordering');
    if (matching && ['matched', 'pending', 'review', 'unmatched'].includes(matching)) query.set('matching_status', matching);
    if (ordering && /^[-a-z_,]{1,120}$/.test(ordering)) query.set('ordering', ordering);
  }
  // The supplier's ledger filters by origin, movement kind, article (a uuid) and, inside a load, by what changed.
  if (new RegExp(`^accounts/${uuid}/ledger(/runs(/${uuid}/changes)?)?$`).test(route)) {
    const values = params;
    for (const [key, pattern] of [['origin', /^[a-z]{1,12}$/], ['kind', /^[a-z]{1,12}$/], ['action', /^[a-z]{1,12}$/], ['item', new RegExp(`^${uuid}$`)]] as const) {
      const value = values.get(key);
      if (value && pattern.test(value)) query.set(key, value);
    }
  }
  // A conversation's chat keeps one of its orders' messages (order=<id>).
  if (new RegExp(`^accounts/${uuid}/threads/${uuid}/messages$`).test(route)) {
    const order = params.get('order');
    if (order && new RegExp(`^${uuid}$`).test(order)) query.set('order', order);
  }
  if (route === 'inbox/conversations') {
    if (params.get('unread') === '1') query.set('unread', '1');
    const side = params.get('side');
    if (side === 'client' || side === 'supplier') query.set('side', side);
    const order = params.get('order');
    if (order && new RegExp(`^${uuid}$`).test(order)) query.set('order', order);
  }
  // Rules filter by one client of the pair (a uuid) or by scope.
  if (new RegExp(`^accounts/${uuid}/pricing-rules$`).test(route)) {
    const client = params.get('client'), scope = params.get('scope');
    if (client && new RegExp(`^${uuid}$`).test(client)) query.set('client', client);
    if (scope === 'all' || scope === 'client') query.set('scope', scope);
  }
  if (route === 'catalog' || route === 'catalog/suggest') {
    for (const key of ['category', 'subcategory', 'supplier', 'availability', 'include_facets']) {
      for (const value of params.getAll(key)) query.append(key, value);
    }
    // Mis repuestos (mall CatalogList.mine_parts): the SKUs in the cesta, the open orders or the favoritos.
    for (const value of new Set(params.getAll('mine'))) if (['cesta', 'ordenes', 'favoritos'].includes(value)) query.append('mine', value);
  }
  // The active account (a uuid): a supplier-only account sees only its own SKUs (mall/catalog_scope.py).
  if (route === 'catalog' || route === 'catalog/suggest') {
    const account = params.get('account');
    if (account && new RegExp(`^${uuid}$`).test(account)) query.set('account', account);
  }
  return query;
}
