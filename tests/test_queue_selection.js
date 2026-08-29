/* The queue remembers what you ticked.
 *
 * Every visit to the Queue reloads it, and the load re-ticked every row. So
 * going to Downloading to check on something and coming back threw away the
 * picking you had gone there to do, and put the whole queue back in its place
 * -- which is the one state where pressing "Download & upload selected" does
 * the most work you did not ask for.
 *
 * Runs the real seedFoundSelection() out of the shipped app.js, because the
 * rule has three cases that a string match cannot tell apart: the first load,
 * a later load, and a later load where the answer is "nothing".
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

// --- lift the function out of the shipped file -----------------------------
//
// app.js is one IIFE with no exports, on purpose: it ships inside a Python
// package and must stay editable without a Node toolchain. So the piece under
// test is cut out by name rather than imported.
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

// eslint-disable-next-line no-new-func
const { seedFoundSelection, state } = new Function(`
  const state = { selectedFound: new Set(), foundSeeded: false };
  ${extract('function seedFoundSelection(found) {')}
  return { seedFoundSelection, state };
`)();

const rows = (...ids) => ids.map((id) => ({ id }));
const ticked = () => [...state.selectedFound].sort();

// --- the first load ticks everything, which is what the page is for --------
seedFoundSelection(rows('a', 'b', 'c'));
check('the first load ticks the whole queue',
      ticked().join(',') === 'a,b,c', ticked().join(','));

// --- and a later one keeps what you left -----------------------------------
state.selectedFound.delete('b');
seedFoundSelection(rows('a', 'b', 'c'));
check('coming back keeps what was ticked',
      ticked().join(',') === 'a,c', ticked().join(','));
check('and does not re-tick what was unticked',
      !state.selectedFound.has('b'), '');

// --- rows that have gone go with it ----------------------------------------
seedFoundSelection(rows('a'));
check('a row that has left the queue leaves the selection',
      ticked().join(',') === 'a', ticked().join(','));

// --- and rows that arrived while you were away stay untouched --------------
// "Download & upload selected" must never act on a release that turned up on
// another tab and that nobody has looked at.
seedFoundSelection(rows('a', 'd'));
check('a row that arrived while you were away is not ticked for you',
      ticked().join(',') === 'a', ticked().join(','));

// --- unticking everything is a selection too -------------------------------
// The case a naive "an empty set means nothing has loaded yet" gets wrong: it
// would helpfully tick the whole queue again, which is the original bug with
// an extra step.
state.selectedFound.clear();
seedFoundSelection(rows('a', 'd'));
check('unticking everything survives a reload',
      ticked().length === 0, ticked().join(','));

// --- a fresh page starts over ----------------------------------------------
state.foundSeeded = false;
state.selectedFound = new Set();
seedFoundSelection(rows('x', 'y'));
check('and a fresh page ticks the whole queue again',
      ticked().join(',') === 'x,y', ticked().join(','));

const failed = results.filter(([, ok]) => !ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (failed.length) {
  console.log('failed: ' + failed.map(([n]) => n).join(', '));
  process.exit(1);
}
