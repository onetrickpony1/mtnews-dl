let archiveTab;
const status = document.querySelector('#status');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const headers = {'X-Bridge-Token': BRIDGE_TOKEN, 'Content-Type': 'application/json'};
function allowed(url, download = false) {
  const u = new URL(url);
  if (u.origin !== 'https://timesofmalta.com' || (download && !/^\/download\/\d{8}-\d+$/.test(u.pathname)))
    throw Error('Unsupported URL; stopped.');
}
async function tab() {
  if (archiveTab) {
    try { return await chrome.tabs.get(archiveTab); } catch (_) { archiveTab = null; }
  }
  const t = await chrome.tabs.create({url: 'about:blank'});
  archiveTab = t.id;
  return t;
}
async function loaded(id) {
  const end = Date.now() + 45000;
  while (Date.now() < end) {
    const t = await chrome.tabs.get(id);
    if (t.status === 'complete') return;
    await sleep(250);
  }
  throw Error('Archive navigation timed out. Check the Chrome archive tab.');
}
async function navigate(id, url) {
  let previousDocument = null;
  try { previousDocument = (await snapshot(id)).documentStarted; } catch (_) {}
  await chrome.tabs.update(id, {url});
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline) {
    const current = await chrome.tabs.get(id);
    if (current.status === 'complete' && !current.pendingUrl) {
      let state;
      try { state = await snapshot(id); } catch (_) { /* document may still be changing */ }
      if (state && state.readyState === 'complete' && state.documentStarted !== previousDocument) {
        const requested = new URL(url), reached = new URL(state.url);
        // Return genuine issue-to-issue redirects to Python for date validation.
        // Never accept the old document just because it still reports complete.
        if (reached.href === requested.href ||
            (reached.origin === requested.origin && reached.pathname === '/archive/issue' && requested.pathname === '/archive/issue'))
          return state;
        throw Error('Requested ' + url + ' but Chrome reached ' + state.url + '. Check sign-in or a site redirect. No PDF requested.');
      }
    }
    await sleep(250);
  }
  throw Error('Chrome did not finish a new document for ' + url + '. No PDF requested.');
}
async function snapshot(id) {
  const t = await chrome.tabs.get(id);
  allowed(t.url);
  const results = await chrome.scripting.executeScript({target: {tabId: id}, func: () => ({
    url: location.href, documentStarted: performance.timeOrigin, readyState: document.readyState, title: document.title, body: document.body.innerText.slice(0, 150000),
    headings: [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].map(e => e.textContent.trim()),
    links: [...document.querySelectorAll('a[href]')].map(e => ({text: e.textContent.trim(), href: e.href}))
  })});
  return results[0].result;
}
async function download(id, url) {
  allowed(url, true);
  // Subscribe before clicking the real site control; never retry an uncertain request.
  let downloadId;
  const listener = item => {
    if (item.url === url || item.finalUrl === url) downloadId = item.id;
  };
  chrome.downloads.onCreated.addListener(listener);
  try {
    await chrome.scripting.executeScript({target: {tabId: id}, args: [url], func: url => {
      const links = [...document.querySelectorAll('a[href]')].filter(a => a.href === url);
      if (links.length !== 1) throw Error('Download control changed; stopped.');
      links[0].click();
    }});
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      if (downloadId !== undefined) {
        const [item] = await chrome.downloads.search({id: downloadId});
        if (item.state === 'interrupted') throw Error('Chrome download interrupted: ' + item.error);
        if (item.state === 'complete') return {filename: item.filename};
      }
      await sleep(250);
    }
    throw Error('No completed PDF download. Check Chrome for a save dialog, quota notice, or access error. Attempt remains counted.');
  } finally { chrome.downloads.onCreated.removeListener(listener); }
}
async function execute(command) {
  if (command.action === 'completed') {
    const files = [];
    for (const url of command.urls) {
      allowed(url, true);
      const entries = await chrome.downloads.search({urlRegex: '^' + url.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$'});
      for (const item of entries) {
        if (item.url === url && ((item.state === 'complete' && item.exists) || item.state === 'in_progress'))
          files.push({url, filename: item.filename, endTime: item.endTime, state: item.state});
      }
    }
    return files;
  }
  const t = await tab();
  if (command.action === 'navigate') {
    allowed(command.url);
    return navigate(t.id, command.url);
  }
  if (command.action === 'snapshot') { await loaded(t.id); return snapshot(t.id); }
  if (command.action === 'download') return download(t.id, command.url);
  throw Error('Unknown command.');
}
async function loop() {
  while (true) {
    try {
      const response = await fetch('http://127.0.0.1:18769/command', {headers});
      if (!response.ok) throw Error('Connection rejected; reload the installed companion extension.');
      const command = await response.json();
      status.textContent = 'Connected to desktop app.';
      if (command) {
        let result;
        try { result = {id: command.id, value: await execute(command)}; }
        catch (error) {
          result = {id: command.id, error: String(error.message || error)};
          if (command.action === 'download') {
            const record = {event: 'download_failure_page', path: new URL(command.url).pathname,
              timestamp: Date.now(), inspection_status: 'failed'};
            try {
              const state = await snapshot(archiveTab);
              record.body = state.body;
              record.inspection_status = 'captured';
            } catch (_) { /* Explicit failure status travels with the command result. */ }
            result.diagnostics = [record];
          }
        }
        const sent = await fetch('http://127.0.0.1:18769/result', {method: 'POST', headers, body: JSON.stringify(result)});
        if (!sent.ok) throw Error('Desktop app rejected result. Stop and review before resuming.');
      }
    } catch (error) { status.textContent = 'Waiting for desktop app: ' + error.message; }
    await sleep(500);
  }
}
loop();

// Independent of the command/download loop. Drop diagnostics if the app is
// unavailable; never delay, retry, or change a publisher request for logging.
let diagnosticsBusy = false;
let deliveryFailures = 0, deliveryDroppedEvents = 0;
setInterval(async () => {
  if (diagnosticsBusy) return;
  diagnosticsBusy = true;
  let events;
  try {
    events = await chrome.runtime.sendMessage({type: 'drainDiagnostics'});
    if (events?.length) {
      for (const event of events) if (event.event === 'health') {
        event.delivery_failures = deliveryFailures;
        event.delivery_dropped_events = deliveryDroppedEvents;
      }
      const response = await fetch('http://127.0.0.1:18769/diagnostics', {
      method: 'POST', headers, body: JSON.stringify(events), signal: AbortSignal.timeout(2000)
      });
      if (!response.ok) throw Error('Diagnostic delivery failed');
    }
  } catch (_) {
    deliveryFailures++;
    deliveryDroppedEvents += events?.length || 0;
  } finally { diagnosticsBusy = false; }
}, 2000);

// Inspect only the app's archive tab. Keep structured signals, never page text.
let lastSignals = '', signalsBusy = false;
setInterval(async () => {
  if (!archiveTab || signalsBusy) return;
  signalsBusy = true;
  try {
    const current = await chrome.tabs.get(archiveTab);
    const pageUrl = new URL(current.url);
    if (pageUrl.origin !== 'https://timesofmalta.com' || !/^\/archive(?:\/issue)?$|^\/download\/\d{8}-\d+$|^\/restricted-access$/.test(pageUrl.pathname)) return;
    const [result] = await chrome.scripting.executeScript({target: {tabId: archiveTab}, func: () => {
      const patterns = {daily_limit: /daily.{0,30}(?:limit|allowance)|(?:limit|allowance).{0,30}(?:today|per day)|300.{0,20}pages/i,
        limit_reached: /(?:limit|quota).{0,30}(?:reached|exceeded|exhausted)/i,
        remaining: /(?:downloads?|pages?).{0,25}remaining|remaining.{0,25}(?:downloads?|pages?)/i,
        reset: /(?:quota|limit|allowance).{0,40}reset|reset.{0,40}(?:quota|limit|allowance)/i,
        try_later: /try again (?:later|tomorrow)|retry after/i,
        access_challenge: /verify you are human|performing security verification/i};
      const lines = (document.body?.innerText || '').split('\n').filter(line =>
        Object.values(patterns).some(pattern => pattern.test(line)));
      return {event: 'page_signals', path: location.pathname, timestamp: Date.now(),
        signals: Object.entries(patterns).filter(([_, pattern]) => lines.some(line => pattern.test(line))).map(([name]) => name),
        numbers: [...new Set(lines.flatMap(line => [...line.matchAll(/\b\d{1,5}\b/g)].map(match => Number(match[0]))))].slice(0, 20)};
    }});
    const record = {...result.result, event: 'page_inspection', inspection_status:
      result.result.signals.length ? 'captured' : 'no_matching_text'};
    const signature = JSON.stringify([record.path, record.signals, record.numbers]);
    if (signature !== lastSignals) {
      lastSignals = signature;
      await chrome.runtime.sendMessage({type: 'pageSignals', record});
    }
  } catch (_) {
    try { await chrome.runtime.sendMessage({type: 'pageSignals', record: {
      event: 'page_inspection', path: '/archive', timestamp: Date.now(), inspection_status: 'failed'}}); } catch (_) {}
  } finally { signalsBusy = false; }
}, 5000);
