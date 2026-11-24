// ExtendScript side of the FPV Sync panel (runs inside Premiere Pro).
var FPV_TICKS = 254016000000;

function fpv_q(s) {
    s = String(s);
    return '"' + s.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/[\r\n\t]/g, ' ') + '"';
}

function fpv_each(seq, fn) {
    var groups = [['video', seq.videoTracks], ['audio', seq.audioTracks]];
    for (var g = 0; g < groups.length; g++) {
        var kind = groups[g][0], tracks = groups[g][1];
        for (var t = 0; t < tracks.numTracks; t++) {
            var clips = tracks[t].clips;
            for (var c = 0; c < clips.numItems; c++) {
                if (fn(kind, t, clips[c]) === false) return;
            }
        }
    }
}

function fpv_find(seq, id) {
    var found = null;
    fpv_each(seq, function (k, t, it) {
        if (it.nodeId === id) { found = it; return false; }
    });
    return found;
}

function fpv_size(seq, vertical) {
    var v = vertical ? seq.frameSizeVertical : seq.frameSizeHorizontal;
    if (!v) {
        try { var s = seq.getSettings(); v = vertical ? s.videoFrameHeight : s.videoFrameWidth; } catch (e) {}
    }
    return Number(v) || (vertical ? 2160 : 3840);
}

// Describe every clip in the active sequence as JSON (no JSON object in ExtendScript).
function fpv_dump() {
    var seq = app.project.activeSequence;
    if (!seq) return '{"error":"Open the sequence you want to sync first."}';
    var tpf = Number(seq.timebase);
    var parts = [];
    fpv_each(seq, function (kind, t, it) {
        var path = '';
        try { path = it.projectItem.getMediaPath(); } catch (e) {}
        var linked = [];
        try {
            var li = it.getLinkedItems();
            if (li) for (var k = 0; k < li.numItems; k++) linked.push(fpv_q(li[k].nodeId));
        } catch (e) {}
        var sel = false;
        try { sel = it.isSelected(); } catch (e) {}
        parts.push('{"id":' + fpv_q(it.nodeId) + ',"kind":"' + kind + '","track":' + t +
            ',"name":' + fpv_q(it.name) + ',"path":' + fpv_q(path) +
            ',"start":' + Math.round(Number(it.start.ticks) / tpf) +
            ',"end":' + Math.round(Number(it.end.ticks) / tpf) +
            ',"src0":' + (Number(it.inPoint.ticks) / FPV_TICKS) +
            ',"selected":' + (sel ? 'true' : 'false') +
            ',"linked":[' + linked.join(',') + ']}');
    });
    return '{"tpf":' + tpf + ',"seq":' + fpv_q(seq.name) +
        ',"width":' + fpv_size(seq, 0) + ',"height":' + fpv_size(seq, 1) +
        ',"clips":[' + parts.join(',') + ']}';
}

// A cheap fingerprint of the open sequence and what's selected in it, so the panel only
// re-estimates the button times when something changed.
function fpv_selSig() {
    var seq = app.project.activeSequence;
    if (!seq) return '';
    var ids = [];
    fpv_each(seq, function (kind, t, it) {
        try { if (it.isSelected()) ids.push(it.nodeId); } catch (e) {}
    });
    return seq.sequenceID + ':' + ids.length + ':' + ids.join(',');
}

// Find an effect on a clip: by match name first, then by (possibly localised) display name.
function fpv_comp(it, matchName, nameRe) {
    var comps = it.components, i;
    for (i = 0; i < comps.numItems; i++) {
        if (comps[i].matchName === matchName) return comps[i];
    }
    for (i = 0; i < comps.numItems; i++) {
        if (nameRe && nameRe.test(comps[i].displayName)) return comps[i];
    }
    return null;
}

function fpv_listComps(it) {
    var out = [], comps = it.components;
    for (var i = 0; i < comps.numItems; i++) out.push(comps[i].displayName + '=' + comps[i].matchName);
    return out.join(', ');
}

// Add the Crop effect through the QE DOM (the regular API can't add effects). Returns a status text.
function fpv_addCrop(seq, it) {
    try {
        app.enableQE();
        var eff = null, tried = [], names = ['Crop', 'Recadrage'];
        try {
            var list = qe.project.getVideoEffectList();
            for (var l = 0; l < list.length; l++) {
                if (/^(crop|recadrage|recadrer|zuschneiden|recortar|ritaglia)$/i.test(list[l])) names.unshift(list[l]);
            }
        } catch (e) { tried.push('no effect list'); }
        for (var n = 0; n < names.length && !eff; n++) {
            try { eff = qe.project.getVideoEffectByName(names[n]); } catch (e) { eff = null; }
            tried.push(names[n] + (eff ? ' found' : ' missing'));
        }
        if (!eff) return 'crop effect not found (' + tried.join(', ') + ')';
        var vt = seq.videoTracks;
        for (var t = 0; t < vt.numTracks; t++) {
            var clips = vt[t].clips;
            for (var c = 0; c < clips.numItems; c++) {
                if (clips[c].nodeId !== it.nodeId) continue;
                // the c-th real clip on the QE track (QE also lists the gaps as 'Empty')
                var qt = qe.project.getActiveSequence().getVideoTrackAt(t), seen = -1;
                for (var k = 0; k < qt.numItems; k++) {
                    var q = qt.getItemAt(k);
                    if (!q || q.type === 'Empty') continue;
                    seen++;
                    if (seen === c) {
                        q.addVideoEffect(eff);
                        return 'added';
                    }
                }
                return 'QE clip not found on track ' + (t + 1);
            }
        }
        return 'clip not found for QE';
    } catch (e) {
        return 'QE error: ' + e;
    }
}

// Scale + position a clip (position normalised 0..1 of the sequence frame) and crop it.
function fpv_place(id, scale, x, y, cropTop, cropRight) {
    var seq = app.project.activeSequence;
    if (!seq) return 'error: no active sequence';
    var it = fpv_find(seq, id);
    if (!it) return 'error: clip not found';
    var steps = [];
    try {
        var m = fpv_comp(it, 'AE.ADBE Motion', /^(motion|trajectoire)$/i);
        if (!m) return 'error: no Motion effect (' + fpv_listComps(it) + ')';
        m.properties[0].setValue([x, y], true);     // Position
        m.properties[1].setValue(scale, true);      // Scale
        var pos = m.properties[0].getValue(), sc = m.properties[1].getValue();
        steps.push('position ' + pos + ' scale ' + sc);
    } catch (e) {
        return 'error: motion: ' + e + ' (' + fpv_listComps(it) + ')';
    }
    if (cropTop > 0 || cropRight > 0) {
        var cropRe = /^(crop|recadrage)$/i;
        var c = fpv_comp(it, 'AE.ADBE AECrop', cropRe);
        if (!c) {
            var r = fpv_addCrop(seq, it);
            steps.push('crop ' + r);
            it = fpv_find(seq, id);
            c = fpv_comp(it, 'AE.ADBE AECrop', cropRe);
        }
        if (!c) return 'placed; crop failed: ' + steps.join('; ') + ' [' + fpv_listComps(it) + ']';
        try {
            c.properties[1].setValue(cropTop, true);    // Top
            c.properties[2].setValue(cropRight, true);  // Right
            steps.push('crop top ' + c.properties[1].getValue() + ' right ' + c.properties[2].getValue());
        } catch (e) {
            return 'placed; crop values failed: ' + e;
        }
    }
    return 'ok: ' + steps.join('; ');
}

// Shift the given track items (ids joined by "|") by a whole number of sequence frames.
// Linked items that Premiere already moved along with the first one are not moved twice.
function fpv_move(idList, frames) {
    var seq = app.project.activeSequence;
    if (!seq) return 'error: no active sequence';
    var tpf = Number(seq.timebase);
    var ids = idList.split('|'), before = {}, i, it;
    for (i = 0; i < ids.length; i++) {
        it = fpv_find(seq, ids[i]);
        if (!it) return 'error: clip not found (' + ids[i] + ')';
        before[ids[i]] = Number(it.start.ticks);
    }
    var shift = new Time();
    shift.ticks = String(frames * tpf);
    for (i = 0; i < ids.length; i++) {
        it = fpv_find(seq, ids[i]);
        if (Math.round((Number(it.start.ticks) - before[ids[i]]) / tpf) === frames) continue;
        it.move(shift);
    }
    var bad = [];
    for (i = 0; i < ids.length; i++) {
        it = fpv_find(seq, ids[i]);
        var d = Math.round((Number(it.start.ticks) - before[ids[i]]) / tpf);
        if (d !== frames) bad.push(it.name + ' moved ' + d);
    }
    return bad.length ? 'error: ' + bad.join('; ') : 'ok';
}

// ------------------------------------------------------- Match & sync selected
var fpvItemsByPath = {};

// Take the selected clips (and whatever is linked to them, e.g. their sound) out of the sequence,
// remembering each file's project item so it can be placed again. Returns 'ok: <first start ticks>'.
function fpv_takeSelected() {
    var seq = app.project.activeSequence;
    if (!seq) return 'error: open the sequence first';
    fpvItemsByPath = {};
    var ids = {}, first = null, n = 0;
    fpv_each(seq, function (kind, t, it) {
        var sel = false;
        try { sel = it.isSelected(); } catch (e) {}
        if (!sel) return;
        ids[it.nodeId] = true;
        n++;
        try { if (it.projectItem) fpvItemsByPath[it.projectItem.getMediaPath()] = it.projectItem; } catch (e) {}
        if (kind === 'video') {
            var st = Number(it.start.ticks);
            if (first === null || st < first) first = st;
        }
        try {
            var li = it.getLinkedItems();
            if (li) for (var k = 0; k < li.numItems; k++) ids[li[k].nodeId] = true;
        } catch (e) {}
    });
    if (!n || first === null) return 'error: nothing selected';
    for (var id in ids) {
        var it = fpv_find(seq, id);
        if (it) it.remove(false, false);
    }
    return 'ok: ' + first;
}

// Is the stretch [fromTicks, toTicks) empty on every track? ('free' / 'busy')
function fpv_isFree(fromTicks, toTicks) {
    var seq = app.project.activeSequence, busy = false;
    fpv_each(seq, function (kind, t, it) {
        if (Number(it.start.ticks) < toTicks && Number(it.end.ticks) > fromTicks) { busy = true; return false; }
    });
    return busy ? 'busy' : 'free';
}

// End of the last clip in the sequence (ticks).
// Make room from fromTicks to toTicks: push every clip that starts at or after fromTicks (all tracks)
// right, just enough that the first of them starts at toTicks. Clips keep their order and spacing.
// Refuses ('blocked: ...') if a clip starts before fromTicks but runs past it (it would be cut).
function fpv_pushAfter(fromTicks, toTicks) {
    var seq = app.project.activeSequence;
    if (!seq) return 'error: no active sequence';
    var later = [], first = null, straddle = null;
    fpv_each(seq, function (kind, t, it) {
        var st = Number(it.start.ticks), en = Number(it.end.ticks);
        if (st >= fromTicks) {
            later.push({ id: it.nodeId, start: st });
            if (first === null || st < first) first = st;
        } else if (en > fromTicks && straddle === null) {
            straddle = it.name;
        }
    });
    if (straddle !== null) return 'blocked: ' + straddle + ' runs across that spot';
    if (first === null || first >= toTicks) return 'ok: 0';
    var shiftTicks = toTicks - first;
    var shift = new Time();
    shift.ticks = String(shiftTicks);
    later.sort(function (a, b) { return b.start - a.start; });     // latest first: nothing slides over a neighbour
    var i, it;
    for (i = 0; i < later.length; i++) {
        it = fpv_find(seq, later[i].id);
        if (!it) continue;
        if (Number(it.start.ticks) - later[i].start === shiftTicks) continue;  // moved along with its linked clip
        it.move(shift);
    }
    var bad = [];
    for (i = 0; i < later.length; i++) {
        it = fpv_find(seq, later[i].id);
        if (it && Number(it.start.ticks) - later[i].start !== shiftTicks) bad.push(it.name);
    }
    return bad.length ? 'error: these did not move right: ' + bad.join(', ') : 'ok: ' + shiftTicks;
}

function fpv_seqEnd() {
    var seq = app.project.activeSequence, end = 0;
    fpv_each(seq, function (kind, t, it) { end = Math.max(end, Number(it.end.ticks)); });
    return String(end);
}

// Put one file on a video track at a tick position. Returns 'ok: <end ticks of the placed clip>'
// so the next piece of a split recording can go right after it.
function fpv_put(path, track, ticks) {
    var seq = app.project.activeSequence;
    var item = fpvItemsByPath[path];
    if (!item) return 'error: not in project: ' + path;
    if (track >= seq.videoTracks.numTracks) track = seq.videoTracks.numTracks - 1;
    var tr = seq.videoTracks[track];
    var tm = new Time();
    tm.ticks = String(ticks);
    tr.overwriteClip(item, tm);
    var end = ticks;
    for (var c = 0; c < tr.clips.numItems; c++) {
        if (Math.abs(Number(tr.clips[c].start.ticks) - ticks) < 1000) end = Number(tr.clips[c].end.ticks);
    }
    return 'ok: ' + end;
}

// Make sure the sequence has at least n video tracks (the stickcam goes above the DVR). The QE DOM's
// addTracks takes (video count, after video index, audio count, audio type, after audio index, ...);
// older builds take fewer arguments, so a few forms are tried and the result is checked.
function fpv_ensureTracks(n) {
    var seq = app.project.activeSequence;
    var have = seq.videoTracks.numTracks;
    if (have >= n) return 'ok: ' + have;
    app.enableQE();
    var q = qe.project.getActiveSequence();
    var tries = [
        function () { q.addTracks(n - have, have, 0, 1, seq.audioTracks.numTracks, 0, 0, 0); },
        function () { q.addTracks(n - have, have, 0); },
        function () { q.addTracks(n - have); }
    ];
    for (var i = 0; i < tries.length; i++) {
        try { tries[i](); } catch (e) {}
        if (app.project.activeSequence.videoTracks.numTracks >= n) return 'ok: ' + app.project.activeSequence.videoTracks.numTracks;
    }
    return 'error: could not add video tracks (have ' + app.project.activeSequence.videoTracks.numTracks + ', need ' + n + ')';
}

// Remove the sound of DVR files (name matches the pattern) within [fromTicks, toTicks).
function fpv_dropAudio(pattern, fromTicks, toTicks) {
    var seq = app.project.activeSequence, re = new RegExp(pattern, 'i'), n = 0;
    for (var t = 0; t < seq.audioTracks.numTracks; t++) {
        var clips = seq.audioTracks[t].clips;
        for (var c = clips.numItems - 1; c >= 0; c--) {
            if (Number(clips[c].start.ticks) >= toTicks || Number(clips[c].end.ticks) <= fromTicks) continue;
            var p = '';
            try { p = clips[c].projectItem.getMediaPath(); } catch (e) {}
            var name = p.substring(Math.max(p.lastIndexOf('\\'), p.lastIndexOf('/')) + 1);
            if (re.test(name)) { clips[c].remove(false, false); n++; }
        }
    }
    return 'ok: ' + n;
}
