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
const evalES = (code, quiet) => new Promise((res) => window.__adobe_cep__.evalScript(code, (out) => {
  if (!quiet) {                          // the background estimate checks aren't logged
    logLine('> ' + code.slice(0, 200));
    logLine('< ' + String(out).slice(0, 2000));
  }
  res(out);
}));
const status = (msg, cls) => { $('status').textContent = msg; $('status').className = TONE[cls] || cls || ''; };
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function runPython(job, onLine) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fpvsync-'));
  const jobFile = path.join(dir, 'job.json'), outFile = path.join(dir, 'out.json');
  fs.writeFileSync(jobFile, JSON.stringify(job));
  return new Promise((resolve, reject) => {
    const child = cp.spawn(cfg.python, ['-u', SCRIPT, '--job', jobFile, outFile], { windowsHide: true });
    let err = '', buf = '';
    child.stdout.on('data', (d) => {
      buf += d.toString();
      const lines = buf.split(/\r?\n/); buf = lines.pop();
      lines.forEach((l) => l && onLine && onLine(l));
    });
    child.stderr.on('data', (d) => { err += d.toString(); });
    child.on('error', (e) => reject(e));
    child.on('close', () => {
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
function render(rows) {
  $('results').hidden = !rows.length;
  $('rows').innerHTML = rows.map((r) => {
    const detail = r.detail !== undefined ? esc(r.detail)
      : (r.status === 'skipped' || r.status === 'blocked' || r.status === 'error')
        ? esc(r.why || '')
        : `ARMED ${r.armed_s}s · props ${esc(r.action)} ${r.props_s}s`;
    const move = r.move_frames !== undefined ? ` ${r.move_frames > 0 ? '+' : ''}${r.move_frames} fr` : '';
    const tone = TONE[r.status] || 'warn';
    return `<tr><td><span class="dot ${tone}"></span><b>${esc(r.dvr)}</b>
      <div class="sub">${detail}${r.note ? '<br>⚠ ' + esc(r.note) : ''}</div></td>
      <td class="st ${tone}">${esc(r.status)}${move}</td></tr>`;
  }).join('');
}

async function readSequence() {
  status('Reading sequence…');
  const raw = await evalES('fpv_dump()');
  let seq;
  try { seq = JSON.parse(raw); } catch (e) { throw new Error('Premiere returned: ' + raw); }
  if (seq.error) throw new Error(seq.error);
  return seq;
}

// ---- progress bar. The analysis sends its own time-left (from file sizes, the drive's measured
// speed, and how fast this run is actually going); the panel adds an estimate for its later steps
// (copying, placing, corner) and shows elapsed + time left. One cheap update per second.
const prog = { t0: 0, last: 0, lo: 0, hi: 1, eta: null, etaAt: 0, later: 0 };
const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
function stage(lo, hi, later = 0) { Object.assign(prog, { lo, hi, later, eta: null }); }
function setEta(sec) { prog.eta = sec; prog.etaAt = Date.now(); }
function bar(frac, label) {
  const el = (Date.now() - prog.t0) / 1000;
  let left = null;
  if (prog.eta !== null) left = Math.max(0, prog.eta - (Date.now() - prog.etaAt) / 1000) + prog.later;
  let f = left !== null ? el / (el + left + 0.5)
    : frac === undefined ? prog.last : prog.lo + (prog.hi - prog.lo) * frac;
  f = Math.max(prog.last, Math.min(0.995, f));                // never goes back; 100 % only when done
  prog.last = f;
  $('fill').style.width = `${(f * 100).toFixed(1)}%`;
  $('pct').textContent = `${Math.round(f * 100)}%`;
  if (label !== undefined) $('plabel').textContent = label;
  $('ptime').textContent = ' · ' + fmt(el) + (left !== null && el > 3 ? ` · ~${fmt(Math.ceil(left))} left` : '');
}
function onPython(line) {                      // "@@ 0.4231 312 HD DJI_0011.MP4" (fraction, s left, label)
  const m = /^@@ ([\d.]+) (-?\d+) (.*)$/.exec(line);
  if (!m) return;
  if (Number(m[2]) >= 0) setEta(Number(m[2]));
  bar(Number(m[1]), m[3]);
}
// a loop of N similar steps (placing clips, moving, corner): time left from the average so far
function loopEta(k, n, t0, guess) {
  const per = k > 0 ? (Date.now() - t0) / 1000 / k : guess;
  setEta(per * (n - k));
}

async function busy(fn) {
  loadConfig();
  $('sync').disabled = $('place').disabled = $('build').disabled = true;
  $('rows').innerHTML = ''; $('results').hidden = true; $('log').textContent = ''; status('');
  Object.assign(prog, { t0: Date.now(), lo: 0, hi: 1, last: 0, eta: null, later: 0, running: true });
  $('progress').hidden = false;
  $('progress').className = '';
  bar(0, 'Starting…');
  const timer = setInterval(() => bar(), 1000);       // keeps the clock and countdown moving
  let ok = false;
  try { await fn(); ok = true; } catch (e) { status(e.message, 'error'); }
  finally {
    clearInterval(timer);
    prog.running = false;
    estSig = null;                        // what's cached changed: re-estimate the buttons
    $('sync').disabled = $('place').disabled = $('build').disabled = false;
    if (ok) {
      $('fill').style.width = '100%'; $('pct').textContent = '100%'; $('progress').className = 'done';
      $('plabel').textContent = `Done in ${fmt((Date.now() - prog.t0) / 1000)}`; $('ptime').textContent = '';
    }
    else { $('progress').className = 'failed'; $('plabel').textContent = 'Stopped'; }
  }
}

async function sync() {
  const seq = await readSequence();
  const fps = 254016000000 / seq.tpf;
  if (!seq.clips.some((c) => c.kind === 'video' && c.selected)) {
    return status('Select the flights to sync first (their DVR or HD clip).', 'blocked');
  }
  status('');
  const nSel = seq.clips.filter((c) => c.kind === 'video' && c.selected).length;
  stage(0, 0.85, nSel * 0.3);
  bar(0, 'Analysing the selected flights');
  const res = await runPython({ fps, clips: seq.clips, dvr: cfg.dvr, action: cfg.action,
    lead: cfg.lead, prior_window: cfg.prior_window, only_selected: true, osd_search: cfg.osd_search || 'auto' },
    onPython);
  $('log').textContent = (res.log || []).join('\n') + (res.trace ? '\n' + res.trace : '');
  if (res.error) throw new Error(res.error);
  const rows = res.rows;
  const todo = rows.filter((x) => x.status === 'move').sort((a, b) => a.order - b.order);
  stage(0.85, 1);
  const tMove = Date.now();
  for (const [k, r] of todo.entries()) {
    loopEta(k, todo.length, tMove, 0.3);
    bar(k / Math.max(1, todo.length), `Moving ${r.dvr}`);
    const out = await evalES(`fpv_move(${JSON.stringify(r.movers.join('|'))}, ${r.move_frames})`);
    if (out === 'ok') r.status = 'moved';
    else { r.status = 'error'; r.why = out; }
  }
  render(rows);
  const n = (s) => rows.filter((r) => r.status === s).length;
  if (!rows.length) status('Select the flights to sync first (their DVR or HD clip).', 'blocked');
  else status(`${n('moved')} moved, ${n('in sync')} already in sync, ` +
    `${n('skipped') + n('blocked') + n('error')} need a look.`, n('moved') ? 'moved' : '');
}

async function place(only) {
  const seq = await readSequence();
  const pip = Object.assign({ scale: 76, crop_top: 0.6, crop_right: 0, dvr_size_fallback: [1280, 960] }, cfg.pip || {});
  const re = new RegExp(cfg.dvr, 'i');
  let dvr = seq.clips.filter((c) => c.kind === 'video' && c.path && re.test(path.basename(c.path)));
  if (only) dvr = dvr.filter((c) => c.start >= only.from && c.start < only.to);   // just-placed clips
  else if (dvr.some((c) => c.selected)) dvr = dvr.filter((c) => c.selected);
  if (!dvr.length) return status('No DVR clips found in this sequence.', 'blocked');
  const sizes = {};
  const rows = [];
  const s = pip.scale / 100;
  const tCorner = Date.now();
  for (const [k, c] of dvr.entries()) {
    loopEta(k, dvr.length, tCorner, 0.6);
    bar(k / dvr.length, `Corner: ${path.basename(c.path)}`);
    if (!(c.path in sizes)) sizes[c.path] = (await probeSize(c.path)) || pip.dvr_size_fallback;
    const [w, h] = sizes[c.path];
    // flush with the bottom-right corner: clip centre half a scaled clip in from each edge
    const x = (seq.width - (s * w) / 2) / seq.width;
    const y = (seq.height - (s * h) / 2) / seq.height;
    logLine(`place ${c.path} size ${w}x${h} seq ${seq.width}x${seq.height} -> ${x}, ${y}`);
    const out = String(await evalES(`fpv_place(${JSON.stringify(c.id)}, ${pip.scale}, ${x}, ${y}, ` +
      `${pip.crop_top}, ${pip.crop_right})`));
    const ok = out.startsWith('ok');
    rows.push({ dvr: path.basename(c.path), status: ok ? 'placed' : 'error', detail: ok ? out.slice(4) : out });
  }
  render(rows);
  const bad = rows.filter((r) => r.status !== 'placed').length;
  status(`${rows.length - bad} placed in the corner` + (bad ? `, ${bad} need a look.` : '.'), bad ? 'error' : 'moved');
}

$('sync').addEventListener('click', () => busy(sync));
$('place').addEventListener('click', () => busy(() => place()));

async function build() {
  const seq = await readSequence();
  const fps = 254016000000 / seq.tpf;
  const tpf = seq.tpf;
  const picked = seq.clips.filter((c) => c.kind === 'video' && c.path && c.selected);
  if (!picked.length) {
    return status('Select the clips to match first: the HD and DVR footage (Ctrl+A for everything).', 'blocked');
  }
  const paths = [...new Set(picked.map((c) => c.path))];
  status('');
  const nDvr = paths.filter((p) => new RegExp(cfg.dvr, 'i').test(path.basename(p))).length;
  stage(0, 0.8, 2 + paths.length * 0.25 + nDvr * 0.6);         // + place every clip, corner
  bar(0, `Analysing ${paths.length} clips (new footage takes minutes, cached footage seconds)`);
  const res = await runPython({ mode: 'build', fps, paths, dvr: cfg.dvr, action: cfg.action,
    lead: cfg.lead, gap: cfg.gap || 30, osd_search: cfg.osd_search || 'auto' }, onPython);
  $('log').textContent = (res.log || []).join('\n') + (res.trace ? '\n' + res.trace : '');
  if (res.error) throw new Error(res.error);

  // replace the selection in place: the layout starts where the first selected clip started; if it
  // would run into clips that weren't selected, those get pushed later (they keep their order and
  // spacing). Only if a clip runs across that spot does the layout go after the end of the sequence.
  stage(0.8, 0.95, nDvr * 0.6);
  setEta(2 + res.placements.length * 0.25);
  bar(0, 'Taking the selected clips out');
  const span = Math.max(...res.placements.map((p) => p.est_start + p.length));
  const tk = String(await evalES('fpv_takeSelected()'));
  if (!tk.startsWith('ok')) throw new Error(tk);
  let base = Math.round(Number(tk.slice(4)) / tpf);
  let note = '';
  if (String(await evalES(`fpv_isFree(${base * tpf}, ${(base + span + 1) * tpf})`)) !== 'free') {
    const gapF = Math.round((cfg.gap || 30) * fps);
    bar(0, 'Making room: pushing the later clips right');
    const push = String(await evalES(`fpv_pushAfter(${base * tpf}, ${(base + span + gapF) * tpf})`));
    if (push.startsWith('ok')) {
      const secs = Math.round(Number(push.slice(4)) / tpf / fps);
      if (secs > 0) note = ` The clips after them were pushed ${secs} s later to make room.`;
    } else if (push.startsWith('blocked')) {
      const end = Math.ceil(Number(await evalES('fpv_seqEnd()')) / tpf);
      base = end + gapF;
      note = ` They went after the end of the sequence: ${push.slice(9)}, so the later clips couldn't be pushed.`;
    } else {
      throw new Error(push);
    }
  }
  // DVRs first, so the HD sound wins wherever the DVR sound landed on the same audio track
  const order = res.placements.filter((p) => p.track > 1).concat(res.placements.filter((p) => p.track === 1));
  const ends = {};                                  // per track: end of the piece just placed
  let n = 0;
  const tPlace = Date.now();
  for (const p of order) {
    loopEta(n, order.length, tPlace, 0.25);
    bar(n / order.length, `Placing ${++n}/${order.length}: ${path.basename(p.path)}`);
    const ticks = p.after_previous && ends[p.track] !== undefined ? ends[p.track] : (base + p.start) * tpf;
    const out = String(await evalES(`fpv_put(${JSON.stringify(p.path)}, ${p.track - 1}, ${ticks})`));
    if (!out.startsWith('ok')) throw new Error(`${path.basename(p.path)}: ${out}`);
    ends[p.track] = Number(out.slice(4));
  }
  const from = base * tpf, to = (base + span + 1) * tpf;
  bar(1, 'Removing the DVR sound');
  await evalES(`fpv_dropAudio(${JSON.stringify(cfg.dvr)}, ${from}, ${to})`);
  stage(0.95, 1, 0);
  await place({ from: base, to: base + span + 1 });
  const rows = res.report.map((r) => r.how === 'no match'
    ? { dvr: r.dvr || r.action, status: 'no match', detail: `${r.kind}, placed in recording order` }
    : { dvr: r.dvr, status: (r.how === 'order' || r.how === 'motion only' || /no ARMED/.test(r.how)) ? 'check' : 'matched',
        detail: `${r.action} · ` + ({
          motion: 'matched by motion', clock: 'matched by clock', order: 'paired by file order only',
          're-armed': 'armed more than once, used the arming that matches',
          'clock, re-armed': 'armed more than once, used the arming that matches',
          'clock, no prop sound': 'no prop sound heard, synced by motion',
          'clock, no ARMED (motion)': 'DVR never shows ARMED, synced by motion: check it',
          'motion only': 'no prop sound in the HD clip, lined up by motion: check it',
          'clock, same DVR': 'second flight in the same DVR recording',
          'clock (motion)': 'a sound before the props, synced by motion',
          'clock, re-armed (motion)': 'armed more than once, synced by motion',
        }[r.how] || r.how) });
  render(rows);
  const m = res.report.filter((r) => r.how !== 'no match');
  const pl = (k, w) => `${k} ${w}${k === 1 ? '' : 's'}`;
  const lone = res.report.length - m.length;
  status(`${pl(m.length, 'flight')} matched and synced` +
    (lone ? `, ${pl(lone, 'clip')} without a match left in recording order.` : '.') + note, 'moved');
}

$('build').addEventListener('click', () => busy(build));

// ---- time estimates on the buttons (clock + "cached"). Every 4 s, while idle and visible, the panel
// asks Premiere for a tiny fingerprint of the selection; only when it changed does it run a quick
// estimate (file sizes, lengths, what's cached, the drive's remembered speed; no footage is read).
const CLOCK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">' +
  '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';
let estSig = null, estBusy = false;
function showEta(id, sec, cached) {
  const el = $(id);
  if (sec === null || sec === undefined) { el.innerHTML = ''; return; }
  const t = sec < 60 ? `~${Math.max(1, Math.round(sec))} s` : `~${Math.round(sec / 60)} min`;
  el.innerHTML = CLOCK + t + (cached ? ' <span class="c">· cached</span>' : '');
}
async function refreshEstimates() {
  if (estBusy || prog.running || document.hidden) return;
  estBusy = true;
  try {
    const sig = String(await evalES('fpv_selSig()', true));
    if (sig === estSig) return;
    estSig = sig;
    if (!sig) { ['eta-build', 'eta-sync', 'eta-place'].forEach((i) => showEta(i, null)); return; }
    const seq = JSON.parse(await evalES('fpv_dump()', true));
    loadConfig();
    const res = await runPython({ mode: 'estimate', clips: seq.clips, dvr: cfg.dvr, action: cfg.action });
    if (prog.running) return;
    showEta('eta-build', res.match, res.match_cached);
    showEta('eta-sync', res.sync, res.sync_cached);
    showEta('eta-place', res.corner, false);
  } catch (e) {
    estSig = null;
  } finally {
    estBusy = false;
  }
}
setInterval(refreshEstimates, 4000);
refreshEstimates();
