// Clicking the toolbar icon opens (or focuses) the connection tab that talks to tom-dl.
chrome.action.onClicked.addListener(async () => {
  const url = chrome.runtime.getURL('bridge.html');
  const tabs = await chrome.tabs.query({url});
  if (tabs.length) await chrome.tabs.update(tabs[0].id, {active: true});
  else await chrome.tabs.create({url});
});

// First install (e.g. loaded by tom-dl's own browser): open the connection tab straight away.
chrome.runtime.onInstalled.addListener(async ({reason}) => {
  if (reason !== 'install') return;
  const url = chrome.runtime.getURL('bridge.html');
  const tabs = await chrome.tabs.query({url});
  if (!tabs.length) await chrome.tabs.create({url});
});
