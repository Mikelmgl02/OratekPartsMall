import { BadgeCheck, CheckCircle2, CircleX, Clock3, Handshake, FileText, PackageCheck, PackageOpen, RotateCcw, Truck, type LucideIcon } from 'lucide-react';
import { DealStatus, dealStatusLabels } from '@/lib/deal-types';
const icons: Record<DealStatus, LucideIcon> = { pending: Clock3, reviewed: CheckCircle2, quoted: FileText, adjustment: RotateCcw, handshaked: Handshake,
  processing: PackageOpen, in_transit: Truck, delivered: PackageCheck, received: BadgeCheck, cancelled: CircleX };
// With onClick (an open document), the badge takes the reader back to the order's current stage.
export default function DealStatusBadge({ status, onClick }: { status: DealStatus; onClick?: () => void }) {
  const Icon = icons[status] ?? Clock3;
  if (onClick) return <button type="button" className={`supplier-request-status ${status} deal-status-link`} title="Ir a la etapa actual de la orden" onClick={onClick}><Icon size={14}/>{dealStatusLabels[status]}</button>;
  return <span className={`supplier-request-status ${status}`}><Icon size={14}/>{dealStatusLabels[status]}</span>;
}
