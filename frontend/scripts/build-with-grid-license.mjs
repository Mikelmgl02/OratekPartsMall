import { existsSync, readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

// Docker mounts the environment file only for this build step. Only the
// browser-side grid license is passed to Next; other credentials stay private.
const secretFile = '/run/secrets/ag_grid_env';
if (!process.env.NEXT_PUBLIC_AG_GRID_LICENSE_KEY && existsSync(secretFile)) {
  const line = readFileSync(secretFile, 'utf8').split(/\r?\n/).find(value => value.startsWith('NEXT_PUBLIC_AG_GRID_LICENSE_KEY='));
  const value = line?.slice(line.indexOf('=') + 1).trim() || '';
  try { process.env.NEXT_PUBLIC_AG_GRID_LICENSE_KEY = value.startsWith('"') ? JSON.parse(value) : value; }
  catch { console.error('The AG Grid license environment value must be a valid quoted or unquoted string.'); process.exit(1); }
}
const result = spawnSync('npm', ['run', 'build'], { stdio: 'inherit', env: process.env });
process.exit(result.status ?? 1);
