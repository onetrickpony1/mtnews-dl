chrome.action.onClicked.addListener(async () => {
  const url = chrome.runtime.getURL('bridge.html');
  const tabs = await chrome.tabs.query({url});
  if (tabs.length) await chrome.tabs.update(tabs[0].id, {active: true});
  else await chrome.tabs.create({url});
});


// Passive observation: no blocking, debugger, response body access, or requests.
const diagnostics = [];
const observerSession = crypto.randomUUID();
let sequence = 0, droppedEvents = 0, lastHealth = 0;
const requestStarts = new Map();
const storageHost = 'tom-media-prod.s3.eu-west-1.amazonaws.com';
let fingerprintPending = 0, fingerprintFailures = 0;
async function fingerprint(value) {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, '0')).join('');
}
// Hash usable URLs in memory; only safe metadata and the digest reach the queue.
async function publish(record, raw, redirectRaw) {
  if (fingerprintPending >= 1000) { droppedEvents++; return; }
  fingerprintPending++;
  try {
    if (raw) {
      const u = new URL(raw);
      if (u.hostname === storageHost && /^\/archive\/\d{8}-\d+\.pdf$/.test(u.pathname))
        record.url_fingerprint = await fingerprint(u.href);
      else if (record.path === '/[observed endpoint]' || record.path === '/[quota endpoint]')
        record.route_fingerprint = await fingerprint(u.origin + u.pathname);
    }
    if (redirectRaw && record.redirect?.host === storageHost)
      record.redirect.url_fingerprint = await fingerprint(new URL(redirectRaw).href);
    push(record);
  } catch (_) { fingerprintFailures++; push(record); }
  finally { fingerprintPending--; }
}
const safeHeaders = new Set(['date', 'retry-after', 'ratelimit', 'ratelimit-policy',
  'ratelimit-limit', 'ratelimit-remaining', 'ratelimit-reset', 'x-ratelimit-limit',
  'x-ratelimit-remaining', 'x-ratelimit-reset', 'content-type', 'content-length',
  'age', 'cache-control', 'expires', 'server', 'cf-cache-status', 'cf-mitigated',
  'last-modified', 'content-range', 'accept-ranges']);
const quotaName = /(?:quota|rate.?limit|remaining|retry.?after|download.?limit|daily.?limit|reset|allowance)/i;
const sensitiveName = /cookie|auth|token|secret|session|account|user|email|key|identifier|(?:^|-)id(?:-|$)/i;
function safeValue(name, value) {
  if (sensitiveName.test(name)) return false;
  return safeHeaders.has(name) || (quotaName.test(name) && /^[0-9\s.,:;=/%()+-]{1,256}$/.test(value));
}
function push(record) {
  if (diagnostics.length >= 1000) { diagnostics.shift(); droppedEvents++; }
  diagnostics.push({...record, observer_session: observerSession, sequence: ++sequence});
}
function destination(raw) {
  try {
    const u = new URL(raw);
    // Preserve only the known newspaper object naming scheme, never arbitrary paths.
    const object = u.hostname === 'tom-media-prod.s3.eu-west-1.amazonaws.com' && /^\/archive\/\d{8}-\d+\.pdf$/.test(u.pathname);
    const path = object || /^\/archive(?:\/issue)?$|^\/download\/\d{8}-\d+$|^\/(?:login|restricted-access|epaper)\/?$/.test(u.pathname)
      ? u.pathname : '/[path omitted]';
    const result = {host: u.hostname, path};
    const q = u.searchParams;
    if (q.has('X-Amz-Algorithm') || q.has('X-Amz-Signature')) {
      const signing = {signature_present: q.has('X-Amz-Signature'),
        credential_present: q.has('X-Amz-Credential'), security_token_present: q.has('X-Amz-Security-Token')};
      for (const [key, param, pattern] of [
        ['algorithm', 'X-Amz-Algorithm', /^AWS4-HMAC-SHA256$/],
        ['issued_at', 'X-Amz-Date', /^\d{8}T\d{6}Z$/],
        ['signed_headers', 'X-Amz-SignedHeaders', /^host$/],
        ['payload', 'X-Amz-Content-Sha256', /^UNSIGNED-PAYLOAD$/]]) {
        const value = q.get(param);
        if (value && pattern.test(value)) signing[key] = value;
      }
      const seconds = q.get('X-Amz-Expires');
      if (/^\d{1,6}$/.test(seconds || '') && Number(seconds) <= 604800) signing.expires_seconds = Number(seconds);
      result.signing = signing;
    }
    return result;
  } catch (_) { return null; }
}
function observe(event, details) {
  try {
    const u = new URL(details.url), path = u.pathname;
    const storage = u.hostname === storageHost && /^\/archive\/\d{8}-\d+\.pdf$/.test(path);
    if (u.hostname !== 'timesofmalta.com' && !storage) return;
    const names = (details.responseHeaders || []).map(h => h.name.toLowerCase());
    const standard = /^\/archive(?:\/issue)?$|^\/download\/\d{8}-\d+$|^\/epaper\/?$/.test(path);
    const target = details.redirectUrl ? destination(details.redirectUrl) : null;
    const issuing = target?.host === storageHost;
    const quota = names.some(n => quotaName.test(n));
    // Discover quota headers on same-host API calls without retaining private routes.
    if (!standard && !storage && !quota && !issuing) return;
    const record = {event, path: standard || storage ? path : (quota ? '/[quota endpoint]' : '/[observed endpoint]'), timestamp: details.timeStamp,
      host: u.hostname, source_kind: storage ? 'storage' : 'publisher',
      request_id: details.requestId, status: details.statusCode, cached: details.fromCache,
      error: details.error, headers: {}, header_names: names.slice(0, 150),
      method: details.method, resource_type: details.type};
    const start = requestStarts.get(details.requestId);
    if (storage) {
      record.destination = destination(details.url);
      record.request_signature_present = u.searchParams.has('X-Amz-Signature') || u.searchParams.has('Signature');
    }
    if (start !== undefined) record.elapsed_ms = Math.max(0, details.timeStamp - start);
    for (const header of details.responseHeaders || []) {
      const name = header.name.toLowerCase(), value = String(header.value || '').slice(0, 256);
      if (safeValue(name, value)) record.headers[name] = value;
    }
    if (target) record.redirect = target;
    void publish(record, details.url, details.redirectUrl);
  } catch (_) {} finally {
    if (['completed', 'error', 'redirect'].includes(event)) requestStarts.delete(details.requestId);
  }
}
const filter = {urls: ['https://timesofmalta.com/*', 'https://' + storageHost + '/archive/*']};
chrome.webRequest.onBeforeRequest.addListener(details => {
  if (requestStarts.size >= 1000) requestStarts.delete(requestStarts.keys().next().value);
  requestStarts.set(details.requestId, details.timeStamp);
}, filter);
chrome.webRequest.onHeadersReceived.addListener(d => observe('headers', d), filter, ['responseHeaders']);
chrome.webRequest.onBeforeRedirect.addListener(d => observe('redirect', d), filter, ['responseHeaders']);
chrome.webRequest.onCompleted.addListener(d => observe('completed', d), filter);
chrome.webRequest.onErrorOccurred.addListener(d => observe('error', d), filter);
const trackedDownloads = new Map();
chrome.downloads.onCreated.addListener(item => {
  try {
    const u = new URL(item.url);
    const archive = u.origin === 'https://timesofmalta.com' && /^\/download\/\d{8}-\d+$/.test(u.pathname);
    const final = destination(item.finalUrl);
    if (!archive && !(final?.host === storageHost && /^\/archive\/\d{8}-\d+\.pdf$/.test(final.path))) return;
    const path = archive ? u.pathname : '/[observed endpoint]';
    if (trackedDownloads.size >= 1000) trackedDownloads.delete(trackedDownloads.keys().next().value);
    trackedDownloads.set(item.id, {path, started: Date.now()});
    void publish({event: 'download', path, timestamp: Date.now(), state: 'in_progress',
      download_id: item.id, destination: final,
      bytes_received: item.bytesReceived, total_bytes: item.totalBytes}, item.finalUrl);
  } catch (_) {}
});
chrome.downloads.onChanged.addListener(async delta => {
  try {
    const tracked = trackedDownloads.get(delta.id);
    if (!tracked) return;
    const {path, started} = tracked;
    if (delta.state || delta.error) push({event: 'download', path, timestamp: Date.now(),
      download_id: delta.id, elapsed_ms: Date.now() - started,
      state: delta.state?.current, download_error: delta.error?.current});
    if (['complete', 'interrupted'].includes(delta.state?.current)) {
      trackedDownloads.delete(delta.id);
      // Read metadata only for this observed download; never query general history.
      const [item] = await chrome.downloads.search({id: delta.id});
      if (item) void publish({event: 'download', path, timestamp: Date.now(),
        download_id: delta.id, state: item.state, elapsed_ms: Date.now() - started,
        destination: destination(item.finalUrl), bytes_received: item.bytesReceived, total_bytes: item.totalBytes}, item.finalUrl);
    }
  } catch (_) {}
});
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id) return;
  if (message.type === 'drainDiagnostics') {
    if (Date.now() - lastHealth >= 30000) {
      push({event: 'health', path: '/archive', timestamp: Date.now(),
        queue_depth: diagnostics.length, dropped_events: droppedEvents,
        fingerprint_pending: fingerprintPending, fingerprint_failures: fingerprintFailures,
        configured_hosts: ['timesofmalta.com', storageHost], observer_version: '1.4.1'});
      lastHealth = Date.now();
    }
    reply(diagnostics.splice(0, 200));
  }
  if (message.type === 'pageSignals') { push(message.record); reply(true); }
});
