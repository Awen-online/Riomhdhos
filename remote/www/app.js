/* app.js - Ríomhdhos phone control
 *
 * Design notes worth keeping:
 *
 * THE APP MUST OPEN WHEN THE RIG IS OFF.
 * That is precisely when you need the power-on button, and it is also when the server
 * that serves this file is unreachable. The service worker caches the shell so the app
 * still launches; every rig call is then expected to fail, and failing is a normal
 * state with its own screen rather than an error.
 *
 * POWER-ON DOES NOT GO THROUGH THE RIG.
 * It is a direct call to the smart plug. Nothing else in this app talks to anything
 * but the agent.
 *
 * DESTRUCTIVE ACTIONS ARM BEFORE THEY FIRE.
 * Shutdown and reboot need two taps, and the arm lapses after a few seconds. A phone
 * in a pocket between tunes should not be one stray tap from killing the rig mid-set.
 */

const $ = (id) => document.getElementById(id);

const cfg = {
  get host()   { return localStorage.getItem('rig.host')   || location.origin; },
  get token()  { return localStorage.getItem('rig.token')  || ''; },
  get plugOn() { return localStorage.getItem('rig.plugOn') || ''; },
  set(k, v)    { if (v) localStorage.setItem('rig.' + k, v); else localStorage.removeItem('rig.' + k); }
};

function log(msg) {
  const el = $('log');
  const t = new Date().toTimeString().slice(0, 8);
  el.textContent = `${t}  ${msg}\n` + el.textContent;
}

async function api(path, opts = {}) {
  const url = cfg.host.replace(/\/$/, '') + path;
  const res = await fetch(url, {
    ...opts,
    headers: { ...(opts.headers || {}), Authorization: 'Bearer ' + cfg.token },
    cache: 'no-store'
  });
  if (res.status === 401) throw new Error('unauthorized — check the token in Settings');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/* ------------------------------------------------------------------ rendering */

function rows(container, items) {
  container.innerHTML = '';
  for (const [k, v, cls] of items) {
    const row = document.createElement('div');
    row.className = 'row';
    const kk = document.createElement('span'); kk.className = 'k'; kk.textContent = k;
    const vv = document.createElement('span'); vv.className = 'v' + (cls ? ' ' + cls : '');
    vv.textContent = v === undefined || v === null || v === '' ? '—' : String(v);
    row.append(kk, vv);
    container.append(row);
  }
}

const MOODS = ['COSMOS', 'CAIRN', 'EIRE', 'DEEP'];

function renderMoods(deep) {
  const grid = $('moodGrid');
  grid.innerHTML = '';
  // "moods" comes back as "COSMOS:muted | CAIRN:ACTIVE | ..." — parsed here rather than
  // trusting mood_active alone, so an absent track shows as absent instead of silently
  // reading as "not the active one".
  const raw = (deep && deep.moods) || '';
  const state = {};
  raw.split('|').forEach(part => {
    const [name, st] = part.trim().split(':');
    if (name) state[name] = st;
  });
  for (const m of MOODS) {
    const el = document.createElement('div');
    const st = state[m];
    el.className = 'mood' + (st === 'ACTIVE' ? ' active' : st === 'absent' ? ' absent' : '');
    el.textContent = st === 'absent' ? m + ' ?' : m;
    grid.append(el);
  }
}

function setBanner(kind, title, sub) {
  $('banner').className = 'banner ' + kind;
  $('dot').className = 'dot ' + kind;
  $('bannerTitle').textContent = title;
  $('bannerSub').textContent = sub;
}

function render(h) {
  const deep = h.deep || {};
  const reaperUp = h.reaper && h.reaper.running;
  const hung = reaperUp && h.reaper.responding === false;
  const audioOut = deep.audio_out || '';
  const audioBad = /Remote Audio|RDP/i.test(audioOut);

  if (hung)          setBanner('bad',  'REAPER is hung', 'Not responding — almost certainly the ASIO exit deadlock. Restart it.');
  else if (!reaperUp) setBanner('warn', 'Rig is up, REAPER is not', 'The box is reachable but REAPER is not running.');
  else if (audioBad) setBanner('bad',  'Wrong audio device', `Bound to "${audioOut}" — started inside RDP. Restart after disconnecting.`);
  else if (h.notes && h.notes.length) setBanner('warn', 'Running with warnings', h.notes[0]);
  else               setBanner('ok',   'Rig is healthy', `${deep.mood_active || '—'} · ${deep.audio_srate || '?'} Hz · ${deep.tempo || '?'} BPM`);

  rows($('audioRows'), [
    ['Device',  deep.audio_out,   audioBad ? 'bad' : (deep.audio_out ? 'ok' : '')],
    ['Mode',    deep.audio_mode],
    ['Rate',    deep.audio_srate ? deep.audio_srate + ' Hz' : ''],
    ['Buffer',  deep.audio_bsize ? deep.audio_bsize + ' samples' : ''],
    ['Input',   deep.audio_in],
  ]);

  const canary = (v) => v === undefined ? ['', ''] :
    (String(v).startsWith('1') ? ['alive', 'ok'] :
     v === 'absent' ? ['absent', 'bad'] :
     [String(v) + ' — loaded but not running', 'bad']);

  const [pb, pbc]  = canary(deep.pushbrain);
  const [pl, plc]  = canary(deep.pushled);

  rows($('rigRows'), [
    ['REAPER',    reaperUp ? `running (pid ${h.reaper.pid}, ${h.reaper.memMB} MB)` : 'not running',
                  reaperUp ? (hung ? 'bad' : 'ok') : 'warn'],
    ['Version',   deep.reaper_version],
    ['Project',   deep.project],
    ['pushbrain', pb, pbc],
    ['pushled',   pl, plc],
    ['Surfaces',  deep.midi_in],
    ['Drums',     deep.drums_seq, /ok/.test(deep.drums_seq || '') ? 'ok' : (deep.drums_seq ? 'warn' : '')],
    ['Tempo',     deep.tempo ? deep.tempo + ' BPM' : ''],
    ['Uptime',    h.host ? h.host.uptimeMin + ' min' : ''],
    ['Address',   h.host && h.host.networks ? h.host.networks.map(n => n.address).join(', ') : ''],
  ]);

  renderMoods(deep);
  (h.notes || []).forEach(n => log(n));
}

function renderOffline(err) {
  setBanner('bad', 'Rig unreachable', 'Powered off, on a different network, or the agent is not running.');
  rows($('audioRows'), [['Device', 'no answer', 'bad']]);
  rows($('rigRows'),   [['Agent', String(err.message || err), 'bad'], ['Address', cfg.host]]);
  renderMoods(null);
}

/* ------------------------------------------------------------------ actions */

let busy = false;
function setBusy(on) {
  busy = on;
  document.querySelectorAll('button.act').forEach(b => { b.disabled = on; });
}

async function refresh() {
  if (busy) return;
  setBusy(true);
  try {
    const h = await api('/api/health');
    render(h);
  } catch (e) {
    renderOffline(e);
  } finally {
    setBusy(false);
  }
}

async function post(path, label) {
  setBusy(true);
  log(label + '…');
  try {
    const r = await api(path, { method: 'POST' });
    log(label + ': ' + (r.message || (r.ok ? 'ok' : 'failed')));
  } catch (e) {
    log(label + ' FAILED: ' + e.message);
  } finally {
    setBusy(false);
    setTimeout(refresh, 1500);
  }
}

/* Two-tap arming for anything that takes the rig away. The arm lapses on its own so a
   half-pressed button never sits waiting to fire later. */
function arm(btn, label, run) {
  let armed = false, timer = null;
  btn.addEventListener('click', () => {
    if (!armed) {
      armed = true;
      btn.classList.add('armed');
      const original = btn.textContent;
      btn.textContent = 'Tap again to confirm';
      timer = setTimeout(() => {
        armed = false; btn.classList.remove('armed'); btn.textContent = original;
      }, 4000);
      return;
    }
    clearTimeout(timer);
    armed = false;
    btn.classList.remove('armed');
    run();
  });
}

$('btnRefresh').addEventListener('click', refresh);
$('btnRestart').addEventListener('click', () => post('/api/reaper/restart', 'Restart REAPER'));
$('btnStartReaper').addEventListener('click', () => post('/api/reaper/start', 'Start REAPER'));
$('btnStopReaper').addEventListener('click', () => post('/api/reaper/stop', 'Stop REAPER'));
arm($('btnShutdown'), 'Shut down', () => post('/api/system/shutdown', 'Shut down rig'));
arm($('btnReboot'),   'Reboot',    () => post('/api/system/reboot', 'Reboot rig'));

$('btnPowerOn').addEventListener('click', async () => {
  const url = cfg.plugOn;
  if (!url) { log('No smart plug URL set — add one in Settings.'); return; }
  log('Power on: calling plug…');
  try {
    // no-cors so a plug that does not send CORS headers still receives the request.
    // The response is opaque, so success here means "sent", not "confirmed" — the real
    // confirmation is the rig appearing in the next refresh.
    await fetch(url, { mode: 'no-cors', cache: 'no-store' });
    log('Power on: sent. Waiting for the rig to boot…');
    let tries = 0;
    const poll = setInterval(async () => {
      tries++;
      try { await api('/api/health?deep=0'); clearInterval(poll); log('Rig is up.'); refresh(); }
      catch { if (tries > 40) { clearInterval(poll); log('Rig did not come up within 2 minutes.'); } }
    }, 3000);
  } catch (e) {
    log('Power on FAILED: ' + e.message);
  }
});

/* ------------------------------------------------------------------ settings */

$('btnSettings').addEventListener('click', () => {
  $('cfgHost').value   = localStorage.getItem('rig.host')   || '';
  $('cfgToken').value  = cfg.token;
  $('cfgPlugOn').value = cfg.plugOn;
  $('settings').showModal();
});
$('settings').addEventListener('close', () => {
  cfg.set('host',   $('cfgHost').value.trim());
  cfg.set('token',  $('cfgToken').value.trim());
  cfg.set('plugOn', $('cfgPlugOn').value.trim());
  log('settings saved');
  refresh();
});

/* ------------------------------------------------------------------ boot */

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(e => log('sw: ' + e.message));
}

// The token can be handed over by opening the app once with ?token=… , which saves
// typing 48 hex characters on a phone. It is stripped from the URL immediately after.
const qs = new URLSearchParams(location.search);
if (qs.get('token')) {
  cfg.set('token', qs.get('token'));
  history.replaceState({}, '', location.pathname);
  log('token saved from url');
}

refresh();
setInterval(() => { if (!document.hidden && !busy) refresh(); }, 10000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
