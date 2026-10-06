import { CircleHelp, CircleCheck, CircleMinus, TriangleAlert } from 'lucide-react';
import type { CatalogAvailability } from '@/lib/types';

const states = {
  low: { label: 'BAJAS EXISTENCIAS', Icon: TriangleAlert },
  high: { label: 'ALTAS EXISTENCIAS', Icon: CircleCheck },
  sold_out: { label: 'AGOTADO', Icon: CircleMinus },
  unknown: { label: 'SIN EXISTENCIAS REPORTADAS', Icon: CircleHelp },
};

export default function StockStatus({ availability, preview = false }: { availability?: CatalogAvailability; preview?: boolean }) {
  if (preview) return <span className="catalog-stock-status unknown">VISTA DE EJEMPLO</span>;
  const status = availability?.status || 'unknown';
  const { label, Icon } = states[status];
  return <span className={`catalog-stock-status ${status}`} title="Disponibilidad reportada por los proveedores, descontando las unidades reservadas. El proveedor confirma las existencias al cotizar."><Icon size={13} aria-hidden="true"/>{label}</span>;
}
