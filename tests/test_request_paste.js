/* A pasted request URL says which tracker it is on, and that is not optional.
 *
 * A request id is only unique within a tracker. Request 80755 exists on RED
 * and on OPS and is a different release on each. The paste box threw the URL
 * away and kept the digits, then checked whichever tracker the toggle above it
 * happened to be set to -- so pasting an orpheus.network link with RED selected
 * looked up RED's 80755, reported back about that one, and there was nothing on
 * screen to say it had answered a different question from the one asked.
 *
 * This runs the real parser out of the shipped app.js, because a string match
 * on the source can tell you a regex exists and not what it matches.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');

const APP = path.join(__dirname, '..', 'lox', 'web', 'static', 'scripts', 'app.js');
const source = fs.readFileSync(APP, 'utf8');

const results = [];
function check(name, ok, detail = '') {
  results.push([name, ok, detail]);
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
}

/** Pull one brace-delimited declaration out of app.js by its opening line. */
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

/** The TRACKER_HOSTS table, which is an array literal rather than a block. */
function extractArray(signature) {
  const start = source.indexOf(signature);
  assert.notStrictEqual(start, -1, `${signature} is not in app.js`);
  const end = source.indexOf('];', start);
  assert.notStrictEqual(end, -1, `${signature} is not closed`);
  return source.slice(start, end + 2);
}

const parts = [
  extractArray('const TRACKER_HOSTS = ['),
  extract('function trackerFromUrl(line) {'),
  extract('function idsFrom(text) {'),
];
// eslint-disable-next-line no-new-func
const { trackerFromUrl, idsFrom } = new Function(
  `${parts.join('\n')}\nreturn { trackerFromUrl, idsFrom };`,
)();

// --- the tracker comes out of the host --------------------------------------
const hosts = [
  ['https://orpheus.network/requests.php?action=view&id=80755', 'OPS'],
  ['https://redacted.sh/requests.php?action=view&id=80755', 'RED'],
  ['https://redacted.ch/requests.php?action=view&id=80755', 'RED'],
  ['https://dicmusic.com/requests.php?action=view&id=1', 'DIC'],
  ['https://www.orpheus.network/requests.php?action=view&id=7', 'OPS'],
  ['HTTPS://ORPHEUS.NETWORK/requests.php?action=view&id=7', 'OPS'],
];
hosts.forEach(([url, want]) => {
  check(`${url.slice(8, 34)} is ${want}`, trackerFromUrl(url) === want, String(trackerFromUrl(url)));
});
check('a bare id names no tracker', trackerFromUrl('80755') === null, String(trackerFromUrl('80755')));
check('and neither does someone else\'s site',
  trackerFromUrl('https://example.com/requests.php?action=view&id=5') === null, '');

// --- the id comes out of the id parameter, not the first digits -------------
const one = (text) => idsFrom(text)[0] || {};
check('the id parameter wins', one('https://orpheus.network/requests.php?action=view&id=80755').id === '80755', '');
check('even with digits earlier in the URL',
  one('https://redacted.ch/requests.php?action=view&id=42').id === '42',
  JSON.stringify(one('https://redacted.ch/requests.php?action=view&id=42')));
check('and the tracker rides along',
  one('https://redacted.ch/requests.php?action=view&id=42').tracker === 'RED', '');
check('a bare id still works', one('80755').id === '80755', '');
check('with no tracker, so it falls back to the selection',
  one('80755').tracker === null, String(one('80755').tracker));

// --- a whole paste ----------------------------------------------------------
const pasted = idsFrom([
  'https://orpheus.network/requests.php?action=view&id=80755',
  '',
  '   ',
  'https://redacted.sh/requests.php?action=view&id=12345',
  '99999',
  'not a request at all',
].join('\n'));
check('blank lines and junk are dropped', pasted.length === 3, String(pasted.length));
check('and each entry carries its own tracker',
  JSON.stringify(pasted.map((e) => e.tracker)) === '["OPS","RED",null]',
  JSON.stringify(pasted.map((e) => e.tracker)));

// --- which is what makes the grouping right ---------------------------------
// The page sends one call per tracker; a bare id belongs to the selection.
const grouped = new Map();
pasted.forEach(({ id, tracker }) => {
  const code = tracker || 'RED';
  if (!grouped.has(code)) grouped.set(code, []);
  grouped.get(code).push(id);
});
check('the OPS link is checked against OPS',
  JSON.stringify(grouped.get('OPS')) === '["80755"]', JSON.stringify(grouped.get('OPS')));
check('even though the selection said RED',
  JSON.stringify(grouped.get('RED')) === '["12345","99999"]', JSON.stringify(grouped.get('RED')));
check('and there is one call per tracker, not per request',
  grouped.size === 2, String(grouped.size));

const failed = results.filter(([, ok]) => !ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (failed.length) {
  console.log('failed: ' + failed.map(([n]) => n).join(', '));
  process.exit(1);
}
