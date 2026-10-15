import { Part, PartReference } from './types';
import { referenceEstadoLabels, referenceEstadoTones, referenceTipoLabels } from './line-types';
import type { ReferenceEstado, ReferenceTipo } from './line-types';

// A code as the storefront and the admin read it: since S7 it carries its Reference's tipo (oem, aftermarket, interno, otro) and estado
// beside the legacy ref_type and kind, which older rows still send alone.
export type TypedReference = PartReference & { tipo?: ReferenceTipo; estado?: ReferenceEstado };

// The tipo of a code: its own, else the one its legacy ref_type or kind maps to (company was a manufacturer's code: aftermarket).
export function referenceTipo(ref: TypedReference): ReferenceTipo | 'unknown' {
  if (ref.tipo) return ref.tipo;
  const type = ref.ref_type || (ref.kind === 'oem' ? 'oem' : ref.kind === 'manufacturer' ? 'company' : 'unknown');
  return type === 'oem' ? 'oem' : type === 'company' ? 'aftermarket' : 'unknown';
}
export function referenceLabel(ref: TypedReference) {
  const tipo = referenceTipo(ref);
  const label = tipo === 'unknown' ? 'SIN CLASIFICAR' : referenceTipoLabels[tipo];
  return `${label}${ref.brand ? ` · ${ref.brand}` : ''}`;
}
// The chip class of a code: oem, company (aftermarket, the class the grids already style) or unknown (interno and otro).
export function referenceClass(ref: TypedReference) {
  const tipo = referenceTipo(ref);
  return tipo === 'oem' ? 'oem' : tipo === 'aftermarket' ? 'company' : 'unknown';
}
export const referenceEstadoLabel = (estado?: ReferenceEstado) => estado ? referenceEstadoLabels[estado] : '';
export const referenceTone = (estado?: ReferenceEstado) => estado ? referenceEstadoTones[estado] : 'neutral';
// Codes a customer never sees (plan S8.8): a code in disputa (unless it is the line's own main, which the codes list leaves out anyway),
// a retired one, and a supplier's interno code.
export const isPublicReference = (ref: TypedReference) => ref.estado !== 'disputa' && ref.estado !== 'retirada' && ref.tipo !== 'interno';

const compact = (code: string) => code.toUpperCase().replace(/[^0-9A-Z]/g, '');
// What a customer sees as the SKU's codes: its own public codes, then the aftermarket codes it reaches through its OEM numbers, each once.
export function visibleReferences(part: Pick<Part, 'codes' | 'equivalents'>): TypedReference[] {
  const codes = (part.codes as TypedReference[]).filter(isPublicReference);
  const seen = new Set(codes.map(code => `${code.brand}:${compact(code.code)}`));
  const reached = (part.equivalents ?? []).filter(code => !seen.has(`${code.brand}:${compact(code.code)}`)).map(code => ({ ...code, ref_type: 'company' as const, tipo: 'aftermarket' as const }));
  return [...codes, ...reached];
}
