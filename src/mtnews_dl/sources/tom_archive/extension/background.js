// Clicking the toolbar icon opens (or focuses) the connection tab that talks to tom-dl.
chrome.action.onClicked.addListener(async () => {
  const url = chrome.runtime.getURL('bridge.html');
  const tabs = await chrome.tabs.query({url});
  if (tabs.length) await chrome.tabs.update(tabs[0].id, {active: true});
  else await chrome.tabs.create({url});
});

// tom-dl's own browser window loads a copy of this extension that also contains autostart.js;
// there the connection tab opens whenever the window starts. In your normal Chrome it opens
// only once, right after you load the extension.
let autostart = false;
try { importScripts('autostart.js'); autostart = self.TOM_DL_AUTOSTART === true; } catch (_) {}
async function openBridge() {
  const url = chrome.runtime.getURL('bridge.html');
  const tabs = await chrome.tabs.query({url});
  if (!tabs.length) await chrome.tabs.create({url, index: 0});
}
chrome.runtime.onInstalled.addListener(({reason}) => { if (reason === 'install' || autostart) openBridge(); });
chrome.runtime.onStartup.addListener(() => { if (autostart) openBridge(); });
