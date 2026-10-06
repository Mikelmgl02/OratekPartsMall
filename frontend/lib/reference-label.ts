import { PartReference } from './types';
export function referenceLabel(ref: PartReference) {
  const type = ref.ref_type || (ref.kind === 'oem' ? 'oem' : ref.kind === 'manufacturer' ? 'company' : 'unknown');
  return type === 'oem' ? `OEM${ref.brand ? ` · ${ref.brand}` : ''}` : type === 'company' ? `EMPRESA${ref.brand ? ` · ${ref.brand}` : ''}` : `SIN CLASIFICAR${ref.brand ? ` · ${ref.brand}` : ''}`;
}
