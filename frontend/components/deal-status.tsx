import { BadgeCheck, CheckCircle2, CircleX, Clock3, Handshake, FileText, PackageCheck, PackageOpen, RotateCcw, Truck, type LucideIcon } from 'lucide-react';
import { DealStatus, dealStatusLabels } from '@/lib/deal-types';
const icons: Record<DealStatus, LucideIcon> = { pending: Clock3, reviewed: CheckCircle2, quoted: FileText, adjustment: RotateCcw, handshaked: Handshake,
  processing: PackageOpen, in_transit: Truck, delivered: PackageCheck, received: BadgeCheck, cancelled: CircleX };
export default function DealStatusBadge({ status }: { status: DealStatus }) {
  const Icon = icons[status] ?? Clock3;
  return <span className={`supplier-request-status ${status}`}><Icon size={14}/>{dealStatusLabels[status]}</span>;
}
