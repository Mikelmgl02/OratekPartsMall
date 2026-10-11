import type { Route } from '@playwright/test';

// Administración → Inventario counts the pending Productos nuevos for its tab badge on every visit. Specs about something else answer
// that count as zero instead of listing it among the unexpected routes.
export function noNewProducts(route: Route) {
  if (new URL(route.request().url()).pathname !== '/api/management/catalog/proposals/summary') return false;
  void route.fulfill({ json: { pending: 0, approved: 0, linked: 0, rejected: 0, resolved: 0 } });
  return true;
}
