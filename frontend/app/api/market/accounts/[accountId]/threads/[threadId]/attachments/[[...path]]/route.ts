// A conversation's chat files by its own id (lib/chat-files-proxy.ts): one that has no order yet (Preguntar al proveedor).
import { chatFilesRoute } from '@/lib/chat-files-proxy';

const handle = chatFilesRoute('threads');
export const GET = handle;
export const POST = handle;
