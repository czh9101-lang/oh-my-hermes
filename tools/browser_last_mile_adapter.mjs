// Explicit host-owned reference, never loaded by core or default plugin wiring.
// Closed loopback form adapter: page JavaScript and service workers are disabled.
import {createRequire} from 'node:module';
import {createHash, randomBytes} from 'node:crypto';
import {createInterface} from 'node:readline';

const {chromium} = createRequire(import.meta.url)(process.env.OMH_EFFECT_PLAYWRIGHT);
const hash = value => createHash('sha256').update(value).digest('hex');
const digest = value => hash(JSON.stringify(value)); // arrays/strings: matches core canonical JSON
const operations = new Set(['submit', 'send', 'publish', 'upload', 'purchase', 'payment', 'credential_change', 'destructive']);
let browser, context, page, lease, base, operation, deadline;
let revision = 0, fingerprint, phase = null, held = new Map(), consumed = new Set(), armed = null;
let lastObservation, allowed = 0, denied = 0, sends = 0, transportErrors = 0;

async function inspect() {
  // Fixed host inspection only. No model script/selector is accepted by this process.
  return page.evaluate(async () => {
    const forms = [...document.forms];
    const form = forms.length === 1 ? forms[0] : null;
    const button = form?.querySelector('button');
    const inputs = form ? [...form.querySelectorAll('input')] : [];
    const fields = [];
    for (const input of inputs) {
      if (input.type === 'file') {
        for (const file of input.files) {
          const bytes = new Uint8Array(await file.arrayBuffer());
          fields.push({name: input.name, filename: file.name, bytes: Array.from(bytes)});
        }
      } else fields.push({name: input.name, value: input.value, type: input.type});
    }
    const output = document.querySelector('output');
    return {url: location.href, html: document.documentElement.outerHTML,
      form: form ? {action: form.action, method: form.method.toUpperCase(), target: form.target,
        operation: form.dataset.operation, fields, name: button?.textContent,
        buttonCount: form.querySelectorAll('button').length,
        unsafe: [...form.attributes, ...inputs.flatMap(e => [...e.attributes])].some(a => a.name.startsWith('on'))} : null,
      output: output ? {attempt: output.dataset.attempt, preview: output.dataset.preview,
        object: output.dataset.object, confirmed: output.dataset.confirmed} : null};
  });
}

function payload(form) {
  if (operation !== 'upload') return {body: Buffer.from(new URLSearchParams(form.fields.map(f => [f.name, f.value])).toString()),
    contentType: 'application/x-www-form-urlencoded'};
  const boundary = 'omh-reference-upload-boundary';
  const parts = [];
  for (const field of form.fields) {
    if (!/^[a-z_]{1,32}$/.test(field.name)) throw Error('unsupported_field');
    if (field.filename) {
      if (!/^[a-z0-9_.-]{1,64}$/.test(field.filename)) throw Error('unsupported_filename');
      parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="${field.name}"; filename="${field.filename}"\r\nContent-Type: application/octet-stream\r\n\r\n`), Buffer.from(field.bytes), Buffer.from('\r\n'));
    } else parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="${field.name}"\r\n\r\n${field.value}\r\n`));
  }
  parts.push(Buffer.from(`--${boundary}--\r\n`));
  return {body: Buffer.concat(parts), contentType: `multipart/form-data; boundary=${boundary}`};
}

function prepared(snapshot, handle) {
  const form = snapshot.form;
  if (!form || form.method !== 'POST' || form.action !== `${base}/effect/${operation}` ||
      form.target || form.unsafe || form.operation !== operation || form.buttonCount !== 1 ||
      snapshot.html.length > 32768 || form.fields.length > 16) throw Error('unsupported_form');
  if (handle !== digest([lease, 'tab-1', revision, digest('effect-target')])) throw Error('stale_handle');
  const encoded = payload(form);
  if (encoded.body.length > 65536) throw Error('payload_capped');
  const ref = digest([lease, handle, operation, hash(JSON.stringify(snapshot)), hash(encoded.body)]);
  return {snapshotHash: hash(JSON.stringify(snapshot)), handle, ...encoded, metadata: {
    schema_version: 'browser_pending_effect/v1', held: true, method: 'POST', origin: base,
    payload_bytes: encoded.body.length, payload_digest: hash(encoded.body), payload_shape: 'form',
    target_role: 'button', target_name_digest: digest(form.name), opens_new_tab: false,
    redirected: false, preview_ref: ref}};
}

async function routeRequest(route) {
  const request = route.request();
  const samePage = request.frame().page() === page && request.frame() === page.mainFrame();
  if (samePage && !request.redirectedFrom() && phase && request.url() === phase && request.method() === 'GET') {
    phase = null; allowed++;
  } else if (samePage && !request.redirectedFrom() && armed && request.url() === `${base}/effect/${operation}` &&
      request.method() === 'POST' && request.headers()['x-omh-once'] === armed.nonce &&
      request.postDataBuffer()?.equals(armed.body)) {
    armed = null; allowed++; sends++;
  } else {denied++; await route.abort('blockedbyclient'); return;}
  // No redirect or transport retry can hide behind continue(). The intercepted
  // request is forwarded once, then fulfilled; any 3xx is an unknown outcome.
  try {
    const response = await route.fetch({maxRedirects: 0, maxRetries: 0, timeout: 4000});
    if (response.status() >= 300 && response.status() < 400) {
      denied++; await route.abort('blockedbyclient'); return;
    }
    await route.fulfill({response});
  } catch (error) {
    if (!(error instanceof Error)) throw error;
    transportErrors++;
    await route.abort('failed');
  }
}

async function start(command) {
  if (browser) throw Error('already_started');
  base = command.origin;
  const url = new URL(base);
  if (url.hostname !== '127.0.0.1' || url.protocol !== 'http:' || url.origin !== base || !operations.has(command.operation)) throw Error('unsupported_scope');
  lease = command.lease; operation = command.operation; deadline = command.deadline;
  browser = await chromium.launch({executablePath: process.env.OMH_EFFECT_CHROMIUM, timeout: 8000, headless: true,
    env: {...process.env, HOME: process.env.OMH_EFFECT_HOME}, args: ['--disable-crash-reporter']});
  context = await browser.newContext({serviceWorkers: 'block', javaScriptEnabled: false, acceptDownloads: false});
  await context.route('**/*', routeRequest);
  page = await context.newPage();
  page.setDefaultTimeout(4000);
  context.on('page', async extra => {if (extra !== page) await extra.close();});
  phase = `${base}/form/${operation}`;
  await page.goto(phase, {waitUntil: 'load'});
  if (operation === 'upload') await page.locator('input[type=file]').setInputFiles({name:'fixture.txt', mimeType:'text/plain', buffer:Buffer.from('PRIVATE fixture upload')});
  return {tabs: ['tab-1']};
}

async function observe() {
  const snapshot = await inspect();
  const next = hash(JSON.stringify(snapshot));
  if (next !== fingerprint) {revision++; fingerprint = next;}
  lastObservation = snapshot;
  const result = {url: snapshot.url, revision, readback: true,
    elements: snapshot.form ? [{role: 'button', name: snapshot.form.name, key: 'effect-target'}] : []};
  if (snapshot.output?.confirmed === 'true') result.effect_readback = {
    schema_version: 'browser_effect_readback/v1', observed: true,
    attempt_id: snapshot.output.attempt, preview_ref: snapshot.output.preview,
    postcondition: 'confirmation', object_digest: hash(snapshot.output.object)};
  return result;
}

async function preview(command) {
  if (command.operation !== operation || command.lease !== lease) throw Error('scope_changed');
  const snapshot = await inspect();
  if (hash(JSON.stringify(snapshot)) !== hash(JSON.stringify(lastObservation))) throw Error('stale_state');
  const record = prepared(snapshot, command.handle);
  if (consumed.has(record.metadata.preview_ref)) throw Error('consumed_preview');
  if (held.size >= 8 && !held.has(record.metadata.preview_ref)) throw Error('held_capped');
  held.set(record.metadata.preview_ref, record);
  return record.metadata;
}

async function resume(command) {
  const record = held.get(command.preview_ref);
  if (!record || consumed.has(command.preview_ref) || command.lease !== lease || Date.now()/1000 >= deadline) throw Error('invalid_resume');
  const current = prepared(await inspect(), record.handle);
  if (current.metadata.preview_ref !== command.preview_ref) throw Error('intent_changed');
  // Consumption is irreversible even if fetch/readback throws or the host dies.
  consumed.add(command.preview_ref); held.delete(command.preview_ref);
  const nonce = randomBytes(24).toString('hex');
  armed = {...record, nonce};
  try {
    await page.evaluate(async ({url, bytes, headers}) => {
      const response = await fetch(url, {method:'POST', body:new Uint8Array(bytes), headers,
        credentials:'omit', redirect:'error', signal:AbortSignal.timeout(4000)});
      if (!response.ok) throw Error('unknown');
      await response.arrayBuffer();
    }, {url:`${base}/effect/${operation}`, bytes:Array.from(record.body), headers: {
      'content-type':record.contentType, 'x-omh-once':nonce,
      'x-omh-attempt':command.attempt_id, 'x-omh-preview':command.preview_ref}});
    phase = `${base}/readback/${operation}`;
    await page.goto(phase, {waitUntil:'load'});
    return {status:'returned'};
  } finally {armed = null; phase = null;}
}

async function dispatch(command) {
  switch(command.cmd) {
    case 'start': return start(command);
    case 'observe': return observe();
    case 'preview': return preview(command);
    case 'resume': return resume(command);
    case 'abort': held.delete(command.preview_ref); consumed.add(command.preview_ref); return {aborted:true};
    case 'stats': return {allowed, denied, sends, transportErrors, contexts:browser?.contexts().length ?? 0,
      service_workers: context.serviceWorkers().length};
    case 'screenshot': await page.screenshot({path:command.path}); return {captured:true};
    case 'probe': {
      // QA-only closed adversarial actions, not an eval or raw URL interface.
      if (command.kind === 'crash') {
        const crashed = page.waitForEvent('crash');
        const cdp = await context.newCDPSession(page);
        const sent = cdp.send('Page.crash').catch(error => {
          if (!(error instanceof Error)) throw error;
          return {status:'crashed'};
        });
        await crashed;
        // Page.crash has no normal protocol reply; context closure settles the
        // pending command after the exact crash event, without polling.
        await context.close();
        await sent;
        return {crashed:true};
      }
      if (command.kind === 'payload') await page.locator('input[name=value]').evaluate(e => {e.value = 'CHANGED';});
      else if (command.kind === 'target') await page.locator('button').evaluate(e => {e.textContent = 'Changed';});
      else if (command.kind === 'state') await page.locator('body').evaluate(e => {e.dataset.changed = '1';});
      else if (command.kind === 'redirect') await page.locator('form').evaluate(e => {e.action = '/redirect';});
      else if (command.kind === 'newtab') await page.locator('form').evaluate(e => {e.target = '_blank';});
      else if (command.kind === 'network') return page.evaluate(async url => {
        try {await fetch(url, {method:'POST', body:'bypass'}); return {blocked:false};}
        catch {return {blocked:true};}
      }, `${base}/effect/${operation}`);
      else if (command.kind === 'service_worker') return page.evaluate(async () => {
        // Playwright's block policy resolves registration to undefined, rather
        // than rejecting. Assert the platform state, not promise rejection.
        try {
          const registration = await navigator.serviceWorker.register('/sw.js');
          const registrations = await navigator.serviceWorker.getRegistrations();
          return {blocked: !registration && registrations.length === 0};
        }
        catch {return {blocked:true};}
      });
      else throw Error('unsupported_probe');
      return {changed:true};
    }
    case 'release': await context.close(); held.clear(); return {reaped:true};
    default: throw Error('unsupported_command');
  }
}

try {
  for await (const line of createInterface({input:process.stdin})) {
    try {console.log(JSON.stringify({result:await dispatch(JSON.parse(line))}));}
    catch (error) {console.log(JSON.stringify({error: error instanceof Error ? 'host_action_unknown' : 'host_failure'}));}
  }
} finally {if (browser) await browser.close();}
