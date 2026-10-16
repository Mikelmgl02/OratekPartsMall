// The inventory refactor's shapes (plan §2; S7's management API: mall/line_views.py, case_views.py, brand_views.py, matching_views.py).
// An OEM line (Part) holds References (PartCode); supplier items (variants) hang from a Reference. "Línea OEM" in the admin, never
// plain "Línea": pricing calls the supplier's discount groups líneas.
import type { CatalogImage, ManagedPart, StockItem } from './types';

export type LineEstado = 'referencia' | 'preparacion' | 'publicada' | 'rechazada' | 'retirada' | 'unificada';
export type LineOrigen = 'catalogo' | 'biblioteca' | 'proveedor';
export type LineTipo = 'OEM' | 'AFTERMARKET' | 'UNKNOWN';
export type RejectReason = 'duplicate' | 'not_a_part' | 'bad_data' | 'other';
export type ReferenceTipo = 'oem' | 'aftermarket' | 'interno' | 'otro';
export type ReferenceEstado = 'verificado' | 'declarado' | 'inferido' | 'disputa' | 'retirada';
export type SourceKind = 'catalogo' | 'biblioteca' | 'proveedor' | 'admin' | 'ia' | 'buscador';

// A line's main Reference (its code is the line's codigo, shown as SKU) as a row carries it.
export type MainReference = { id: number; code: string; brand: string; tipo: ReferenceTipo; estado: ReferenceEstado };
// A live Reference as a lines/ row lists it (the main left out: the codigo shows it).
export type LineCode = { id: number; code: string; brand: string; tipo: ReferenceTipo; estado: ReferenceEstado };
export type LineDecision = { reason: RejectReason | ''; note: string; decided_by: string | null; decided_at: string | null };

// lines/ and lines/<id>/ (LineSerializer). sku is the codigo (the main's code while the line is live, frozen once unificada).
export type OEMLine = {
  id: string; sku: string; name: string; description: string; estado: LineEstado; origen: LineOrigen; tipo: LineTipo; version: number;
  brand: string; main: MainReference | null; grupo: { id: string; nombre: string } | null; subgrupo: { id: string; name: string } | null;
  category: string; subcategory: string; aplicaciones_texto: string; merged_into: string | null; decision: LineDecision;
  reference_count: number; variant_count: number; merged_count: number; images: CatalogImage[]; field_sources: Record<string, unknown>;
  technical_revision: number; created_at: string | null; updated_at: string | null; changed_at: string | null;
  codes?: LineCode[];
};
// A 409 from a line write: the version the line has now.
export type LineConflict = { detail: string; version?: number; line?: { id: string; sku: string } | null; owners?: { id: string; sku: string; active?: boolean }[] };

export type ReferenceSource = { kind: SourceKind; citation: string; marks: string[]; retirada: boolean; created_at: string };
// lines/<id>/references/ (LineReferenceSerializer): every Reference of the line, retired ones too, main first.
export type Reference = {
  id: number; uid: string; line: string; code: string; clave: string; brand: string; tipo: ReferenceTipo; estado: ReferenceEstado; is_main: boolean;
  supplier: { id: string; name: string } | null; printed_forms: string[]; dispute_note: string; notes: string; detail: Record<string, unknown>;
  reemplazado_por: number | null; version: number; variant_count: number; sources: ReferenceSource[]; created_at: string | null; updated_at: string | null;
};
export type ReferenceAdded = { added: boolean; reference: Reference };

// A supplier item as a variant (S3): the Reference it hangs from, a code change waiting for review, and what the supplier says about it.
export type Calidad = 'original' | 'oes' | 'alternativo';
export type Condicion = 'nuevo' | 'remanufacturado';
export type VariantFacts = { calidad?: Calidad | ''; condicion?: Condicion | ''; garantia?: string; unidades_por_empaque?: number | null };
export type OfferReference = { id: number; code: string; brand: string; tipo: ReferenceTipo };
export type Variant = StockItem & VariantFacts & { line_estado?: LineEstado | null };
// An offer's item on the storefront (S7): the variant's data and the Reference it sells.
export type VariantOfferItem = { id: string; codigo: string; brand: string; description: string; reference?: OfferReference | null } & VariantFacts;

// Matching Cases (case_result): reference | code_change | variant | line | merge (S10 converted the kinds from before S6).
export type CaseKind = 'reference' | 'code_change' | 'variant' | 'line' | 'merge';
export type CaseStatus = 'review' | 'applied' | 'dismissed';
export type CaseAction = 'approve' | 'dismiss' | 'separar_referencia' | 'merge' | 'keep_apart';
export type CaseRoute = 'lote' | 'caso' | '';
type CaseCode = { brand: string; code: string };
// A case row: a supplier's item (sku is its codigo) or an OEM line (sku is its codigo).
export type CaseRow = { id: string; sku: string; description: string; name?: string; brand?: string; estado?: string; supplier_invent_id?: string;
  references?: CaseCode[]; codes?: CaseCode[]; codigo_pendiente?: string | null; brand_pendiente?: string | null };
export type CaseLine = { id: string; sku: string; description: string; estado: string; name?: string; codes?: CaseCode[] };
export type CaseReference = { id: number; code: string; brand: string; tipo?: string; estado?: string; line?: string; line_sku?: string };
export type MatchingCase = {
  id: string; kind: CaseKind | string; sources: CaseRow[]; candidates: CaseRow[]; target_sku: string; method?: string; reason: string;
  status: CaseStatus | string; fingerprint: string; supplier_name: string; ai: { target_sku?: string; reason?: string; error?: string; confidence?: number };
  reference?: CaseReference | null; new_code?: string; new_brand?: string; lines?: CaseLine[]; evidence?: Record<string, unknown>; guard?: string;
  route?: CaseRoute | string; batch_key?: string; decided_by?: string; decided_at?: string | null; created_at?: string | null; item_id?: string | null;
};
export type CaseDecision = { action: CaseAction; line?: string; target_sku?: string };
// cases/: the page, plus the cases of the status asked for by kind (without the kind filter).
export type CasePage = { count: number; next: string | null; previous: string | null; kinds: Record<string, number>; results: MatchingCase[] };
export type CaseBatchIndexRow = { batch_key: string; counts: Record<string, number>; kinds: Record<string, number>; count: number; created_at: string | null };
export type CaseBatch = { batch_key: string; counts: Record<string, number>; kinds: Record<string, number>; count: number; results: MatchingCase[] };
export type CaseBatchResult = { batch_key: string; decided: string[]; failed: { id: string; detail: string }[]; results: MatchingCase[] };

// merges/: the automatic merges, each with its line, the lines it joined and Separar SKU's route.
export type MergeDigestRow = { id: string; created_at: string; reason: string; target: { id: string; sku: string; description: string; estado: LineEstado };
  sources: { id: string; sku: string; still_merged: boolean }[]; split: string };

// brands/ (BrandSerializer): one spelling per brand (nombre), its aliases (TOY → TOYOTA) and its familia (KAIYI → CHERY, one level).
export type BrandTipo = 'vehiculo' | 'repuesto' | '';
export type BrandRef = { id: number; nombre: string };
export type BrandAlias = { id: number; alias: string };
export type Brand = { id: number; nombre: string; tipo: BrandTipo; familia: BrandRef | null; aliases: BrandAlias[]; miembros: BrandRef[]; notas: string;
  reference_count: number; created_at: string; updated_at: string };
export type BrandAliasAdded = { alias: BrandAlias; moved: number; cases: string[]; brand: Brand };

// grupos/: every grupo with its subgrupos (the PartType rows the technical sheet keys on) and their catalog line counts.
export type Subgrupo = { id: string; name: string; line_count: number };
export type Grupo = { id: string; nombre: string; subgrupos: Subgrupo[]; line_count: number };

// ---------------------------------------------------------------- labels (Spanish, as the screens show them)
export const lineEstadoLabels: Record<LineEstado, string> = { referencia: 'REFERENCIA', preparacion: 'EN PREPARACIÓN', publicada: 'PUBLICADA',
  rechazada: 'RECHAZADA', retirada: 'RETIRADA', unificada: 'UNIFICADA' };
export const lineEstadoHints: Record<LineEstado, string> = {
  referencia: 'Un número OEM que ningún producto lleva todavía (pestaña Referencia de Inventario).', preparacion: 'Un producto nuevo por revisar: no se vende hasta publicarlo.',
  publicada: 'En el catálogo: los clientes la ven y sus artículos cuentan en las existencias.', rechazada: 'Descartada con su motivo; sus referencias se conservan.',
  retirada: 'Fuera de la venta; conserva sus códigos y su historial.', unificada: 'Unificada en otra línea OEM; Separar SKU la devuelve.',
};
export const lineTipoLabels: Record<LineTipo, string> = { OEM: 'OEM', AFTERMARKET: 'AFTERMARKET', UNKNOWN: 'SIN CLASIFICAR' };
export const lineOrigenLabels: Record<LineOrigen, string> = { catalogo: 'CATÁLOGO', biblioteca: 'BIBLIOTECA OEM', proveedor: 'PROVEEDOR' };
export const rejectReasonLabels: Record<RejectReason, string> = { duplicate: 'Ya existe en el catálogo', not_a_part: 'No es un repuesto',
  bad_data: 'Datos del proveedor incorrectos', other: 'Otro motivo' };
// The moves a person may make (mall.lines.TRANSITIONS); preparación's go through Productos nuevos.
export const lineTransitions: Partial<Record<LineEstado, LineEstado[]>> = { referencia: ['publicada', 'retirada'], preparacion: ['publicada', 'rechazada'],
  publicada: ['retirada'], retirada: ['publicada'], rechazada: ['preparacion'] };
export const transitionLabels: Partial<Record<LineEstado, string>> = { publicada: 'Publicar', retirada: 'Retirar', rechazada: 'Rechazar', preparacion: 'Volver a preparación' };

export const referenceTipoLabels: Record<ReferenceTipo, string> = { oem: 'OEM', aftermarket: 'AFTERMARKET', interno: 'INTERNO', otro: 'OTRO' };
export const referenceEstadoLabels: Record<ReferenceEstado, string> = { verificado: 'VERIFICADA', declarado: 'DECLARADA', inferido: 'INFERIDA',
  disputa: 'EN DISPUTA', retirada: 'RETIRADA' };
// The tone a Reference's estado chip takes (globals.css .reference-estado.<tone>).
export const referenceEstadoTones: Record<ReferenceEstado, 'good' | 'neutral' | 'weak' | 'warn' | 'off'> = { verificado: 'good', declarado: 'neutral',
  inferido: 'weak', disputa: 'warn', retirada: 'off' };
export const sourceKindLabels: Record<SourceKind, string> = { catalogo: 'CATÁLOGO DE MARCA', biblioteca: 'BIBLIOTECA OEM', proveedor: 'PROVEEDOR',
  admin: 'ADMINISTRADOR', ia: 'INVESTIGACIÓN IA', buscador: 'BUSCADOR OEM' };
export const sourceMarkLabels: Record<string, string> = { '*': 'MARCADO CON *', 'W/HOUSING': 'CON CARCASA', index_only: 'SOLO EN EL ÍNDICE' };

export const caseKindLabels: Record<string, string> = { code_change: 'CAMBIO DE CÓDIGO', reference: 'REFERENCIA DECLARADA', variant: 'VARIANTE EN REVISIÓN',
  line: 'ELEGIR LÍNEA OEM', merge: 'UNIFICAR LÍNEAS', supplier: 'ARTÍCULO DE PROVEEDOR', catalog: 'FAMILIA DEL CATÁLOGO', catalog_reference: 'ALTERNO DEL CATÁLOGO',
  seed: 'FAMILIA SIN SKU PRINCIPAL' };
export const CASE_KINDS: CaseKind[] = ['code_change', 'reference', 'variant', 'line', 'merge'];
export const caseStatusLabels: Record<string, string> = { review: 'POR REVISAR', applied: 'APLICADOS', dismissed: 'DESCARTADOS' };
/** One case's status, on its card (the plural labels name the tabs and counts). */
export const caseStatusLabel: Record<string, string> = { review: 'POR REVISAR', applied: 'APLICADO', dismissed: 'DESCARTADO' };
export const batchLabels: Record<string, string> = { 'legacy-auto-merge': 'UNIFICACIONES AUTOMÁTICAS ANTERIORES' };
export const brandTipoLabels: Record<Exclude<BrandTipo, ''>, string> = { vehiculo: 'MARCA DE VEHÍCULO', repuesto: 'MARCA DE REPUESTOS' };
export const calidadLabels: Record<Calidad, string> = { original: 'ORIGINAL', oes: 'OES (EQUIPO ORIGINAL)', alternativo: 'ALTERNATIVO' };
export const condicionLabels: Record<Condicion, string> = { nuevo: 'NUEVO', remanufacturado: 'REMANUFACTURADO' };

// The line estados the Inventario grid lists by tab; the catalog (publicada and retirada) first, as lines/ reads by default.
export const LINE_TABS: { key: string; estado: string; label: string }[] = [
  { key: 'catalogo', estado: 'publicada,retirada', label: 'Publicadas y retiradas' }, { key: 'preparacion', estado: 'preparacion', label: 'En preparación' },
  { key: 'referencia', estado: 'referencia', label: 'Referencia' }, { key: 'rechazada', estado: 'rechazada', label: 'Rechazadas' },
  { key: 'unificada', estado: 'unificada', label: 'Unificadas' },
];

// A line's name as the admin reads it: its description, else a name that says more than the codigo.
export const lineText = (line: Pick<OEMLine, 'sku' | 'name' | 'description'>) => line.description || (line.name !== line.sku ? line.name : '') || '';
export const subgrupoLabel = (line: Pick<OEMLine, 'grupo' | 'subgrupo' | 'category' | 'subcategory'>) =>
  [line.grupo?.nombre ?? line.category, line.subgrupo?.name ?? line.subcategory].filter(Boolean).join(' / ');
// A line as the screens that still take catalog/ rows read it (Agrupar con…, Imágenes, the Ficha técnica, the OEM lookup).
export function asManagedPart(line: OEMLine): ManagedPart {
  return { id: line.id, sku: line.sku, is_OEM: line.tipo === 'OEM', name: line.name, description: line.description,
    category: line.grupo?.nombre ?? line.category, subcategory: line.subgrupo?.name ?? line.subcategory, part_type: line.subgrupo?.id ?? null,
    codes: (line.codes ?? []).map(code => ({ id: code.id, brand: code.brand, code: code.code, ref_type: code.tipo === 'oem' ? 'oem' as const : code.tipo === 'aftermarket' ? 'company' as const : 'unknown' as const })),
    images: line.images, active: line.estado === 'publicada', stock_record_count: line.variant_count, merged_count: line.merged_count,
    identity: { ref_type: line.main?.tipo === 'oem' ? 'oem' : line.main?.tipo === 'aftermarket' ? 'company' : 'unknown', brand: line.brand } };
}
// What a supplier import row will do (mall.supplier_import's preview, S6): a code change that waits for review, and how its item is placed
// (resolucion: attach, new_reference_on_line, case, no_line, or the rule that kept it, such as pendiente).
export type ImportRowPlan = { codigo_pendiente?: string | null; brand_pendiente?: string | null; resolucion?: string; regla?: string } & VariantFacts;
// The case or review a row opens, as the preview names it ('' when it opens none).
export function importRowCase(row: ImportRowPlan & { matching_status?: string; part_sku?: string | null }) {
  if (row.codigo_pendiente || row.brand_pendiente || row.resolucion === 'pendiente') return `CÓDIGO NUEVO EN REVISIÓN: ${[row.codigo_pendiente, row.brand_pendiente].filter(Boolean).join(' · ')}`.replace(/: $/, '');
  if (row.resolucion === 'case') return 'ABRE UN CASO: ELEGIR LÍNEA OEM (EL CÓDIGO ESTÁ EN VARIAS)';
  if (row.resolucion === 'no_line') return 'VA A PRODUCTOS NUEVOS';
  if (row.matching_status === 'review') return 'ABRE UN CASO: VARIANTE EN REVISIÓN';
  return '';
}
export const variantFactsText = (row: VariantFacts) => [row.calidad ? calidadLabels[row.calidad] : '', row.condicion ? condicionLabels[row.condicion] : '',
  row.garantia ? `GARANTÍA ${row.garantia}` : '', row.unidades_por_empaque ? `${row.unidades_por_empaque} POR EMPAQUE` : ''].filter(Boolean).join(' · ');
