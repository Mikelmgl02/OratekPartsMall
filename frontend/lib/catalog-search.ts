// The storefront search rules of mall/catalog_search.py, for the example catalog guests browse: every word has to match, in any
// order; accents and case are ignored; a code is compared by its clave (the Reference's key, plan §2): without separators when it is
// OEM-like (8+ characters, 3+ digits: 51750J9000 finds 51750-J9000), as printed otherwise (EC01 is not EC-01).
import type { Part } from './types';

// Words that join a search but name nothing. Not DEL: the catalog writes it for delantero.
const filler = new Set(['DE', 'LA', 'LAS', 'EL', 'LOS', 'PARA', 'CON', 'Y', 'EN', 'POR', 'UN', 'UNA']);

export function fold(text: string) { return text.normalize('NFD').replace(/[̀-ͯ]/g, '').toUpperCase(); }
export function compact(text: string) { return fold(text).replace(/[^A-Z0-9]/g, ''); }
// The clave, as mall.references.clave computes it (and the database's generated column).
export function clave(code: string) {
  const key = compact(code);
  return key.length >= 8 && (code.match(/[0-9]/g)?.length ?? 0) >= 3 ? key : fold(code).trim().replace(/\s+/g, ' ');
}

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
  // An OEM-like word matches a code without its separators; a shorter one only as printed (the text holds every code as printed).
  return words.every(word => text.includes(word) || (/\d/.test(word) && clave(word) === compact(word) && codes.some(code => code.includes(compact(word)))));
}
