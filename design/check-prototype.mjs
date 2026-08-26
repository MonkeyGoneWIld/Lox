/* Does the prototype's logic actually run, and do the clicks lead anywhere?
 *
 * The artboard renders inside a sandboxed iframe with an opaque origin, so
 * nothing outside it can click its buttons. The logic class is plain JS
 * though, so it can be lifted out and driven here: every handler renderVals()
 * hands the template is called, and the state it produces is checked.
 *
 * This says the flow works. It cannot say the flow looks right.
 */

import fs from 'node:fs';
import assert from 'node:assert';

const src = fs.readFileSync(new URL('./Main.dc.html', import.meta.url), 'utf8');
const body = src.slice(
  src.indexOf('class Component extends DCLogic'),
  src.lastIndexOf('</script>'),
);

class DCLogic {
  constructor(props) { this.props = props || {}; }
  setState(patch) { this.state = { ...this.state, ...patch }; }
}

const Component = new Function('DCLogic', `${body}\nreturn Component;`)(DCLogic);

const results = [];
const check = (name, ok, detail = '') => {
  results.push([name, ok, detail]);
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
};

const c = new Component({});
const v = () => c.renderVals();

// --- it renders at all ------------------------------------------------
let r = v();
check('renderVals returns without throwing', !!r, '');
check('the rail is built', r.findNav.length === 4 && r.pipeNav.length === 3,
  `${r.findNav.length} + ${r.pipeNav.length}`);
check('it opens on the run that needs you', r.isUploading === true, r.title);
check('and the rail says so from anywhere',
  r.pipeNav[2].fg === '#e8a33d' || r.pipeNav[2].bg === '#e8a33d', r.pipeNav[2].fg);

// --- the upload flow advances -----------------------------------------
check('step 1 is the group question', r.atGroup === true, r.stepLabel);
v().pickGroup();
r = v();
check('answering it moves to the lossy check', r.atLossy === true, r.stepLabel);
check('and the strip marks the first step done', r.steps[0].bg === '#7fb069', r.steps[0].bg);

v().notLossy();
r = v();
check('then the metadata form', r.atMeta === true, r.stepLabel);
v().saveMeta();
r = v();
check('then the request question', r.atRequest === true, r.stepLabel);
check('with two fillable requests offered', r.fillables.length === 2, '');

v().fillables[0].pick();
r = v();
check('picking one finishes the run', r.atDone === true, r.stepLabel);
check('the run stops saying it needs you', r.stateTag === 'FINISHED', r.stateTag);
check('and the payload names the request it filled',
  r.payload.some((p) => p.k === 'requestid' && p.v === '8811'),
  JSON.stringify(r.payload.find((p) => p.k === 'requestid')));
check('and the group it chose',
  r.payload.some((p) => p.k === 'groupid' && p.v === '2087126'), '');
check('a dry run says nothing was posted',
  r.doneTitle.toLowerCase().includes('nothing was posted'), r.doneTitle);
check('and offers the files it left behind', r.leftovers.length === 2, '');

v().restart();
r = v();
check('start over returns to the first question', r.atGroup === true, r.stepLabel);

// --- the other branch through the flow --------------------------------
v().pickNewGroup();
v().isLossy();
v().saveMeta();
v().fillNone();
r = v();
check('the other route finishes too', r.atDone === true, r.stepLabel);
check('a new group is reported as one',
  r.payload.some((p) => p.k === 'groupid' && p.v === 'a new group'), '');
check('filling nothing says so', r.doneNote === 'No request was filled.', r.doneNote);

// --- dry run toggles ---------------------------------------------------
v().toggleDry();
r = v();
check('the dry-run chip toggles to live', r.dryLabel === 'LIVE', r.dryLabel);
check('and the result changes with it', r.doneTitle === 'Uploaded to RED', r.doneTitle);
v().toggleDry();

// --- the queue ---------------------------------------------------------
v().findNav[0].pick();
r = v();
check('the rail moves you to Search', r.isSearch === true, r.title);
r.recent[0].queue();
r = v();
check('and Add to queue lands on the Queue', r.isQueue === true, r.title);

const before = v().queue.length;
check('the queue starts with two ticked', v().queueAction === 'Download 2 selected', v().queueAction);
v().queue[2].toggle();
check('ticking a third counts it', v().queueAction === 'Download 3 selected', v().queueAction);
v().clearSel();
check('Clear unticks everything', v().queueAction === 'Download 0 selected', v().queueAction);

v().queue[0].toggle();
v().startDownload();
r = v();
check('downloading moves you to Downloading', r.isDownloading === true, r.title);
check('the row leaves the queue', v().queue.length === before - 1,
  `${before} -> ${v().queue.length}`);
check('and the rail counts follow',
  v().pipeNav[0].count === '6' && v().pipeNav[1].count === '3',
  `queue ${v().pipeNav[0].count}, downloading ${v().pipeNav[1].count}`);
check('every download has a width the bar can use',
  v().downloads.every((d) => /^\d+$/.test(d.pct)), '');

// --- requests ----------------------------------------------------------
v().pipeNav[0].pick();
v().findNav[3].pick();
r = v();
check('the rail reaches Requests', r.isRequests === true, r.title);
check('nothing is checked yet',
  r.requests.every((x) => x.result === 'not checked'), '');
check('and it says what checking will cost', r.reqCost.includes('105'), r.reqCost);

v().checkRequests();
r = v();
check('checking starts a bar', r.reqRunning === true && r.reqPct === '20', r.reqPct);
check('the first row gets a verdict', r.requests[0].result === '94% match', r.requests[0].result);
for (let i = 0; i < 3; i += 1) v().checkRequests();
r = v();
check('a filled request is never sent to Deezer',
  r.requests[3].result === 'already filled', r.requests[3].result);
check('and is marked filled in its own column',
  r.requests[3].filled === 'filled', r.requests[3].filled);
v().checkRequests();
r = v();
check('the bar finishes and stops', r.reqRunning === false && r.reqAction === 'Checked',
  r.reqAction);

// --- no style hole is ever left undefined ------------------------------
//
// A style is interpolated into a CSS string, so an undefined one renders the
// word "undefined" into the declaration and silently drops it.
const holes = [];
const walk = (val, path) => {
  if (val === undefined) holes.push(path);
  else if (Array.isArray(val)) val.forEach((x, i) => walk(x, `${path}[${i}]`));
  else if (val && typeof val === 'object' && typeof val !== 'function') {
    for (const k of Object.keys(val)) walk(val[k], `${path}.${k}`);
  }
};
for (const view of ['search', 'queue', 'downloading', 'uploading', 'requests', 'settings']) {
  c.setState({ view });
  for (let step = 0; step < 5; step += 1) {
    c.setState({ step });
    const vals = c.renderVals();
    for (const k of Object.keys(vals)) walk(vals[k], `${view}/${step}:${k}`);
  }
}
check('no value handed to the template is undefined', holes.length === 0,
  holes.slice(0, 4).join(', '));

const failed = results.filter(([, ok]) => !ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (failed.length) console.log('failed: ' + failed.map(([n]) => n).join(', '));
assert.strictEqual(failed.length, 0);
