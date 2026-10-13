// The server cesta changed from outside the storefront's own controls (Pedir lo que falta, in a deal): CESTA_EVENT asks the storefront to
// read the account's cesta again and, with open, to show it (the conversations panel closes for it). A page without the storefront takes
// the person there instead.
export const CESTA_EVENT = 'motionpartes:cesta';
export type CestaRequest = { accountId: string; open: boolean };

export function cestaChanged(accountId: string, open = false) {
  const event = new CustomEvent<CestaRequest>(CESTA_EVENT, { detail: { accountId, open }, cancelable: true });
  window.dispatchEvent(event);
  // The storefront takes it (preventDefault); elsewhere the cesta opens on its own page, under that account.
  if (open && !event.defaultPrevented) window.location.assign(`/?vista=cesta&cuenta=${encodeURIComponent(accountId)}`);
}
