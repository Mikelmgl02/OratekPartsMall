import type { BasketLine } from './types';
import type { RequestSubmissionResult } from './request-types';

export type DraftSubmission = { fingerprint: string; submissionId: string; receipt?: RequestSubmissionResult; draftCleared?: boolean };
export function submissionStorageKey(accountId: string) { return `motionpartes:request-submissions:${accountId}`; }
export function requestPayloadLines(lines: BasketLine[]) {
  return lines.map(line => ({ supplier_item_id: line.offer.selected_item?.id || '', quantity: line.quantity,
    expected_part_id: line.part.id, expected_codigo: line.offer.selected_item?.codigo || '', expected_brand: line.offer.selected_item?.brand || '' }))
    .sort((a, b) => a.supplier_item_id.localeCompare(b.supplier_item_id));
}
export function readSubmissions(accountId: string): DraftSubmission[] {
  const records: unknown = JSON.parse(localStorage.getItem(submissionStorageKey(accountId)) || '[]');
  if (!Array.isArray(records) || !records.every(item => item && typeof item.fingerprint === 'string' && typeof item.submissionId === 'string')) throw new Error('invalid history');
  return records;
}
function remainingDraft(lines: BasketLine[], fingerprint: string) {
  const sent: ReturnType<typeof requestPayloadLines> = JSON.parse(fingerprint);
  if (!Array.isArray(sent)) throw new Error('invalid submission');
  return lines.filter(line => {
    const selected = requestPayloadLines([line])[0];
    return !sent.some(item => item.supplier_item_id === selected.supplier_item_id && item.quantity === selected.quantity
      && item.expected_part_id === selected.expected_part_id && item.expected_codigo === selected.expected_codigo
      && item.expected_brand === selected.expected_brand);
  });
}
export function finishDraftSubmission(accountId: string, submission: DraftSubmission, receipt: RequestSubmissionResult) {
  const key = submissionStorageKey(accountId);
  const history = readSubmissions(accountId);
  const completed = { ...submission, receipt, draftCleared: false };
  const index = history.findIndex(item => item.submissionId === submission.submissionId);
  if (index < 0) history.push(completed); else history[index] = completed;
  // Record the confirmed receipt before changing the draft, so interrupted cleanup is recoverable.
  localStorage.setItem(key, JSON.stringify(history));
  const draftKey = `partsmall:basket:${accountId}`;
  const draft: BasketLine[] = JSON.parse(localStorage.getItem(draftKey) || '[]');
  if (!Array.isArray(draft)) throw new Error('invalid draft');
  const lines = remainingDraft(draft, submission.fingerprint);
  localStorage.setItem(draftKey, JSON.stringify(lines));
  completed.draftCleared = true;
  // If this final marker cannot be saved, the next load repeats only the safe draft cleanup.
  try { localStorage.setItem(key, JSON.stringify(history)); } catch { /* Receipt is already persisted. */ }
  return lines;
}
export function recoverSentDraft(accountId: string, lines: BasketLine[]) {
  let receipt: RequestSubmissionResult | undefined;
  for (const submission of readSubmissions(accountId)) {
    if (submission.draftCleared || !submission.receipt?.requests?.length
      || submission.receipt.submission_id !== submission.submissionId) continue;
    const next = finishDraftSubmission(accountId, submission, submission.receipt);
    if (next.length < lines.length) receipt = submission.receipt;
    lines = next;
  }
  return { lines, receipt };
}
