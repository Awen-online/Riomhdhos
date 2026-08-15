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

/* Moods are gone on purpose. This is an OS and audio-config tool; the Push is the
   instrument's controller. What replaced them is diagnostics — each check states a
   verdict and, when something is wrong, what to do about it. A status line you have to
   remember the correct value of is not a diagnostic. */
function renderChecks(checks) {
  const box = $('checks');
  box.innerHTML = '';
  if (!checks || !checks.length) {
    box.innerHTML = '<div class="check"><div class="stripe"></div>' +
                    '<div class="body"><div class="detail">no checks reported</div></div></div>';
    return;
  }
  for (const c of checks) {
    const el = document.createElement('div');
    el.className = 'check ' + (c.state || '');
    const stripe = document.createElement('div');
    stripe.className = 'stripe ' + (c.state || '');
    const body = document.createElement('div');
    body.className = 'body';
    const label = document.createElement('div');
    label.className = 'label'; label.textContent = c.label;
    const detail = document.createElement('div');
    detail.className = 'detail'; detail.textContent = c.detail || '—';
    body.append(label, detail);
    if (c.fix) {
      const fix = document.createElement('div');
      fix.className = 'fix'; fix.textContent = c.fix;
      body.append(fix);
    }
    el.append(stripe, body);
    box.append(el);
  }
}

/* Driver picker. Switching restarts REAPER, so it arms before it fires — the same
   two-tap rule as shutdown, for the same reason: this is not something to do by accident
   with the phone in your hand between tunes. */
let driversCache = null;

async function loadDrivers() {
  try {
    driversCache = await api('/api/audio/devices');
    renderDrivers(driversCache);
  } catch (e) {
    $('drivers').innerHTML = '';
  }
}

function renderDrivers(info) {
  const box = $('drivers');
  box.innerHTML = '';
  const list = (info && info.devices) || [];
  if (!list.length) {
    box.innerHTML = '<div class="cardnote">No ASIO drivers registered.</div>';
    return;
  }
  for (const d of list) {
    const btn = document.createElement('button');
    btn.className = 'drv' + (d.current ? ' current' : '') + (d.present ? '' : ' absent');
    const nm = document.createElement('span');
    nm.className = 'nm';
    // Trim the boilerplate: "UMC ASIO Driver" reads better as "UMC" on a phone.
    nm.textContent = d.name.replace(/\s*ASIO Driver\s*$/i, '');
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = d.current ? 'in use' : (d.present ? 'connected' : 'not plugged in');
    btn.append(nm, tag);

    if (d.current) {
      btn.disabled = true;
    } else {
      let armed = false, timer = null;
      btn.addEventListener('click', () => {
        if (!armed) {
          armed = true;
          btn.classList.add('armed');
          tag.textContent = d.present ? 'tap again to switch' : 'not plugged in — tap again anyway';
          timer = setTimeout(() => {
            armed = false; btn.classList.remove('armed');
            tag.textContent = d.present ? 'connected' : 'not plugged in';
          }, 4000);
          return;
        }
        clearTimeout(timer);
        armed = false;
        btn.classList.remove('armed');
        switchDriver(d.name);
      });
    }
    box.append(btn);
  }
}

async function switchDriver(name) {
  setBusy(true);
  log(`Switching to ${name} — saving and restarting REAPER…`);
  try {
    const r = await api('/api/audio/device?name=' + encodeURIComponent(name), { method: 'POST' });
    (r.steps || []).forEach(s => log('  ' + s));
    if (r.warning) log('WARNING: ' + r.warning);
    log(r.message || (r.ok ? 'done' : 'failed'));
  } catch (e) {
    log('Switch FAILED: ' + e.message);
  } finally {
    setBusy(false);
    // REAPER takes a while to come back with Kontakt loaded; look again once it should be up.
    setTimeout(() => { refresh(); loadDrivers(); }, 12000);
  }
}

function renderHardware(audio) {
  const items = [];
  const drivers = (audio && audio.asioDrivers) || [];
  if (drivers.length) {
    for (const d of drivers) items.push([`ASIO ${d.bits}-bit`, d.description, 'ok']);
  } else {
    items.push(['ASIO', 'none registered', 'bad']);
  }
  const devs = (audio && audio.devices) || [];
  for (const d of devs) {
    items.push([d.status === 'OK' ? 'Device' : 'Device !', d.name, d.status === 'OK' ? 'ok' : 'bad']);
  }
  if (!devs.length) items.push(['Devices', 'none enumerated', 'warn']);
  for (const e of ((audio && audio.errors) || [])) items.push(['Error', e, 'bad']);
  rows($('hwRows'), items);
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
  else               setBanner('ok',   'Rig is healthy',
                       `${deep.audio_mode || '?'} · ${deep.audio_srate || '?'} Hz · ${deep.audio_bsize || '?'} samples`);

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

  renderChecks(h.checks);
  renderHardware(h.audio);
  (h.notes || []).forEach(n => log(n));
}

function renderOffline(err) {
  setBanner('bad', 'Rig unreachable', 'Powered off, on a different network, or the agent is not running.');
  rows($('audioRows'), [['Device', 'no answer', 'bad']]);
  rows($('hwRows'),    [['Hardware', 'no answer', 'bad']]);
  rows($('rigRows'),   [['Agent', String(err.message || err), 'bad'], ['Address', cfg.host]]);
  renderChecks([{
    label: 'Connection', state: 'bad', detail: String(err.message || err),
    fix: 'If the rig is on a different network — a phone hotspot, say — tap "Find rig on this network".'
  }]);
}

/* ------------------------------------------------------------------ discovery
 *
 * WHY THIS EXISTS
 * On the home LAN the rig is at a known address. On a phone hotspot Android hands it a
 * fresh lease on a different subnet, and nothing in a browser can tell you which. MAC
 * addresses are no help: they are link-layer, they do not route, and a page cannot see
 * them. So the app finds the rig the only way it can — by asking every address on the
 * candidate subnets whether it is the rig.
 *
 * /api/whoami exists precisely for this: sweeping a /24 means up to 254 requests, and
 * pointing that at the full health endpoint would hammer the machine we are looking for.
 *
 * The candidate list is hotspot subnets first, because that is the case this solves.
 */
const HOTSPOT_SUBNETS = [
  '192.168.43', '192.168.42', '192.168.44', '192.168.137',  // common Android/Windows hotspot ranges
  '192.168.49', '192.168.1', '192.168.0', '10.0.0'
];

async function probeHost(base, timeoutMs = 900) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(`${base}/api/whoami`, {
      headers: { Authorization: 'Bearer ' + cfg.token },
      signal: ctl.signal, cache: 'no-store'
    });
    if (!res.ok) return null;
    const j = await res.json();
    return j && j.rig ? { base, rig: j.rig } : null;
  } catch { return null; }
  finally { clearTimeout(timer); }
}

async function findRig() {
  if (!cfg.token) { log('Set the token in Settings first — discovery needs it.'); return; }
  setBusy(true);
  log('Searching for the rig…');
  try {
    // Try the addresses we already know before sweeping anything.
    const known = [cfg.host, 'http://192.168.1.232:8765', 'http://100.98.84.34:8765'];
    for (const b of known) {
      const hit = await probeHost(b, 1500);
      if (hit) { log(`Found ${hit.rig} at ${hit.base}`); cfg.set('host', hit.base); refresh(); return; }
    }

    for (const net of HOTSPOT_SUBNETS) {
      log(`scanning ${net}.0/24 …`);
      // Batched rather than all 254 at once: a phone will happily queue hundreds of
      // sockets and then time every one of them out together.
      for (let start = 1; start < 255; start += 32) {
        const batch = [];
        for (let i = start; i < Math.min(start + 32, 255); i++) {
          batch.push(probeHost(`http://${net}.${i}:8765`));
        }
        const hit = (await Promise.all(batch)).find(Boolean);
        if (hit) {
          log(`Found ${hit.rig} at ${hit.base}`);
          cfg.set('host', hit.base);
          refresh();
          return;
        }
      }
    }
    log('No rig found. Is it powered on and on this network?');
  } finally {
    setBusy(false);
  }
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
    // Driver list is polled far less often than health: it only changes when hardware is
    // plugged in or a switch happens, and it costs a PnP enumeration on the rig.
    if (!driversCache) loadDrivers();
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
$('btnFind').addEventListener('click', findRig);
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
