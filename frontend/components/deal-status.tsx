import { CheckCircle2, Clock3, Handshake, FileText, RotateCcw } from 'lucide-react';
import { DealStatus, dealStatusLabels } from '@/lib/deal-types';
export default function DealStatusBadge({ status }: { status: DealStatus }) {
  const Icon = status === 'handshaked' ? Handshake : status === 'adjustment' ? RotateCcw : status === 'quoted' ? FileText : status === 'reviewed' ? CheckCircle2 : Clock3;
  return <span className={`supplier-request-status ${status}`}><Icon size={14}/>{dealStatusLabels[status]}</span>;
}
