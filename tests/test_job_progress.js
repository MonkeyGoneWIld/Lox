/* A running job shows a bar, and the bar goes forwards.
 *
 * Every long operation here reports the same shape -- phase, current, total --
 * and every one of them used to print it as a sentence: "working 3/25". That is
 * two numbers to read and divide, next to a Stop button, with no sense of how
 * much is left. Two of them (the album check, the Found re-check) reported
 * nothing at all beyond a spinner.
 *
 * This runs the real jobProgress() out of the shipped app.js against a DOM
 * small enough to write here, because a string match on the source can tell you
 * the function exists and not that the bar moves.
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

// --- the smallest DOM these functions touch --------------------------------
class Node {
  constructor(tag) {
    this.tagName = String(tag || '').toUpperCase();
    this.children = [];
    this.className = '';
    this.style = {};
    this.hidden = false;
    this._text = '';
  }

  get firstElementChild() {
    return this.children[0] || null;
  }

  get textContent() {
    return this.children.length ? this.children.map((c) => c.textContent).join('') : this._text;
  }

  set textContent(value) {
    this.children = [];
    this._text = String(value);
  }

  setAttribute() {}
  addEventListener() {}

  append(...kids) {
    for (const kid of kids) {
      this.children.push(kid instanceof Node ? kid : Object.assign(new Node('#text'), { _text: String(kid) }));
    }
  }

  replaceChildren(...kids) {
    this.children = [];
    this._text = '';
    this.append(...kids);
  }

  querySelector(sel) {
    const want = sel.replace('.', '');
    for (const kid of this.children) {
      if (String(kid.className).split(/\s+/).includes(want)) return kid;
      const deeper = kid.querySelector ? kid.querySelector(sel) : null;
      if (deeper) return deeper;
    }
    return null;
  }
}

global.document = { createElement: (tag) => new Node(tag) };

// --- lift the functions under test out of the shipped file -----------------
//
// app.js is one IIFE with no exports, on purpose: it ships inside a Python
// package and must stay editable without a Node toolchain. So the pieces are
// cut out by name rather than imported.
const source = fs.readFileSync(APP, 'utf8');

function extract(signature) {
  const start = source.indexOf(signature);
  assert.notStrictEqual(start, -1, `${signature} is not in app.js`);
  // Walk from the brace that opens the body, which is the last character of
  // the signature. Starting at the first '{' after `start` finds the one in
  // `attrs = {}` and closes the function before it has begun.
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

const parts = [
  extract('function el(tag, attrs = {}, ...children) {'),
  extract('function jobLine(job) {'),
  extract('function jobProgress(box, job, extra = \'\') {'),
  extract('function jobFinished(box, text) {'),
];
// eslint-disable-next-line no-new-func
const { el, jobProgress, jobFinished } = new Function(
  `${parts.join('\n')}\nreturn { el, jobLine, jobProgress, jobFinished };`,
)();

// --- what it has to do ------------------------------------------------------
const box = new Node('div');
box.hidden = true;

jobProgress(box, { status: 'running', progress: { phase: 'checking', current: 3, total: 25 } });
const bar = box.querySelector('.joblog-bar');
check('a running job draws a bar', bar !== null, String(bar && bar.className));
check('the box is no longer hidden', box.hidden === false, String(box.hidden));
check('the bar is filled to the fraction done',
  bar.firstElementChild.style.width === '12%', String(bar.firstElementChild.style.width));
check('and the numbers are still written out',
  box.textContent.includes('checking 3/25'), JSON.stringify(box.textContent));

const first = bar;
jobProgress(box, { status: 'running', progress: { phase: 'checking', current: 20, total: 25 } });
check('the bar advances', bar.firstElementChild.style.width === '80%',
  String(bar.firstElementChild.style.width));
check('and is the same element, not a new one',
  box.querySelector('.joblog-bar') === first,
  'rebuilding it every poll restarts the CSS transition and makes it twitch');

// A job that cannot say how far along it is must not draw an empty bar.
jobProgress(box, { status: 'running', progress: {} });
check('a job with no total hides the bar rather than showing zero',
  box.querySelector('.joblog-bar').hidden === true,
  String(box.querySelector('.joblog-bar').hidden));

// Extra lines ride under the same bar.
jobProgress(box, { status: 'running', progress: { current: 1, total: 2 } }, 'source: 40 albums');
check('extra detail is kept under the bar',
  box.textContent.includes('source: 40 albums'), JSON.stringify(box.textContent));

// Over-reporting must not overflow the track.
jobProgress(box, { status: 'running', progress: { current: 30, total: 25 } });
check('a job past its own total stops at full',
  box.querySelector('.joblog-bar').firstElementChild.style.width === '100%',
  String(box.querySelector('.joblog-bar').firstElementChild.style.width));

jobFinished(box, 'Done. 25 request(s) checked.');
check('finishing drops the bar', box.querySelector('.joblog-bar') === null, '');
check('and leaves the sentence', box.textContent === 'Done. 25 request(s) checked.',
  JSON.stringify(box.textContent));

// --- every job display uses it ---------------------------------------------
//
// The point of the shared helper is that no long operation is left printing a
// bare line, which is what four of the five were doing.
for (const site of [
  'jobProgress(log, job)',            // requests check, Found re-check
  'jobProgress(log, job,',            // scan collect, with its source lines
  'onUpdate: (job) => jobProgress(',  // Found re-check
]) {
  check(`a job display uses the bar: ${site}`, source.includes(site), '');
}
check('no job display sets its progress line by hand any more',
  !/log\.textContent = jobLine\(/.test(source), '');

const failed = results.filter(([, ok]) => !ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (failed.length) console.log('failed: ' + failed.map(([n]) => n).join(', '));
process.exit(failed.length ? 1 : 0);
