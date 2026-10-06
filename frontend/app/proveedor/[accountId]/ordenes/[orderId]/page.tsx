import SupplierOrderPage from '@/components/supplier-order-page';

export const metadata = { title: 'Orden de cliente — MotionPartes' };

export default async function Page({ params }: { params: Promise<{ accountId: string; orderId: string }> }) {
  const { accountId, orderId } = await params;
  return <SupplierOrderPage key={`${accountId}:${orderId}`} accountId={accountId} orderId={orderId}/>;
}
