// The storefront search rules of mall/catalog_search.py, for the example catalog guests browse: every word has to match, in any
// order; accents and case are ignored; a word with a digit is a code, compared without separators (51750J9000 finds 51750-J9000).
import type { Part } from './types';

// Words that join a search but name nothing. Not DEL: the catalog writes it for delantero.
const filler = new Set(['DE', 'LA', 'LAS', 'EL', 'LOS', 'PARA', 'CON', 'Y', 'EN', 'POR', 'UN', 'UNA']);

export function fold(text: string) { return text.normalize('NFD').replace(/[̀-ͯ]/g, '').toUpperCase(); }
export function compact(text: string) { return fold(text).replace(/[^A-Z0-9]/g, ''); }

export function searchWords(search: string) {
  const words = fold(search).split(/\s+/).filter(word => compact(word)).slice(0, 8);
  const meaningful = words.filter(word => !filler.has(word));
  return meaningful.length ? meaningful : words;
}

// A code search lists the exact code first (the server ranks it); any other search keeps SKU order.
export function isCodeSearch(search: string) {
  const key = compact(search);
  return key.length >= 4 && /\d/.test(key);
}

export function matchesSearch(part: Part, search: string) {
  const words = searchWords(search);
  const text = fold([part.sku, part.name, part.description, part.category, part.subcategory, ...part.codes.flatMap(code => [code.code, code.brand])].join(' '));
  const codes = [part.sku, ...part.codes.map(code => code.code)].map(compact);
  return words.every(word => text.includes(word) || (/\d/.test(word) && codes.some(code => code.includes(compact(word)))));
}
