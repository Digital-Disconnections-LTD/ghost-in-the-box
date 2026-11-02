#!/usr/bin/env node
// Narrow, sanitized bridge to the locally installed MIT threads-mcp browser code.
// Reads one private JSON command from stdin. Only one final status JSON is written.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const output = process.stdout.write.bind(process.stdout);
console.log = () => {};
console.warn = () => {};
console.error = () => {};
// The Python caller owns the state directory and passes the pinned source path.
const sourceFlag = process.argv.indexOf('--source');
const base = (sourceFlag > -1 ? process.argv[sourceFlag + 1] : '') ||
  process.env.SOCIAL_FARM_THREADS_MCP_SOURCE || '';
let closeContext;
let phase = 'setup';

function emit(status, more = {}) {
  output(JSON.stringify({ status, ...more }) + '\n');
}

async function command() {
  let raw = '';
  for await (const part of process.stdin) raw += part;
  const job = JSON.parse(raw);
  if (!base || !path.isAbsolute(base)) {
    emit('bridge_unavailable');
    return;
  }
  if (!/^[A-Za-z0-9._]+$/.test(job.account) ||
      typeof job.body !== 'string' || !job.body.trim() || job.body.length > 500 ||
      !['post', 'reply'].includes(job.action)) {
    emit('pre_send_failure');
    return;
  }
  const url = (part) => pathToFileURL(path.join(base, 'build', part)).href;
  const session = await import(url('browser/session.js'));
  const client = await import(url('api/client.js'));
  const extract = await import(url('api/extract.js'));
  const write = await import(url('tools/write.js'));
  closeContext = session.closeContext;
  phase = 'browser';
  const ctx = await session.getContext(false);
  phase = 'cookies';
  let cookies = await ctx.cookies([session.BASE_URL, 'https://www.instagram.com']);
  const sessionCookie = () => cookies.find((c) =>
    c.name === 'sessionid' && c.domain.includes('threads.com'));
  const seedFromIg = async () => {
    if (!job.sessionid || !job.user_id) return false;
    const hosts = ['.threads.com', '.instagram.com'];
    await ctx.addCookies(hosts.flatMap((domain) => [
      { name: 'sessionid', value: job.sessionid, domain, path: '/',
        secure: true, httpOnly: true, sameSite: 'Lax' },
      { name: 'ds_user_id', value: job.user_id, domain, path: '/',
        secure: true, httpOnly: false, sameSite: 'Lax' },
    ]));
    cookies = await ctx.cookies([session.BASE_URL, 'https://www.instagram.com']);
    return true;
  };
  if (!sessionCookie() || sessionCookie().value.length <= 8) {
    if (!(await seedFromIg())) {
      emit('auth_unavailable', { account_action: 'browser_login_required' });
      return;
    }
  }
  if (!sessionCookie() || sessionCookie().value.length <= 8) {
    emit('auth_unavailable', { account_action: 'browser_login_required' });
    return;
  }
  phase = 'identity_request';
  const readOwnHandle = async () => {
    const page = await session.getPage();
    await page.goto(`${session.BASE_URL}/`, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForTimeout(1800);
    const handle = await page.evaluate(() => {
      const anchors = Array.from(document.querySelectorAll('a[href^="/@"]'));
      const profile = anchors.find((a) => /profile/i.test(a.getAttribute('aria-label') || ''));
      const pick = profile || anchors[anchors.length - 1];
      return pick?.getAttribute('href')?.match(/^\/@([A-Za-z0-9._]+)/)?.[1];
    }).catch(() => undefined);
    const loginPage = /\/login(?:\/|\?|$)/.test(page.url()) ||
      (await page.locator('input[type="password"]').count()) > 0;
    return { handle, loginPage };
  };
  let identity = await readOwnHandle().catch(() => ({ handle: undefined, loginPage: false }));
  if (identity.handle?.toLowerCase() !== job.account.toLowerCase() &&
      job.sessionid && sessionCookie()?.value !== job.sessionid) {
    await seedFromIg();
    identity = await readOwnHandle().catch(() => ({ handle: undefined, loginPage: false }));
  }
  if (identity.loginPage ||
      (identity.handle && identity.handle.toLowerCase() !== job.account.toLowerCase())) {
    emit('auth_unavailable', { account_action: 'browser_login_required' });
    return;
  }
  const target = job.action === 'reply' ? job.target_url : client.profileUrl(job.account);
  if (job.action === 'reply' &&
      (!/^https:\/\/(www\.)?threads\.(com|net)\/@[A-Za-z0-9._]+\/post\/[A-Za-z0-9_-]+\/?$/.test(target || ''))) {
    emit('pre_send_failure');
    return;
  }
  const ownPosts = (bodies) => extract.extractPosts(bodies).filter((p) =>
    p.user?.username?.toLowerCase() === job.account.toLowerCase());
  const readPosts = async () => {
    const bodies = await client.threadsCapture(target,
      job.action === 'reply' ? 'PostPage' : 'ProfileThreadsTab', { dwellMs: 5000 });
    return ownPosts(bodies);
  };
  phase = 'profile_read';
  const profileBodies = await client.threadsCapture(client.profileUrl(job.account),
    'ProfileThreadsTab', { dwellMs: 5000 });
  const profile = extract.extractUser(profileBodies, job.account);
  const cookieUserId = cookies.find((c) =>
    c.name === 'ds_user_id' && c.domain.includes('threads.com'))?.value;
  const profileId = String(profile?.pk || profile?.id || '');
  if (!profile || !cookieUserId) {
    emit('provider_unavailable');
    return;
  }
  if ((job.user_id && cookieUserId !== job.user_id) ||
      (profileId && profileId !== cookieUserId)) {
    emit('auth_unavailable', { account_action: 'browser_login_required' });
    return;
  }
  const before = job.action === 'post' ? ownPosts(profileBodies) : await readPosts();
  if (job.mode === 'verify') {
    emit('verified', { account: job.account });
    return;
  }
  const matches = (posts) => posts.filter((p) =>
    typeof p.caption?.text === 'string' &&
    p.caption.text.trimEnd() === job.body.trimEnd());
  if (matches(before).length) {
    emit('possible_duplicate');
    return;
  }
  const seen = new Set(before.map((p) => String(p.pk || p.id || p.code)));
  phase = 'sending';
  if (job.action === 'post') {
    const result = await write.publishThread({ text: job.body });
    if (!result.startsWith('✅')) {
      emit('uncertain');
      return;
    }
  } else {
    await session.onPage(async (page) => {
      await page.goto(target, { waitUntil: 'domcontentloaded', timeout: 45000 });
      const editor = page.locator('[contenteditable="true"]').first();
      await editor.waitFor({ state: 'visible', timeout: 10000 });
      await editor.click();
      await page.keyboard.type(job.body, { delay: 15 });
      await page.waitForTimeout(700);
      await page.keyboard.press('Control+Enter');
    });
  }
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const found = matches(await readPosts()).filter((p) =>
        !seen.has(String(p.pk || p.id || p.code)));
      if (found.length === 1 && found[0].code && (found[0].pk || found[0].id)) {
        emit('published', {
          provider_id: String(found[0].pk || found[0].id),
          permalink: client.postUrl(job.account, found[0].code),
        });
        return;
      }
      if (found.length > 1) break;
    } catch { /* keep one submit and retry only the receipt read */ }
    await new Promise((resolve) => setTimeout(resolve, 2500));
  }
  emit('uncertain');
}

try {
  await command();
} catch {
  emit(phase === 'sending' ? 'uncertain' :
       ['profile_read', 'identity_request'].includes(phase) ? 'provider_unavailable' :
       'bridge_unavailable');
} finally {
  if (closeContext) await closeContext().catch(() => {});
}
