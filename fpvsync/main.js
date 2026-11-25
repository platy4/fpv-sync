/* FPV Sync panel: asks Premiere for the open sequence, runs fpvsync.py, moves the DVR clips. */
const fs = require('fs');
const os = require('os');
const path = require('path');
const cp = require('child_process');

const panelDir = fs.realpathSync(path.dirname(
  decodeURIComponent(location.pathname).replace(/^\/([A-Za-z]:)/, '$1')));
// installed copy: fpvsync.py sits next to the panel; development copy: one folder up
const APP_DIR = fs.existsSync(path.join(panelDir, 'fpvsync.py')) ? panelDir : path.join(panelDir, '..');
const SCRIPT = path.join(APP_DIR, 'fpvsync.py');
const LOG = path.join(APP_DIR, 'panel_log.txt');
let cfg;
function loadConfig() {          // re-read on every click, so config edits apply without reopening
  cfg = { python: 'python', dvr: 'orqa', action: '', lead: 1, prior_window: 30 };
  try { Object.assign(cfg, JSON.parse(fs.readFileSync(path.join(panelDir, 'config.json'), 'utf8'))); } catch (e) {}
  if (cfg.python !== 'python' && !fs.existsSync(cfg.python)) cfg.python = 'python';
}
loadConfig();

function logLine(s) {
  try { fs.appendFileSync(LOG, new Date().toISOString() + '  ' + s + '\n'); } catch (e) {}
}

const $ = (id) => document.getElementById(id);
// the version at the top, from the panel's own manifest (so it can't go out of date)
try {
  const mv = /ExtensionBundleVersion="([^"]+)"/.exec(fs.readFileSync(path.join(panelDir, 'CSXS', 'manifest.xml'), 'utf8'));
  if (mv) $('ver').textContent = 'v' + mv[1].replace(/\.0$/, '');
} catch (e) {}
const evalES = (code, quiet) => new Promise((res) => window.__adobe_cep__.evalScript(code, (out) => {
  if (!quiet) {
    logLine('> ' + code.slice(0, 200));
    logLine('< ' + String(out).slice(0, 2000));
  }
  res(out);
}));
const status = (msg, cls) => { $('status').textContent = msg; $('status').className = TONE[cls] || cls || ''; };
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const base = (p) => path.basename(p || '');

// the analysis that's running (a button's run): Stop talks to it.
// First click: a "stop" file tells it to read no more footage and line up what it has.
// Second click: cancel - the analysis is ended and nothing in the sequence changes.
let active = null;
function showStop(on) {
  $('stop').hidden = !on;
  $('stoptxt').textContent = 'Stop';
  $('stop').title = "Stop: line up what's analysed so far";
}
$('stop').addEventListener('click', () => {
  if (!active) return;
  if (active.stop === 0) {
    active.stop = 1;
    try { fs.writeFileSync(path.join(active.dir, 'stop'), '1'); } catch (e) {}
    $('stoptxt').textContent = 'Stopping… click to cancel';
    $('stop').title = 'Click again to cancel everything: nothing in the sequence changes';
    bar(undefined, "Stopping: lining up what's analysed so far");
  } else if (active.stop === 1) {
    active.stop = 2;
    $('stoptxt').textContent = 'Cancelling…';
    cp.exec(`taskkill /pid ${active.child.pid} /T /F`, { windowsHide: true }, () => {});
  }
});

function runPython(job, onLine) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fpvsync-'));
  const jobFile = path.join(dir, 'job.json'), outFile = path.join(dir, 'out.json');
  fs.writeFileSync(jobFile, JSON.stringify(job));
  const tracked = !!onLine;
  return new Promise((resolve, reject) => {
    const child = cp.spawn(cfg.python, ['-u', SCRIPT, '--job', jobFile, outFile], { windowsHide: true });
    if (tracked) { active = { child, dir, stop: 0 }; showStop(true); }
    let err = '', buf = '';
    child.stdout.on('data', (d) => {
      buf += d.toString();
      const lines = buf.split(/\r?\n/); buf = lines.pop();
      lines.forEach((l) => l && onLine && onLine(l));
    });
    child.stderr.on('data', (d) => { err += d.toString(); });
    child.on('error', (e) => reject(e));
    child.on('close', () => {
      if (tracked) {
        const cancelled = active && active.child === child && active.stop === 2;
        active = null;
        showStop(false);
        drawLive();                         // skip / undo only while the analysis runs
        if (cancelled) return reject(new Error('Cancelled: nothing in the sequence was changed.'));
      }
      if (fs.existsSync(outFile)) return resolve(JSON.parse(fs.readFileSync(outFile, 'utf8')));
      reject(new Error(err || 'no result from fpvsync.py'));
    });
  });
}

function probeSize(file) {
  return new Promise((resolve) => {
    cp.execFile('ffprobe', ['-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height',
      '-of', 'csv=p=0', file], { windowsHide: true }, (err, out) => {
      const m = !err && /(\d+),(\d+)/.exec(out || '');
      resolve(m ? [Number(m[1]), Number(m[2])] : null);
    });
  });
}

const TONE = { move: 'ok', moved: 'ok', 'in sync': 'ok', placed: 'ok', matched: 'ok',
               check: 'warn', 'no match': 'warn', blocked: 'warn', skipped: 'bad', error: 'bad' };
// a run's outcome per file, as the last chip on its row in the live list (no separate results list):
// what happened to it in the sequence, and anything worth a look (blocked, check by eye, why skipped)
function render(rows) {
  const tone = { ok: 'ok', warn: 'warn', bad: 'bad' };
  rows.forEach((r) => {
    if (!r.path || r.why === 'skipped by you') return;       // its row already shows it's skipped
    const t = TONE[r.status] || 'warn';
    const move = r.move_frames !== undefined && r.status === 'moved'
      ? ` ${r.move_frames > 0 ? '+' : ''}${r.move_frames} fr` : '';
    let text = r.status + move;
    if (r.status === 'skipped' || r.status === 'blocked' || r.status === 'error') text += ': ' + (r.why || '');
    else if (r.status === 'check') text = 'check: ' + (r.detail || '');
    if (r.note) text += ' · ' + r.note;
    if (r.status === 'matched' || r.status === 'no match' || r.status === 'not analysed') return;   // already said
    finalChip(r.path, tone[t] || 'warn', text);
  });
  drawLiveNow();
}

async function readSequence(quiet) {
  if (!quiet) status('Reading sequence…');
  const raw = await evalES('fpv_dump()', quiet);
  let seq;
  try { seq = JSON.parse(raw); } catch (e) { throw new Error('Premiere returned: ' + raw); }
  if (seq.error) throw new Error(seq.error);
  return seq;
}

// ---- progress bar: percentage only (how long a trip takes can't be told in advance)
const prog = { t0: 0, last: 0, lo: 0, hi: 1 };
const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
function stage(lo, hi) { Object.assign(prog, { lo, hi }); }
function bar(frac, label) {
  let f = frac === undefined ? prog.last : prog.lo + (prog.hi - prog.lo) * frac;
  f = Math.max(prog.last, Math.min(0.995, f));                // never goes back; 100 % only when done
  prog.last = f;
  $('fill').style.width = `${(f * 100).toFixed(1)}%`;
  $('pct').textContent = `${Math.round(f * 100)}%`;
  if (label !== undefined) $('plabel').innerHTML = label;
}

// ---- live file list + counters, fed by the analysis ("@@files [...]", "@@ev {...}")
const ROLE_NAME = { dvr: 'DVR', hd: 'HD', stick: 'Stickcam' };
const live = { files: [], byPath: {}, cur: null, synced: 0, mode: 'build', skip: new Set(), sel: null };
function resetLive() {
  Object.assign(live, { files: [], byPath: {}, cur: null, synced: 0, mode: typeof action === 'string' ? action : 'build',
                        skip: new Set(), sel: null, selWas: null });
  $('tiles').innerHTML = ''; $('files').innerHTML = ''; $('livebox').hidden = true;
}
function setFiles(list) {
  live.files = list.map((f) => ({ p: f.p, r: f.r, chips: [], done: null }));
  live.byPath = {};
  live.files.forEach((f) => { live.byPath[f.p] = f; });
  $('livebox').hidden = !live.files.length;
  drawLive();
}
function onEvent(e) {
  const f = live.byPath[e.p];
  if (!f) return;
  if (e.c) {
    const old = f.chips.find((c) => c.c === e.c);
    if (old) Object.assign(old, { s: e.s, t: e.t }); else f.chips.push({ c: e.c, s: e.s, t: e.t });
    if (e.s === 'run') { live.cur = f.p; bar(undefined, `${esc(e.t)} · <b>${esc(base(f.p))}</b>`); }
  }
  if (e.d) f.done = e.d;
  if (e.d === 'skipped') {                                  // its step stopped there: nothing runs on it now
    f.chips = f.chips.filter((c) => c.s !== 'run');
    if (live.cur === f.p) live.cur = null;
    if (e.final) live.skip.add(f.p);                        // the run is over and it was left out
    else if (!live.skip.has(f.p)) { f.done = null; f.chips = []; }  // un-skipped meanwhile: waits to be processed again
  }
  if (e.d === 'part' && /^continues /.test(e.t || '')) f.head = e.t.slice(10);
  if (e.d && e.d !== 'skipped' && e.c === null && !live.skip.has(f.p)) {
    f.chips = f.chips.filter((c) => c.s !== 'run');          // processed again after an un-skip
  }
  drawLive();
}
let drawPending = false;
function drawLive() {                       // at most ~10 redraws a second
  if (drawPending) return;
  drawPending = true;
  setTimeout(() => { drawPending = false; drawLiveNow(); }, 100);
}
function drawLiveNow() {
  const rows = live.files.filter((f) => f.done !== 'part');
  const of = (r) => rows.filter((f) => f.r === r);
  const armed = (r) => of(r).filter((f) => f.chips.some((c) => c.c === 'arm' && c.s === 'ok')).length;
  const tile = (v, tot, k, cls, role) => `<div class="tile ${cls || ''}"><div class="v">${v}` +
    (tot !== undefined ? `<small> / ${tot}</small>` : '') + `</div><div class="k">` +
    (role ? `<span class="sw ${role}"></span>` : '') + `${k}</div></div>`;
  const roles = ['dvr', 'hd', 'stick'].filter((r) => of(r).length);
  const matched = rows.filter((f) => f.done === 'matched' && f.r !== 'hd').length;
  const nomatch = rows.filter((f) => f.done === 'nomatch').length;
  const corner = live.mode === 'place';       // DVR in the Corner: no matching, so no counters
  $('tiles').innerHTML = corner ? '' : `<div class="tiles">${tile(matched, undefined, 'matched', 'ok')}` +
    `${tile(live.synced, undefined, 'synced', 'ok')}${tile(nomatch, undefined, 'no match', nomatch ? 'bad' : '')}</div>`;
  const short = (t) => String(t).replace(/\.(mp4|mov|avi)(\.mp4)?/gi, '');
  const chip = (c) => `<span class="chip ${c.s}">${c.s === 'ok' ? '✓ ' : c.s === 'bad' ? '✗ ' : c.s === 'warn' ? '⚠ ' : ''}` +
    `${esc(short(c.t))}</span>`;
  // one column per kind of footage (side by side when the panel is wide enough), headed by its
  // "arming detected" count; the file being worked on is highlighted where it is (no scrolling)
  // each row: click it to jump to the clip in the timeline; the target icon does the same; the skip
  // icon (any file, any time while it runs) leaves it out, and turns into undo to process it again
  const icons = (f, sk) => `<div class="acts"><span class="ib show" title="Show in the timeline">${ICON_TARGET}</span>` +
    (prog.running && active && !corner ? (sk ? `<span class="ib undo" title="Skipped: click to process it again">${ICON_UNDO}</span>`
                                    : `<span class="ib skip" title="Skip this clip">${ICON_SKIP}</span>`) : '') + `</div>`;
  $('files').innerHTML = `<div class="cols">` + roles.map((r) => `<div class="col">` +
    (corner ? `<div class="grp"><span class="sw ${r}"></span>${ROLE_NAME[r]}</div>`
      : tile(armed(r), of(r).length, `${ROLE_NAME[r]} · arming detected`, '', r)) + of(r).map((f) => {
      const sk = live.skip.has(f.p) || (f.done === 'skipped' && !prog.running);
      // skipped: a step still shown as running says "skipped" instead (it stops on the click)
      const real = f.chips.filter((c) => c.c !== 'final' || !sk)
        .map((c) => (sk && c.s === 'run' ? { c: c.c, s: '', t: 'skipped' } : c));
      return `<div class="row${f.p === live.cur && !f.done && !sk ? ' cur' : ''}${sk ? ' skipped' : ''}` +
        `${f.p === live.sel ? ' picked' : ''}" data-p="${esc(f.p)}">` +
        `<div class="n" title="${esc(f.p)}">${esc(base(f.p).replace(/\.(mp4|mov|avi)(\.mp4)?$/i, ''))}</div><div class="chips">` +
        (real.length ? real.map(chip).join('')
                     : `<span class="chip">${sk || f.done === 'skipped' ? 'skipped' : 'waiting'}</span>`) +
        `</div>${icons(f, sk)}</div>`;
    }).join('') + `</div>`).join('') + `</div>`;
}
// the other way round: one clip selected in the timeline -> its row is outlined and scrolled to
// (only when the selection changes; selecting several clips does nothing)
let selBusy = false;
function rowFor(p) {
  const f = live.byPath[p];
  if (!f) return null;
  if (f.done !== 'part') return f.p;
  const head = live.files.find((x) => base(x.p) === f.head);       // a split file: its first file's row
  return head ? head.p : null;
}
async function followSelection() {
  if (document.hidden || !live.files.length || selBusy) return;
  selBusy = true;                                     // one question to Premiere at a time
  let p;
  try { p = String(await evalES('fpv_selOne()', true)); } finally { selBusy = false; }
  if (p === live.selWas) return;                      // (a new run resets it: what's selected shows again)
  live.selWas = p;
  const r = p ? rowFor(p) : null;
  if (r === live.sel) return;
  live.sel = r;
  drawLiveNow();
  const el = r && [...$('files').querySelectorAll('.row')].find((x) => x.dataset.p === r);
  if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
}
setInterval(followSelection, 1000);
const ICON_TARGET = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="7"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/></svg>';
const ICON_SKIP = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M5 5l9 7-9 7zM16 5h3v14h-3z"/></svg>';
const ICON_SPK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/></svg>';
const ICON_MUTE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M17 9l5 6M22 9l-5 6"/></svg>';
const ICON_UNDO = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/></svg>';
// the analysis reads skip.json (in its job folder) whenever it changes
function writeSkip() {
  if (!active) return;
  try { fs.writeFileSync(path.join(active.dir, 'skip.json'), JSON.stringify([...live.skip])); } catch (e) {}
}
async function showInTimeline(p) {
  const out = String(await evalES(`fpv_show(${JSON.stringify(p)})`));
  if (out === 'missing') status(`${base(p)} isn't in the sequence right now.`, 'blocked');
}
$('files').addEventListener('click', (e) => {
  const row = e.target.closest('.row');
  if (!row) return;
  const p = row.dataset.p;
  if (e.target.closest('.ib.skip, .ib.undo')) {
    if (live.skip.has(p)) {
      live.skip.delete(p);
      const f = live.byPath[p];
      if (f && f.done === 'skipped') { f.done = null; f.chips = []; }    // back to waiting: processed again later
    } else live.skip.add(p);
    writeSkip();
    drawLiveNow();
    return;
  }
  showInTimeline(p);
});
// the run's last word on a file, as one more chip (what the old results list said)
function finalChip(p, s, t, key = 'final') {
  const f = live.byPath[p];
  if (!f) return;
  const old = f.chips.find((c) => c.c === key);
  if (old) Object.assign(old, { s, t }); else f.chips.push({ c: key, s, t });
}
function onPython(line) {
  if (line.startsWith('@@files ')) return setFiles(JSON.parse(line.slice(8)));
  if (line.startsWith('@@ev ')) return onEvent(JSON.parse(line.slice(5)));
  const m = /^@@ ([\d.]+) (-?\d+) (.*)$/.exec(line);     // "@@ 0.4231 312 HD DJI_0011.MP4"
  if (m) bar(Number(m[1]), esc(m[3]));
}

async function busy(fn) {
  loadConfig();
  $('sync').disabled = $('place').disabled = $('build').disabled = true;
  $('log').textContent = ''; status('');
  Object.assign(prog, { t0: Date.now(), lo: 0, hi: 1, last: 0, running: true, stopped: false });
  $('camgo').disabled = true;                                  // the camera list is locked while it runs
  document.querySelectorAll('#camlist select, #pipbox input').forEach((s_) => { s_.disabled = true; });
  resetLive();
  $('progress').hidden = false;
  $('progress').className = '';
  bar(0, 'Starting…');
  const tick = () => { $('ptime').textContent = ' · ' + fmt((Date.now() - prog.t0) / 1000); };   // elapsed only
  tick();
  const timer = setInterval(tick, 1000);
  let ok = false;
  try { await fn(); ok = true; } catch (e) { status(e.message, 'error'); }
  finally {
    clearInterval(timer);
    $('ptime').textContent = '';
    prog.running = false;
    showStop(false);
    $('sync').disabled = $('place').disabled = $('build').disabled = false;
    live.cur = null; drawLiveNow();
    cams.sig = null; refreshCams(true);                         // the selection has changed: read it again
    if (ok) {
      $('fill').style.width = '100%'; $('pct').textContent = '100%'; $('progress').className = 'done';
      $('plabel').textContent = (prog.stopped ? 'Stopped early · done in ' : 'Done in ') + fmt((Date.now() - prog.t0) / 1000);
    }
    else { $('progress').className = 'failed'; $('plabel').textContent = 'Stopped'; }
  }
}

// ---- actions: the three buttons are a menu (the picked one is blue); Start is the only way to run one.
const ACTIONS = {
  build: { start: 'Start matching',
    help: 'Select your HD, DVR and stickcam footage in the timeline (Ctrl+A for everything), set what each camera ' +
          'is below, then Start. Flights are matched, synced and laid out in recording order where the selection ' +
          "was; clips you didn't select are never changed." },
  sync: { start: 'Start syncing',
    help: 'Place each DVR and stickcam clip roughly over its HD footage, select them in the timeline, set what each ' +
          "camera is below, then Start. Each clip is synced to the HD footage it overlaps; clips you didn't select " +
          'are never changed.' },
  place: { start: 'Start PiP layout',
    help: 'Select the clips in the timeline, set what each camera is below, then Start. Each DVR and stickcam ' +
          "clip is resized and moved as set below; clips you didn't select are never changed." },
};
let action = 'build';

// ---- cameras: which footage is DVR, HD or stickcam. The list shows the cameras in the selection (from the
// files' metadata, one row per camera and resolution) and follows the selection in Premiere. The user sets
// each one's role, which is remembered for that camera; nothing is guessed (a new camera starts on "Choose…").
const ROLE_OPTS = [['dvr', 'DVR'], ['hd', 'HD'], ['stick', 'Stickcam'], ['leave', 'None']];
const EMPTY_MSG = 'Select the clips in the timeline (Ctrl+A for everything).';
const cams = { groups: [], ctx: null, sig: null, busy: false, again: false, startAfter: false, chosen: {}, note: '' };

function rolesNow() {                       // { camera key: role } as the list shows it
  return Object.fromEntries(cams.groups.map((g) => [g.key, cams.chosen[g.key] || g.role || '']));
}
function micPaths() {                       // the DVR files whose audio you keep (goggles with a mic)
  const roles = rolesNow();
  return cams.groups.filter((g) => roles[g.key] === 'dvr' && audioKept(g.key)).flatMap((g) => g.paths || []);
}
function roleOfPath() {                     // { file: role } for the files in the selection
  const roles = rolesNow(), out = {};
  cams.groups.forEach((g) => (g.paths || []).forEach((p) => { out[p] = roles[g.key]; }));
  return out;
}
function readyCheck() {                     // every camera needs a role; Match & Sync and Sync need an HD camera
  const roles = Object.values(rolesNow());
  const why = !roles.length ? ''
    : roles.some((r) => !r) ? 'Choose what each camera is.'
    : action === 'place'
      ? (!roles.some((r) => r === 'dvr' || r === 'stick') ? 'Set a camera to DVR (or Stickcam): those go in the corners.' : '')
    : !roles.includes('hd') ? 'Set at least one camera to HD: the DVR and stickcam are synced to it.'
    : '';
  $('camgo').disabled = prog.running || !roles.length || !!why;
  $('camgo').title = !roles.length ? EMPTY_MSG : why;
  $('camwhy').textContent = why;
}
function drawPicker() {
  if (!cams.groups.length) {
    $('camlist').innerHTML = `<div class="empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round"><path d="M4 4l7 16 2.5-6.5L20 11z"/></svg><div><b>${esc(cams.note || EMPTY_MSG)}</b>
      <span>The cameras in the selection show up here.</span></div></div>`;
  } else {
    const roles = rolesNow();
    $('camlist').innerHTML = cams.groups.map((g, k) => {
      const role = roles[g.key];
      return `<div class="cam"><span class="nm"><b>${esc(g.name)}</b>
      <span class="sub">${g.clips} clip${g.clips === 1 ? '' : 's'} · ${esc(g.formats.join(', '))}</span>
      <span class="why">${esc(g.folders.join(', '))}${g.known ? '' : ' · <i>new camera</i>'}</span></span>
      ${role === 'dvr' ? `<button class="snd${audioKept(g.key) ? '' : ' off'}" data-k="${k}"${prog.running ? ' disabled' : ''}
        title="${audioKept(g.key) ? 'Click to remove the DVR audio (goggles without a mic record silence)'
                                    : 'Click to keep the DVR audio (goggles with a mic)'}">` +
        `${audioKept(g.key) ? ICON_SPK + 'Audio kept' : ICON_MUTE + 'Audio removed'}</button>` : ''}
      <select class="role ${role || 'none'}" data-k="${k}"${prog.running ? ' disabled' : ''}>` +
      (role ? '' : '<option value="" selected disabled>Choose…</option>') +
      ROLE_OPTS.map(([v, t]) => `<option value="${v}"${v === role ? ' selected' : ''}>${t}</option>`).join('') +
      `</select></div>`;
    }).join('');
    $('camlist').querySelectorAll('select').forEach((s) => s.addEventListener('change', () => {
      s.className = 'role ' + s.value;
      cams.chosen[cams.groups[Number(s.dataset.k)].key] = s.value;
      drawPicker();                            // the Audio button follows the DVR role
    }));
    $('camlist').querySelectorAll('button.snd').forEach((b) => b.addEventListener('click', () => {
      const key = cams.groups[Number(b.dataset.k)].key;
      pipSet.audio[key] = !audioKept(key);
      savePip();
      drawPicker();
    }));
  }
  readyCheck();
  if (typeof drawPip === 'function') drawPip();
}
// still images (logos, title cards): never part of a run, and left where they are in the timeline
const IMAGE_RE = '\\.(png|jpe?g|tiff?|psd|bmp|gif|webp|exr|dpx)$';
const isImage = (p) => new RegExp(IMAGE_RE, 'i').test(p || '');
// the files whose cameras matter for the picked action: the selection (Sync: and the clips it overlaps)
async function collect() {
  const seq = await readSequence(true);
  const video = seq.clips.filter((c) => c.kind === 'video' && c.path && !isImage(c.path));
  const sel = video.filter((c) => c.selected);
  const files = action === 'sync'
    ? video.filter((c) => sel.some((s) => Math.min(s.end, c.end) > Math.max(s.start, c.start)))
    : sel;
  return { seq, paths: sel.length ? [...new Set(files.map((c) => c.path))] : [] };
}
// re-read the cameras when the selection changed (checked every 1.5 s), or right away (force)
async function refreshCams(force) {
  if (prog.running || document.hidden) return;
  if (cams.busy) { if (force) cams.again = true; return; }
  cams.busy = true;
  let changed = false;                       // redraw only then (a redraw would cut a drag or a width being typed)
  try {
    const sig = String(await evalES('fpv_selSig()', true));
    if (!force && sig === cams.sig) return;
    changed = true;
    cams.sig = sig;
    const c = await collect();
    const r = c.paths.length ? await runPython({ mode: 'cameras', paths: c.paths, dvr: cfg.dvr }) : { groups: [] };
    if (r.error) throw new Error(r.error);
    Object.assign(cams, { groups: r.groups, ctx: c, note: '' });
  } catch (e) {
    changed = true;
    logLine('cameras: ' + e.message);
    Object.assign(cams, { groups: [], ctx: null, sig: null,
      note: /sequence/i.test(e.message) ? 'Open a sequence, then select the clips in the timeline.' : '' });
  } finally {
    cams.busy = false;
    if (changed) drawPicker();
    if (cams.again) { cams.again = false; refreshCams(true); }
    else if (cams.startAfter) { cams.startAfter = false; if (!$('camgo').disabled) $('camgo').click(); }
  }
}
setInterval(() => refreshCams(false), 1500);

// ---- PiP Layout: where the DVR and stickcam go and how wide (px in the sequence). One set of settings for
// the PiP Layout action and the tickbox in Match & Sync / Sync, saved next to the panel.
const SETTINGS_FILE = path.join(APP_DIR, 'panel_settings.json');
const ANCHORS = { tl: [0, 0], tc: [0.5, 0], tr: [1, 0], ml: [0, 0.5], c: [0.5, 0.5], mr: [1, 0.5],
                  bl: [0, 1], bc: [0.5, 1], br: [1, 1] };
const ANCHOR_NAME = { tl: 'Top-left', tc: 'Top-centre', tr: 'Top-right', ml: 'Middle-left', c: 'Centre',
                      mr: 'Middle-right', bl: 'Bottom-left', bc: 'Bottom-centre', br: 'Bottom-right' };
const pipSet = { on: { build: true, sync: true }, dvr: { at: 'br', width: null }, stick: { at: 'bl', width: null },
                 audio: {} };                // audio: { DVR camera key: false } where its audio is removed
try {
  const saved = JSON.parse(fs.readFileSync(SETTINGS_FILE, 'utf8')).pip || {};
  ['on', 'dvr', 'stick', 'audio'].forEach((k) => Object.assign(pipSet[k], saved[k] || {}));
} catch (e) {}
const audioKept = (key) => pipSet.audio[key] !== false;          // kept unless you removed it
function savePip() { try { fs.writeFileSync(SETTINGS_FILE, JSON.stringify({ pip: pipSet }, null, 1)); } catch (e) {} }
const pipCfg = () => Object.assign({ dvr_width: 970, crop_top: 0.6, crop_right: 0, dvr_size_fallback: [1280, 960] }, cfg.pip || {});
function srcSize(kind) {                     // pixel size of the selection's camera with that role
  const roles = rolesNow();
  const g = cams.groups.find((x) => roles[x.key] === kind && x.size && x.size[0] && x.size[1]);
  return g ? g.size : kind === 'dvr' ? pipCfg().dvr_size_fallback : [1920, 1080];
}
const dvrDefault = () => Math.round(pipCfg().dvr_width);   // the same on-screen width whatever the DVR's resolution
const pipWidth = (kind) => pipSet[kind].width || dvrDefault();                   // the stickcam: as wide as the DVR
function pipKinds() {                        // the PiP roles in the selection
  const r = Object.values(rolesNow());
  return ['dvr', 'stick'].filter((k) => r.includes(k));
}
function drawPip() {
  const box = $('pipbox');
  const kinds = pipKinds();
  if (!cams.groups.length || !kinds.length) { box.innerHTML = ''; box.className = ''; return; }
  const tick = action !== 'place';
  const on = !tick || pipSet.on[action];
  const seq = cams.ctx && cams.ctx.seq;
  const SW = (seq && seq.width) || 1920, SH = (seq && seq.height) || 1080;
  const PW = 300, PH = Math.round(PW * SH / SW), M = 6, k = PW / SW;
  const dims = (kind) => { const w = pipWidth(kind), [sw, sh] = srcSize(kind); return [w, Math.round(w * sh / sw)]; };
  const rows = kinds.map((kind) => {
    const [w, h] = dims(kind);
    return `<div class="crow"><span class="who"><span class="sw ${kind}"></span>${ROLE_NAME[kind]}</span>` +
      `<input data-kind="${kind}" value="${w}"${prog.running ? ' disabled' : ''}><span class="dim">× ${h} px</span>` +
      `<span class="hint">${kind === 'dvr' ? `default ${dvrDefault()} px` : 'default: as wide as the DVR'}</span></div>`;
  });
  const spot = (kind, at) => {               // a box's place in the preview at an anchor
    const [w, h] = dims(kind).map((v) => v * k), [fx, fy] = ANCHORS[at];
    return [M + fx * (PW - 2 * M - w), M + fy * (PH - 2 * M - h), w, h];
  };
  const boxes = kinds.map((kind) => {
    const [l, t, w, h] = spot(kind, pipSet[kind].at);
    return `<div class="pip ${kind}" data-kind="${kind}" style="left:${l}px;top:${t}px;width:${w}px;height:${h}px">` +
      `${kind === 'dvr' ? 'DVR' : 'Stick'}</div>`;
  });
  box.className = 'pipbox' + (tick ? (on ? '' : ' off') : ' plain');
  box.innerHTML = (tick ? `<label><input type="checkbox" id="pipon"${on ? ' checked' : ''}${prog.running ? ' disabled' : ''}>PiP Layout</label>`
                        : '<div class="lbl">PiP Layout</div>') +
    `<div class="body"><div class="pipgrid"><div class="piprows">${rows.join('')}` +
    `<div class="pipnote" id="pipnote">Drag a box to move it: it snaps to the corners, edges and centre.</div></div>` +
    `<div class="preview" id="pipprev" style="width:${PW}px;height:${PH}px">${boxes.join('')}` +
    `<span class="tag">${SW}×${SH} sequence</span></div></div>` +
    `<div class="pipnote">Only the cameras in the selection are listed. Settings are remembered.</div></div>`;
  if (tick) $('pipon').addEventListener('change', (e) => { pipSet.on[action] = e.target.checked; savePip(); drawPip(); });
  box.querySelectorAll('.crow input').forEach((inp) => inp.addEventListener('change', () => {
    const v = Math.round(Number(inp.value));
    const kind = inp.dataset.kind;
    if (v > 0) pipSet[kind].width = v === dvrDefault() ? null : v;     // the default stays "default" (follows the DVR)
    savePip();
    drawPip();
  }));
  // drag a box: it follows the mouse, the nearest of the 9 spots lights up, and it lands there on release
  const prev = $('pipprev');
  prev.querySelectorAll('.pip').forEach((el) => el.addEventListener('mousedown', (e) => {
    if (prog.running) return;
    e.preventDefault();
    const kind = el.dataset.kind;
    const x0 = e.clientX, y0 = e.clientY, l0 = el.offsetLeft, t0 = el.offsetTop;
    el.classList.add('dragging');
    const ghost = document.createElement('div');
    ghost.className = 'ghost';
    prev.appendChild(ghost);
    const dots = Object.keys(ANCHORS).map((at) => {
      const d = document.createElement('div');
      const [fx, fy] = ANCHORS[at];
      d.className = 'snap';
      d.style.left = `${M + fx * (PW - 2 * M)}px`;
      d.style.top = `${M + fy * (PH - 2 * M)}px`;
      prev.appendChild(d);
      return [at, d];
    });
    let best = pipSet[kind].at;
    const move = (ev) => {
      const l = Math.max(0, Math.min(PW - el.offsetWidth, l0 + ev.clientX - x0));
      const t = Math.max(0, Math.min(PH - el.offsetHeight, t0 + ev.clientY - y0));
      el.style.left = `${l}px`;
      el.style.top = `${t}px`;
      best = Object.keys(ANCHORS).reduce((b, at) => {
        const [al, aT] = spot(kind, at), [bl_, bt] = spot(kind, b);
        return Math.hypot(al - l, aT - t) < Math.hypot(bl_ - l, bt - t) ? at : b;
      }, best);
      const [gl, gt, gw, gh] = spot(kind, best);
      Object.assign(ghost.style, { left: `${gl}px`, top: `${gt}px`, width: `${gw}px`, height: `${gh}px` });
      dots.forEach(([at, d]) => d.classList.toggle('on', at === best));
      $('pipnote').textContent = ANCHOR_NAME[best];
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      pipSet[kind].at = best;
      savePip();
      drawPip();
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
    move(e);
  }));
}

function selectAction(a) {
  if (prog.running) return;
  action = a;
  ['build', 'sync', 'place'].forEach((b) => $(b).classList.toggle('primary', b === a));
  $('actionhelp').textContent = ACTIONS[a].help;
  $('camgo').textContent = ACTIONS[a].start;
  readyCheck();
  drawPip();
  refreshCams(true);                         // Sync looks at the clips the selection overlaps too
}
['build', 'sync', 'place'].forEach((b) => $(b).addEventListener('click', () => selectAction(b)));

$('camgo').addEventListener('click', () => {
  if (prog.running || $('camgo').disabled) return;
  if (cams.busy) { cams.startAfter = true; return; }      // the list is refreshing: start right after
  const roles = rolesNow(), ctx = cams.ctx;
  if (!ctx) return;
  const run = { build: runBuild, sync: runSync, place: runPlace }[action];
  busy(() => run(roles, ctx));
});

async function runSync(roles, ctx) {
  const seq = ctx.seq;
  const fps = 254016000000 / seq.tpf;
  stage(0, 0.85);
  bar(0, 'Analysing the selected flights');
  const res = await runPython({ fps, clips: seq.clips, dvr: cfg.dvr, action: cfg.action, roles,
    lead: cfg.lead, prior_window: cfg.prior_window, only_selected: true, osd_search: cfg.osd_search || 'auto',
    dvr_audio: micPaths() },
    onPython);
  if (res.error) {
    $('log').textContent = (res.log || []).join('\n') + (res.trace ? '\n' + res.trace : '');
    throw new Error(res.error);
  }
  prog.stopped = !!res.stopped;
  const rows = res.rows;
  live.synced = rows.filter((r) => r.status === 'in sync').length;
  const todo = rows.filter((x) => x.status === 'move').sort((a, b) => a.order - b.order);
  stage(0.85, pipSet.on.sync ? 0.95 : 1);
  for (const [k, r] of todo.entries()) {
    bar(k / Math.max(1, todo.length), `Moving <b>${esc(r.dvr)}</b>`);
    const out = await evalES(`fpv_move(${JSON.stringify(r.movers.join('|'))}, ${r.move_frames})`);
    if (out === 'ok') { r.status = 'moved'; live.synced++; drawLive(); }
    else { r.status = 'error'; r.why = out; }
  }
  render(rows);
  if (pipSet.on.sync) {                       // then the PiP layout on what was synced
    stage(0.95, 1);
    const ids = new Set(rows.filter((r) => r.status === 'moved' || r.status === 'in sync').flatMap((r) => r.movers || [r.id]));
    const role = roleOfPath();
    const now = await readSequence(true);
    const clips = now.clips.filter((c) => c.kind === 'video' && ids.has(c.id) && (role[c.path] === 'dvr' || role[c.path] === 'stick'));
    await corner(now, clips.map((c) => [c, role[c.path]]));
  }
  const n = (s) => rows.filter((r) => r.status === s).length;
  if (!rows.length) status('Nothing to sync: select DVR or stickcam clips placed over their HD footage.', 'blocked');
  else status((res.stopped ? 'Stopped early: ' : '') + `${n('moved')} moved, ${n('in sync')} already in sync, ` +
    `${n('skipped') + n('blocked') + n('error')} need a look.`, n('moved') ? 'moved' : '');
}

// PiP Layout: each DVR / stickcam clip resized to its width in px and moved to its spot (flush with the
// frame's edges, or centred). clips: [[{ id, path }, 'dvr' | 'stick']] in sequence seq. Returns the failures.
async function corner(seq, clips) {
  const pc = pipCfg();
  const sizes = {};
  const size = async (p) => { if (!(p in sizes)) sizes[p] = (await probeSize(p)) || srcSize(roleOfPath()[p] || 'dvr'); return sizes[p]; };
  const bad = [];
  for (const [k, [c, kind]] of clips.entries()) {
    bar(k / clips.length, `PiP layout: <b>${esc(base(c.path))}</b>`);
    const [w, h] = await size(c.path);
    const width = pipWidth(kind), sc = width / w;
    const sw = sc * w, sh = sc * h;
    const crop = kind === 'dvr' ? pc.crop_top / 100 * sh : 0;         // the DVR's top crop hides this much
    const at = pipSet[kind].at, [fx, fy] = ANCHORS[at];
    const x = fx === 0 ? sw / 2 : fx === 1 ? seq.width - sw / 2 : seq.width / 2;
    const y = fy === 0 ? sh / 2 - crop : fy === 1 ? seq.height - sh / 2 : (seq.height - crop) / 2;
    const out = String(await evalES(`fpv_place(${JSON.stringify(c.id)}, ${sc * 100}, ${x / seq.width}, ${y / seq.height}, ` +
      `${kind === 'dvr' ? pc.crop_top : 0}, ${kind === 'dvr' ? pc.crop_right : 0})`));
    const ok = out.startsWith('ok');
    finalChip(c.path, ok ? 'ok' : 'bad', ok ? `${ANCHOR_NAME[at]} · ${Math.round(width)} px` : out, 'pip');
    if (!ok) bad.push(`${base(c.path)} (${out})`);
  }
  drawLiveNow();
  return bad;
}

async function runPlace(roles, ctx) {
  // the roles are remembered as for the other actions (the corner itself doesn't need the analysis)
  runPython({ mode: 'cameras', paths: ctx.paths, dvr: cfg.dvr, save_roles: roles }).catch(() => {});
  const seq = await readSequence(true);                    // where the clips are now
  const role = roleOfPath();
  const sel = seq.clips.filter((c) => c.kind === 'video' && c.path && c.selected);
  const clips = sel.filter((c) => role[c.path] === 'dvr').map((c) => [c, 'dvr'])
    .concat(sel.filter((c) => role[c.path] === 'stick').map((c) => [c, 'stick']));
  if (!clips.length) return status('No DVR or stickcam clip in the selection.', 'blocked');
  setFiles(clips.map(([c, k]) => ({ p: c.path, r: k })));
  const bad = await corner(seq, clips);
  status(`${clips.length - bad.length} laid out` + (bad.length ? `; not done: ${bad.join(', ')}.` : '.'),
    bad.length ? 'error' : 'moved');
}

const HOW = {
  motion: 'matched by motion', clock: 'matched by clock', order: 'paired by file order only',
  're-armed': 'armed more than once, used the arming that matches',
  'clock, re-armed': 'armed more than once, used the arming that matches',
  'clock, no prop sound': 'no prop sound heard, synced by motion',
  'clock, no ARMED (motion)': 'DVR never shows ARMED, synced by motion: check it',
  'motion only': 'no prop sound in the HD clip, lined up by motion: check it',
  'clock, same DVR': 'second flight in the same DVR recording',
  'clock (motion)': 'a sound before the props, synced by motion',
  'clock, re-armed (motion)': 'armed more than once, synced by motion',
};

async function runBuild(roles, ctx) {
  const { seq, paths } = ctx;
  const fps = 254016000000 / seq.tpf;
  const tpf = seq.tpf;
  stage(0, 0.8);
  bar(0, `Analysing ${paths.length} clips`);
  const res = await runPython({ mode: 'build', fps, paths, dvr: cfg.dvr, action: cfg.action, roles, dvr_audio: micPaths(),
    lead: cfg.lead, gap: cfg.gap || 30, osd_search: cfg.osd_search || 'auto' }, onPython);
  if (res.error) {
    $('log').textContent = (res.log || []).join('\n') + (res.trace ? '\n' + res.trace : '');
    throw new Error(res.error);
  }
  prog.stopped = !!res.stopped;

  // replace the selection in place: the layout starts where the first selected clip started; if it
  // would run into clips that weren't selected, those get pushed later (they keep their order and
  // spacing). Only if a clip runs across that spot does the layout go after the end of the sequence.
  stage(0.8, 0.95);
  bar(0, 'Taking the selected clips out');
  const span = Math.max(...res.placements.map((p) => p.est_start + p.length));
  // the clips selected when Start was pressed (still images and clips without a file stay put)
  const picked = seq.clips.filter((c) => c.kind === 'video' && c.selected && c.path && !isImage(c.path)).map((c) => c.id);
  const tk = String(await evalES(`fpv_takeClips(${JSON.stringify(JSON.stringify(picked))})`));
  if (tk.startsWith('missing: ')) {
    const gone = new Set(tk.slice(9).split(','));
    const names = [...new Set(seq.clips.filter((c) => gone.has(c.id)).map((c) => base(c.path)))];
    throw new Error(`${names.join(', ')} left the sequence during the run, so nothing was changed. ` +
      'Start again: it goes much faster the second time.');
  }
  if (!tk.startsWith('ok')) throw new Error(tk);
  let base0 = Math.round(Number(tk.slice(4)) / tpf);
  let note = '';
  if (String(await evalES(`fpv_isFree(${base0 * tpf}, ${(base0 + span + 1) * tpf})`)) !== 'free') {
    const gapF = Math.round((cfg.gap || 30) * fps);
    bar(0, 'Making room: pushing the later clips right');
    const push = String(await evalES(`fpv_pushAfter(${base0 * tpf}, ${(base0 + span + gapF) * tpf})`));
    if (push.startsWith('ok')) {
      const secs = Math.round(Number(push.slice(4)) / tpf / fps);
      if (secs > 0) note = ` The clips after them were pushed ${secs} s later to make room.`;
    } else if (push.startsWith('blocked')) {
      const end = Math.ceil(Number(await evalES('fpv_seqEnd()')) / tpf);
      base0 = end + gapF;
      note = ` They went after the end of the sequence: ${push.slice(9)}, so the later clips couldn't be pushed.`;
    } else {
      throw new Error(push);
    }
  }
  const topTrack = Math.max(...res.placements.map((p) => p.track));
  const tr = String(await evalES(`fpv_ensureTracks(${topTrack})`));
  if (!tr.startsWith('ok')) logLine('tracks: ' + tr);
  // the clips synced to an HD clip (DVR, stickcam): counted as synced once they're in place
  const syncedPaths = new Set(res.report.filter((r) => r.how !== 'no match')
    .map((r) => r.dvr_path || r.stick_path).filter(Boolean));
  // DVRs first, so the HD sound wins wherever the DVR sound landed on the same audio track
  const order = res.placements.filter((p) => p.track > 1).concat(res.placements.filter((p) => p.track === 1));
  const ends = {};                                  // per track: end of the piece just placed
  let n = 0;
  for (const p of order) {
    bar(n / order.length, `Placing ${++n}/${order.length}: <b>${esc(base(p.path))}</b>`);
    const ticks = p.after_previous && ends[p.track] !== undefined ? ends[p.track] : (base0 + p.start) * tpf;
    const out = String(await evalES(`fpv_put(${JSON.stringify(p.path)}, ${p.track - 1}, ${ticks})`));
    if (!out.startsWith('ok')) throw new Error(`${base(p.path)}: ${out}`);
    ends[p.track] = Number(out.slice(4));
    if (!p.after_previous && syncedPaths.has(p.path)) { live.synced++; drawLive(); }
  }
  const from = base0 * tpf, to = (base0 + span + 1) * tpf;
  bar(1, 'Removing the DVR sound');
  const dvrFiles = new Set(live.files.filter((f) => f.r === 'dvr').map((f) => f.p));
  const keep = new Set(micPaths());
  const dvrNames = [...new Set([...dvrFiles].filter((p_) => !keep.has(p_)).map(base))];
  if (dvrNames.length) await evalES(`fpv_dropAudio(${JSON.stringify('^(' + dvrNames.map(reEsc).join('|') + ')$')}, ${from}, ${to})`);
  stage(0.95, 1);
  // PiP layout (when ticked) on the clips just placed: every DVR, and the stickcam clips that were lined up
  if (pipSet.on.build) {
    const stickFiles = new Set(res.report.filter((r) => r.stick && r.how !== 'no match').map((r) => r.stick_path));
    const now = await readSequence(true);
    const placed = now.clips.filter((c) => c.kind === 'video' && c.path && c.start >= base0 && c.start < base0 + span + 1);
    await corner(now, placed.filter((c) => dvrFiles.has(c.path)).map((c) => [c, 'dvr'])
      .concat(placed.filter((c) => stickFiles.has(c.path)).map((c) => [c, 'stick'])));
  }
  const rows = res.report.map((r) => {
    if (r.stick) {
      return r.how === 'no match'
        ? { dvr: r.stick, path: r.stick_path, status: r.unread ? 'not analysed' : 'no match', detail: 'stickcam, placed at the end' }
        : { dvr: r.stick, path: r.stick_path, status: 'matched', detail: `stickcam · ${r.action} · lined up by sound` };
    }
    return r.how === 'no match'
      ? { dvr: r.dvr || r.action, path: r.path, status: r.unread ? 'not analysed' : 'no match',
          detail: `${r.kind}, ` + (r.unread ? 'stopped before it was read; placed in recording order' : 'placed in recording order') }
      : { dvr: r.dvr, path: r.dvr_path, status: (r.how === 'order' || r.how === 'motion only' || /no ARMED/.test(r.how)) ? 'check' : 'matched',
          detail: HOW[r.how] || r.how };
  });
  render(rows);
  const flights = res.report.filter((r) => r.how !== 'no match' && !r.stick);
  const sticks = res.report.filter((r) => r.how !== 'no match' && r.stick);
  const pl = (k, w) => `${k} ${w}${k === 1 ? '' : 's'}`;
  const unread = res.report.filter((r) => r.unread).length;
  const skipped = res.report.filter((r) => r.skipped).length;
  const lone = res.report.filter((r) => r.how === 'no match').length - unread - skipped;
  status((res.stopped ? 'Stopped early: ' : '') + `${pl(flights.length, 'flight')} matched and synced` +
    (sticks.length ? `, ${pl(sticks.length, 'stickcam clip')} lined up` : '') +
    (lone ? `, ${pl(lone, 'clip')} without a match left in recording order` : '') +
    (skipped ? `, ${pl(skipped, 'clip')} skipped (left in recording order)` : '') +
    (unread ? `, ${pl(unread, 'clip')} not analysed (placed in recording order)` : '') + '.' + note, 'moved');
}

selectAction('build');
