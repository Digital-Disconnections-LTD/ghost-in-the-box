#!/usr/bin/env node
// One fresh web login in a temporary private profile. Never prints inputs.
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
let stage = 'setup';

function emit(status, needsCode = false) {
  output(JSON.stringify({ status, needs_code: needsCode, stage }) + '\n');
}

async function firstVisible(page, selectors) {
  for (const selector of selectors) {
    const loc = page.locator(selector).first();
    if (await loc.count() && await loc.isVisible().catch(() => false)) return loc;
  }
  return null;
}

async function waitVisible(page, selectors, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  do {
    const found = await firstVisible(page, selectors);
    if (found) return found;
    await page.waitForTimeout(300);
  } while (Date.now() < deadline);
  return null;
}

async function submit(page) {
  const button = await firstVisible(page, [
    'button[type="submit"]', 'input[type="submit"]',
    'button:has-text("Log in")', 'button:has-text("Continue")',
  ]);
  if (!button) return false;
  await button.click({ timeout: 10000 });
  return true;
}

async function main() {
  let raw = '';
  for await (const part of process.stdin) raw += part;
  const input = JSON.parse(raw);
  if (!/^[A-Za-z0-9._]+$/.test(input.username) || !input.password ||
      (input.code && !/^\d{6}(?:\d{2})?$/.test(input.code))) {
    emit('local_inputs_invalid');
    return;
  }
  const url = (part) => pathToFileURL(path.join(base, 'build', part)).href;
  const session = await import(url('browser/session.js'));
  const client = await import(url('api/client.js'));
  const extract = await import(url('api/extract.js'));
  closeContext = session.closeContext;
  stage = 'browser_launch';
  const ctx = await session.getContext(false);
  await ctx.clearCookies();
  let page = ctx.pages().find((p) => !p.isClosed()) || await ctx.newPage();
  stage = 'login_navigation';
  const loginResponse = await page.goto(`${session.BASE_URL}/login`, {
    waitUntil: 'domcontentloaded', timeout: 60000 });
  if (loginResponse?.status() === 429) {
    emit('provider_throttled');
    return;
  }
  stage = 'login_form';
  let password = await waitVisible(page, ['input[type="password"]'], 6000);
  if (!password) {
    const option = page.getByRole('button', { name: /log in with instagram|continue with instagram/i }).first();
    const link = page.getByRole('link', { name: /log in with instagram|continue with instagram/i }).first();
    const choice = await option.count() ? option : await link.count() ? link : null;
    if (choice) {
      stage = 'instagram_choice';
      await choice.click({ timeout: 10000 });
      await page.waitForTimeout(1800);
      page = ctx.pages().filter((p) => !p.isClosed()).at(-1) || page;
      password = await waitVisible(page, ['input[type="password"]'], 12000);
    }
  }
  const username = await waitVisible(page, [
    'input[name="username"]', 'input[autocomplete="username"]',
    'input[type="text"]', 'input[type="email"]',
  ], 8000);
  if (!username || !password) {
    emit('browser_login_unavailable');
    return;
  }
  await username.fill(input.username);
  await password.fill(input.password);
  stage = 'credentials_submit';
  if (!(await submit(page))) {
    emit('browser_login_unavailable');
    return;
  }
  await page.waitForTimeout(3500);
  page = ctx.pages().filter((p) => !p.isClosed()).at(-1) || page;
  const codeField = await firstVisible(page, [
    'input[autocomplete="one-time-code"]', 'input[name="verificationCode"]',
    'input[name="code"]', 'input[inputmode="numeric"]',
  ]);
  if (codeField) {
    stage = 'two_factor';
    if (!input.code) {
      emit('two_factor_required', true);
      return;
    }
    await codeField.fill(input.code);
    if (!(await submit(page))) {
      emit('checkpoint_required');
      return;
    }
    await page.waitForTimeout(3500);
  }
  if (/checkpoint|challenge/i.test(page.url())) {
    emit('checkpoint_required');
    return;
  }
  const cookies = await ctx.cookies([session.BASE_URL]);
  stage = 'identity_check';
  const sessionid = cookies.find((c) => c.name === 'sessionid' &&
    c.domain.includes('threads.com'))?.value;
  const userId = cookies.find((c) => c.name === 'ds_user_id' &&
    c.domain.includes('threads.com'))?.value;
  if (!sessionid || !userId) {
    emit('login_rejected');
    return;
  }
  const bodies = await client.threadsCapture(client.profileUrl(input.username),
    'ProfileThreadsTab', { dwellMs: 5000 });
  const profile = extract.extractUser(bodies, input.username);
  if (!profile || (profile.pk && String(profile.pk) !== userId)) {
    emit('identity_unverified');
    return;
  }
  emit('saved');
}

try {
  await main();
} catch {
  emit('browser_login_unavailable');
} finally {
  if (closeContext) await closeContext().catch(() => {});
}
