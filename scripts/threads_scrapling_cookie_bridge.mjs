#!/usr/bin/env node
// Offline cookie transfer into the existing browser profile. No navigation.
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const output = process.stdout.write.bind(process.stdout);
console.log = console.warn = console.error = () => {};
let closeContext;
try {
  let raw = '';
  for await (const part of process.stdin) raw += part;
  const input = JSON.parse(raw);
  if (input.version !== 1 || !/^[A-Za-z0-9._]+$/.test(input.account) ||
      !Array.isArray(input.cookies)) throw new Error();
  const allowed = new Set(['threads.com', 'www.threads.com', 'instagram.com', 'www.instagram.com']);
  const keys = ['name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite'];
  const cookies = input.cookies.filter(c => allowed.has(c.domain.replace(/^\./, '')))
    .map(c => Object.fromEntries(keys.filter(k => k in c).map(k => [k, c[k]])));
  const own = cookies.filter(c => /^(?:\.)?(?:www\.)?threads\.com$/.test(c.domain));
  if (!own.some(c => c.name === 'sessionid' && c.value.length > 8) ||
      !own.some(c => c.name === 'ds_user_id' && /^\d+$/.test(c.value))) throw new Error();
  const source = process.env.SOCIAL_FARM_THREADS_MCP_SOURCE;
  if (!source || !process.env.THREADS_PROFILE_DIR) throw new Error();
  const session = await import(pathToFileURL(path.join(source, 'build/browser/session.js')).href);
  closeContext = session.closeContext;
  const context = await session.getContext(false);
  // Refuse all requests, including pages restored by the old Chromium profile.
  await context.route('**/*', route => route.abort());
  for (const page of context.pages()) await page.close();
  await context.clearCookies();
  await context.addCookies(cookies);
  const readback = await context.cookies(['https://www.threads.com', 'https://www.instagram.com']);
  if (!cookies.every(c => readback.some(r => r.name === c.name && r.domain === c.domain &&
      r.path === c.path && r.value === c.value))) throw new Error();
  output(JSON.stringify({status: 'imported'}) + '\n');
} catch {
  output(JSON.stringify({status: 'transfer_unavailable'}) + '\n');
  process.exitCode = 2;
} finally {
  if (closeContext) await closeContext().catch(() => {});
}
