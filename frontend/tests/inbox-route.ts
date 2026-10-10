import type { Route } from '@playwright/test';

// The deal inbox polls from every storefront page and the deal chat marks what it shows as read. Specs about something else answer
// those calls as signed out instead of listing them among the unexpected routes.
export function signedOutInbox(route: Route) {
  if (!new URL(route.request().url()).pathname.startsWith('/api/market/inbox')) return false;
  void route.fulfill({ status: 401, json: { detail: 'Inicia sesión para continuar.' } });
  return true;
}
