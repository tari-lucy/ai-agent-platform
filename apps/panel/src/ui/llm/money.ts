// Exact decimal money on strings: no floating point, so no 0.1 + 0.2 drift in a bill.
// Currencies are never added to each other; callers keep one total per currency.
const pattern = /^(0|[1-9][0-9]*)(\.[0-9]+)?$/;
type Parsed = { units: bigint; scale: number };
function parse(text: string): Parsed {
  if (!pattern.test(text)) throw new Error('INVALID_MONEY');
  const [whole, fraction = ''] = text.split('.');
  return { units: BigInt(whole + fraction), scale: fraction.length };
}
const lift = (value: Parsed, scale: number) => value.units * 10n ** BigInt(scale - value.scale);
export function addMoney(a: string, b: string): string {
  const x = parse(a); const y = parse(b);
  const scale = Math.max(x.scale, y.scale);
  return render(lift(x, scale) + lift(y, scale), scale);
}
export function compareMoney(a: string, b: string): number {
  const x = parse(a); const y = parse(b);
  const scale = Math.max(x.scale, y.scale);
  const left = lift(x, scale); const right = lift(y, scale);
  return left < right ? -1 : left > right ? 1 : 0;
}
function render(units: bigint, scale: number): string {
  const digits = units.toString().padStart(scale + 1, '0');
  const whole = digits.slice(0, digits.length - scale);
  const fraction = digits.slice(digits.length - scale).replace(/0+$/, '');
  return fraction ? `${whole}.${fraction}` : whole;
}
export const isMoney = (text: string) => pattern.test(text) && text.length <= 64;
