import { Part, Offer } from './types';
export const previewParts: Part[] = [
  { id: 'preview-1', category: 'FRENOS', subcategory: 'DISCOS', sku: 'MP-EJEMPLO-001', name: 'Disco de freno delantero', description: 'Disco de freno ventilado para un frenado uniforme. Confirma la compatibilidad con tu vehículo con el proveedor.', codes: [{ brand: 'BREMBO', code: '09.9078.10' }] },
  { id: 'preview-2', category: 'FILTROS', subcategory: 'ACEITE', sku: 'MP-EJEMPLO-002', name: 'Filtro de aceite del motor', description: 'Filtro de aceite enroscable. Solicita al proveedor que verifique la compatibilidad y el intervalo de reemplazo para tu vehículo.', codes: [{ brand: 'MANN-FILTER', code: 'W 610/3' }] },
  { id: 'preview-3', category: 'ENCENDIDO', subcategory: 'BUJÍAS', sku: 'MP-EJEMPLO-003', name: 'Bujía de iridio', description: 'Bujía de iridio. Confirma el grado térmico y la aplicación antes de solicitar una cotización.', codes: [{ brand: 'NGK', code: 'BKR6EIX' }] },
  { id: 'preview-4', category: 'RODAMIENTOS', subcategory: 'RUEDA', sku: 'MP-EJEMPLO-004', name: 'Rodamiento de rueda', description: 'Conjunto de rodamiento de rueda. Tu proveedor puede confirmar la posición y la compatibilidad correctas.', codes: [{ brand: 'SKF', code: 'VKBA 3596' }] },
  { id: 'preview-5', category: 'FRENOS', subcategory: 'PASTILLAS', sku: 'MP-EJEMPLO-005', name: 'Pastillas de freno delanteras', description: 'Juego de pastillas de freno delanteras. Consulta la aplicación y los accesorios incluidos con tu proveedor.', codes: [{ brand: 'BOSCH', code: 'BP986' }] },
  { id: 'preview-6', category: 'FILTROS', subcategory: 'AIRE', sku: 'MP-EJEMPLO-006', name: 'Filtro de aire', description: 'Filtro de aire del motor tipo panel. Confirma las dimensiones y la compatibilidad con tu proveedor.', codes: [{ brand: 'MANN-FILTER', code: 'C 25 114' }] },
];
export const previewOffers: Offer[] = [
  { supplier_id: 'preview-supplier-1', supplier_name: 'Repuestos Central', in_stock: true },
  { supplier_id: 'preview-supplier-2', supplier_name: 'Distribuidora Motor', in_stock: true },
];
