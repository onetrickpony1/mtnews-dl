let archiveTab;
const status = document.querySelector('#status');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
// Must match BRIDGE_VERSION in chrome_bridge.py; tom-dl asks an older tab to reload itself.
const BRIDGE_VERSION = '3';
const headers = {'X-Bridge-Token': BRIDGE_TOKEN, 'X-Bridge-Version': BRIDGE_VERSION,
                 'Content-Type': 'application/json'};
function allowed(url, download = false) {
  const u = new URL(url);
  if (u.origin !== 'https://timesofmalta.com' || (download && !/^\/download\/\d{8}-\d+$/.test(u.pathname)))
    throw Error('Unsupported URL; stopped.');
}
async function tab() {
  // localStorage is shared by this extension's pages, so a reopened connection tab finds the
  // archive tab the previous one used.
  if (!archiveTab) archiveTab = Number(localStorage.getItem('archiveTab')) || null;
  if (archiveTab) {
    try { return await chrome.tabs.get(archiveTab); } catch (_) { archiveTab = null; }
  }
  const t = await chrome.tabs.create({url: 'about:blank'});
  archiveTab = t.id;
  localStorage.setItem('archiveTab', String(t.id));
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
      if (downloadId === undefined) {
        // A refused download opens a notice page in the tab instead of starting a file.
        const t = await chrome.tabs.get(id);
        if (t.status === 'complete' && t.url && new URL(t.url).pathname.startsWith('/download/')) {
          let text = '';
          try {
            const [r] = await chrome.scripting.executeScript({target: {tabId: id},
              func: () => document.body ? document.body.innerText.slice(0, 5000) : ''});
            text = r.result || '';
          } catch (_) { /* a PDF viewer or a page still loading */ }
          if (/download limit.{0,30}(reached|exceeded)|daily.{0,30}limit.{0,30}reached/i.test(text))
            throw Error('SITE_LIMIT: the site says "Download limit exceeded for today". No PDF was served.');
        }
      }
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
      status.textContent = 'Connected to tom-dl.';
      if (command && command.action === 'reload') { location.reload(); return; }
      if (command) {
        let result;
        try { result = {id: command.id, value: await execute(command)}; }
        catch (error) { result = {id: command.id, error: String(error.message || error)}; }
        const sent = await fetch('http://127.0.0.1:18769/result', {method: 'POST', headers, body: JSON.stringify(result)});
        if (!sent.ok) throw Error('tom-dl rejected a result. Stop and review before resuming.');
      }
    } catch (error) { status.textContent = 'Waiting for tom-dl: ' + error.message; }
    await sleep(500);
  }
}
// One connection tab only: the newest wins (it runs the current code after an update) and
// closes older ones, taking over their archive tab instead of opening another.
async function onlyConnection() {
  const me = await chrome.tabs.getCurrent();
  const tabs = await chrome.tabs.query({url: chrome.runtime.getURL('bridge.html')});
  if (tabs.some(t => t.id > me.id)) { await chrome.tabs.remove(me.id); return false; }
  const older = tabs.filter(t => t.id < me.id).map(t => t.id);
  if (older.length) await chrome.tabs.remove(older);
  return true;
}
onlyConnection().then(only => { if (only) loop(); }, () => loop());
