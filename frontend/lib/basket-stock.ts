import type { BasketLine } from './types';

// A cesta line's Disponible: the supplier's public stock band for its article (as the catalog's: no units), or No disponible when the line
// can no longer be asked for. Shared by the cesta grid and the catalog card's En cesta summary.
export type StockState = 'high' | 'medium' | 'low' | 'sold_out' | 'unknown' | 'off';
const labels = { high: 'Existencia alta', medium: 'Existencia media', low: 'Baja existencia', sold_out: 'Sin existencias', unknown: 'Con existencias', off: 'No disponible' } as const;
export const stockShort: Record<StockState, string> = { high: 'Alta', medium: 'Media', low: 'Baja', sold_out: 'Sin exist.', off: 'No disp.', unknown: 'Hay' };
export const stockHelp = 'Existencias que reporta este proveedor, descontando las reservadas. Las confirma al cotizar.';
export const stockOf = (line: BasketLine): StockState => line.available === false ? 'off' : line.offer.stock ?? (line.offer.in_stock ? 'unknown' : 'sold_out');
export const stockLabel = (line: BasketLine) => labels[stockOf(line)];
export const stockRank = (line?: BasketLine) => line ? ['off', 'sold_out', 'low', 'unknown', 'medium', 'high'].indexOf(stockOf(line)) : 0;
