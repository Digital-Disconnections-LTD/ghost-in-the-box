#!/usr/bin/env node
// Bounded Threads browser reads. Provider bodies and cookies stay in process.
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const output = process.stdout.write.bind(process.stdout);
console.log = () => {};
console.warn = () => {};
console.error = () => {};
const base = process.env.SOCIAL_FARM_THREADS_MCP_SOURCE ||
  path.join(os.homedir(), '.batbox/social-farm-state/threads-browser-fallback/source');
let closeContext;

function emit(status, more = {}) {
  output(JSON.stringify({ status, ...more }) + '\n');
}

async function main() {
  let raw = '';
  for await (const part of process.stdin) raw += part;
  const request = JSON.parse(raw);
  if (!/^[A-Za-z0-9._]+$/.test(request.account) ||
      !['me', 'own', 'replies'].includes(request.operation) ||
      !Number.isInteger(request.limit) || request.limit < 1 || request.limit > 25) {
    emit('invalid_request');
    return;
  }
  if (request.operation === 'replies' &&
      (!/^https:\/\/(www\.)?threads\.(com|net)\/@[A-Za-z0-9._]+\/post\/[A-Za-z0-9_-]+\/?$/.test(request.target_url || '') ||
       !/^\d+$/.test(String(request.post_id || '')))) {
    emit('invalid_request');
    return;
  }
  const url = (part) => pathToFileURL(path.join(base, 'build', part)).href;
  const session = await import(url('browser/session.js'));
  const client = await import(url('api/client.js'));
  const extract = await import(url('api/extract.js'));
  closeContext = session.closeContext;
  const ctx = await session.getContext(false);
  const hosts = [session.BASE_URL, 'https://www.instagram.com'];
  let cookies = await ctx.cookies(hosts);
  const threadsSession = () => cookies.find((c) =>
    c.name === 'sessionid' && c.domain.includes('threads.com'))?.value;
  const seed = async () => {
    if (!request.sessionid || !request.user_id) return false;
    await ctx.addCookies(['.threads.com', '.instagram.com'].flatMap((domain) => [
      { name: 'sessionid', value: request.sessionid, domain, path: '/',
        secure: true, httpOnly: true, sameSite: 'Lax' },
      { name: 'ds_user_id', value: request.user_id, domain, path: '/',
        secure: true, httpOnly: false, sameSite: 'Lax' },
    ]));
    cookies = await ctx.cookies(hosts);
    return true;
  };
  if (!threadsSession() && !(await seed())) {
    emit('auth_unavailable');
    return;
  }
  const profileUrl = client.profileUrl(request.account);
  const captureProfile = async () => {
    const bodies = await client.threadsCapture(profileUrl, 'ProfileThreadsTab', { dwellMs: 5000 });
    const user = extract.extractUser(bodies, request.account);
    const cookieId = cookies.find((c) =>
      c.name === 'ds_user_id' && c.domain.includes('threads.com'))?.value;
    if (!user || !cookieId ||
        (request.user_id && cookieId !== request.user_id) ||
        (user.pk && String(user.pk) !== cookieId)) throw new Error('identity_unavailable');
    return bodies;
  };
  let profileBodies;
  try {
    profileBodies = await captureProfile();
  } catch {
    if (request.sessionid && threadsSession() !== request.sessionid && await seed()) {
      profileBodies = await captureProfile();
    } else {
      emit('provider_unavailable');
      return;
    }
  }
  if (request.operation === 'me') {
    const currentId = cookies.find((c) =>
      c.name === 'ds_user_id' && c.domain.includes('threads.com'))?.value;
    emit('ok', { result: { id: currentId, username: request.account } });
    return;
  }
  const bodies = request.operation === 'own' ? profileBodies :
    await client.threadsCapture(request.target_url, 'PostPage', { dwellMs: 5000 });
  const posts = extract.extractPosts(bodies);
  const selected = request.operation === 'own' ? posts.filter((p) =>
    p.user?.username?.toLowerCase() === request.account.toLowerCase()) :
    posts.filter((p) => String(p.pk || p.id || '') !== String(request.post_id));
  const normalized = selected.map((p) => {
    const username = p.user?.username || '';
    const id = String(p.pk || p.id || '');
    return {
      id, username, body: p.caption?.text || '',
      timestamp: typeof p.taken_at === 'number' ?
        new Date(p.taken_at * 1000).toISOString() : null,
      reply_count: p.text_post_app_info?.direct_reply_count || 0,
      permalink: username && p.code ? client.postUrl(username, p.code) : null,
    };
  }).filter((p) => p.id && p.permalink).slice(0, request.limit);
  emit('ok', { result: request.operation === 'own' ?
    { posts: normalized, next_cursor: '' } :
    { post_id: String(request.post_id), posts: normalized, next_cursor: '' } });
}

try {
  await main();
} catch {
  emit('provider_unavailable');
} finally {
  if (closeContext) await closeContext().catch(() => {});
}
