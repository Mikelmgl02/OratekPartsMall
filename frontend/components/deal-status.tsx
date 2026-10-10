import { CheckCircle2, CircleX, Clock3, Handshake, FileText, PackageCheck, RotateCcw } from 'lucide-react';
import { DealStatus, dealStatusLabels } from '@/lib/deal-types';
export default function DealStatusBadge({ status }: { status: DealStatus }) {
  const Icon = status === 'delivered' ? PackageCheck : status === 'cancelled' ? CircleX : status === 'handshaked' ? Handshake : status === 'adjustment' ? RotateCcw
    : status === 'quoted' ? FileText : status === 'reviewed' ? CheckCircle2 : Clock3;
  return <span className={`supplier-request-status ${status}`}><Icon size={14}/>{dealStatusLabels[status]}</span>;
}
