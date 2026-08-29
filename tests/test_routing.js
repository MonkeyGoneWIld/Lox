/* Every screen, tab, list and page has an address, and the address is what
 * decides what you are looking at.
 *
 * The app used to change the screen and, where somebody had got round to it,
 * mention it to history afterwards. The places nobody had got round to were
 * the ones people use: a search, a Browse tab, a genre, a channel, a request,
 * the excluded rows in the queue and a place on the settings page all carried
 * the address of whichever screen you had arrived from. Pressing Back skipped
 * every one of them at once, a reload threw them away, and none of them could
 * be bookmarked or sent to anybody.
 *
 * This runs the real router out of the shipped app.js against a stand-in for
 * location and history, because a string match on the source can tell you a
 * route exists and not where it goes.
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

/** One single-line declaration, by its opening words. */
function line(signature) {
  const start = source.indexOf(signature);
  assert.notStrictEqual(start, -1, `${signature} is not in app.js`);
  const end = source.indexOf('\n', start);
  return source.slice(start, end);
}

const parts = [
  extract('const VIEW_PATHS = {'),
  extract('const BROWSE_PATHS = {'),
  line('const SEARCH_TYPES = ['),
  line('const ROUTE_KEYS = ['),
  extract('function addr(path, params = {}) {'),
  line('const here = () =>'),
  line('const typeParam = () =>'),
  line('const genreParam = () =>'),
  extract('function go(target, { replace = false } = {}) {'),
  line('let showingUrl = null;'),
  line('let leavingUrl = null;'),
  line('let leavingTitle = null;'),
  extract('function splitAddr(target) {'),
  extract('function renderRoute(target) {'),
  extract('function navAddr(view) {'),
];

// Everything the router calls to put a screen on the screen. None of them are
// under test here: what is under test is which one an address reaches, and
// with what.
const STUBS = `
  const seen = [];
  const record = (name) => (...args) => seen.push([name, ...args]);
  const showSearch = record('showSearch');
  const showBrowse = record('showBrowse');
  const showScan = record('showScan');
  const showRequests = record('showRequests');
  const showQueue = record('showQueue');
  const showSettings = record('showSettings');
  const setView = record('setView');
  const openAlbum = record('openAlbum');
  const openArtist = record('openArtist');
  const openRequest = record('openRequest');
  const syncSearchControls = record('syncSearchControls');
  // Nothing is ever restored from the pane stack here, so every address is
  // drawn from scratch, which is the path a reload and a bookmark both take.
  const restorePane = () => false;
`;

/** A router wired to a stand-in for the address bar. */
function router(startPath = '/search', startSearch = '') {
  const location = { pathname: startPath, search: startSearch };
  const document = { title: 'lox' };
  const moves = [];
  const setLocation = (url) => {
    const cut = url.indexOf('?');
    location.pathname = cut < 0 ? url : url.slice(0, cut);
    location.search = cut < 0 ? '' : url.slice(cut);
  };
  const history = {
    pushState(_s, _t, url) { moves.push(['push', url]); setLocation(url); },
    replaceState(_s, _t, url) { moves.push(['replace', url]); setLocation(url); },
    go(n) { moves.push(['go', n]); },
  };
  const state = {
    view: 'search', searchType: 'all', exploreTab: 'channels', exploreGenre: '0',
    exploreChannel: '', scanTab: 'run', requestTab: 'find', requestsTracker: null,
    showHeld: false, settingsSection: '',
  };
  const box = { value: '' };
  const $ = (sel) => (sel === '#search-input' ? box : null);

  // eslint-disable-next-line no-new-func
  const built = new Function(
    'location', 'history', 'document', 'state', '$',
    `${STUBS}\n${parts.join('\n')}\nreturn { addr, go, renderRoute, navAddr, here, seen, VIEW_PATHS };`,
  )(location, history, document, state, $);
  return { ...built, state, box, moves, location };
}

// --- an address names one screen, and says which part of it -----------------
const routes = [
  ['/search', ['showSearch', '']],
  ['/search?q=daft+punk', ['showSearch', 'daft punk']],
  ['/browse', ['showBrowse', 'channels', '', '']],
  ['/browse/channels', ['showBrowse', 'channels', '', '']],
  ['/browse/charts?genre=132', ['showBrowse', 'charts', '132', '']],
  ['/browse/releases', ['showBrowse', 'releases', '', '']],
  ['/browse/channel/rap-fr', ['showBrowse', 'channels', '', 'rap-fr']],
  ['/scan', ['showScan', 'run']],
  ['/scan/history', ['showScan', 'history']],
  ['/requests', ['showRequests', 'find', '']],
  ['/requests?tracker=ops', ['showRequests', 'find', 'ops']],
  ['/requests/history', ['showRequests', 'history', '']],
  ['/requests/red/80755', ['openRequest', 'red', '80755']],
  ['/queue', ['showQueue']],
  ['/downloading', ['setView', 'downloads']],
  ['/uploading', ['setView', 'uploads']],
  ['/album/1000982941', ['openAlbum', '1000982941']],
  ['/artist/27', ['openArtist', '27']],
  ['/settings', ['showSettings', '']],
  ['/settings/torrent', ['showSettings', 'torrent']],
];

routes.forEach(([address, want]) => {
  const app = router();
  app.renderRoute(address);
  // The search box is put where the address says before anything is drawn, so
  // that step is not the answer to "where does this address go".
  const got = app.seen.filter(([name]) => name !== 'syncSearchControls').pop();
  check(`${address} opens ${want[0]}(${want.slice(1).map(String).join(', ')})`,
    JSON.stringify(got) === JSON.stringify(want), JSON.stringify(got));
});

// The second segment of /requests is a tab where there is one, and a request
// everywhere else. Getting this the wrong way round would make the history
// tab try to open request "history".
{
  const app = router();
  app.renderRoute('/requests/history');
  check('/requests/history is the tab, not request "history"',
    app.seen.some(([n]) => n === 'showRequests'), JSON.stringify(app.seen));
}

// --- an address nobody can get to is corrected, not shown blank -------------
{
  const app = router();
  app.renderRoute('/nonsense');
  check('an address the app has no way to lands on Search',
    app.seen.some(([n, q]) => n === 'showSearch' && q === ''), JSON.stringify(app.seen));
  check('and replaces its entry, so Back does not return to it',
    app.moves.length === 1 && app.moves[0][0] === 'replace', JSON.stringify(app.moves));
}

// --- the search box is filled from the address ------------------------------
{
  const app = router();
  app.renderRoute('/search?q=hello&type=track');
  check('the box and the kind filter are set from the address',
    JSON.stringify(app.seen[0]) === JSON.stringify(['syncSearchControls', 'hello', 'track']),
    JSON.stringify(app.seen[0]));
}

// --- what the address carries and what it drops -----------------------------
{
  // A ?token= link is how a bookmark authenticates. Dropping it on the first
  // click would sign the page out.
  const app = router('/search', '?q=old&token=abc123');
  check('a parameter that is not ours survives a move',
    app.addr('/queue') === '/queue?token=abc123', app.addr('/queue'));
  check('and one that is ours does not follow us off its own screen',
    !app.addr('/queue').includes('q=old'), app.addr('/queue'));
  check('an empty value writes no key at all',
    app.addr('/browse/charts', { genre: '' }) === '/browse/charts?token=abc123',
    app.addr('/browse/charts', { genre: '' }));
  check('and a set one does',
    app.addr('/browse/charts', { genre: '132' }) === '/browse/charts?token=abc123&genre=132',
    app.addr('/browse/charts', { genre: '132' }));
  // One screen, one address: the route's keys go in a fixed order, so two
  // ways of reaching the same place cannot leave two entries behind.
  check('the route writes its keys in one order, whatever the caller does',
    app.addr('/search', { type: 'album', q: 'x' }) === app.addr('/search', { q: 'x', type: 'album' }),
    app.addr('/search', { type: 'album', q: 'x' }));
}

// --- go() adds one entry, and only when it is going somewhere ---------------
{
  const app = router('/search', '');
  app.go('/queue');
  app.go('/queue');
  check('going somewhere adds one entry', app.moves.length === 1, JSON.stringify(app.moves));
  check('going where you already are adds none',
    app.moves.filter(([kind]) => kind === 'push').length === 1, JSON.stringify(app.moves));
  app.go('/scan', { replace: true });
  check('a correction overwrites the entry rather than adding one',
    app.moves[app.moves.length - 1][0] === 'replace', JSON.stringify(app.moves));
}

// --- the rail returns you to a screen as you left it ------------------------
{
  const app = router();
  app.state.searchType = 'album';
  app.box.value = 'daft punk';
  check('Search goes back to the search you ran',
    app.navAddr('search') === '/search?q=daft+punk&type=album', app.navAddr('search'));

  app.state.exploreTab = 'charts';
  app.state.exploreGenre = '132';
  check('Browse goes back to the list and the genre you were reading',
    app.navAddr('explore') === '/browse/charts?genre=132', app.navAddr('explore'));

  app.state.exploreChannel = 'rap-fr';
  check('or to the channel, when one is open',
    app.navAddr('explore') === '/browse/channel/rap-fr', app.navAddr('explore'));

  app.state.requestTab = 'history';
  check('Requests goes back to the tab you had open',
    app.navAddr('requests') === '/requests/history', app.navAddr('requests'));

  app.state.requestTab = 'find';
  app.state.requestsTracker = 'ops';
  check('and to the tracker whose requests you were reading',
    app.navAddr('requests') === '/requests?tracker=ops', app.navAddr('requests'));

  app.state.scanTab = 'history';
  check('Scan the same', app.navAddr('missing') === '/scan/history', app.navAddr('missing'));

  // The Queue has one address now. The list of rows the rules kept out was
  // removed -- everything in it was something nobody wanted, and offering it
  // as a second list to work through made the queue look like it was hiding
  // work -- so there is no longer a second state for the address to carry.
  check('the Queue has one address', app.navAddr('found') === '/queue', app.navAddr('found'));

  app.state.settingsSection = 'torrent';
  check('Settings goes back to the section you were reading',
    app.navAddr('settings') === '/settings/torrent', app.navAddr('settings'));
}

// --- and every screen in the rail has one at all ----------------------------
{
  const app = router();
  Object.keys(app.VIEW_PATHS).forEach((view) => {
    const to = app.navAddr(view);
    check(`the ${view} rail item has an address`, typeof to === 'string' && to.startsWith('/'), to);
  });
}

const failed = results.filter(([, ok]) => !ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (failed.length) {
  console.log('failed: ' + failed.map(([n]) => n).join(', '));
  process.exit(1);
}
