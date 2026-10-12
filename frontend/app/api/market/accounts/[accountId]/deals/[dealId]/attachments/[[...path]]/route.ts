// An order's chat files (lib/chat-files-proxy.ts): the conversation with its other party, through the order.
import { chatFilesRoute } from '@/lib/chat-files-proxy';

const handle = chatFilesRoute('deals');
export const GET = handle;
export const POST = handle;
