// Money stays in BigInt cents end to end: no float ever touches a price.
export function quantityValue(value: string) {
  return /^\d{1,4}$/.test(value.trim()) ? Number(value) : null;
}
export function priceCents(value: string): bigint | null {
  const normalized = value.trim().replace(',', '.');
  if (!/^\d{1,10}(?:\.\d{1,2})?$/.test(normalized)) return null;
  const [whole, fraction = ''] = normalized.split('.');
  return BigInt(whole) * BigInt(100) + BigInt(fraction.padEnd(2, '0'));
}
export function decimal(cents: bigint) { return `${cents / BigInt(100)}.${String(cents % BigInt(100)).padStart(2, '0')}`; }
export function moneyFormatter(currency: string) {
  const formatter = new Intl.NumberFormat('es-PA', { style: 'currency', currency });
  return (cents: bigint) => formatter.formatToParts(cents / BigInt(100)).map(part => part.type === 'fraction' ? String(cents % BigInt(100)).padStart(2, '0') : part.value).join('');
}
