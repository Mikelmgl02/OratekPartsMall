import { Part, PartReference } from './types';
export function referenceLabel(ref: PartReference) {
  const type = ref.ref_type || (ref.kind === 'oem' ? 'oem' : ref.kind === 'manufacturer' ? 'company' : 'unknown');
  return type === 'oem' ? `OEM${ref.brand ? ` · ${ref.brand}` : ''}` : type === 'company' ? `EMPRESA${ref.brand ? ` · ${ref.brand}` : ''}` : `SIN CLASIFICAR${ref.brand ? ` · ${ref.brand}` : ''}`;
}

const compact = (code: string) => code.toUpperCase().replace(/[^0-9A-Z]/g, '');
// What a customer sees as the SKU's codes: its own alternos, then the aftermarket codes it reaches through its OEM numbers, each once.
export function visibleReferences(part: Pick<Part, 'codes' | 'equivalents'>): PartReference[] {
  const seen = new Set(part.codes.map(code => `${code.brand}:${compact(code.code)}`));
  const reached = (part.equivalents ?? []).filter(code => !seen.has(`${code.brand}:${compact(code.code)}`)).map(code => ({ ...code, ref_type: 'company' as const }));
  return [...part.codes, ...reached];
}
