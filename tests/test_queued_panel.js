/* Requests that got queued are listed while the check is still running.
 *
 * Asked for repeatedly and shipped broken twice, both times because the wiring
 * was checked by reading it rather than by running it.
 *
 * The first attempt re-ordered the results table, which leaves the three
 * matches that matter among the two hundred that do not. The second built a
 * table of its own but drew it from one caller -- the path a pasted list of
 * ids takes -- while a search checks each page as it lands through a different
 * one, so the run that most needs the table was the only run that never got
 * it. A string match on the source could see the function and the panel and
 * tell you neither of those things.
 *
 * So this runs the real applyRequestResult() and renderQueued() out of the
 * shipped app.js: a fillable result goes in, and the panel has to come out
 * visible with that release in it.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');

const APP = path.join(__dirname, '..', 'lox', 'web', 'static', 'scripts', 'app.js');

const results = [];
function check(name, ok, detail = '') {
  results.push([name, ok, detail]);
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
}

// --- the smallest DOM these two functions touch ----------------------------
class Node {
  constructor(tag) {
    this.tagName = String(tag || '').toUpperCase();
    this.children = [];
    this.className = '';
    this.hidden = false;
    this._text = '';
  }

  get textContent() {
    return this.children.length ? this.children.map((c) => c.textContent).join('') : this._text;
  }

  setAttribute() {}
  addEventListener() {}

  append(...kids) { this.children.push(...kids); }

  replaceChildren(...kids) { this.children = kids; }

  querySelector() { return null; }
}

global.document = { createElement: (tag) => new Node(tag) };
global.CSS = { escape: (v) => String(v) };

// --- lift the two functions out of the shipped file ------------------------
//
// app.js is one IIFE with no exports, on purpose: it ships inside a Python
// package and must stay editable without a Node toolchain. So the pieces are
// cut out by name rather than imported.
const source = fs.readFileSync(APP, 'utf8');

function extract(signature) {
  const start = source.indexOf(signature);
  assert.notStrictEqual(start, -1, `${signature} is not in app.js`);
  let depth = 0;
  for (let i = start + signature.length - 1; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`${signature} is not closed`);
}

const panel = new Node('div');
panel.hidden = true;
const host = new Node('div');

// What dataTable was handed, which is what the table would have drawn.
let drawn = null;

const scope = `
  const state = { requestResults: new Map(), requestMatches: new Map(),
                  requestRows: [], requestsTracker: 'OPS' };
  const resultKey = (tracker, id) => \`\${tracker || state.requestsTracker || ''}:\${id}\`;
  const $ = (sel) => (sel === '#requests-queued-panel' ? PANEL
                     : sel === '#requests-queued' ? HOST : null);
  const el = (tag) => document.createElement(tag);
  const dataTable = (spec) => { DRAW(spec); return document.createElement('div'); };
  const fillPastedRequestRow = () => {};
  const requestResultCell = () => null;
  const albumHref = (id) => '/album/' + id;
  const goAlbum = () => {};
  const trackerSummary = () => '';
  const trackerParts = () => [];
  const trackerTags = () => [];
  const rejectMatch = () => {};
  const dismissRows = () => {};
  const queueRowFor = (m) => m;
  ${extract('function renderQueued() {')}
  ${extract('function applyRequestResult(match) {')}
  return { applyRequestResult, renderQueued, state };
`;

// eslint-disable-next-line no-new-func
const { applyRequestResult, state } = new Function('PANEL', 'HOST', 'DRAW', scope)(
  panel, host, (spec) => { drawn = spec; },
);

const match = (over) => ({
  request_id: 80162,
  tracker: 'OPS',
  fillable: true,
  confidence: 0.97,
  deezer_id: '930724701',
  deezer_artist: 'Vestjysk Orken',
  deezer_title: 'LSDREI',
  request_url: 'https://orpheus.network/requests.php?action=view&id=80162',
  ...over,
});

// --- nothing queued, nothing shown -----------------------------------------
check('the panel is hidden before anything is matched', panel.hidden === true, '');

// --- a result that cannot fill anything leaves it hidden -------------------
applyRequestResult(match({ request_id: 1, fillable: false }));
check('a request that cannot be filled does not open it',
      panel.hidden === true && drawn === null, String(panel.hidden));

// --- and one that can, opens it, during the run ----------------------------
applyRequestResult(match());
check('the first match opens the panel', panel.hidden === false, String(panel.hidden));
check('without waiting for the run to finish', drawn !== null, '');
check('with the release that was matched',
      drawn && drawn.rows.length === 1 && drawn.rows[0].deezer_title === 'LSDREI',
      drawn ? JSON.stringify(drawn.rows.map((r) => r.deezer_title)) : 'nothing drawn');
check('and the request it fills',
      drawn && String(drawn.rows[0].id) === '80162', drawn ? String(drawn.rows[0].id) : '');

// --- every match, however the check was started ----------------------------
// The point of hanging this off applyRequestResult: a pasted list and a
// page-by-page search go through different callers and both land here.
applyRequestResult(match({ request_id: 90001, tracker: 'RED', confidence: 0.8,
                           deezer_title: 'Another One' }));
// Read through a helper rather than dereferenced: when the wiring is broken
// nothing is ever drawn, and a test that throws on the first null stops
// reporting the rest of what is wrong.
const rows = () => (drawn && drawn.rows) || [];
check('a second match joins it', rows().length === 2, String(rows().length));
check('regardless of which tracker it was found on',
      rows().some((r) => r.tracker === 'RED'), '');
check('best match first, because that is the order worth reading in',
      rows()[0] && rows()[0].deezer_title === 'LSDREI', rows()[0] ? rows()[0].deezer_title : '');

// --- a request keyed by tracker, not by id alone ---------------------------
// Request 80162 exists on both trackers and is a different release on each.
applyRequestResult(match({ tracker: 'RED', deezer_title: 'Same Number Elsewhere' }));
check('the same request number on two trackers is two rows',
      rows().length === 3, String(rows().length));

// --- and the results are what drives it ------------------------------------
check('the table is built from the results, not from the rows on screen',
      state.requestRows.length === 0 && rows().length === 3,
      `rows on screen: ${state.requestRows.length}`);

const failed = results.filter(([, ok]) => !ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (failed.length) {
  console.log('failed: ' + failed.map(([n]) => n).join(', '));
  process.exit(1);
}
