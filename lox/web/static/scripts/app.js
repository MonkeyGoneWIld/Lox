/* lox web UI.
 *
 * No build step and no framework on purpose: this ships inside a Python package
 * and has to stay editable without a Node toolchain.
 *
 * The one rule worth remembering while reading: nothing here calls a tracker
 * except missingScan(), missingCheck(), requestsFetch() and requestsCheck(). Everything
 * else is Deezer-only and free.
 */

(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const state = {
    view: 'search',
    searchType: 'all',
    exploreTab: 'channels',
    exploreGenre: '0',
    candidates: [],
    settings: null,
    pending: {},
    selectedCandidates: new Set(),
    missingTrackers: new Set(),
    requestsTracker: null,
    requestFilters: null,
    requestFiltersFor: null,
    album: null,
    // Panes you can go back to, innermost last.
    paneStack: [],
    found: [],
    selectedFound: new Set(),
    // Rows the Settings queue rules kept out, and the rule in words.
    foundHeld: [],
    selectedHeld: new Set(),
    heldGroups: [],
    droppedNote: '',
    foundRule: '',
    showHeld: false,
    // Narrowing what is on screen. Not persisted: it is a way of reading the
    // list, not a setting.
    // Releases ticked for a batch action, by album id.
    picked: new Map(),
    uploadTrackers: new Set(),
    albumCheck: null,
    watchlists: [],
    linking: false,
    requestRows: [],
    selectedRequests: new Set(),
    // The running search, so Cancel has something to stop and a second click
    // on Search cannot start a parallel one.
    requestsAbort: null,
    requestTab: 'find',
    scanTab: 'run',
    // A channel page inside Browse, and a place on the settings page. Both are
    // somewhere you can be, so both are somewhere with an address.
    exploreChannel: '',
    settingsSection: '',
    // Which detail page is open, so the address can say so and Back can leave.
    openAlbumId: null,
    openArtistId: null,
    scanHistory: [],
    scanHistorySelected: new Set(),
    scanWindow: 30,
    recheckDays: 30,
    history: [],
    historySelected: new Set(),
    // Check results by request id, so the split view can show what was found.
    requestMatches: new Map(),
    trackers: [],
    seedboxes: [],
    seedboxFields: [],
    flows: new Set(),
    pollers: new Map(),
  };

  // ---------------------------------------------------------------- helpers

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    // The session cookie expired or was cleared: bounce to the login page
    // rather than showing every panel an "authentication required" error.
    if (response.status === 401) {
      location.replace(`/login?next=${encodeURIComponent(location.pathname + location.search)}`);
      throw new Error('Session expired');
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
    return data;
  }

  function toast(message, kind = '') {
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.textContent = message;
    $('#toasts').append(el);
    setTimeout(() => el.remove(), 6000);
  }

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    }
    node.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
    return node;
  }

  // ------------------------------------------------------------------
  // Tables
  // ------------------------------------------------------------------
  // Every list in the app had its own header row, its own filter controls in
  // a bar somewhere above it, and its own idea of what "selected" meant. Three
  // consequences, all of which had been reported: shift-click selected
  // nothing, because no list tracked where the last click was; filters sat
  // detached from the columns they filtered, so which control narrowed which
  // column was guesswork; and the count came from walking the DOM, so a row
  // whose id was missing added `undefined` to the set and "17 selected" was
  // one more than the list held.
  //
  // Selection here is derived from the row data, never from the checkboxes.
  // A count that disagrees with the list is then not expressible.

  const tableState = new Map();

  /** The sort and per-column filters for one table, kept across re-renders. */
  function tableView(name) {
    if (!tableState.has(name)) {
      tableState.set(name, { sort: null, dir: 1, filters: {}, lastIndex: null });
    }
    return tableState.get(name);
  }

  /**
   * A sortable, filterable table with optional row selection.
   *
   * @param {object} spec
   *   `name` keys the retained sort/filter state. `rows` is the data. Each
   *   column has a `label`, a `cell(row)` renderer, an optional `value(row)`
   *   for sorting and filtering, and `filter: 'text' | 'choice' | false`.
   *   `selection` is `{ set, onChange }` to add checkboxes.
   * @returns {HTMLElement} The table, filters and all.
   */
  function dataTable({ name, rows, columns, selection = null, onShown = null,
                      idOf = (row) => row.id, empty: emptyText = 'Nothing here.' }) {
    const view = tableView(name);
    // What identifies a row. Usually its id; for a list of requests it is the
    // tracker and the id together, because request 70001 exists on both and
    // is a different release on each.
    view.idOf = idOf;

    // --- filtering, column by column ---------------------------------
    const valueOf = (column, row) =>
      column.value ? column.value(row) : '';
    let shown = rows;
    for (const column of columns) {
      const wanted = view.filters[column.label];
      if (column.filter === 'range') {
        // Two limits, either of which may be left empty: "1990 to blank"
        // means everything from 1990 on, which is how people ask.
        const low = wanted && wanted.low !== '' ? Number(wanted.low) : null;
        const high = wanted && wanted.high !== '' ? Number(wanted.high) : null;
        if (low === null && high === null) continue;
        shown = shown.filter((row) => {
          const value = Number(valueOf(column, row));
          if (!Number.isFinite(value)) return false;
          if (low !== null && value < low) return false;
          return !(high !== null && value > high);
        });
        continue;
      }
      if (!wanted) continue;
      shown = shown.filter((row) => {
        const value = String(valueOf(column, row) ?? '').toLowerCase();
        return column.filter === 'choice'
          ? value === String(wanted).toLowerCase()
          : value.includes(String(wanted).toLowerCase());
      });
    }

    // --- sorting -------------------------------------------------------
    const sorted = [...shown];
    const sortColumn = columns.find((c) => c.label === view.sort);
    if (sortColumn) {
      sorted.sort((a, b) => {
        const x = valueOf(sortColumn, a);
        const y = valueOf(sortColumn, b);
        if (typeof x === 'number' && typeof y === 'number') return (x - y) * view.dir;
        return String(x ?? '').localeCompare(String(y ?? ''), undefined, { numeric: true }) * view.dir;
      });
    }

    // What the table actually drew, after its own column filters. The
    // buttons above it act on this: selecting everything, then narrowing a
    // column, then pressing Download would otherwise have downloaded rows
    // that were no longer on screen.
    view.shown = sorted;
    if (onShown) onShown(sorted);

    const rerender = () => {
      const host = document.querySelector(`[data-table="${name}"]`);
      if (!host) return;
      // Which filter box had the caret, so typing survives the rebuild. Without
      // this the cursor jumped back to the start after every debounce.
      const focused = document.activeElement;
      const mark = focused && host.contains(focused)
        ? { cls: focused.className, ph: focused.placeholder, at: focused.selectionStart }
        : null;
      host.replaceWith(dataTable({ name, rows, columns, selection, onShown, idOf, empty: emptyText }));
      if (!mark) return;
      const fresh = document.querySelector(`[data-table="${name}"]`);
      const again = fresh && [...fresh.querySelectorAll('thead .th-filter')]
        .find((box) => box.className === mark.cls && box.placeholder === mark.ph);
      if (!again) return;
      again.focus();
      try { again.setSelectionRange(mark.at, mark.at); } catch { /* number inputs refuse */ }
    };

    // --- the header ----------------------------------------------------
    const header = el('tr', {},
      selection
        ? el('th', { class: 'col-pick' }, el('input', {
            type: 'checkbox',
            checked: sorted.length > 0 && sorted.every((r) => selection.set.has(idOf(r))),
            title: 'Select everything shown',
            onchange: (e) => {
              // From the rows on screen, not from the boxes. Selecting what is
              // filtered out is the other half of the same bug.
              for (const row of sorted) {
                if (e.target.checked) selection.set.add(idOf(row));
                else selection.set.delete(idOf(row));
              }
              selection.onChange();
              rerender();
            },
          }))
        : null,
      ...columns.map((column) => {
        const active = view.sort === column.label;
        const sortButton = el('button', {
          class: `th-sort${active ? ' active' : ''}`,
          title: `Sort by ${column.label.toLowerCase()}`,
          onclick: () => {
            if (view.sort === column.label) view.dir = -view.dir;
            else { view.sort = column.label; view.dir = 1; }
            rerender();
          },
        }, column.label, active ? el('span', { class: 'th-arrow' }, view.dir > 0 ? '▲' : '▼') : null);

        // The filter lives in the column it filters -- it used to sit in a bar
        // above the table, where nothing said which column it applied to --
        // and every cell gets the same two rows whether or not it has one.
        // Without the empty slot, columns with no filter were a row shorter
        // and the header stepped up and down across the table.
        let control = null;
        if (column.filter === 'choice') {
          const options = [...new Set(rows.map((r) => String(valueOf(column, r) ?? '')).filter(Boolean))].sort();
          control = el('select', {
            class: 'th-filter',
            onchange: (e) => { view.filters[column.label] = e.target.value; rerender(); },
          },
          el('option', { value: '', selected: !view.filters[column.label] }, 'all'),
          ...options.map((option) =>
            el('option', { value: option, selected: view.filters[column.label] === option }, option)));
        } else if (column.filter === 'range') {
          const current = view.filters[column.label] || { low: '', high: '' };
          const limit = (which, placeholder) => el('input', {
            class: 'th-filter th-range',
            type: 'number',
            placeholder,
            value: current[which],
            oninput: (e) => {
              const next = { ...(view.filters[column.label] || { low: '', high: '' }) };
              next[which] = e.target.value;
              view.filters[column.label] = next;
              clearTimeout(view.timer);
              view.timer = setTimeout(rerender, 260);
            },
          });
          control = el('div', { class: 'th-range-pair' },
            limit('low', column.lowLabel || 'min'),
            limit('high', column.highLabel || 'max'));
        } else if (column.filter) {
          control = el('input', {
            class: 'th-filter',
            type: 'search',
            placeholder: 'filter',
            value: view.filters[column.label] || '',
            oninput: (e) => {
              view.filters[column.label] = e.target.value;
              clearTimeout(view.timer);
              // Typing should not rebuild the table on every keystroke.
              view.timer = setTimeout(rerender, 220);
            },
          });
        }

        // The column's class dresses the DATA cell, not the header. Applying
        // it to both put `display: flex` on the trackers header, which laid
        // the label and its filter out side by side while every other header
        // stacked them -- the row stepped up and down across the table.
        return el('th', {},
          el('div', { class: 'th-label' }, sortButton),
          el('div', { class: 'th-filter-slot' }, control));
      }));

    if (!sorted.length) {
      return el('div', { 'data-table': name, class: 'datatable' },
        el('table', { class: 'table' }, el('thead', {}, header)),
        empty(rows.length ? 'Nothing matches these filters.' : emptyText));
    }

    // --- the rows ------------------------------------------------------
    const body = sorted.map((row, index) =>
      el('tr', {},
        selection
          ? el('td', { class: 'col-pick' }, el('input', {
              type: 'checkbox',
              checked: selection.set.has(idOf(row)),
              onclick: (e) => {
                // Shift extends from the last box that was clicked, which is
                // what every list of checkboxes has done for thirty years and
                // what none of these did.
                if (e.shiftKey && view.lastIndex !== null) {
                  const [from, to] = [view.lastIndex, index].sort((a, b) => a - b);
                  for (let i = from; i <= to; i++) {
                    if (e.target.checked) selection.set.add(idOf(sorted[i]));
                    else selection.set.delete(idOf(sorted[i]));
                  }
                } else if (e.target.checked) {
                  selection.set.add(idOf(row));
                } else {
                  selection.set.delete(idOf(row));
                }
                view.lastIndex = index;
                selection.onChange();
                rerender();
              },
            }))
          : null,
        ...columns.map((column) => el('td', { class: column.class || '' }, column.cell(row)))));

    return el('div', { 'data-table': name, class: 'datatable' },
      el('table', { class: 'table' },
        el('thead', {}, header),
        el('tbody', {}, ...body)));
  }

  /**
   * How many of `rows` are selected -- from the data, never the checkboxes.
   *
   * @param {Array} rows - The rows on screen.
   * @param {Set} set - The selection.
   * @param {Function} [idOf] - What identifies a row; defaults to its id.
   * @returns {number} How many of them are in the set.
   */
  function countSelected(rows, set, idOf = (row) => row.id) {
    return rows.reduce((n, row) => n + (set.has(idOf(row)) ? 1 : 0), 0);
  }

  const duration = (seconds) => {
    if (!seconds) return '';
    const m = Math.floor(seconds / 60);
    const s = String(Math.floor(seconds % 60)).padStart(2, '0');
    return `${m}:${s}`;
  };

  const empty = (message) => el('p', { class: 'empty' }, message);
  const spinner = (label) => el('p', { class: 'empty' }, el('span', { class: 'spinner' }), ' ' + label);

  // ---------------------------------------------------------------- routing

  // A count on the pipeline rail. Blank rather than zero: a stage with nothing
  // in it should be quiet, not report a nought.
  function railCount(sel, n) {
    const el2 = $(sel);
    if (!el2) return;
    el2.textContent = n ? String(n) : '';
    // A stage holding something gets a live marker on the pipeline line, so
    // the rail reads before the numbers beside it do.
    el2.closest('.nav-item')?.classList.toggle('has-work', n > 0);
  }

  // The one interruption worth colouring from anywhere in the app: a run that
  // has stopped and is waiting on an answer. Without this the only way to find
  // out was to already be looking at Uploading.
  function railNeedsYou(n) {
    railCount('#upload-count-rail', n);
    $(`.nav-item[data-view="uploads"]`)?.classList.toggle('needs-you', n > 0);
  }

  // ------------------------------------------------------------------
  // Addresses
  // ------------------------------------------------------------------
  // The address bar is where the app is, not a note it writes afterwards.
  // Every move goes through go(): it puts the address up and then draws
  // whatever that address names, and nothing else paints a screen.
  //
  // The two used to be separate -- a click changed the screen and then, where
  // somebody had got round to it, mentioned itself to history -- and the
  // places nobody had got round to were the ones people actually use. A
  // search, a Browse tab, a genre, a channel, a request, the excluded rows in
  // the queue and a place on the settings page all carried the address of
  // whichever screen you had arrived from. Back skipped every one of them at
  // once, a reload threw them away, and none of them could be bookmarked or
  // sent to anybody.
  //
  // The view names are internal and half of them are wrong from outside --
  // "missing" is the Scan tab, "found" is the Queue -- so the path is what the
  // screen is called, and this maps between the two.

  const VIEW_PATHS = {
    search: '/search',
    explore: '/browse',
    missing: '/scan',
    requests: '/requests',
    found: '/queue',
    downloads: '/downloading',
    uploads: '/uploading',
    settings: '/settings',
  };

  // Browse is three lists, not one. Which of them you are reading is part of
  // where you are, and it was lost on every reload.
  const BROWSE_PATHS = {
    channels: '/browse/channels',
    charts: '/browse/charts',
    releases: '/browse/releases',
  };

  const SEARCH_TYPES = ['all', 'album', 'track', 'artist'];

  // The query keys the routes own. Anything else in the address belongs to
  // somebody else -- ?token= above all, which is how a bookmark authenticates
  // -- so it is carried through every move rather than dropped.
  const ROUTE_KEYS = ['q', 'type', 'genre', 'tracker', 'held'];

  /**
   * An address: a path, this route's own parameters, and whatever else the
   * current address is carrying.
   *
   * An empty value drops its key rather than writing `?genre=`, and the keys
   * are written in ROUTE_KEYS order rather than the caller's, so a screen has
   * exactly one address. Two ways of saying the same thing would otherwise
   * differ by the order of the query and become two history entries, one of
   * which Back would step through for no reason.
   *
   * @param {string} path - The path part.
   * @param {Object} [params] - The route's own query parameters. A key that
   *   is not one of ROUTE_KEYS is not the route's to set, and is ignored.
   * @returns {string} The address to hand to go().
   */
  function addr(path, params = {}) {
    const query = new URLSearchParams(location.search);
    ROUTE_KEYS.forEach((key) => query.delete(key));
    for (const key of ROUTE_KEYS) {
      const value = params[key];
      if (value !== null && value !== undefined && value !== '') query.set(key, String(value));
    }
    const rest = query.toString();
    return rest ? `${path}?${rest}` : path;
  }

  /** The address on screen: path and query together, which is what Back sees. */
  const here = () => location.pathname + location.search;

  /** The kind filter as the address writes it. `all` is the absence of one. */
  const typeParam = () => (state.searchType === 'all' ? '' : state.searchType);

  /** The genre as the address writes it. `0` is Deezer's own "every genre". */
  const genreParam = () => (state.exploreGenre === '0' ? '' : state.exploreGenre);

  /**
   * Go somewhere: put the address up, then draw it.
   *
   * @param {string} target - Where to go, from addr().
   * @param {boolean} [options.replace] - Overwrite the current entry rather
   *   than adding one. For the first paint and for corrections, neither of
   *   which is somewhere Back should return to.
   */
  function go(target, { replace = false } = {}) {
    if (target === here() && !replace) return;
    if (replace) history.replaceState({ url: target }, '', target);
    else history.pushState({ url: target }, '', target);
    renderRoute(target);
  }

  // The address whose page is on screen, and the one before it. A detail page
  // covers the pane it opened from and keeps the nodes; that entry has to
  // remember which address those nodes belong to, or Back cannot pair the two
  // up again.
  let showingUrl = null;
  let leavingUrl = null;
  // And what that page was called. Read here rather than where the pane is
  // stacked, which happens after the new screen has already renamed the tab:
  // Back out of a release came home to a page of search results titled
  // "Search".
  let leavingTitle = null;

  /** An address split into its path and its parsed query. */
  function splitAddr(target) {
    const cut = target.indexOf('?');
    if (cut < 0) return [target, new URLSearchParams()];
    return [target.slice(0, cut), new URLSearchParams(target.slice(cut + 1))];
  }

  /**
   * Draw whatever an address names.
   *
   * Never touches history: go() and the browser's own buttons are the only
   * things that do, which is what keeps the bar and the screen agreeing.
   *
   * @param {string} target - The address to draw.
   */
  function renderRoute(target) {
    leavingUrl = showingUrl;
    leavingTitle = document.title;
    showingUrl = target;
    const [path, query] = splitAddr(target);
    const param = (key) => query.get(key) || '';

    // The box and the kind filter are part of the address whether the results
    // are fetched again or handed back by restorePane below.
    if (path === '/search') syncSearchControls(param('q'), param('type'));

    // Coming back to a page whose nodes are still in hand: put them back
    // rather than fetching them a second time. Brings the scroll position and
    // the ticks with them, and costs no API call.
    if (restorePane(target)) return;

    const album = path.match(/^\/album\/(.+)$/);
    if (album) { openAlbum(decodeURIComponent(album[1])); return; }
    const artist = path.match(/^\/artist\/(.+)$/);
    if (artist) { openArtist(decodeURIComponent(artist[1])); return; }
    const channel = path.match(/^\/browse\/channel\/(.+)$/);
    if (channel) { showBrowse('channels', '', decodeURIComponent(channel[1])); return; }
    // Two segments, so /requests/history stays a tab and never a request.
    const request = path.match(/^\/requests\/([^/]+)\/([^/]+)$/);
    if (request) { openRequest(decodeURIComponent(request[1]), decodeURIComponent(request[2])); return; }
    const settings = path.match(/^\/settings\/([^/]+)$/);
    if (settings) { showSettings(decodeURIComponent(settings[1])); return; }

    switch (path) {
      case '/search': showSearch(param('q')); return;
      case '/browse':
      case '/browse/channels': showBrowse('channels', '', ''); return;
      case '/browse/charts': showBrowse('charts', param('genre'), ''); return;
      case '/browse/releases': showBrowse('releases', param('genre'), ''); return;
      case '/scan': showScan('run'); return;
      case '/scan/history': showScan('history'); return;
      case '/requests': showRequests('find', param('tracker')); return;
      case '/requests/history': showRequests('history', param('tracker')); return;
      case '/queue': showQueue(param('held') === '1'); return;
      case '/downloading': setView('downloads'); return;
      case '/uploading': setView('uploads'); return;
      case '/settings': showSettings(''); return;
      default: break;
    }
    // An address the app has no way to. Correct it rather than leaving a blank
    // frame, replacing the entry so Back does not lead straight back to it.
    go(addr('/search'), { replace: true });
  }

  /**
   * The address a nav item goes to.
   *
   * Not simply the screen's path: the tab you last had open on Requests or
   * Scan, the genre you were browsing and the search you ran are all part of
   * where that screen is, so pressing its name in the rail returns you to it
   * as you left it rather than to its empty first page.
   *
   * @param {string} view - The internal view name from the rail.
   * @returns {string} Where that item goes.
   */
  function navAddr(view) {
    if (view === 'search') {
      return addr('/search', { q: $('#search-input')?.value.trim() || '', type: typeParam() });
    }
    if (view === 'explore') {
      if (state.exploreChannel) return addr(`/browse/channel/${encodeURIComponent(state.exploreChannel)}`);
      return addr(BROWSE_PATHS[state.exploreTab] || '/browse/channels',
                  { genre: state.exploreTab === 'channels' ? '' : genreParam() });
    }
    if (view === 'missing') return addr(state.scanTab === 'history' ? '/scan/history' : '/scan');
    if (view === 'requests') {
      return state.requestTab === 'history'
        ? addr('/requests/history')
        : addr('/requests', { tracker: state.requestsTracker || '' });
    }
    if (view === 'found') return addr('/queue', { held: state.showHeld ? '1' : '' });
    if (view === 'settings') {
      return addr(state.settingsSection ? `/settings/${state.settingsSection}` : '/settings');
    }
    return addr(VIEW_PATHS[view] || '/search');
  }

  // Opening a release or an artist is going to its page, so it goes through
  // the address rather than straight to the function that draws it. These are
  // also real hrefs, so the status bar names where a link goes and a
  // middle-click opens it in a tab, which a `href="#"` could never do.
  const albumHref = (id) => addr(`/album/${encodeURIComponent(id)}`);
  const artistHref = (id) => addr(`/artist/${encodeURIComponent(id)}`);
  const goAlbum = (id) => go(albumHref(id));
  const goArtist = (id) => go(artistHref(id));

  /**
   * Name the tab after whatever is on screen.
   *
   * Kept out of the address layer, which runs before the view has changed on
   * the way in -- the first paint of a deep link set the title from the view
   * the app had not left yet, so opening /requests/history said "Search".
   *
   * @param {string} [name] - An override, for a page that knows what it is
   *   showing.
   */
  function setTitle(name) {
    document.title = name ? `lox — ${name}` : `lox — ${navLabel(state.view)}`;
  }

  // ------------------------------------------------------------------
  // The screens, one per address
  // ------------------------------------------------------------------
  // Each of these draws a screen from an address and nothing else: no history,
  // and no assumption about where the reader came from. That is what makes a
  // reload, a bookmark and Back the same thing rather than three code paths
  // with three sets of bugs.

  /**
   * Put the search box and the kind filter where the address says.
   *
   * @param {string} query - The text searched for.
   * @param {string} type - all, album, track or artist.
   */
  function syncSearchControls(query, type) {
    const wanted = SEARCH_TYPES.includes(type) ? type : 'all';
    // Narrowing to one kind is a different list, so it is not the batch you
    // picked from the last one.
    if (state.searchType !== wanted) clearPicks();
    state.searchType = wanted;
    const box = $('#search-input');
    if (box && box.value !== query) box.value = query;
    $$('#search-type button').forEach((b) => b.classList.toggle('active', b.dataset.type === wanted));
  }

  /**
   * The Search screen, showing whatever the address asked for.
   *
   * The query is in the address, so results survive a reload, can be sent to
   * somebody, and Back out of a release returns to them. Pressing Search used
   * to change the page and nothing else, so Back from a search left the app
   * and a reload lost what you had typed.
   *
   * @param {string} query - The text to search for, or "" for the empty page.
   */
  async function showSearch(query) {
    setView('search');
    // A search is a root: there is nothing behind it to go back to.
    state.paneStack.length = 0;
    setTitle(query ? `“${query}”` : undefined);
    if (!query) { searchPane('grid').replaceChildren(); return; }
    await runSearch(query);
  }

  /**
   * The Browse screen: which of the three lists, which genre, and the channel
   * if one is open.
   *
   * @param {string} tab - channels, charts or releases.
   * @param {string} genre - A Deezer genre id, or "" for all of them.
   * @param {string} channel - A channel slug, when a channel page is open.
   */
  function showBrowse(tab, genre, channel) {
    const wanted = BROWSE_PATHS[tab] ? tab : 'channels';
    const wantedGenre = genre || '0';
    const wantedChannel = channel || '';
    // A batch belongs to the list it was picked from, and each of these is a
    // different list.
    if (state.exploreTab !== wanted || state.exploreGenre !== wantedGenre
        || state.exploreChannel !== wantedChannel) clearPicks();
    state.exploreTab = wanted;
    state.exploreGenre = wantedGenre;
    state.exploreChannel = wantedChannel;
    $$('#explore-tabs button').forEach((b) => b.classList.toggle('active', b.dataset.explore === wanted));
    setView('explore');
    if (wantedChannel) setTitle(wantedChannel);
  }

  /**
   * The Scan screen, on one of its two tabs.
   *
   * @param {string} tab - run or history.
   */
  function showScan(tab) {
    const wanted = tab === 'history' ? 'history' : 'run';
    state.scanTab = wanted;
    setView('missing');
    showScanTab(wanted);
  }

  /**
   * The Requests screen: which tab, and whose requests.
   *
   * The tracker is in the address because the whole form under it belongs to
   * that tracker -- the two sites do not offer the same filters -- so a page
   * without it in the address is not the page you bookmarked.
   *
   * @param {string} tab - find or history.
   * @param {string} tracker - A tracker code, or "" to keep the current one.
   */
  function showRequests(tab, tracker) {
    const wanted = tab === 'history' ? 'history' : 'find';
    if (tracker && tracker !== state.requestsTracker) {
      state.requestsTracker = tracker;
      // The counts on screen belonged to the other tracker's search.
      requestsSummary({ shown: null });
    }
    // Only the ticks move: rebuilding all three pickers from here would fight
    // with the correction renderTrackerPickers makes when the status arrives.
    $$('#requests-tracker button').forEach(
      (b) => b.classList.toggle('active', b.dataset.tracker === state.requestsTracker));
    setView('requests');
    showRequestTab(wanted);
  }

  /**
   * The Queue, with or without the rows the queue rules kept out.
   *
   * @param {boolean} held - Whether the excluded rows are shown.
   */
  function showQueue(held) {
    const showing = state.view === 'found';
    state.showHeld = held;
    const toggle = $('#found-held-toggle');
    if (toggle) toggle.textContent = held ? 'Hide excluded' : 'Show excluded';
    // Already here: re-draw rather than re-fetch, so showing the excluded rows
    // and hiding them again does not clear what you had ticked.
    if (showing) { renderFound(); setTitle(); return; }
    setView('found');
  }

  /**
   * The Settings page, at the part the address names.
   *
   * @param {string} section - A category slug, a section id, or "" for the top.
   */
  function showSettings(section) {
    state.settingsSection = section || '';
    // Already here: scroll, and do not re-fetch. Rebuilding the page would
    // throw away every edit that has not been saved yet.
    if (state.view === 'settings' && state.settings) {
      setTitle();
      revealSettingsSection(true);
      return;
    }
    setView('settings');
  }

  /**
   * Show one of the top-level screens.
   *
   * The address is go()'s business; this only swaps what is on screen and
   * starts whatever that screen loads.
   *
   * @param {string} view - The internal view name.
   */
  function setView(view) {
    // A batch belongs to the list it was picked from. Leaving the list --
    // to the Queue, to Downloading, to anywhere -- leaves you holding
    // releases you can no longer see, and a count that matches nothing on
    // screen. Every other place that changes what is listed does the same.
    if (state.view !== view) clearPicks();
    state.view = view;
    $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
    $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${view}`));
    $('#view-title').textContent = navLabel(view);
    if (view === 'explore') loadExplore();
    if (view === 'missing') { loadWatchlists(); loadScanFilters(); }
    if (view === 'found') loadFound();
    if (view === 'requests' && state.requestFiltersFor !== state.requestsTracker) loadRequestFilters();
    if (view === 'downloads') pollDownloads(true);
    if (view === 'uploads') { loadFolders(); resumeFlows(); }
    if (view === 'settings') loadSettings();
    // Leaving a detail page for a list is leaving the detail page.
    state.openAlbumId = null;
    state.openArtistId = null;
    setTitle();
  }

  // ---------------------------------------------------------------- status

  async function refreshStatus() {
    let status;
    try {
      status = await api('/api/status');
    } catch {
      $('#deezer-status .dot').dataset.state = 'bad';
      $('#deezer-status .status-text').textContent = 'Server unreachable';
      return;
    }

    const dot = $('#deezer-status .dot');
    const text = $('#deezer-status .status-text');
    if (!status.deezer.configured) {
      dot.dataset.state = 'warn';
      text.textContent = 'No ARL configured';
    } else if (status.deezer.authenticated) {
      dot.dataset.state = 'ok';
      text.textContent = `Deezer ${status.deezer.country || ''}`.trim();
    } else {
      dot.dataset.state = 'bad';
      text.textContent = 'ARL rejected';
    }

    state.trackers = status.trackers.filter((t) => t.configured);
    renderBudgets();
    if (!state.missingTrackers.size) state.trackers.forEach((t) => state.missingTrackers.add(t.code));
    renderTrackerPickers();

    railCount('#dl-badge', status.downloads.active);
    $('#downloads-dir').textContent = `Saving to ${status.downloads.directory} as ${status.downloads.format}`;
    $('#uploads-dir').textContent = status.downloads.directory;
    renderProblems(status.problems);
    syncUploadToggles(status.upload);
  }

  // Reflect the stored setting, unless you are mid-click on the box itself --
  // a poll landing at the wrong moment should not undo what you just did.
  function syncUploadToggles(upload) {
    if (!upload) return;
    for (const [id, value] of [['upload-dry-run', upload.dry_run], ['upload-yes-all', upload.yes_all]]) {
      const box = $(`#${id}`);
      if (box && box !== document.activeElement) box.checked = !!value;
    }
  }

  // Writes through to the one setting rather than keeping a per-page copy.
  async function setUploadFlag(key, box, label) {
    const value = box.checked;
    box.disabled = true;
    try {
      await api('/api/settings', { method: 'PUT', body: { changes: { [key]: value } } });
      toast(`${label} ${value ? 'on' : 'off'}`, 'ok');
    } catch (e) {
      box.checked = !value;
      toast(e.message, 'bad');
    } finally {
      box.disabled = false;
    }
  }

  // A misconfigured path no longer stops the server booting, so it has to be
  // impossible to miss once it has booted -- and one click from being fixed.
  function renderProblems(problems) {
    const banner = $('#config-problems');
    const list = problems || [];
    banner.hidden = !list.length;
    if (!list.length) return;
    banner.replaceChildren(
      el('strong', {}, list.length === 1 ? 'Configuration problem' : `${list.length} configuration problems`),
      el('ul', {}, ...list.map((p) => el('li', {}, p.message))),
      el('button', { class: 'link', onclick: () => go(addr('/settings')) }, 'Open settings'),
    );
  }

  function renderBudgets() {
    const container = $('#tracker-budgets');
    container.replaceChildren(
      ...state.trackers.map((t) => {
        const pct = t.budget ? (t.remaining / t.budget) * 100 : 0;
        const cls = pct === 0 ? 'out' : pct < 25 ? 'low' : '';
        const note = t.cooldown_seconds ? ` · cooling ${t.cooldown_seconds}s` : '';
        return el(
          'div',
          { class: 'budget' },
          el('div', { class: 'budget-head' }, el('span', {}, t.code), el('span', {}, `${t.remaining}/${t.budget}${note}`)),
          el('div', { class: 'budget-bar' }, el('div', { class: `budget-fill ${cls}`, style: `width:${pct}%` })),
        );
      }),
    );
  }

  function renderTrackerPickers() {
    const missing = $('#missing-trackers');
    missing.replaceChildren(
      ...state.trackers.map((t) =>
        el(
          'button',
          {
            type: 'button',
            class: state.missingTrackers.has(t.code) ? 'active' : '',
            onclick: () => {
              state.missingTrackers.has(t.code)
                ? state.missingTrackers.delete(t.code)
                : state.missingTrackers.add(t.code);
              renderTrackerPickers();
              renderCandidates();
            },
          },
          t.code,
        ),
      ),
    );

    // The tracker can arrive from the address, which is written before the
    // status says which trackers this install has. One that is not configured
    // is corrected here, and the address is corrected with it rather than left
    // naming a tracker that is not the one on screen.
    if (state.trackers.length && !state.trackers.some((t) => t.code === state.requestsTracker)) {
      state.requestsTracker = state.trackers[0].code;
      if (location.pathname === '/requests') {
        go(addr('/requests', { tracker: state.requestsTracker }), { replace: true });
      } else if (state.view === 'requests') {
        loadRequestFilters();
      }
    }
    $('#requests-tracker').replaceChildren(
      ...state.trackers.map((t) =>
        el(
          'button',
          {
            type: 'button',
            'data-tracker': t.code,
            class: state.requestsTracker === t.code ? 'active' : '',
            // A different tracker is a different form, a different budget and
            // a different list, so it is a different address.
            onclick: () => go(addr('/requests', { tracker: t.code })),
          },
          t.code,
        ),
      ),
    );

    if (!state.uploadTrackers.size && state.trackers.length) state.uploadTrackers.add(state.trackers[0].code);
    $('#upload-tracker').replaceChildren(
      ...state.trackers.map((t) =>
        el(
          'button',
          {
            type: 'button',
            class: state.uploadTrackers.has(t.code) ? 'active' : '',
            title: 'Uploading to several trackers creates one hardlinked folder each',
            onclick: () => {
              state.uploadTrackers.has(t.code)
                ? state.uploadTrackers.delete(t.code)
                : state.uploadTrackers.add(t.code);
              renderTrackerPickers();
            },
          },
          t.code,
        ),
      ),
    );

    requestsCost();
  }

  // ---------------------------------------------------------------- cards

  function card(item) {
    const isAlbum = item.type === 'album' || item.album_id;
    const albumId = item.type === 'album' ? item.id : item.album_id;
    if (isAlbum && albumId) PICKABLE.set(String(albumId), item);
    const node = el(
      'div',
      {
        class: `card ${item.type}`,
        // The id in the DOM is what makes a shift-range possible: the range is
        // "everything between these two on screen", and on screen is the only
        // place that order exists.
        'data-album': isAlbum && albumId ? String(albumId) : null,
        // Once anything is ticked you are choosing a batch, so a click on a
        // card joins the batch rather than leaving the page you are building
        // it on. Reaching for the little circle every time was the alternative,
        // and it is the one thing on screen small enough to miss.
        onclick: (e) => {
          if (isAlbum && albumId && selecting()) {
            e.preventDefault();
            const id = String(albumId);
            pickClicked(id, item, !state.picked.has(id), e.shiftKey);
            return;
          }
          if (item.type === 'artist') goArtist(item.id);
          else if (albumId) goAlbum(albumId);
        },
      },
      el(
        'div',
        { class: 'card-art', style: item.image ? `background-image:url('${item.image}')` : '' },
        // Ticking a release does not open it. One at a time is a click; a
        // batch is a tick, then one decision for all of them.
        isAlbum && albumId
          ? el(
              'label',
              { class: 'card-pick', onclick: (e) => e.stopPropagation(), title: 'Select' },
              el('input', {
                type: 'checkbox',
                checked: state.picked.has(String(albumId)),
                // click rather than change, because change does not carry the
                // shift key and a range select is the whole point of it.
                onclick: (e) => {
                  e.stopPropagation();
                  pickClicked(String(albumId), item, e.target.checked, e.shiftKey);
                },
              }),
            )
          : null,
        isAlbum && albumId
          ? el(
              'div',
              { class: 'card-actions' },
              el(
                'button',
                {
                  class: 'icon-btn',
                  title: 'Download',
                  onclick: (e) => {
                    e.stopPropagation();
                    if (selecting()) return;
                    download(albumId);
                  },
                },
                '↓',
              ),
              el(
                'button',
                {
                  class: 'icon-btn upload',
                  title: 'Download and upload',
                  onclick: (e) => {
                    e.stopPropagation();
                    if (selecting()) return;
                    downloadAndUpload(albumId, item);
                  },
                },
                '↑',
              ),
            )
          : null,
      ),
      el('div', { class: 'card-title', title: item.title }, item.title || ''),
      item.artist_id
        ? el(
            'a',
            {
              class: 'card-sub card-link',
              title: item.artist,
              href: artistHref(item.artist_id),
              onclick: (e) => { e.preventDefault(); e.stopPropagation(); goArtist(item.artist_id); },
            },
            item.artist,
          )
        : el('div', { class: 'card-sub', title: item.artist },
            item.artist || (item.albums ? `${item.albums} albums` : '')),
      item.date || item.record_type
        ? el(
            'div',
            { class: 'card-meta' },
            [item.date ? item.date.slice(0, 4) : null,
             item.record_type && item.record_type !== 'album' ? item.record_type.toUpperCase() : null,
             item.tracks ? `${item.tracks} tracks` : null].filter(Boolean).join(' · '),
          )
        : null,
    );
    if (state.picked.has(String(albumId))) node.classList.add('picked');
    return node;
  }

  // ------------------------------------------------------------ selection

  // The one place a release becomes picked or unpicked. Everything visible
  // about that -- the set, the outline, the circle -- is set here together,
  // because when the circle was left to whichever caller happened to come
  // through a checkbox, picking from the card body left an empty circle on a
  // picked card, and pressing that circle then argued with the state behind it.
  function togglePick(albumId, item, on) {
    const id = String(albumId);
    if (on) state.picked.set(id, item);
    else state.picked.delete(id);
    // Every card showing this release, not just the first. A chart lists the
    // same album under Albums and again under Tracks, and marking one of them
    // left the other looking unpicked while the id sat in the set.
    document.querySelectorAll(`.card[data-album="${id}"]`).forEach((card) => {
      card.classList.toggle('picked', on);
      const box = card.querySelector('.card-pick input');
      if (box) box.checked = on;
    });
    renderPickBar();
  }

  // Set while a range or a select-all is running. Without it the bar is rebuilt
  // once per card, which throws away the button the click is still inside.
  let bulkPicking = false;

  function inBulk(fn) {
    bulkPicking = true;
    try { fn(); } finally { bulkPicking = false; }
    renderPickBar();
  }

  /** Whether a batch is being built, which changes what a plain click means. */
  const selecting = () => state.picked.size > 0;

  // Every album currently rendered, by id. Cards register here as they are
  // built so a range or a select-all can reach an item without re-fetching it.
  const PICKABLE = new Map();

  //: The card whose box was ticked last, which is where a shift-range starts.
  let lastPickedId = null;

  /** The selectable cards on screen, in the order they are laid out. */
  const pickableCards = () => [...document.querySelectorAll('.card[data-album]')];

  function setPick(albumId, on) {
    togglePick(albumId, PICKABLE.get(String(albumId)), on);
  }

  // Ticking with shift held picks everything between the last tick and this
  // one, the way a file list does. Without it, taking twenty of a page of
  // thirty is twenty clicks.
  function pickClicked(albumId, item, on, shift) {
    PICKABLE.set(albumId, item);
    if (shift && lastPickedId && lastPickedId !== albumId) {
      const ids = pickableCards().map((c) => c.dataset.album);
      const from = ids.indexOf(lastPickedId);
      const to = ids.indexOf(albumId);
      if (from !== -1 && to !== -1) {
        const [lo, hi] = from < to ? [from, to] : [to, from];
        // The whole range takes the state of the box you just clicked, so
        // shift-unticking clears a range as well.
        inBulk(() => ids.slice(lo, hi + 1).forEach((id) => setPick(id, on)));
        lastPickedId = albumId;
        return;
      }
    }
    togglePick(albumId, item, on);
    lastPickedId = albumId;
  }

  /** Tick every selectable card on screen. */
  function selectAllVisible() {
    const ids = pickableCards().map((c) => c.dataset.album);
    // Only ever adds. It lives in a bar that exists because something is
    // selected, so a version that could empty the batch would be a button
    // that removes itself mid-press.
    inBulk(() => ids.forEach((id) => setPick(id, true)));
    lastPickedId = ids.length ? ids[ids.length - 1] : null;
  }

  function clearPicks() {
    state.picked.clear();
    $$('.card.picked').forEach((c) => {
      c.classList.remove('picked');
      const box = c.querySelector('.card-pick input');
      if (box) box.checked = false;
    });
    lastPickedId = null;
    renderPickBar();
  }

  // A bar that only exists while something is selected, so the page is not
  // permanently carrying controls for a thing you are usually not doing.
  function renderPickBar() {
    const bar = $('#pick-bar');
    const count = state.picked.size;
    // The whole page behaves differently while a batch is open, so it is said
    // once, here, rather than asked for by every rule that cares.
    document.body.classList.toggle('picking', count > 0);
    bar.hidden = !count;
    if (!count) {
      // Emptied, not just hidden. A hidden bar holding "96 selected" is a
      // stale claim that a screen reader will still read out.
      bar.replaceChildren();
      return;
    }
    const items = () => [...state.picked.entries()].map(([id, item]) => ({ id, item }));
    const onScreen = pickableCards().length;
    const allTaken = onScreen > 0 && pickableCards().every((c) => state.picked.has(c.dataset.album));
    bar.replaceChildren(
      el('strong', {}, `${count} selected`),
      el('button', { onclick: () => bulkDownload(items()) }, 'Download'),
      el('button', { onclick: () => bulkDownloadAndUpload(items()) }, 'Download & upload'),
      el('button', { onclick: () => bulkCheck(items()) }, 'Check trackers'),
      // Everything above acts on the batch. Everything after the gap changes
      // what the batch is, which is a different kind of thing, so it sits at
      // the other end of the row.
      el('span', { class: 'bar-gap' }),
      onScreen && !allTaken
        ? el('button', { class: 'ghost', onclick: selectAllVisible }, 'Select all')
        : null,
      el('button', { class: 'ghost', onclick: clearPicks }, 'Clear all'),
    );
  }

  async function bulkDownload(entries) {
    try {
      const { queued, failed } = await api('/api/download', {
        method: 'POST',
        body: { album_ids: entries.map((e) => e.id) },
      });
      toast(`Queued ${queued.length} download${queued.length === 1 ? '' : 's'}` +
            (failed.length ? `, ${failed.length} failed` : ''), failed.length ? 'bad' : 'ok');
      clearPicks();
      go(addr('/downloading'));
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  // Everything downloads at once; the uploads queue behind each other.
  //
  // These are two different jobs with two different reasons. Downloads are
  // network-bound and the downloader already limits its own concurrency, so
  // running them one release at a time left the connection idle between
  // albums. Uploads ask questions, and two uploads asking at once gives you a
  // prompt you cannot tell the release for -- so those still run in turn, each
  // starting as soon as its own download lands.
  async function bulkDownloadAndUpload(entries) {
    const trackers = [...state.uploadTrackers];
    if (!trackers.length) return toast('Pick a tracker to upload to first', 'bad');
    clearPicks();
    go(addr('/downloading'));
    toast(`Downloading ${entries.length} together. Uploads start as each one lands.`);

    const started = await Promise.all(entries.map(async ({ id, item }) => ({
      id,
      label: `${item?.artist || ''} - ${item?.title || ''}`.trim() || String(id),
      queued: await download(id),
    })));

    // One at a time from here, in the order they were picked.
    for (const { id, label, queued } of started) {
      if (!queued) continue;
      const job = await waitForDownload(queued.id);
      if (!job || job.status !== 'done' || !job.folder) {
        toast(`${label}: ${job?.error || 'download did not finish'}`, 'bad');
        continue;
      }
      go(addr('/uploading'));
      await startUpload(job.folder, trackers, id);
    }
  }

  async function bulkCheck(entries) {
    const trackers = checkTrackers();
    if (!trackers.length) return toast('No tracker configured', 'bad');
    const urls = entries
      .map(({ item }) => item.url || (item.id ? `https://www.deezer.com/album/${item.id}` : ''))
      .filter(Boolean);
    if (!urls.length) return toast('Nothing to check', 'bad');

    go(addr('/scan'));
    const box = $('#missing-sources');
    // Only what was just picked. Appending to whatever was left in the box
    // meant pressing Check on four albums could spend budget on forty from a
    // scan you set up an hour ago.
    box.value = urls.join('\n');
    clearPicks();
    // And then actually check them. This used to stop here, having moved you
    // to another tab and pasted some URLs, with the button you had already
    // pressed -- "Check trackers" -- waiting to be pressed again under a
    // different name.
    await missingScan();
  }

  function renderGrid(container, items, emptyMessage) {
    container.replaceChildren(...(items.length ? items.map(card) : [empty(emptyMessage)]));
  }

  // The search pane is shared by the results grid and the artist page, which
  // want different layouts. Each caller states the one it needs, rather than
  // one of them clearing the class and the next inheriting whatever was left:
  // viewing an artist used to strip `grid` and never put it back, so every
  // later search rendered its covers as full-width squares.
  function searchPane(layout) {
    const pane = $('#search-results');
    pane.className = layout;
    return pane;
  }

  // Opening a release replaces the results you opened it from, so the way back
  // is kept: the actual nodes, not a note to re-run the search. Restoring them
  // brings back the scroll position, the selection ticks and the section you
  // were in, and costs no API calls.
  // `from` is the view the pane belongs to. Opening a request from the Requests
  // tab shows it in the search pane, so without this Back would restore
  // whatever the search pane happened to hold and leave you on the wrong tab --
  // or, if you had never searched, push nothing at all and leave no way back.
  function pushPane(label, from) {
    // Nothing was on screen before this one: a deep link, or the first paint.
    // There is no pane worth keeping and nowhere inside the app for Back to
    // go -- and an entry claiming this very address would be restored, empty,
    // the next time somebody came back to it.
    if (!leavingUrl) return;
    const pane = $('#search-results');
    state.paneStack.push({
      cls: pane.className,
      nodes: [...pane.childNodes],
      scroll: window.scrollY,
      // A pane borrowed from another tab is named after that tab. Calling it
      // "Results" would point at a search the user never ran.
      label: from && from !== 'search' ? viewLabel(from) : label || 'Back',
      view: from || state.view,
      // The address these nodes belong to, and the tab name that went with
      // them. Back is a history entry, so the way back to a pane has to be one
      // too -- otherwise the crumb and the browser's own button disagree about
      // where back is, which is the bug that made Back skip whole detours.
      url: leavingUrl,
      title: leavingTitle,
    });
    if (state.paneStack.length > 12) state.paneStack.shift();
  }

  /**
   * Put back the pane an address was left at, if its nodes are still held.
   *
   * Searches from the top down because one Back can cross several entries at
   * once: a crumb three deep is history.go(-3), and the browser reports the
   * landing address, not the steps.
   *
   * @param {string} target - The address being returned to.
   * @returns {boolean} False when nothing was kept for it -- after a reload,
   *   or going forward into a page that has since been dropped -- and the
   *   route should draw it again.
   */
  function restorePane(target) {
    for (let i = state.paneStack.length - 1; i >= 0; i -= 1) {
      const entry = state.paneStack[i];
      if (entry.url !== target) continue;
      state.paneStack.length = i;
      if (entry.view && entry.view !== state.view) setView(entry.view);
      const pane = $('#search-results');
      pane.className = entry.cls;
      pane.replaceChildren(...entry.nodes);
      window.scrollTo(0, entry.scroll);
      if (entry.title) document.title = entry.title;
      else setTitle();
      return true;
    }
    return false;
  }

  // Go back to the crumb at `index`. The last one is the plain Back; index 0 is
  // the crumb at the far left.
  //
  // These are history entries, so this is the browser's own Back rather than a
  // second way of moving. Restoring the nodes here and leaving history alone
  // was the whole bug: the address bar went on naming a page you were no
  // longer looking at, and the next Back stepped *forward* into it.
  function popPaneTo(index) {
    if (index < 0 || index >= state.paneStack.length) return;
    history.go(index - state.paneStack.length);
  }

  const popPane = () => popPaneTo(state.paneStack.length - 1);

  // What a tab is called, for the crumb that goes back to it.
  function viewLabel(view) {
    return navLabel(view) || 'Back';
  }

  // The name of a tab, without the step number or the count beside it.
  function navLabel(view) {
    const item = $(`.nav-item[data-view="${view}"]`);
    return (item?.querySelector('.nav-label') || item)?.textContent.trim() || '';
  }

  // A name for the pane being left behind, taken from what it is showing.
  function paneLabel() {
    const pane = $('#search-results');
    return pane.querySelector('.album-title')?.textContent
      || pane.querySelector('.artist-name')?.textContent
      || ($('#search-input').value.trim() ? `“${$('#search-input').value.trim()}”` : 'Results');
  }

  // Where you are and how you got here, each step clickable. A lone Back button
  // says there is a way out but not where it goes: two hops in, from a search
  // through an artist to a release, "Back" is a guess.
  // Never returns null. It used to, when nothing had been pushed yet, and
  // `replaceChildren(null, ...)` writes the string "null" into the page -- so
  // opening a request straight from the Requests tab, with no search behind it,
  // put the word "null" where the back button should have been.
  function breadcrumbs(current) {
    const trail = [];
    state.paneStack.forEach((entry, i) => {
      trail.push(
        el('button', { type: 'button', class: 'crumb', onclick: () => popPaneTo(i) }, entry.label),
        el('span', { class: 'crumb-sep' }, '›'),
      );
    });
    trail.push(el('span', { class: 'crumb current' }, current || ''));
    return el(
      'nav',
      { class: 'breadcrumbs', 'aria-label': 'Breadcrumb' },
      state.paneStack.length
        ? el('button', { type: 'button', class: 'ghost back-btn', onclick: popPane }, '← Back')
        : null,
      ...trail,
    );
  }

  // ---------------------------------------------------------------- search

  const SECTION_LABEL = { album: 'Albums', track: 'Tracks', artist: 'Artists' };

  /**
   * Fetch and draw one search. Called only by showSearch, which is called only
   * by the router -- the query comes from the address, never from the box, so
   * the results on screen and the address that names them cannot drift apart.
   *
   * @param {string} query - What to search Deezer for.
   */
  async function runSearch(query) {
    // Unfiltered results stack as sections, each holding its own grid; a single
    // kind is just a grid.
    const single = state.searchType !== 'all';
    const results = searchPane(single ? 'grid' : 'search-sections');
    results.replaceChildren(spinner('Searching Deezer'));
    try {
      const data = await api(`/api/search?q=${encodeURIComponent(query)}&type=${state.searchType}`);
      if (single) {
        // A wrapper rather than making the pane itself the grid: the select-all
        // bar has to sit above the covers, and a child of a CSS grid becomes a
        // cell in it.
        const grid = el('div', { class: 'grid' });
        results.className = 'search-sections';
        results.replaceChildren(grid);
        renderGrid(grid, data.results, 'Nothing found.');
        return;
      }

      const sections = Object.entries(data.sections || {}).filter(([, rows]) => rows.length);
      if (!sections.length) {
        results.replaceChildren(empty('No matches. Try fewer words, or just the artist.'));
        return;
      }
      results.replaceChildren(
        ...sections.flatMap(([kind, rows]) => [
          // The heading is the control. It used to be a label with an "Only
          // these" link stranded at the far right of the row -- a second thing
          // to find, a foot away from the thing it acts on, saying in two words
          // what the heading already names. Press "Albums (30)" and you get the
          // albums on their own.
          el(
            'h3',
            {
              class: 'section-head',
              role: 'button',
              tabindex: '0',
              title: `Show only ${(SECTION_LABEL[kind] || kind).toLowerCase()}`,
              onclick: () => selectSearchType(kind),
              onkeydown: (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  selectSearchType(kind);
                }
              },
            },
            el('span', { class: 'section-title' }, SECTION_LABEL[kind] || kind),
            el('span', { class: 'section-count' }, String(rows.length)),
            el('span', { class: 'section-go', 'aria-hidden': 'true' }),
          ),
          el('div', { class: 'grid' }, ...rows.map(card)),
        ]),
      );
    } catch (e) {
      results.replaceChildren(empty(e.message));
    }
  }

  // Narrowing to one kind is a different page of the same search, so it is an
  // address of its own: it survives a reload, and Back returns to the mixed
  // list you narrowed from rather than out of the app.
  function selectSearchType(type) {
    go(addr('/search', {
      q: $('#search-input').value.trim(),
      type: type === 'all' ? '' : type,
    }));
  }

  // ---------------------------------------------------------------- explore

  async function loadExplore() {
    const body = $('#explore-body');
    const filters = $('#explore-filters');
    // A channel is a page inside Browse, not a state the Channels grid happens
    // to be in, so the router hands it here the same way it hands over a tab.
    if (state.exploreChannel) {
      clearGenreFilter(filters);
      return loadChannel(state.exploreChannel);
    }
    body.replaceChildren(spinner('Loading'));

    try {
      if (state.exploreTab === 'channels') {
        clearGenreFilter(filters);
        const { channels } = await api('/api/explore/channels');
        if (!channels.length) {
          body.replaceChildren(empty('No channels returned. Deezer channels need a valid ARL.'));
          return;
        }
        const grid = el('div', { class: 'grid' });
        grid.append(
          ...channels.map((c) =>
            el(
              'div',
              { class: 'card', onclick: () => go(addr(`/browse/channel/${encodeURIComponent(c.slug)}`)) },
              el('div', {
                class: 'card-art',
                style: c.image ? `background-image:url('${c.image}')` : `background:${c.colour || 'var(--bg-input)'}`,
              }),
              el('div', { class: 'card-title' }, c.title),
            ),
          ),
        );
        body.replaceChildren(grid);
        return;
      }

      await renderGenreFilter(filters);
      if (state.exploreTab === 'charts') {
        const chart = await api(`/api/explore/charts?genre=${state.exploreGenre}`);
        body.replaceChildren();
        for (const [label, key] of [['Albums', 'albums'], ['Tracks', 'tracks'], ['Artists', 'artists']]) {
          if (!chart[key]?.length) continue;
          const grid = el('div', { class: 'grid' });
          grid.append(...chart[key].map(card));
          body.append(el('h2', { class: 'section-title' }, label), grid);
        }
        if (!body.children.length) body.replaceChildren(empty('Deezer has no chart for this selection.'));
      } else {
        const data = await api(`/api/explore/releases?genre=${state.exploreGenre}`);
        const grid = el('div', { class: 'grid' });
        renderGrid(grid, data.results, data.note || 'No new releases.');
        body.replaceChildren(
          ...[
            data.note ? el('p', { class: 'hint' }, data.note) : null,
            grid,
          ].filter(Boolean),
        );
      }
    } catch (e) {
      body.replaceChildren(empty(e.message));
    }
  }

  // Emptying the bar has to take the "already built" flag with it. Leaving
  // Charts for Channels wiped the chips, and coming back found the flag still
  // set, returned early, and drew no chips at all -- so the genre filter
  // disappeared for the rest of the session.
  function clearGenreFilter(container) {
    container.replaceChildren();
    delete container.dataset.loaded;
  }

  async function renderGenreFilter(container) {
    if (container.dataset.loaded) {
      $$('.chip', container).forEach((c) => c.classList.toggle('active', c.dataset.genre === state.exploreGenre));
      return;
    }
    const { genres } = await api('/api/explore/genres');
    container.dataset.loaded = '1';
    container.replaceChildren(
      ...[{ id: '0', title: 'All' }, ...genres].map((g) =>
        el(
          'button',
          {
            class: `chip ${state.exploreGenre === g.id ? 'active' : ''}`,
            'data-genre': g.id,
            // Which genre you are reading is where you are, so it is in the
            // address: a chart worth coming back to can be bookmarked, and
            // Back returns to the genre before it.
            onclick: () => go(addr(BROWSE_PATHS[state.exploreTab] || '/browse/charts',
                                   { genre: g.id === '0' ? '' : g.id })),
          },
          g.title,
        ),
      ),
    );
  }

  /** Draw one channel. Reached only through /browse/channel/<slug>. */
  async function loadChannel(slug) {
    const body = $('#explore-body');
    body.replaceChildren(spinner(`Loading ${slug}`));
    try {
      const channel = await api(`/api/explore/channel/${encodeURIComponent(slug)}`);
      if (state.exploreChannel === slug) setTitle(channel.title || slug);
      body.replaceChildren(
        el('div', { class: 'row toolbar' },
           el('button', { class: 'ghost', onclick: () => go(addr('/browse/channels')) }, '← Channels')),
        el('h2', { class: 'section-title' }, channel.title),
      );
      for (const section of channel.sections) {
        const grid = el('div', { class: 'grid' });
        grid.append(...section.items.map(card));
        body.append(
          el(
            'div',
            { class: 'row' },
            el('h2', { class: 'section-title' }, section.title || 'Selection'),
            section.id
              ? el(
                  'button',
                  {
                    class: 'ghost',
                    title: 'Send this module to the Scan tab',
                    onclick: () => sendToMissing(`https://www.deezer.com/en/channels/module/${section.id}`),
                  },
                  'Scan module',
                )
              : null,
          ),
          grid,
        );
      }
      if (!channel.sections.length) body.append(empty('Deezer sent nothing back for this channel.'));
    } catch (e) {
      body.replaceChildren(empty(e.message));
    }
  }

  async function openArtist(artistId) {
    // Read before the switch: an artist opened from Explore or Found has to
    // come back to Explore or Found, not to the search pane it borrowed.
    const from = state.view;
    setView('search');
    state.openArtistId = String(artistId);
    pushPane(paneLabel(), from);
    // Its own sections, each with an inner grid, so the pane itself is a plain
    // block here.
    const results = searchPane('artist-page');
    results.replaceChildren(spinner('Loading artist'));
    try {
      const artist = await api(`/api/artist/${artistId}`);
      if (state.openArtistId === String(artistId)) setTitle(artist.name || 'Artist');
      const total = artist.groups.reduce((n, g) => n + g.albums.length, 0);

      results.replaceChildren(
        breadcrumbs(artist.name || 'Artist'),
        el(
          'div',
          { class: 'artist-head' },
          artist.picture ? el('img', { class: 'artist-pic', src: artist.picture, alt: '' }) : null,
          el(
            'div',
            {},
            el('h2', { class: 'artist-name' }, artist.name || ''),
            el(
              'p',
              { class: 'hint' },
              [
                artist.fans ? `${Number(artist.fans).toLocaleString()} fans` : null,
                `${total} release${total === 1 ? '' : 's'}`,
                artist.groups.map((g) => `${g.albums.length} ${g.label.toLowerCase()}`).join(' · '),
              ].filter(Boolean).join(' — '),
            ),
            artist.url
              ? el('a', { class: 'linkbtn', href: artist.url, target: '_blank', rel: 'noopener' }, 'Open on Deezer')
              : null,
          ),
        ),
        // One section per release type, newest first inside each.
        ...artist.groups.flatMap((group) => [
          el(
            'h3',
            { class: 'section-title' },
            `${group.label} (${group.albums.length})`,
          ),
          el('div', { class: 'grid' }, ...group.albums.map(card)),
        ]),
      );
    } catch (e) {
      results.replaceChildren(...[breadcrumbs('Artist'), empty(e.message)].filter(Boolean));
    }
  }

  function sendToMissing(url) {
    const box = $('#missing-sources');
    box.value = box.value ? `${box.value.trim()}\n${url}` : url;
    go(addr('/scan'));
    toast('Added to the Scan tab. Nothing has touched a tracker yet.');
  }

  // ---------------------------------------------------------------- detail

  // A page, not a drawer. A release is the thing you are deciding about, and
  // deciding needs the tracklist, the credits and the tracker verdict side by
  // side rather than a 380px column you scroll through a slot at a time.
  async function openAlbum(albumId) {
    const from = state.view;
    setView('search');
    state.openAlbumId = String(albumId);
    pushPane(paneLabel(), from);
    const pane = searchPane('album-page');
    pane.replaceChildren(spinner('Loading album'));

    try {
      const album = await api(`/api/album/${albumId}`);
      state.album = album;
      if (state.openAlbumId === String(albumId)) setTitle(album.title || 'Album');
      const availability = album.availability;
      // Matched on title, because the public track ids and the private ones
      // do not always agree and the names are what the reader is looking at.
      const unplayable = new Set(
        ((availability && availability.unreadable) || []).map((t) => String(t).toLowerCase()),
      );
      const verdict = availability
        ? availability.uploadable
          ? el('span', { class: 'tag ok' }, 'All FLAC, all streamable')
          : el('span', { class: 'tag bad' }, availability.reason || 'Not uploadable')
        : el('span', { class: 'tag dim' }, album.availability_error || 'Availability needs an ARL');

      const artistLink = (id, name) =>
        id
          ? el('a', { href: artistHref(id), onclick: (e) => { e.preventDefault(); goArtist(id); } }, name)
          : el('span', {}, name);

      const featured = (album.contributors || []).filter((c) => c.id !== album.artist_id);
      const facts = [
        album.nb_tracks ? `${album.nb_tracks} tracks` : null,
        album.duration ? duration(album.duration) : null,
        album.release_date,
        album.record_type,
        (album.genres || []).join(', ') || null,
      ].filter(Boolean);

      pane.replaceChildren(
        breadcrumbs(album.title || 'Album'),
        el(
          'div',
          { class: 'album-head' },
          album.cover
            ? el('img', { class: 'album-art', src: album.cover, alt: '' })
            : el('div', { class: 'album-art' }),
          el(
            'div',
            { class: 'album-head-body' },
            album.explicit ? el('span', { class: 'tag dim' }, 'EXPLICIT') : null,
            el('h1', { class: 'album-title' }, album.title || ''),
            el('p', { class: 'album-artist' }, artistLink(album.artist_id, album.artist || '')),
            el('p', { class: 'album-facts' }, facts.join(' · ')),
            featured.length
              ? el(
                  'p',
                  { class: 'album-facts featured' },
                  'With ',
                  ...featured.flatMap((c, i) => [i ? el('span', {}, ', ') : null, artistLink(c.id, c.name)])
                    .filter(Boolean),
                )
              : null,
            el(
              'div',
              { class: 'row album-actions' },
              el('button', { class: 'primary', onclick: () => download(album.id) }, 'Download'),
              el('button', { onclick: () => downloadAndUpload(album.id, album) }, 'Download & upload'),
              el('button', { id: 'album-check-btn', onclick: (e) => checkAlbum(album, checkTrackers(), e.target) },
                'Check trackers'),
              el('span', { class: 'hint', id: 'album-check-when' }),
              album.url
                ? el('a', { class: 'linkbtn', href: album.url, target: '_blank', rel: 'noopener' }, 'Open on Deezer')
                : null,
            ),
            el('p', { class: 'album-verdict' }, verdict,
               availability ? el('span', { class: 'card-sub' }, ` ${availability.flac_count}/${availability.total} FLAC`) : null),
            el('p', { class: 'hint' },
               'Checking spends tracker budget. Nothing above this line has contacted a tracker.'),
            el('div', { id: 'album-check-body' }),
          ),
        ),
        el(
          'div',
          { class: 'table-scroll' },
          el(
            'table',
            { class: 'table tracklist-table' },
            el(
              'thead',
              {},
              el(
                'tr',
                {},
                el('th', { class: 'num-col' }, '#'),
                el('th', {}, 'Track'),
                el('th', {}, 'Featured artists'),
                el('th', { class: 'dur-col' }, 'Length'),
              ),
            ),
            el(
              'tbody',
              {},
              ...(album.tracks || []).map((tr) =>
                el(
                  'tr',
                  // A release is only as good as the tracks that will actually
                  // download. Saying "4 of 11" above the list and then showing
                  // eleven identical rows leaves the reader to work out which
                  // seven, from a list that gives them nothing to go on.
                  { class: unplayable.has((tr.title || '').toLowerCase()) ? 'track-missing' : '' },
                  el('td', { class: 'num-col' }, String(tr.number || '')),
                  el(
                    'td',
                    {},
                    el('span', { class: 'track-title' }, tr.title || ''),
                    tr.explicit ? el('span', { class: 'tag dim explicit-tag' }, 'E') : null,
                    unplayable.has((tr.title || '').toLowerCase())
                      ? el('span', { class: 'tag bad' }, 'not on Deezer yet')
                      : null,
                  ),
                  // The private records name the whole cast and carry their
                  // ids, so each credit goes to that artist. The public ones
                  // name only the headline act, so this falls back to the
                  // track artist without an ARL.
                  el(
                    'td',
                    { class: 'featured-col' },
                    ...featuredCredits(tr, album, artistLink),
                  ),
                  el('td', { class: 'dur-col' }, duration(tr.duration)),
                ),
              ),
            ),
          ),
        ),
      );

      // Whatever a previous check found, shown straight away and for free.
      showSavedCheck(album);
    } catch (e) {
      pane.replaceChildren(...[breadcrumbs('Album'), empty(e.message)].filter(Boolean));
    }
  }

  // Every credit on a track, comma separated, each one a link when Deezer gave
  // an id for it. A name without an id stays plain text rather than becoming a
  // link to nowhere.
  function featuredCredits(track, album, artistLink) {
    const people = track.featured || [];
    if (!people.length) {
      const fallback = track.artist && track.artist !== album.artist ? track.artist : '';
      return [fallback ? artistLink(track.artist_id, fallback) : '—'];
    }
    return people.flatMap((person, i) => [
      i ? el('span', {}, ', ') : null,
      person.id ? artistLink(person.id, person.name) : el('span', {}, person.name),
    ]).filter(Boolean);
  }

  // A check costs budget and its answer does not change minute to minute, so
  // the last one is shown on arrival. Asking again stays a deliberate press --
  // the button just stops pretending nothing is known.
  async function showSavedCheck(album) {
    let saved;
    try {
      ({ check: saved } = await api(`/api/album/${album.id}/check`));
    } catch {
      return;
    }
    if (!saved || !$('#album-check-body')) return;

    const button = $('#album-check-btn');
    if (button) button.textContent = 'Check again';
    const when = $('#album-check-when');
    if (when) when.textContent = `Last checked ${ago(saved.checked_at)}`;

    // Stored verdicts render through the same path as a live check, so the two
    // views cannot drift apart.
    if (saved.verdicts?.length) {
      renderAlbumCheck(album, { verdicts: saved.verdicts });
      return;
    }
    // Older entries kept only the summary.
    const parts = [
      saved.found_on?.length ? `already on ${saved.found_on.join(' and ')}` : '',
      saved.missing_from?.length ? `not on ${saved.missing_from.join(' and ')}` : '',
    ].filter(Boolean);
    $('#album-check-body').replaceChildren(
      el('p', { class: 'hint' }, parts.join(' · ') || 'Checked before, with no verdict recorded.'),
    );
  }

  // Rough age of a stored result. Precision past "days" is not a decision input.
  function ago(seconds) {
    if (!seconds) return 'at some point';
    const mins = Math.max(0, Math.round((Date.now() / 1000 - seconds) / 60));
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins} minute${mins === 1 ? '' : 's'} ago`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
    const days = Math.round(hours / 24);
    return `${days} day${days === 1 ? '' : 's'} ago`;
  }

  // Trackers to ask, from the pickers the rest of the app already uses.
  function checkTrackers() {
    const picked = [...state.missingTrackers];
    return picked.length ? picked : state.trackers.map((t) => t.code);
  }

  // ------------------------------------------------------ per-album check

  async function checkAlbum(album, trackers, button) {
    const target = $('#album-check-body');
    if (!target) return;
    if (button) button.disabled = true;
    target.replaceChildren(spinner(`Asking ${trackers.join(' and ')}`));

    try {
      const { job_id } = await api(`/api/album/${album.id}/check`, { method: 'POST', body: { trackers } });
      const log = el('div', { class: 'joblog' });
      target.replaceChildren(workingOn(`Asking ${trackers.join(' and ')}`, job_id), log);
      followJob(job_id, {
        onUpdate: (job) => {
          jobProgress(log, job);
          if (job.results.length) state.albumCheck = job.results[job.results.length - 1];
        },
        onDone: (job) => {
          if (button) button.disabled = false;
          refreshStatus();
          if (job.error) return target.replaceChildren(empty(job.error));
          renderAlbumCheck(album, state.albumCheck);
        },
      });
    } catch (e) {
      if (button) button.disabled = false;
      target.replaceChildren(empty(e.message));
    }
  }

  // Titled the way the tracker titles it: "Madonna – Bedtime Stories [1994
  // Album]". Every part is optional, and a missing one is left out rather than
  // leaving its separator behind -- an unknown artist used to render as
  // "— Bedtime Stories (1994)", which reads as though the dash were the name.
  function groupTitle(hit) {
    const bracket = [hit.year, hit.release_type].filter(Boolean).join(' ');
    return [
      [hit.artist, hit.name].filter(Boolean).join(' – '),
      bracket ? `[${bracket}]` : '',
    ].filter(Boolean).join(' ');
  }

  function renderAlbumCheck(album, check) {
    const target = $('#album-check-body');
    if (!target || !check) return;

    // Headline first: found or not. The groups it looked at are the evidence,
    // not the answer, so they live behind a disclosure.
    const blocks = check.verdicts.map((v) => {
      const verdictTag =
        v.status === 'missing' ? ['ok', 'not on tracker'] :
        v.status === 'found' ? ['dim', 'already there'] :
        ['warn', v.status];

      const head = el(
        'div',
        { class: 'row verdict-head' },
        el('strong', {}, v.tracker),
        el('span', { class: `tag ${verdictTag[0]}` }, verdictTag[1]),
        v.match
          ? el('a', { href: v.match.url, target: '_blank', rel: 'noopener' },
               groupTitle(v.match) || 'view group')
          : null,
        el('span', { class: 'card-sub' }, `${v.calls_used} call(s)`),
      );

      const inspected = v.inspected.length
        ? el(
            'details',
            { class: 'inspected' },
            el('summary', {}, `What it looked at (${v.inspected.length})`),
            el(
              'ul',
              { class: 'hitlist' },
              ...v.inspected.map((h) =>
                el(
                  'li',
                  { class: h.matched ? 'hit matched' : 'hit' },
                  el('a', { href: h.url, target: '_blank', rel: 'noopener' }, groupTitle(h)),
                  el('span', { class: 'card-sub' }, h.matched ? 'matched' : h.reason),
                ),
              ),
            ),
          )
        : v.error
          ? el('p', { class: 'hint' }, v.error)
          : null;

      return el('div', { class: 'verdict' }, head, inspected);
    });

    // Derived when absent, which it is for a check read back from storage.
    // Taking the missing field as "nothing to upload" told you every tracker
    // already had it while a verdict on the same screen said otherwise.
    const uploadable = check.uploadable_to
      || check.verdicts.filter((v) => v.status === 'missing').map((v) => v.tracker);
    target.replaceChildren(
      ...blocks,
      el(
        'div',
        { class: 'row' },
        uploadable.length
          ? el(
              'button',
              { class: 'primary', onclick: () => uploadAlbum(album, uploadable) },
              `Download & upload to ${uploadable.join(' + ')}`,
            )
          : el('span', { class: 'hint' }, 'Nothing to upload — every tracker checked already has it.'),
      ),
    );
  }

  async function uploadAlbum(album, trackers) {
    // The release has to exist on disk before there is anything to upload. If a
    // matching folder is already there, go straight to the upload; otherwise
    // queue the download and hand off to the Downloads tab.
    state.uploadTrackers = new Set(trackers);
    renderTrackerPickers();

    let folders = [];
    try {
      ({ folders } = await api('/api/folders'));
    } catch {
      folders = [];
    }

    const needle = `${album.artist} - ${album.title}`.toLowerCase();
    const existing = folders.find((f) => f.name.toLowerCase().startsWith(needle.slice(0, 40)));

    if (existing) {
      go(addr('/uploading'));
      startUpload(existing.path, trackers);
      return;
    }

    await download(album.id);
    go(addr('/downloading'));
    toast(`Downloading first. When it finishes, upload it from Uploading — ${trackers.join(' and ')} are preselected.`);
  }

  // ---------------------------------------------------------------- watchlists

  async function loadWatchlists() {
    const container = $('#watchlists');
    try {
      const { watchlists } = await api('/api/watchlists');
      state.watchlists = watchlists;
      if (!watchlists.length) {
        container.replaceChildren(el('p', { class: 'hint' }, 'No saved searches yet.'));
        return;
      }
      container.replaceChildren(
        ...watchlists.map((w) =>
          el(
            'div',
            { class: 'row watchrow' },
            el('strong', {}, w.name),
            el('span', { class: 'tag dim' }, w.kind_label),
            el('span', { class: 'card-sub' }, w.target),
            w.last_run ? el('span', { class: 'card-sub' }, `${w.last_count} last run`) : null,
            el('button', { onclick: () => runWatchlist(w) }, 'Run'),
            el('button', { class: 'ghost', onclick: () => deleteWatchlist(w.id) }, 'Delete'),
          ),
        ),
      );
    } catch (e) {
      container.replaceChildren(empty(e.message));
    }
  }

  async function saveWatchlist(event) {
    event.preventDefault();
    const name = $('#watchlist-name').value.trim();
    const kind = $('#watchlist-kind').value;
    const target = $('#watchlist-target').value.trim() || '0';
    try {
      await api('/api/watchlists', { method: 'POST', body: { name, kind, target } });
      $('#watchlist-name').value = '';
      $('#watchlist-target').value = '';
      loadWatchlists();
      toast('Saved', 'ok');
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  async function deleteWatchlist(id) {
    await api(`/api/watchlists/${id}`, { method: 'DELETE' });
    loadWatchlists();
  }

  async function runWatchlist(watch) {
    toast(`Running "${watch.name}"…`);
    try {
      const { results } = await api(`/api/watchlists/${watch.id}/run`, { method: 'POST' });
      if (!results.length) return toast('No albums returned', 'bad');
      const box = $('#missing-sources');
      const urls = results.map((a) => `https://www.deezer.com/album/${a.id}`);
      box.value = urls.join('\n');
      toast(`${results.length} album(s) loaded into the collect box. Still no tracker contacted.`, 'ok');
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  // ---------------------------------------------------------------- downloads

  async function download(albumId) {
    try {
      const result = await api('/api/download', { method: 'POST', body: { album_id: String(albumId) } });
      if (result.failed?.length) {
        toast(result.failed[0].error, 'bad');
        return null;
      }
      toast('Queued for download', 'ok');
      pollDownloads(true);
      return result.queued?.[0] || null;
    } catch (e) {
      toast(e.message, 'bad');
      return null;
    }
  }

  // Resolve once a download job stops moving, with the job itself, so the
  // caller knows where the files landed.
  function waitForDownload(jobId) {
    return new Promise((resolve) => {
      const tick = async () => {
        try {
          const { jobs } = await api('/api/downloads');
          const job = jobs.find((j) => j.id === jobId);
          if (!job) return resolve(null);
          if (job.status === 'done' || job.status === 'failed') return resolve(job);
        } catch {
          return resolve(null);
        }
        setTimeout(tick, 1500);
      };
      tick();
    });
  }

  // Download, wait for it, then upload what it produced. Resolves only when the
  // upload flow has finished, which is what lets a batch run one at a time.
  async function downloadAndUpload(albumId, item, { quiet = false } = {}) {
    const trackers = [...state.uploadTrackers];
    if (!trackers.length) return toast('Pick at least one tracker under Uploads', 'bad');

    const queued = await download(albumId);
    if (!queued) return;
    if (!quiet) {
      toast('Downloading. The upload starts by itself when it finishes.');
      go(addr('/downloading'));
    }

    const label = `${item?.artist || ''} - ${item?.title || ''}`.trim() || String(albumId);
    const job = await waitForDownload(queued.id);
    if (!job || job.status !== 'done' || !job.folder) {
      return toast(`${label}: ${job?.error || 'download did not finish'}`, 'bad');
    }
    go(addr('/uploading'));
    await startUpload(job.folder, trackers, albumId);
  }

  // Deleting a release is not undoable, so it asks first and names what it is
  // about to remove rather than saying "are you sure".
  async function deleteFolder(path, name, after) {
    if (!confirm(`Delete "${name}"?\n\n${path}\n\nThis removes the files from disk and cannot be undone.`)) {
      return;
    }
    try {
      await api('/api/folders/delete', { method: 'POST', body: { folder: path } });
      toast(`Deleted ${name}`, 'ok');
      await after?.();
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  async function pollDownloads(immediate = false) {
    if (state.pollers.has('downloads') && !immediate) return;
    const tick = async () => {
      try {
        const { jobs } = await api('/api/downloads');
        renderDownloads(jobs);
        const active = jobs.some((j) => ['queued', 'running'].includes(j.status));
        railCount('#dl-badge', jobs.filter((j) => ['queued', 'running'].includes(j.status)).length);
        if (active) {
          state.pollers.set('downloads', setTimeout(tick, 1000));
        } else {
          state.pollers.delete('downloads');
        }
      } catch {
        state.pollers.delete('downloads');
      }
    };
    clearTimeout(state.pollers.get('downloads'));
    tick();
  }

  function renderDownloads(jobs) {
    const list = $('#downloads-list');
    if (!jobs.length) {
      list.replaceChildren(empty('Nothing downloading. Add something from Search or Browse.'));
      return;
    }
    list.replaceChildren(
      ...jobs.map((job) => {
        const cls = job.status === 'done' ? 'done' : job.status === 'failed' ? 'failed' : '';
        return el(
          'div',
          { class: 'dl' },
          el('div', { class: 'dl-art', style: job.cover ? `background-image:url('${job.cover}')` : '' }),
          el(
            'div',
            {},
            el('div', { class: 'dl-title' }, `${job.artist} — ${job.title}`),
            el(
              'div',
              { class: 'dl-sub' },
              job.error || `${job.status} · ${job.done}/${job.total} tracks${job.folder ? ` · ${job.folder}` : ''}`,
            ),
            el('div', { class: 'bar' }, el('div', { class: `bar-fill ${cls}`, style: `width:${job.percent}%` })),
          ),
          el(
            'div',
            { class: 'row dl-actions' },
            // Running downloads are cancellable too, not just queued ones --
            // a 30-track album you picked by mistake should not have to finish.
            job.status === 'queued' || job.status === 'running'
              ? el('button', { class: 'ghost', onclick: () => cancelDownload(job.id) }, 'Cancel')
              : el('span', { class: `tag ${cls === 'done' ? 'ok' : cls === 'failed' ? 'bad' : 'dim'}` }, `${job.percent}%`),
            // Only once there is something on disk to remove.
            job.status === 'done' && job.folder
              ? el(
                  'button',
                  {
                    class: 'danger',
                    onclick: () => deleteFolder(job.folder, job.title || job.folder, () => pollDownloads(true)),
                  },
                  'Delete',
                )
              : null,
          ),
        );
      }),
    );
  }

  async function cancelDownload(jobId) {
    await api(`/api/downloads/${jobId}/cancel`, { method: 'POST' });
    pollDownloads(true);
  }

  // ---------------------------------------------------------------- jobs

  function followJob(jobId, { onUpdate, onDone, interval = 900 }) {
    let seen = 0;
    const tick = async () => {
      let job;
      try {
        job = await api(`/api/jobs/${jobId}?since=${seen}`);
      } catch (e) {
        toast(e.message, 'bad');
        return;
      }
      seen += job.results.length;
      onUpdate?.(job);
      if (job.status === 'running') setTimeout(tick, interval);
      else {
        clearJobCancel(jobId);
        onDone?.(job);
      }
    };
    tick();
  }

  // Anything that keeps working after you press it can be stopped. Scans and
  // checks spend tracker budget per album, so being unable to call one off
  // means watching it spend the rest.
  function jobCancel(jobId, label = 'Cancel') {
    const button = el(
      'button',
      {
        class: 'ghost job-cancel',
        'data-job': jobId,
        onclick: async (e) => {
          e.target.disabled = true;
          e.target.textContent = 'Stopping…';
          try {
            await api(`/api/jobs/${jobId}/cancel`, { method: 'POST' });
          } catch (err) {
            toast(err.message, 'bad');
            e.target.disabled = false;
            e.target.textContent = label;
          }
        },
      },
      label,
    );
    return button;
  }

  function clearJobCancel(jobId) {
    $$(`.job-cancel[data-job="${jobId}"]`).forEach((b) => b.remove());
  }

  // A spinner you can call off, for the moment between pressing and finishing.
  function workingOn(message, jobId) {
    return el('p', { class: 'empty' },
      el('span', { class: 'spinner' }), ` ${message} `, jobCancel(jobId));
  }

  function jobLine(job) {
    const p = job.progress || {};
    if (p.total) return `${p.phase || 'working'} ${p.current}/${p.total}${p.album ? ` · ${p.album}` : ''}`;
    return job.status;
  }

  // A running job's progress as a bar, with its line underneath.
  //
  // "working 3/25" is two numbers you have to read and divide. Everything long
  // running here reports the same shape -- phase, current, total -- so they all
  // get the same bar, the same one the downloads list and an upload already use.
  //
  // Rebuilt only when the box is not already a bar and a line: replacing the
  // children on every poll restarts the CSS transition, which makes the bar
  // twitch instead of advance.
  function jobProgress(box, job, extra = '') {
    if (!box) return;
    box.hidden = false;
    let bar = box.querySelector('.joblog-bar');
    if (!bar) {
      box.replaceChildren(
        el('div', { class: 'bar joblog-bar' }, el('div', { class: 'bar-fill' })),
        el('div', { class: 'joblog-line' }),
      );
      bar = box.querySelector('.joblog-bar');
    }
    const p = job.progress || {};
    const pct = p.total ? Math.min(100, Math.round((p.current / p.total) * 100)) : null;
    bar.hidden = pct === null;
    if (pct !== null) bar.firstElementChild.style.width = `${pct}%`;
    const line = box.querySelector('.joblog-line');
    line.textContent = [jobLine(job), extra].filter(Boolean).join('\n');
  }

  // The end of a job: the bar goes, the sentence stays.
  function jobFinished(box, text) {
    if (!box) return;
    box.hidden = false;
    box.replaceChildren(el('div', { class: 'joblog-line' }, text));
  }

  // ---------------------------------------------------------------- missing

  // One press. Expanding the sources and checking what comes out were two
  // buttons with a list of every album in between, all of it ticked, waiting
  // for you to press the second one -- a question with one sensible answer is
  // not worth asking. Expanding is free and checking is not, so the cost is
  // still stated, and both halves can be stopped while they run.
  async function missingScan() {
    const sources = $('#missing-sources').value.split('\n').map((s) => s.trim()).filter(Boolean);
    if (!sources.length) return toast('Add at least one URL', 'bad');

    const log = $('#missing-collect-log');
    log.hidden = false;
    log.textContent = 'Expanding sources…';
    $('#missing-scan').disabled = true;
    state.candidates = [];

    try {
      const { job_id } = await api('/api/missing/collect', {
        method: 'POST',
        body: { sources, skip_known: $('#missing-skip-known').checked },
      });
      // Beside the log rather than inside it: the log is written with
      // textContent, which would wipe a child button on the next tick.
      log.after(jobCancel(job_id, 'Stop'));
      followJob(job_id, {
        onUpdate: (job) => {
          state.candidates.push(...job.results);
          const events = job.events.filter((e) => e.event.startsWith('source')).slice(-4);
          jobProgress(log, job,
            events.map((e) => `${e.source}: ${e.error || `${e.albums} albums`}`).join('\n'));
        },
        onDone: (job) => {
          if (job.error) {
            $('#missing-scan').disabled = false;
            return toast(job.error, 'bad');
          }
          jobFinished(log, `${state.candidates.length} album(s) after the Deezer-side filters.`);
          // Seeded once, here. Deriving it inside the render meant unticking
          // "select all" emptied the set and the next render immediately
          // refilled it, so the control appeared to do nothing.
          state.selectedCandidates = new Set(state.candidates.map((c) => c.album_id));
          renderCandidates();
          if (!state.candidates.length) {
            $('#missing-scan').disabled = false;
            return;
          }
          // Straight on to the trackers, which is what you pressed Scan for.
          missingCheck({ all: true }).finally(() => ($('#missing-scan').disabled = false));
        },
      });
    } catch (e) {
      $('#missing-scan').disabled = false;
      toast(e.message, 'bad');
    }
  }

  // Shift-click selects the run between the row you last touched and this one,
  // taking this row's new state -- so shift-click extends a selection and
  // shift-unclick clears a stretch of it. Ticking fifty rows one at a time is
  // not a decision fifty times over.
  //
  // Bound to click rather than change: change is not a mouse event and carries
  // no shiftKey. The anchor lives out here because every tick re-renders the
  // table, which would otherwise reset it on each use.
  function rangeSelector(idsFn, selectedFn, rerender) {
    let anchor = null;
    return (id, event) => {
      const ids = idsFn();
      // Read the Set each time: these are reassigned wholesale when a scan or a
      // load seeds a fresh selection, so a captured reference would end up
      // writing into a Set nothing renders from.
      const selected = selectedFn();
      const index = ids.indexOf(id);
      const checked = event.target.checked;

      if (event.shiftKey && anchor !== null && anchor !== -1 && index !== -1 && anchor !== index) {
        const [from, to] = anchor < index ? [anchor, index] : [index, anchor];
        for (let i = from; i <= to; i++) {
          checked ? selected.add(ids[i]) : selected.delete(ids[i]);
        }
        // Shift-clicking inside a table also drags a text selection across it.
        document.getSelection()?.removeAllRanges();
      } else {
        checked ? selected.add(id) : selected.delete(id);
      }

      anchor = index;
      rerender();
    };
  }

  const pickCandidate = rangeSelector(
    () => state.candidates.map((c) => c.album_id), () => state.selectedCandidates, () => renderCandidates());
  const pickRequest = rangeSelector(
    () => state.requestRows.map((r) => r.id), () => state.selectedRequests, () => renderRequestRows());
  const pickFound = rangeSelector(
    () => state.found.map((f) => f.id), () => state.selectedFound, () => renderFound());

  // The tick in a table header, with the third state that makes it honest: all,
  // none, or a dash for some. It reports what the rows say rather than being a
  // switch with its own opinion, so it cannot claim everything is selected when
  // it is not.
  function selectAllBox(allIds, selected, onChange) {
    const box = el('input', {
      type: 'checkbox',
      title: 'Select all',
      onchange: (e) => {
        selected.clear();
        if (e.target.checked) allIds.forEach((id) => selected.add(id));
        onChange();
      },
    });
    box.checked = allIds.length > 0 && selected.size === allIds.length;
    box.indeterminate = selected.size > 0 && selected.size < allIds.length;
    return box;
  }

  function renderCandidates() {
    const panel = $('#missing-candidates-panel');
    panel.hidden = state.candidates.length === 0;
    if (!state.candidates.length) return;

    const trackers = [...state.missingTrackers];
    $('#missing-cost').textContent =
      `${state.candidates.length} album(s) to check: about ${state.candidates.length * 3} call(s) ` +
      `per tracker on ${trackers.join(', ') || 'no tracker'}. The scan stops rather than overdraw a budget.`;

    const table = $('#missing-table');
    table.replaceChildren(
      el(
        'thead',
        {},
        el(
          'tr',
          {},
          el('th', {}, selectAllBox(
            state.candidates.map((c) => c.album_id),
            state.selectedCandidates,
            () => renderCandidates(),
          )),
          el('th', {}, 'Album'), el('th', {}, 'Year'), el('th', {}, 'Tracks'),
          el('th', {}, 'Source'), el('th', {}, 'Result'),
        ),
      ),
      el(
        'tbody',
        {},
        ...state.candidates.map((c) =>
          el(
            'tr',
            { 'data-album': c.album_id },
            el(
              'td',
              {},
              el('input', {
                type: 'checkbox',
                checked: state.selectedCandidates.has(c.album_id),
                onclick: (e) => pickCandidate(c.album_id, e),
              }),
            ),
            el(
              'td',
              {},
              el('a', {
                href: albumHref(c.album_id),
                onclick: (e) => { e.preventDefault(); goAlbum(c.album_id); },
              }, `${c.artist} — ${c.title}`),
            ),
            el('td', {}, c.year || ''),
            el('td', {}, String(c.tracks)),
            el('td', { class: 'card-sub' }, c.source || ''),
            el('td', { class: 'result' }, el('span', { class: 'tag dim' }, 'not checked')),
          ),
        ),
      ),
    );
  }

  // ``all`` checks everything that was just collected; without it the ticked
  // rows are checked, which is what "Check again" on the results is for.
  async function missingCheck({ all = false } = {}) {
    const trackers = [...state.missingTrackers];
    if (!trackers.length) return toast('Pick at least one tracker', 'bad');
    const candidates = all
      ? state.candidates
      : state.candidates.filter((c) => state.selectedCandidates.has(c.album_id));
    if (!candidates.length) return toast('Nothing to check', 'bad');

    const log = $('#missing-check-log');
    log.hidden = false;
    log.textContent = 'Starting…';
    $('#missing-check').disabled = true;

    try {
      const { job_id } = await api('/api/missing/check', { method: 'POST', body: { candidates, trackers } });
      log.after(jobCancel(job_id, 'Stop checking'));
      // Resolves when the check finishes, so a scan knows when it is over.
      await new Promise((resolve) => {
        followJob(job_id, {
          onUpdate: (job) => {
            jobProgress(log, job);
            job.results.forEach(applyScanResult);
          },
          onDone: (job) => {
            $('#missing-check').disabled = false;
            refreshStatus();
            const stopped = job.events.find((e) => e.event === 'budget_exhausted');
            jobFinished(log, stopped
              ? `Stopped early to protect the budget: ${stopped.checked} checked, ${stopped.remaining ?? '?'} left. Run again when the window rolls over.`
              : `Done. ${job.result_count} album(s) checked.`);
            if (job.error) toast(job.error, 'bad');
            resolve();
          },
        });
      });
    } catch (e) {
      $('#missing-check').disabled = false;
      toast(e.message, 'bad');
    }
  }

  function applyScanResult(result) {
    const row = $(`#missing-table tr[data-album="${result.album_id}"] .result`);
    if (!row) return;
    const parts = [];
    result.missing_from.forEach((t) => parts.push(el('span', { class: 'tag ok' }, `missing on ${t}`)));
    result.found_on.forEach((t) => parts.push(el('span', { class: 'tag dim' }, `on ${t}`)));
    Object.entries(result.errors || {}).forEach(([t, e]) => parts.push(el('span', { class: 'tag warn', title: e }, `${t} error`)));
    row.replaceChildren(...(parts.length ? parts : [el('span', { class: 'tag dim' }, 'no result')]));
  }

  // ---------------------------------------------------------------- requests

  // A labelled field with optional help, matching the settings page.
  function filterField(id, label, control, help) {
    return el(
      'div',
      { class: 'setting' },
      el('label', { for: id }, label),
      control,
      help ? el('p', { class: 'hint setting-help' }, help) : null,
    );
  }

  // One row of the search form: a label on the left, controls on the right.
  // The trackers lay their own form out this way and it is the right shape --
  // fifteen release types read as a paragraph of options, not as a column you
  // have to scroll.
  function formRow(label, ...controls) {
    return el('div', { class: 'reqrow' },
      el('div', { class: 'reqlabel' }, label),
      el('div', { class: 'reqfield' }, ...controls.filter(Boolean)));
  }

  // A group of ticks, with the All and the "Only specified" the site puts
  // above it.
  //
  // All is not a filter of its own -- it is a shortcut that ticks the rest,
  // which is what it does on both sites. It also follows along: tick every
  // option by hand and All shows itself ticked, because a box that says "all"
  // while all of them are on and it is not is simply wrong. Some groups have
  // no All at all: RED's categories are seven bare boxes, because leaving them
  // clear is how you ask RED for every category.
  function checkGroup(id, options, { withAll = true, checked = [], strictId = '' } = {}) {
    // `checked` is the tracker's own default selection, not a blanket on/off:
    // the form opens on the search almost everyone runs rather than on every
    // box ticked, which matched nothing useful and had to be undone by hand.
    const on = new Set(checked);
    const boxes = el('div', { class: 'reqchecks', id },
      ...options.map((name) =>
        el('label', { class: 'check' },
          el('input', {
            type: 'checkbox',
            value: name,
            checked: on.has(name),
            onchange: () => { syncAll(id); requestsCost(); },
          }),
          name)));

    const head = [];
    if (withAll) {
      head.push(el('label', { class: 'check' },
        el('input', {
          type: 'checkbox',
          id: `${id}-all`,
          checked: options.length > 0 && options.every((name) => on.has(name)),
          onchange: (e) => {
            $$(`#${id} input`).forEach((box) => { box.checked = e.target.checked; });
            requestsCost();
          },
        }),
        'All'));
    }
    if (strictId) {
      head.push(el('label', { class: 'check', title: 'Exclude requests that leave this open to anything' },
        el('input', { type: 'checkbox', id: strictId }), 'Only specified'));
    }

    return el('div', { class: 'reqgroup' },
      head.length ? el('div', { class: 'reqgroup-head' }, ...head) : null,
      boxes);
  }

  /** Keep a group's All in step with the ticks under it. */
  function syncAll(id) {
    const all = $(`#${id}-all`);
    if (!all) return;
    const boxes = [...$$(`#${id} input`)];
    all.checked = boxes.length > 0 && boxes.every((b) => b.checked);
  }

  const chosen = (id) => [...$$(`#${id} input:checked`)].map((i) => i.value);

  // Rebuilt whenever the tracker changes. The tracker describes its own form
  // -- which groups, in what order, called what, ticked or not -- because the
  // two sites disagree about all four and the page should look like whichever
  // one is selected.
  async function loadRequestFilters() {
    const host = $('#requests-filters');
    if (!state.requestsTracker) {
      host.replaceChildren(empty('No tracker set up yet. Add one in Settings to see requests.'));
      return;
    }
    let spec;
    try {
      spec = await api(`/api/requests/filters?tracker=${encodeURIComponent(state.requestsTracker)}`);
    } catch (e) {
      host.replaceChildren(empty(e.message));
      return;
    }
    state.requestFilters = spec;
    if (typeof spec.recheck_after_days === 'number') state.recheckDays = spec.recheck_after_days;

    const GROUP_IDS = {
      categories: 'requests-category',
      release_types: 'requests-release-type',
      formats: 'requests-format',
      encodings: 'requests-encoding',
      media: 'requests-media',
    };
    const STRICT_IDS = {
      'strict-format': 'requests-strict-format',
      'strict-encoding': 'requests-strict-encoding',
      'strict-media': 'requests-strict-media',
    };

    const rows = (spec.form || []).map((item) => {
      if (item.kind === 'search') {
        return formRow('Search terms',
          el('input', { type: 'search', id: 'requests-search', placeholder: 'Artist, album or both' }),
          spec.descriptions
            ? el('label', { class: 'check', title: 'Only affects what the search text above matches' },
                el('input', { type: 'checkbox', id: 'requests-descriptions' }),
                'Include desc/comments')
            : null);
      }
      if (item.kind === 'tags') {
        return formRow('Tags',
          el('input', { type: 'search', id: 'requests-tags', placeholder: 'hip.hop, jazz' }),
          // Two radios, the way the form has it.
          el('label', { class: 'check' },
            el('input', { type: 'radio', name: 'requests-tags-mode', value: 'any', checked: true }), 'Any'),
          el('label', { class: 'check' },
            el('input', { type: 'radio', name: 'requests-tags-mode', value: 'all' }), 'All'));
      }
      if (item.kind === 'toggle') {
        return formRow(item.label,
          el('input', {
            type: 'checkbox',
            id: `requests-${item.key.replace(/_/g, '-')}`,
            checked: !!item.default,
          }));
      }
      if (item.kind === 'bounty') {
        return formRow(item.label,
          el('input', { type: 'text', id: 'requests-bounty-min', class: 'reqsmall', placeholder: 'min' }),
          el('input', { type: 'text', id: 'requests-bounty-max', class: 'reqsmall', placeholder: 'max' }),
          el('span', { class: 'hint reqhint' }, 'add M or T for MiB or TiB'));
      }
      if (item.kind === 'group' && item.options.length) {
        return formRow(item.label, checkGroup(GROUP_IDS[item.key], item.options, {
          withAll: item.all,
          checked: item.checked || [],
          strictId: STRICT_IDS[item.strict] || '',
        }));
      }
      return null;
    }).filter(Boolean);

    // The same setting as Settings > Queue, offered where it bites. Deciding
    // how long an answer is good for is part of setting up a search, and
    // sending someone to another page to change it and back again is how a
    // setting ends up never being changed.
    rows.push(formRow('Look up again if checked more than',
      ...durationControl({
        id: 'requests-recheck',
        days: state.recheckDays,
        never: true,
        onChange: async (days) => {
          state.recheckDays = days;
          try {
            await api('/api/settings', {
              method: 'PUT',
              body: { changes: { 'checker.request_recheck_after_days': String(days) } },
            });
          } catch (err) {
            toast(err.message, 'bad');
          }
        },
      }),
      el('span', { class: 'hint reqhint' }, 'ago')));

    rows.push(formRow('Pages to fetch',
      // Pages, not a row count. One page is one call against the budget.
      el('input', { id: 'requests-limit', type: 'number', class: 'reqsmall', min: '1', step: '1',
                    value: '4', oninput: requestsCost }),
      el('span', { class: 'hint reqhint' },
         `${state.requestsTracker} serves ${spec.page_size} per page — one call each`)));

    host.replaceChildren(el('div', { class: 'reqform' }, ...rows));
    if (!spec.mapped && spec.note) {
      host.append(el('p', { class: 'hint setting-help filter-note' }, spec.note));
    }
    for (const id of ['requests-search', 'requests-tags']) {
      $(`#${id}`).addEventListener('keydown', (e) => e.key === 'Enter' && requestsFetch());
    }
    state.requestFiltersFor = state.requestsTracker;
    requestsCost();
  }

  // The running cost used to be printed beside the button and again under it,
  // on every visit, whether or not anything was about to be spent. The budget
  // is on screen permanently in the sidebar, so this now speaks only when
  // asking for more pages than the tracker has calls left -- the one case the
  // sidebar cannot tell you about, because it is about what you just typed.
  function requestsCost() {
    const limitEl = $('#requests-limit');
    const cost = $('#requests-cost');
    if (!limitEl || !cost) return;
    const pages = Number(limitEl.value) || 1;
    const budget = state.trackers.find((t) => t.code === state.requestsTracker);
    const over = budget && pages > budget.remaining;
    cost.classList.toggle('cost-over', !!over);
    cost.textContent = over
      ? `Only ${budget.remaining} call${budget.remaining === 1 ? '' : 's'} left on ${budget.code} — the search will stop when they run out.`
      : '';
  }

  // Durations are a number and a unit.
  //
  // They were a dropdown of seven guesses -- a day, a week, a month, three
  // months, a year -- which is fine until someone wants two months or three
  // years, and then there is nothing to pick. Everything is stored as days;
  // this is only how it is typed and read back.
  const UNITS = [['days', 1], ['weeks', 7], ['months', 30], ['years', 365]];

  /** Days as the largest whole unit that fits, so 30 reads back as 1 month. */
  function daysToParts(days) {
    const total = Number(days) || 0;
    if (total <= 0) return { amount: 0, unit: 'days' };
    for (let i = UNITS.length - 1; i >= 0; i--) {
      const [unit, size] = UNITS[i];
      if (total % size === 0) return { amount: total / size, unit };
    }
    return { amount: total, unit: 'days' };
  }

  function partsToDays(amount, unit) {
    const size = (UNITS.find(([name]) => name === unit) || UNITS[0])[1];
    return Math.max(0, Math.round(Number(amount) || 0)) * size;
  }

  /**
   * A number box and a unit picker.
   *
   * @param {object} opts - `id` prefixes both controls, `days` is the current
   *   value, `onChange` receives the new value in days, and `never` adds a
   *   unit that means "no limit" rather than making zero mean it silently.
   * @returns {Array} The two elements, to spread into a row.
   */
  function durationControl({ id, days, onChange, never = false }) {
    const { amount, unit } = daysToParts(days);
    const isNever = never && (Number(days) || 0) <= 0;

    const amountBox = el('input', {
      id: `${id}-amount`,
      class: 'reqsmall',
      type: 'number',
      min: '1',
      step: '1',
      value: String(isNever ? '' : amount || 1),
      disabled: isNever,
      onchange: () => { pluralise(); emit(); },
      oninput: pluralise,
    });

    const unitBox = el('select', {
      id: `${id}-unit`,
      onchange: () => {
        amountBox.disabled = unitBox.value === 'never';
        emit();
      },
    },
    ...UNITS.map(([name]) =>
      el('option', { value: name, selected: !isNever && name === unit }, name)),
    never ? el('option', { value: 'never', selected: isNever }, 'never') : null);

    // "1 months" is the sort of thing that makes a page look unfinished.
    function pluralise() {
      const one = Number(amountBox.value) === 1;
      for (const option of unitBox.options) {
        if (option.value === 'never') continue;
        option.textContent = one ? option.value.slice(0, -1) : option.value;
      }
    }

    function emit() {
      onChange(unitBox.value === 'never' ? 0 : partsToDays(amountBox.value, unitBox.value));
    }

    pluralise();

    return [amountBox, unitBox];
  }

  const ticked = (id) => !!$(`#${id}`)?.checked;

  // The search bar and its Cancel. Both are hidden until something is running,
  // because a progress bar sitting at zero with nothing behind it is furniture.
  function requestsProgress(done, total, label) {
    const box = $('#requests-progress');
    const bar = $('#requests-progress-bar');
    if (!box || !bar) return;
    box.hidden = false;
    $('#requests-cancel').hidden = false;
    $('#requests-fetch').disabled = true;
    $('#requests-fetch-check').disabled = true;
    bar.style.width = `${total ? Math.round((done / total) * 100) : 0}%`;
    $('#requests-progress-label').textContent = label;
  }

  // How much of the search is on screen, and how much there was.
  //
  // This was a toast: it said "25 requests from 1 of 871 pages -- about 21,775
  // match in total" and then vanished, which for the one number that decides
  // whether to fetch more pages is the wrong place to put it. It stays until
  // the next search replaces it.
  function requestsSummary({ shown, pages, totalPages, estimate, filtered, cancelled, running }) {
    const host = $('#requests-summary');
    if (!host) return;
    if (shown === null) { host.hidden = true; host.replaceChildren(); return; }

    const partial = totalPages > pages;
    const parts = [];
    parts.push(partial
      ? `Showing ${shown.toLocaleString()} of about ${estimate.toLocaleString()} matching requests.`
      : `Showing all ${shown.toLocaleString()} matching request${shown === 1 ? '' : 's'}.`);
    if (totalPages) {
      parts.push(`Read ${pages.toLocaleString()} of ${totalPages.toLocaleString()} page${totalPages === 1 ? '' : 's'}.`);
    }
    if (filtered) parts.push(`${filtered} already filled, left out.`);
    if (cancelled) parts.push('Stopped early.');
    if (running) parts.push('Still reading…');

    host.hidden = false;
    host.className = `requests-summary${partial ? ' partial' : ''}`;
    host.replaceChildren(
      el('span', {}, parts.join(' ')),
      // The number is only useful next to the thing that acts on it.
      partial && !running
        ? el('button', {
            class: 'ghost',
            onclick: () => {
              const box = $('#requests-limit');
              if (box) { box.value = String(Math.min(totalPages, pages * 2)); box.focus(); requestsCost(); }
            },
          }, 'Read more pages')
        : null);
  }

  function requestsProgressDone() {
    const box = $('#requests-progress');
    if (box) box.hidden = true;
    const cancel = $('#requests-cancel');
    if (cancel) cancel.hidden = true;
    for (const id of ['requests-fetch', 'requests-fetch-check']) {
      const button = $(`#${id}`);
      if (button) button.disabled = false;
    }
    state.requestsAbort = null;
  }

  /** Everything the form is asking for, minus which page. */
  function requestsParams() {
    const params = new URLSearchParams({
      tracker: state.requestsTracker,
      search: $('#requests-search').value,
      tags: $('#requests-tags').value,
      tags_all: $('input[name="requests-tags-mode"]:checked')?.value === 'all' ? '1' : '0',
      show_filled: ticked('requests-show-filled') ? '1' : '0',
      strict_format: ticked('requests-strict-format') ? '1' : '0',
      strict_media: ticked('requests-strict-media') ? '1' : '0',
      strict_encoding: ticked('requests-strict-encoding') ? '1' : '0',
      include_old: ticked('requests-include-old') ? '1' : '0',
      descriptions: ticked('requests-descriptions') ? '1' : '0',
      bounty_min: $('#requests-bounty-min')?.value || '',
      bounty_max: $('#requests-bounty-max')?.value || '',
    });
    // Repeated keys, one per ticked box.
    for (const [key, id] of [
      ['format', 'requests-format'],
      ['media', 'requests-media'],
      ['encoding', 'requests-encoding'],
      ['release_type', 'requests-release-type'],
      ['category', 'requests-category'],
    ]) {
      for (const value of chosen(id)) params.append(key, value);
    }
    return params;
  }

  // One page per call, rather than one call for the lot.
  //
  // The page count is a budget the user typed, and a fetch of it used to be a
  // single request that could not be watched or stopped: ask for forty pages
  // and the only options were to wait for all forty or reload the page, having
  // spent forty tracker calls either way. Now each page is its own call, the
  // bar moves as they land, results appear as they arrive, and Cancel stops
  // before the next one is paid for.
  async function requestsFetch({ thenCheck = false } = {}) {
    if (!state.requestsTracker) return toast('No tracker configured', 'bad');
    if (state.requestsAbort) return;

    const pages = Number($('#requests-limit')?.value) || 1;
    const pageSize = state.requestFilters?.page_size || 25;
    const params = requestsParams();
    const container = $('#requests-results');
    const tracker = state.requestsTracker;

    // Only a run that was going to look things up looks things up. The box
    // below decides *when* -- it used to decide *whether*, which meant ticking
    // it turned "show me the list" into a run that spent budget on every row.
    const pipeline = thenCheck && ticked('requests-pipeline');

    const abort = new AbortController();
    state.requestsAbort = abort;
    let cancelled = false;
    abort.signal.addEventListener('abort', () => {
      cancelled = true;
      // Stop the lookup this search started, not just the pages. Leaving it
      // running after Cancel is what "cancel" is supposed to prevent.
      state.checkCancelButton?.click();
    });

    const rows = [];
    const seen = new Set();
    let calls = 0;
    let filtered = 0;
    let totalPages = 0;
    let estimate = 0;
    let ranDry = false;

    // --- the pipelined half ------------------------------------------------
    // Reading a page is a tracker call with a pause either side of it; looking
    // a request up is a tracker call, a Deezer search and some matching. Doing
    // all the reading and then all the looking up means the second half starts
    // when the first is completely finished, and on a twenty-page search that
    // is a long time to watch a list of rows that say "not checked".
    //
    // So each page's requests go off to be looked up as soon as that page
    // lands. Chained rather than parallel: two check jobs at once race the
    // budget guard, and the tracker end is serialised by the gateway anyway,
    // so the gain is in overlapping the Deezer half with the next page's wait.
    const log = $('#requests-log');
    let chain = Promise.resolve();
    let pipelineChecked = 0;
    const pipelineSkipped = [];
    let pipelineWindow = 0;

    if (pipeline) {
      log.hidden = false;
      log.textContent = 'Starting…';
      $$('.requests-check-btn').forEach((b) => { b.disabled = true; });
    }

    const lookUpLater = (ids) => {
      if (!ids.length) return;
      chain = chain
        .then(async () => {
          if (cancelled) return;
          const job = await runCheckJob(tracker, ids, {
            onSkipped: (note) => {
              // Collected rather than shown per page: twenty pages would be
              // twenty panels saying the same thing.
              pipelineSkipped.push(...(note.requests || []));
              pipelineWindow = note.recheck_after_days;
            },
          });
          pipelineChecked += job.result_count || 0;
        })
        .catch((e) => { if (!cancelled) toast(e.message, 'bad'); });
    };

    // --- reading the pages -------------------------------------------------
    container.replaceChildren(spinner(`Reading page 1 of ${pages}`));
    requestsProgress(0, pages, `Page 1 of ${pages}`);
    requestsSummary({ shown: null });

    try {
      for (let page = 1; page <= pages; page++) {
        if (cancelled) break;
        requestsProgress(page - 1, pages, `Page ${page} of ${pages}`);
        const query = new URLSearchParams(params);
        query.set('limit', String(pageSize));
        query.set('start_page', String(page));

        let data;
        try {
          data = await api(`/api/requests/list?${query}`, { signal: abort.signal });
        } catch (e) {
          if (cancelled || e.name === 'AbortError') break;
          throw e;
        }

        calls += data.calls || 0;
        filtered += data.filtered || 0;
        totalPages = data.pages || totalPages;
        estimate = data.total_estimate || estimate;

        const fresh = [];
        for (const row of data.requests || []) {
          if (seen.has(row.id)) continue;
          seen.add(row.id);
          rows.push(row);
          fresh.push(row.id);
        }

        // Show what has landed rather than making the whole thing wait.
        state.requestRows = rows;
        state.selectedRequests = new Set(rows.map((r) => r.id));
        renderRequestRows();
        requestsProgress(page, pages, `Page ${page} of ${pages} — ${rows.length} so far`);
        requestsSummary({
          shown: rows.length, pages: page, totalPages, estimate, filtered,
          cancelled: false, running: true,
        });
        if (pipeline) lookUpLater(fresh);

        // The tracker has no more to give: stop rather than paying for pages
        // of nothing. An empty page that only dropped filled rows is not the
        // end, so both have to be zero.
        if (!(data.requests || []).length && !data.filtered) { ranDry = true; break; }
        if (totalPages && page >= totalPages) { ranDry = true; break; }
      }
    } catch (e) {
      requestsProgressDone();
      container.replaceChildren(empty(e.message));
      requestsSummary({ shown: null });
      return;
    }

    requestsProgressDone();
    if (!rows.length && cancelled) {
      container.replaceChildren(empty('Search cancelled.'));
      requestsSummary({ shown: null });
      refreshStatus();
      return;
    }

    // How much of the search this is. A four-page read of a search the tracker
    // answers with four hundred pages used to report "100 requests" and look
    // like the whole result, so a search matching ten thousand and one
    // matching a hundred were indistinguishable.
    const read = ranDry ? calls : Math.min(calls, totalPages || calls);
    requestsSummary({
      shown: rows.length,
      pages: read,
      totalPages: ranDry ? read : totalPages,
      estimate,
      filtered,
      cancelled,
      running: false,
    });
    refreshStatus();

    if (pipeline) {
      await chain;
      if (pipelineSkipped.length) {
        showSkipped(tracker, {
          count: pipelineSkipped.length,
          requests: pipelineSkipped,
          recheck_after_days: pipelineWindow,
        });
      }
      $$('.requests-check-btn').forEach((b) => { b.disabled = false; });
      jobFinished(log, `Done. ${pipelineChecked} request(s) checked.`);
      refreshStatus();
      return;
    }

    if (!cancelled && rows.length && thenCheck) {
      await requestsCheck(rows.map((r) => r.id));
    }
  }

  function renderRequestRows() {
    const container = $('#requests-results');
    if (!state.requestRows.length) {
      container.replaceChildren(empty('Nothing matched. Widen the filters, or fetch more pages.'));
      return;
    }
    container.replaceChildren(
      // The action belongs with the thing it acts on. This used to sit above
      // the paste box, pressable with an empty list behind it.
      el(
        'div',
        { class: 'row requests-actions' },
        el('button', {
          class: 'primary requests-check-btn',
          disabled: state.selectedRequests.size === 0,
          onclick: () => requestsCheck([...state.selectedRequests]),
        }, `Check ${state.selectedRequests.size} selected`),
        el('span', { class: 'hint' },
           `${state.selectedRequests.size} of ${state.requestRows.length} selected — one call each.`),
      ),
      el(
        'table',
        { class: 'table' },
        el('thead', {}, el(
          'tr',
          {},
          el('th', {}, selectAllBox(
            state.requestRows.map((r) => r.id),
            state.selectedRequests,
            () => renderRequestRows(),
          )),
          el('th', {}, 'Request'), el('th', {}, 'Year'), el('th', {}, 'Bounty'),
          el('th', {}, 'Age'), el('th', {}, 'Filled'), el('th', {}, 'Result'),
        )),
        el(
          'tbody',
          {},
          ...state.requestRows.map((r) =>
            el(
              'tr',
              { 'data-request': r.id },
              el(
                'td',
                {},
                el('input', {
                  type: 'checkbox',
                  checked: state.selectedRequests.has(r.id),
                  onclick: (e) => pickRequest(r.id, e),
                }),
              ),
              el('td', {}, el('a', {
                class: 'rowlink',
                href: r.url,
                title: 'Open the request beside the Deezer release',
                onclick: (e) => {
                  e.preventDefault();
                  go(addr(`/requests/${encodeURIComponent(state.requestsTracker || '')}`
                          + `/${encodeURIComponent(r.id)}`));
                },
              }, `${r.artist} — ${r.title}`)),
              el('td', {}, r.year || ''),
              el('td', {}, r.bounty || ''),
              // How long it has sat open. A request from 2019 that nothing has
              // filled is a different proposition from one raised yesterday,
              // and the row already carried the timestamp to say so.
              el('td', { class: 'req-age', title: r.created || '' }, r.age || ''),
              // Filled or not, stated rather than inferred. A filled request
              // cannot be filled again, so checking one is wasted budget.
              el('td', {},
                 r.filled
                   ? el('span', { class: 'tag dim', title: r.filled_by ? `by ${r.filled_by}` : '' }, 'filled')
                   : el('span', { class: 'tag ok' }, 'open')),
              el('td', { class: 'result' }, el('span', { class: 'tag dim' }, 'not checked')),
            ),
          ),
        ),
      ),
    );
  }

  // Check a specific set of request ids.
  //
  // It used to read the paste box and the selection itself, from a button that
  // sat above an empty list and was pressable with nothing to press it on. The
  // caller says what to check now, and each caller only exists where there is
  // something to check.
  // One check job, start to finish.
  //
  // Pulled out of requestsCheck because there are now two ways to run one: all
  // the requests once the search has finished, or a page at a time while the
  // search is still going. Both want the same job, the same log line and the
  // same Stop button; only the batching differs.
  async function runCheckJob(tracker, ids, { recheck = false, prefix = '', onSkipped = null } = {}) {
    const log = $('#requests-log');
    const { job_id: jobId } = await api('/api/requests/check', {
      method: 'POST',
      body: { tracker, request_ids: ids, recheck },
    });
    const cancel = jobCancel(jobId, 'Stop checking');
    log.after(cancel);
    // So the search's own Cancel can stop the lookup it started, rather than
    // only stopping the pages and leaving the lookup running behind it.
    state.checkCancelButton = cancel;

    let reported = false;
    const job = await new Promise((resolve) => {
      followJob(jobId, {
        onUpdate: (j) => {
          jobProgress(log, j, prefix);
          j.results.forEach(applyRequestResult);
          const note = j.events.find((e) => e.event === 'skipped' && e.count);
          if (note && !reported) {
            reported = true;
            if (onSkipped) onSkipped(note);
          }
        },
        onDone: resolve,
      });
    });
    cancel.remove();
    state.checkCancelButton = null;
    return job;
  }

  async function requestsCheck(entries, { placeholders = false, recheck = false } = {}) {
    // Callers used to hand over bare ids; they hand over {id, tracker} now,
    // and a bare id still works so a caller that has only an id is not forced
    // to invent a tracker for it.
    const items = entries.map((e) => (typeof e === 'object' ? e : { id: String(e), tracker: null }));
    if (!items.length) return toast('Nothing to check', 'bad');

    const log = $('#requests-log');
    log.hidden = false;
    log.textContent = 'Starting…';
    $$('.requests-check-btn').forEach((b) => { b.disabled = true; });

    // Pasted or uploaded ids have no rows yet, so stand some up to fill in.
    // They carry their tracker so a mixed paste says which is which before a
    // single call is spent.
    if (placeholders) {
      // These rows did not come from a page search, so the line saying how
      // much of one is on screen is now about a list that is not there. It
      // outlived the results it described: paste ten ids and it still said
      // "showing 25 of about 42,925".
      requestsSummary({ shown: null });
      state.requestRows = items.map(({ id, tracker }) => ({
        id,
        tracker: tracker || state.requestsTracker,
        artist: '',
        title: `Request ${id}`,
        year: '',
        bounty: '',
        age: '',
        filled: false,
        url: '#',
      }));
      state.selectedRequests = new Set(items.map((i) => i.id));
      renderRequestRows();
    }

    const done = () => $$('.requests-check-btn').forEach((b) => { b.disabled = false; });

    // One call per tracker, in order. A paste can name both, and the tracker
    // is part of what identifies a request rather than a mode the page is in.
    const groups = new Map();
    items.forEach(({ id, tracker }) => {
      const code = tracker || state.requestsTracker;
      if (!groups.has(code)) groups.set(code, []);
      groups.get(code).push(id);
    });

    let checked = 0;
    let stoppedEarly = null;
    try {
      for (const [tracker, ids] of groups) {
        // eslint-disable-next-line no-await-in-loop -- deliberately serial:
        // two trackers at once would race the budget guard on both.
        const job = await runCheckJob(tracker, ids, {
          recheck,
          prefix: groups.size > 1 ? `${tracker}: ` : '',
          onSkipped: (note) => showSkipped(tracker, note),
        });
        checked += job.result_count || 0;
        stoppedEarly = stoppedEarly || job.events.find((e) => e.event === 'budget_exhausted');
        if (stoppedEarly) break;
      }
      done();
      refreshStatus();
      jobFinished(log, stoppedEarly
        ? `Stopped early to protect the budget after ${stoppedEarly.checked} request(s).`
        : `Done. ${checked} request(s) checked.`);
    } catch (e) {
      done();
      toast(e.message, 'bad');
    }
  }

  // What a run did not do, and why.
  //
  // Skipping requests that already have an answer is the point -- a check is a
  // tracker call and a Deezer search each -- but a run that silently did a
  // tenth of what was asked is indistinguishable from one that broke. So it
  // says how many it passed over, on what grounds, and offers to do them
  // anyway.
  function showSkipped(tracker, note) {
    const rows = note.requests || [];
    const host = $('#requests-log');
    const window_ = note.recheck_after_days;
    const summary = window_
      ? `${note.count} already checked in the last ${window_} day${window_ === 1 ? '' : 's'} — skipped.`
      : `${note.count} already checked — skipped.`;

    host.after(el('div', { class: 'panel skipped-note' },
      el('div', { class: 'row' },
        el('strong', {}, summary),
        el('button', {
          onclick: async (e) => {
            e.target.closest('.skipped-note').remove();
            await requestsCheck(rows.map((r) => ({ id: r.id, tracker: r.tracker })),
                                { placeholders: true, recheck: true });
          },
        }, 'Check them anyway'),
        el('button', {
          onclick: (e) => {
            e.target.closest('.skipped-note').remove();
            showRequestTab('history');
          },
        }, 'Show me what they said')),
      // The first few by name, so the number is not the only thing on offer.
      el('ul', { class: 'skipped-list' },
        ...rows.slice(0, 8).map((r) =>
          el('li', {}, `${r.artist || '?'} — ${r.album || 'Request ' + r.id}: ${r.reason}`)),
        rows.length > 8 ? el('li', { class: 'hint' }, `and ${rows.length - 8} more`) : null)));
  }

  // ------------------------------------------------------------------
  // Scanning: its filters, and what it has already looked up
  // ------------------------------------------------------------------

  function showScanTab(name) {
    state.scanTab = name;
    $('#scan-tab-run').hidden = name !== 'run';
    $('#scan-tab-history').hidden = name !== 'history';
    $$('#scan-tabs button').forEach((b) => {
      b.classList.toggle('active', b.dataset.scantab === name);
    });
    if (name === 'history') loadScanHistory();
  }

  /**
   * Save one scan filter.
   *
   * @param {string} key - The config key under `checker.`.
   * @param {string} value - What to store; blank disables the filter.
   */
  async function saveScanFilter(key, value) {
    try {
      await api('/api/settings', {
        method: 'PUT',
        body: { changes: { [`checker.${key}`]: String(value) } },
      });
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  // The filters a scan applies before contacting a tracker. They govern
  // scanning and nothing else -- the request checker, the album page and the
  // search results never consult them -- so they sit with the scan rather
  // than on the settings page, where they read as rules the whole app obeys.
  function renderScanFilters(filters, window_) {
    const host = $('#scan-filters');
    const textRow = (key, label, placeholder, hint) => formRow(label,
      el('input', {
        id: `scan-${key}`,
        type: 'text',
        class: 'reqsmall',
        placeholder,
        value: filters[key] || '',
        onchange: (e) => saveScanFilter(key, e.target.value.trim()),
      }),
      el('span', { class: 'hint reqhint' }, hint));

    host.replaceChildren(
      formRow('Ignore albums with fewer tracks than',
        el('input', {
          id: 'scan-min-tracks',
          type: 'number',
          class: 'reqsmall',
          min: '0',
          step: '1',
          value: String(filters.min_tracks || 0),
          onchange: (e) => saveScanFilter('min_tracks', e.target.value || '0'),
        }),
        el('span', { class: 'hint reqhint' }, '0 checks every album')),
      textRow('min_date', 'Ignore releases before', '2025-01-01', 'YYYY-MM-DD, or blank'),
      textRow('max_date', 'Ignore releases after', '2026-12-31', 'YYYY-MM-DD, or blank'),
      formRow('Look up again if checked more than',
        ...durationControl({
          id: 'scan-recheck',
          days: window_,
          never: true,
          onChange: (days) => saveScanFilter('album_recheck_after_days', days),
        }),
        el('span', { class: 'hint reqhint' }, 'ago')),
    );
  }

  const scanKey = (r) => r.album_id;

  function scanHistoryPick() {
    const shown = tableView('scanhistory').shown || state.scanHistory;
    const n = countSelected(shown, state.scanHistorySelected, scanKey);
    $('#scanhistory-rerun').disabled = n === 0;
    $('#scanhistory-rerun').textContent = n ? `Check ${n} again` : 'Check again';
    $('#scanhistory-forget').disabled = n === 0;
  }

  /**
   * Draw the scan filters from the config the page already has.
   *
   * The panel is on the run tab, so it cannot wait for the history tab to be
   * opened before it exists.
   */
  async function loadScanFilters() {
    if (!$('#scan-filters')) return;
    try {
      const config = await api('/api/config');
      const checker = config.checker || {};
      renderScanFilters(
        {
          min_tracks: checker.min_tracks,
          min_date: checker.min_date || '',
          max_date: checker.max_date || '',
        },
        checker.album_recheck_after_days ?? 30,
      );
    } catch (e) {
      $('#scan-filters').replaceChildren(empty(e.message));
    }
  }

  async function loadScanHistory() {
    const host = $('#scanhistory-results');
    host.replaceChildren(spinner('Loading'));
    try {
      const data = await api('/api/scan/history');
      state.scanHistory = data.albums;
      state.scanWindow = data.recheck_after_days;
      state.scanHistorySelected = new Set();
      renderScanFilters(data.filters, data.recheck_after_days);
      $('#scanhistory-count').textContent = data.total === data.shown
        ? `${data.total} looked up`
        : `showing ${data.shown} of ${data.total} looked up`;
      renderScanHistoryRows();
      scanHistoryPick();
    } catch (e) {
      host.replaceChildren(empty(e.message));
    }
  }

  function renderScanHistoryRows() {
    $('#scanhistory-results').replaceChildren(dataTable({
      name: 'scanhistory',
      rows: state.scanHistory,
      selection: { set: state.scanHistorySelected, onChange: scanHistoryPick },
      onShown: scanHistoryPick,
      idOf: scanKey,
      empty: 'No scan has looked anything up yet.',
      columns: [
        {
          label: 'Release',
          value: (r) => `${r.artist || ''} ${r.title || ''}`.trim(),
          filter: 'text',
          cell: (r) => el('a', {
            href: albumHref(r.album_id),
            onclick: (e) => { e.preventDefault(); goAlbum(r.album_id); },
          }, `${r.artist || '?'} — ${r.title || r.album_id}`),
        },
        {
          label: 'Outcome',
          value: (r) => r.outcome,
          filter: 'choice',
          cell: (r) => el('span', {},
            el('span', { class: 'pill' }, r.outcome),
            r.reason ? el('div', { class: 'hint' }, r.reason) : null),
        },
        {
          label: 'Trackers',
          class: 'found-trackers',
          value: trackerSummary,
          filter: 'choice',
          cell: (r) => el('span', {}, ...trackerTags(r)),
        },
        {
          label: 'Source',
          value: (r) => r.source || '',
          filter: 'choice',
          cell: (r) => el('span', { class: 'tag dim' }, r.source || '—'),
        },
        {
          label: 'Added',
          value: (r) => r.added_at || 0,
          filter: false,
          class: 'nowrap',
          cell: (r) => el('span', {}, r.added_at ? ago(r.added_at) : '—'),
        },
        {
          label: 'Days since lookup',
          value: (r) => (r.checked_days_ago === null ? -1 : r.checked_days_ago),
          filter: 'range',
          class: 'nowrap',
          cell: (r) => {
            const days = r.checked_days_ago;
            const stale = state.scanWindow > 0 && days !== null && days >= state.scanWindow;
            return el('span', {},
              el('div', {}, days === null ? 'unknown' : days < 1 ? 'today' : `${Math.round(days)}d ago`),
              el('span', { class: 'hint' },
                 [checkedOn(r.checked_at), stale ? 'due a re-check' : ''].filter(Boolean).join(' · ')));
          },
        },
      ],
    }));
  }

  /** Re-check the ticked albums against the trackers, window or no window. */
  async function scanHistoryRerun() {
    const shown = tableView('scanhistory').shown || state.scanHistory;
    const picked = shown.filter((r) => state.scanHistorySelected.has(scanKey(r)));
    if (!picked.length) return;
    showScanTab('run');
    await recheckReleases(picked);
  }

  /** Forget the stored answers, so the next scan looks them up again. */
  async function scanHistoryForget() {
    const shown = tableView('scanhistory').shown || state.scanHistory;
    const ids = shown.filter((r) => state.scanHistorySelected.has(scanKey(r))).map(scanKey);
    if (!ids.length) return;
    try {
      await api('/api/found/dismiss', { method: 'POST', body: { ids, blacklist: false } });
      toast(`${ids.length} forgotten. The next scan will look them up again.`, 'ok');
      state.scanHistorySelected = new Set();
      await loadScanHistory();
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  // ------------------------------------------------------------------
  // Lookup history
  // ------------------------------------------------------------------
  // Every request that has been looked up, and what came of it. The answers
  // were already being kept -- they are what stops a second run paying for the
  // same tracker calls -- but nothing showed them, so a request checked last
  // week was indistinguishable from one that had never been touched.

  const HISTORY_STATUS = {
    fillable: ['Can be filled', 'ok'],
    filled: ['Already filled', ''],
    skipped: ['Nothing usable', ''],
    error: ['Check failed', 'bad'],
  };

  function showRequestTab(name) {
    state.requestTab = name;
    $('#requests-tab-find').hidden = name !== 'find';
    $('#requests-tab-history').hidden = name !== 'history';
    $$('#requests-tabs button').forEach((b) => {
      b.classList.toggle('active', b.dataset.reqtab === name);
    });
    if (name === 'history') loadHistory();
  }

  /**
   * A stored epoch time as a plain date, or "" when there is not one.
   *
   * @param {number} stamp - Epoch seconds.
   * @returns {string} YYYY-MM-DD.
   */
  function checkedOn(stamp) {
    const seconds = Number(stamp);
    if (!seconds) return '';
    return new Date(seconds * 1000).toISOString().slice(0, 10);
  }

  async function loadHistory() {
    const host = $('#history-results');
    host.replaceChildren(spinner('Loading'));
    try {
      const data = await api('/api/requests/history');
      state.history = data.requests;
      state.historyWindow = data.recheck_after_days;
      state.historySelected = new Set();
      historyPick();
      $('#history-count').textContent = data.total === data.shown
        ? `${data.total} looked up`
        : `showing ${data.shown} of ${data.total} looked up`;
      renderHistoryRows();
    } catch (e) {
      host.replaceChildren(empty(e.message));
    }
  }

  /** What identifies one looked-up request. */
  const historyKey = (r) => r.key || `${r.tracker}:${r.id}`;

  function historyPick() {
    const shown = tableView('history').shown || state.history;
    const n = countSelected(shown, state.historySelected, historyKey);
    const button = $('#history-rerun');
    button.disabled = n === 0;
    button.textContent = n ? `Check ${n} again` : 'Check again';
  }

  function renderHistoryRows() {
    // Same table as the queue, so the filters sit in the columns they filter
    // and the numeric ones take a lower and an upper limit. It used to be a
    // form of its own above the list, with a fixed dropdown of ages, which
    // could not express "between 1990 and 1995" or "more than 2 GB".
    const dateCell = (relative, exact, note) => el('span', {},
      el('div', {}, relative),
      exact || note
        ? el('span', { class: 'hint' }, [exact, note].filter(Boolean).join(' · '))
        : null);

    $('#history-results').replaceChildren(dataTable({
      name: 'history',
      rows: state.history,
      selection: { set: state.historySelected, onChange: historyPick },
      onShown: historyPick,
      idOf: (r) => r.key || `${r.tracker}:${r.id}`,
      empty: 'Nothing has been looked up yet.',
      columns: [
        {
          label: 'Request',
          value: (r) => `${r.artist || ''} ${r.album || ''} ${r.id}`.trim(),
          filter: 'text',
          cell: (r) => {
            const name = `${r.artist || '?'} — ${r.album || 'Request ' + r.id}`;
            return el('span', {},
              el('div', {}, r.request_url
                ? el('a', { href: r.request_url, target: '_blank', rel: 'noreferrer' }, name)
                : name),
              el('span', { class: 'hint' }, `${r.tracker || '?'} #${r.id}`));
          },
        },
        {
          label: 'Tracker',
          value: (r) => r.tracker || '',
          filter: 'choice',
          cell: (r) => el('span', {}, r.tracker || '—'),
        },
        {
          label: 'Outcome',
          value: (r) => (HISTORY_STATUS[r.status] || [r.status || 'Unknown'])[0],
          filter: 'choice',
          cell: (r) => {
            const pair = HISTORY_STATUS[r.status] || [r.status || 'Unknown', ''];
            return el('span', {},
              el('span', { class: `pill ${pair[1]}` }, pair[0]),
              r.reason ? el('div', { class: 'hint' }, r.reason) : null);
          },
        },
        {
          label: 'Year',
          value: (r) => Number(String(r.year || '').slice(0, 4)) || 0,
          filter: 'range',
          lowLabel: 'from',
          highLabel: 'to',
          class: 'nowrap',
          cell: (r) => el('span', {}, String(r.year || '—')),
        },
        {
          // Compared in GB, which is the unit the number is written in on the
          // tracker. Stored as a string, so sorting it as text put 900 MB
          // above 1 TB.
          label: 'Bounty (GB)',
          value: (r) => (Number(r.bounty_bytes) || 0) / (1024 ** 3),
          filter: 'range',
          class: 'nowrap',
          cell: (r) => el('span', {}, r.bounty || '—'),
        },
        {
          label: 'Opened',
          value: (r) => r.created || '',
          filter: false,
          class: 'nowrap',
          cell: (r) => (r.created_age
            ? dateCell(`${r.created_age} ago`, (r.created || '').slice(0, 10), '')
            : el('span', {}, '—')),
        },
        {
          label: 'Days since lookup',
          value: (r) => (r.checked_days_ago === null ? -1 : r.checked_days_ago),
          filter: 'range',
          class: 'nowrap',
          cell: (r) => {
            const days = r.checked_days_ago;
            const window_ = state.historyWindow;
            const stale = window_ > 0 && days !== null && days >= window_;
            return dateCell(
              days === null ? 'unknown' : days < 1 ? 'today' : `${Math.round(days)}d ago`,
              checkedOn(r.checked_at),
              stale ? 'due a re-check' : '',
            );
          },
        },
      ],
    }));
  }

  /** Re-run the ticked rows, whatever their stored answer says. */
  async function historyRerun() {
    const shown = tableView('history').shown || state.history;
    const picked = shown.filter((r) => state.historySelected.has(historyKey(r))).map(historyKey);
    if (!picked.length) return;
    const byTracker = new Map();
    for (const key of picked) {
      const parts = key.split(':');
      if (!byTracker.has(parts[0])) byTracker.set(parts[0], []);
      byTracker.get(parts[0]).push(parts[1]);
    }
    showRequestTab('find');
    for (const [tracker, ids] of byTracker) {
      // eslint-disable-next-line no-await-in-loop -- serial on purpose: two
      // trackers at once race the same budget guard.
      await requestsCheck(ids.map((id) => ({ id, tracker })), { placeholders: true, recheck: true });
    }
  }

  // Which tracker a pasted request URL belongs to. The id alone is not enough
  // to identify a request: request 80755 exists on both trackers and is a
  // different release on each. Pasting an orpheus.network link while the
  // toggle said RED checked RED's 80755 and reported back about that one.
  const TRACKER_HOSTS = [
    [/(^|\.)redacted\.(sh|ch)\b/i, 'RED'],
    [/(^|\.)orpheus\.network\b/i, 'OPS'],
    [/(^|\.)dicmusic\.com\b/i, 'DIC'],
  ];

  function trackerFromUrl(line) {
    const host = (line.match(/https?:\/\/([^/\s]+)/i) || [])[1];
    if (!host) return null;
    const hit = TRACKER_HOSTS.find(([re]) => re.test(host));
    return hit ? hit[1] : null;
  }

  // Requests out of a pasted blob or an uploaded file, each carrying the
  // tracker its URL named. A bare id has no tracker and falls back to the
  // one selected above.
  //
  // The id is read from the id= parameter when there is one, because a URL can
  // carry other numbers -- redacted.ch, a port, a numeric in a path -- and
  // taking the first run of digits picked those up.
  function idsFrom(text) {
    return String(text || '')
    .split(/\r?\n/)
    .map((line) => {
      const trimmed = line.trim();
      if (!trimmed) return null;
      const id = (trimmed.match(/[?&]id=(\d+)/i) || trimmed.match(/(\d+)\s*$/) || trimmed.match(/(\d+)/) || [])[1];
      return id ? { id, tracker: trackerFromUrl(trimmed) } : null;
    })
    .filter(Boolean);
  }

  // Backfill a row from what the check learned. Only ever fills blanks: a row
  // that came from a fetch already knows its own details, and the check's copy
  // is no better. Cells are patched in place rather than re-rendering, because
  // a re-render would wipe the result cells of every other row in the batch.
  function fillPastedRequestRow(match) {
    const id = String(match.request_id);
    const row = state.requestRows.find((r) => String(r.id) === id);
    const tr = $(`#requests-results tr[data-request="${id}"]`);
    if (!row || !tr) return;

    const learned = {
      artist: match.artist || '',
      title: match.album || '',
      year: match.year || '',
      bounty: match.bounty || '',
      url: match.request_url || '',
      tracker: match.tracker || row.tracker || '',
    };
    let touched = false;
    Object.entries(learned).forEach(([key, value]) => {
      const blank = !row[key] || (key === 'url' && row.url === '#') || (key === 'title' && row.title === `Request ${id}`);
      if (value && blank) { row[key] = value; touched = true; }
    });
    if (!touched) return;

    const link = tr.children[1].querySelector('a');
    if (link) {
      link.textContent = row.artist || row.title ? `${row.artist} — ${row.title}` : `Request ${id}`;
      if (row.url && row.url !== '#') link.href = row.url;
    }
    tr.children[2].textContent = row.year || '';
    tr.children[3].textContent = row.bounty || '';
  }

  function applyRequestResult(match) {
    const cell = $(`#requests-results tr[data-request="${match.request_id}"] .result`);
    if (!cell) return;
    // A pasted request arrives as a placeholder that knows nothing but its own
    // id, and the check is what learns the rest. Without this the row stayed
    // "— Request 80755" with an empty year and bounty even after the tracker
    // had answered with all of it.
    fillPastedRequestRow(match);
    // A request that was already filled is not a failed check, it is a closed
    // request -- so it says so, and the Filled column is corrected in place for
    // a row that was fetched before somebody filled it.
    if (match.status === 'filled') {
      cell.replaceChildren(el('span', { class: 'tag warn', title: match.reason || '' }, match.reason || 'already filled'));
      const row = cell.closest('tr');
      const filledCell = row && row.children[5];
      if (filledCell) {
        filledCell.replaceChildren(
          el('span', { class: 'tag dim', title: match.filled_by ? `by ${match.filled_by}` : '' }, 'filled'));
      }
      return;
    }
    if (!match.fillable) {
      cell.replaceChildren(el('span', { class: 'tag dim', title: match.reason || '' }, match.reason || match.status));
      return;
    }
    state.requestMatches.set(String(match.request_id), match);
    cell.replaceChildren(
      el('span', { class: 'tag ok' }, `${(match.confidence * 100).toFixed(0)}% match`),
      ' ',
      // Whether the tracker already has it. A request left open after somebody
      // uploaded the release is not worth filling twice.
      match.already_on_tracker === true
        ? el('a', { class: 'tag warn', href: match.tracker_group_url || '#', target: '_blank', rel: 'noopener' },
             'already on tracker')
        : match.already_on_tracker === false
          ? el('span', { class: 'tag dim' }, 'not on tracker')
          : null,
      ' ',
      // Opens both sides rather than throwing you at Deezer: deciding whether
      // this fills that request means reading them together.
      el('button', {
        class: 'link',
        onclick: () => go(addr(`/requests/${encodeURIComponent(match.tracker || state.requestsTracker || '')}`
                               + `/${encodeURIComponent(match.request_id)}`)),
      }, match.deezer_title || 'Compare'),
      ' ',
      el('button', { class: 'ghost', onclick: () => download(match.deezer_id) }, 'Download'),
    );
  }

  // ------------------------------------------------------- request compare

  // The request and the release that might fill it, side by side.
  //
  // The tracker's own page was in a frame here, and could never have worked:
  // RED and OPS both send X-Frame-Options, which is the browser refusing on the
  // tracker's instruction, and no attribute on our side overrides it. The
  // request is fetched and drawn instead -- the same record the tracker builds
  // its page from, description and comments included -- and the link out is
  // still there for the parts only the live page has, like voting.
  /**
   * One request beside the release that might fill it.
   *
   * Takes a tracker and an id rather than a row, because that is all an
   * address carries: the page has to draw itself for somebody arriving on a
   * link, with no list behind it and nothing in memory. Anything already
   * known about the request is a shortcut to a nicer first paint, not a
   * requirement.
   *
   * @param {string} tracker - Which tracker holds it.
   * @param {string} id - The request id on that tracker.
   */
  async function openRequest(tracker, id) {
    const match = state.requestMatches.get(String(id));
    const row = state.requestRows.find((r) => String(r.id) === String(id)) || {};
    const url = row.url || match?.request_url || '';
    const code = tracker || match?.tracker || state.requestsTracker || '';
    const from = state.view;

    // Borrowing the search pane to draw in, not going to Search: the address
    // says Requests, which is where the reader still is.
    setView('search');
    pushPane(paneLabel(), from);
    const pane = searchPane('split-page');
    const named = `${row.artist || match?.artist || ''} — ${row.title || match?.album || `Request ${id}`}`;
    setTitle(named.replace(/^\s*—\s*/, ''));

    const left = el('div', { class: 'split-side' });
    const right = el('div', { class: 'split-side' });
    pane.replaceChildren(
      breadcrumbs(named),
      el('div', { class: 'split' }, left, right),
    );

    left.replaceChildren(
      el('div', { class: 'row split-head' },
        el('h3', { class: 'section-title' }, `Request on ${code || 'the tracker'}`),
        url ? el('a', { class: 'filebtn', href: url, target: '_blank', rel: 'noopener noreferrer' },
                 'Open in a tab ↗') : null),
      spinner('Loading the request'),
    );

    const deezerSide = (async () => {
      if (!match?.deezer_id) {
        right.replaceChildren(
          el('h3', { class: 'section-title' }, 'Deezer release'),
          empty('No Deezer match yet. Check this request to find one.'),
        );
        return;
      }
      right.replaceChildren(spinner('Loading release'));
      try {
        const album = await api(`/api/album/${match.deezer_id}`);
        right.replaceChildren(el('h3', { class: 'section-title' }, 'Deezer release'), albumPanel(album));
      } catch (e) {
        right.replaceChildren(el('h3', { class: 'section-title' }, 'Deezer release'), empty(e.message));
      }
    })();

    const head = left.firstChild;
    if (!code || !id) {
      left.replaceChildren(head, empty('This request has no tracker or id to look up.'));
    } else {
      try {
        const detail = await api(
          `/api/requests/detail?tracker=${encodeURIComponent(code)}&id=${encodeURIComponent(id)}`);
        left.replaceChildren(head, requestPanel(detail));
        // Somebody who arrived on the link had nothing to name the page with
        // until now. The tracker's own record has it, so use it.
        const title = [detail.artist, detail.title].filter(Boolean).join(' — ');
        if (title) {
          const crumb = $('.crumb.current', pane);
          if (crumb) crumb.textContent = title;
          setTitle(title);
        }
      } catch (e) {
        left.replaceChildren(head, empty(e.message));
      }
    }
    await deezerSide;
  }

  // A request as the tracker holds it: the terms it will accept, what it is
  // worth, who wants it, the description, and every comment on it.
  function requestPanel(d) {
    const facts = [];
    const fact = (label, value) => {
      if (value === null || value === undefined || value === '' || value === 0) return;
      facts.push(el('div', { class: 'fact-label' }, label), el('div', { class: 'fact-value' }, value));
    };
    const list = (label, values) => fact(label, (values || []).join(', '));

    fact('Requested by', d.requestor
      ? el('a', { class: 'plain', href: `${trackerBase(d.url)}/user.php?id=${d.requestor_id}`,
                  target: '_blank', rel: 'noopener noreferrer' }, d.requestor)
      : '');
    fact('Created', when(d.created));
    fact('Category', d.category);
    fact('Release type', d.release_type);
    fact('Record label', d.record_label);
    fact('Catalogue number', d.catalogue_number);
    fact('OCLC', d.oclc);
    list('Acceptable bitrates', d.bitrates);
    list('Acceptable formats', d.formats);
    list('Acceptable media', d.media);
    fact('Log / cue', d.log_cue);
    fact('Votes', d.votes ? String(d.votes) : '');
    fact('Vote cost', d.minimum_vote);
    fact('Last voted', when(d.last_vote));
    fact('Bounty', d.bounty);
    (d.people || []).forEach((group) => fact(group.role, group.names.join(', ')));
    if (d.filled) {
      fact('Filled by', d.filled_by);
      fact('Filled', when(d.filled_at));
      if (d.torrent_url) {
        facts.push(
          el('div', { class: 'fact-label' }, 'Torrent'),
          el('div', { class: 'fact-value' },
             el('a', { class: 'plain', href: d.torrent_url, target: '_blank', rel: 'noopener noreferrer' },
                `#${d.torrent_id} ↗`)),
        );
      }
    }
    Object.entries(d.extra || {}).forEach(([key, value]) => fact(prettyKey(key), String(value)));

    return el(
      'div',
      { class: 'request-view' },
      el('div', { class: 'request-head' },
        d.image ? el('img', { class: 'request-cover', src: d.image, loading: 'lazy',
                              referrerpolicy: 'no-referrer', alt: '' }) : null,
        el('div', { class: 'request-headings' },
          el('div', { class: 'album-title' },
             [d.artist, d.title].filter(Boolean).join(' – ') + (d.year ? ` [${d.year}]` : '')),
          d.filled ? el('span', { class: 'tag ok' }, 'Filled') : el('span', { class: 'tag' }, 'Open'),
          (d.tags || []).length
            ? el('div', { class: 'request-tags' }, ...d.tags.map((t) => el('span', { class: 'tag dim' }, t)))
            : null)),
      facts.length ? el('div', { class: 'facts' }, ...facts) : null,
      (d.contributors || []).length
        ? el('div', { class: 'request-section' },
            el('h4', {}, 'Top contributors'),
            el('div', { class: 'facts' },
               ...d.contributors.flatMap((c) => [
                 el('div', { class: 'fact-label' }, c.name),
                 el('div', { class: 'fact-value' }, c.bounty),
               ])))
        : null,
      d.description_html
        ? el('div', { class: 'request-section' },
            el('h4', {}, 'Description'),
            el('div', { class: 'bb', html: d.description_html }))
        : null,
      el('div', { class: 'request-section' },
        el('h4', {}, (d.comments || []).length
          ? `Comments (${d.comments.length}${d.comment_pages > 1 ? ` of ${d.comment_pages} pages` : ''})`
          : 'Comments'),
        (d.comments || []).length
          ? el('div', { class: 'comments' }, ...d.comments.map((c) =>
              el('div', { class: 'comment' },
                el('div', { class: 'comment-head' },
                  el('strong', {}, c.author || 'someone'),
                  el('span', { class: 'dim' }, when(c.added)),
                  c.edited ? el('span', { class: 'dim' },
                               `edited ${c.edited_by ? `by ${c.edited_by} ` : ''}${when(c.edited)}`) : null),
                el('div', { class: 'bb', html: c.html }))))
          : empty('No comments on this request.')),
    );
  }

  // "recordLabel" -> "Record label", for fields a tracker sends that we have no
  // name of our own for. They are shown rather than dropped.
  function prettyKey(key) {
    const spaced = String(key).replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/[_-]+/g, ' ');
    return spaced.charAt(0).toUpperCase() + spaced.slice(1).toLowerCase();
  }

  // The tracker's origin, taken from a URL it gave us.
  function trackerBase(url) {
    try { return new URL(url).origin; } catch { return ''; }
  }

  // Timestamps arrive as ISO or as whatever the tracker felt like; show a real
  // date when it parses and the original string when it does not.
  function when(value) {
    if (!value) return '';
    const at = new Date(value);
    return Number.isNaN(at.getTime()) ? String(value) : at.toLocaleString();
  }

  // Everything about a release, for the half of a split that shows one.
  function albumPanel(album) {
    const availability = album.availability;
    const artistLink = (id, name) =>
      id ? el('a', { href: artistHref(id), onclick: (e) => { e.preventDefault(); goArtist(id); } }, name)
         : el('span', {}, name);
    const facts = [
      album.nb_tracks ? `${album.nb_tracks} tracks` : null,
      album.duration ? duration(album.duration) : null,
      album.release_date,
      album.record_type,
      (album.genres || []).join(', ') || null,
      album.label,
      album.upc ? `UPC ${album.upc}` : null,
    ].filter(Boolean);

    return el(
      'div',
      {},
      el('div', { class: 'row' },
        album.cover ? el('img', { class: 'split-art', src: album.cover, alt: '' }) : null,
        el('div', { class: 'split-meta' },
          el('p', { class: 'album-title-sm' }, album.title || ''),
          el('p', { class: 'album-facts' }, artistLink(album.artist_id, album.artist || '')),
          el('p', { class: 'album-facts' }, facts.join(' · ')),
          availability
            ? el('span', { class: `tag ${availability.uploadable ? 'ok' : 'bad'}` },
                 availability.uploadable
                   ? `All FLAC, all streamable · ${availability.flac_count}/${availability.total}`
                   : availability.reason || 'Not uploadable')
            : null)),
      el('div', { class: 'row split-actions' },
        el('button', { class: 'primary', onclick: () => download(album.id) }, 'Download'),
        el('button', { onclick: () => downloadAndUpload(album.id, album) }, 'Download & upload'),
        album.url ? el('a', { class: 'linkbtn', href: album.url, target: '_blank', rel: 'noopener' },
                       'Open on Deezer ↗') : null),
      el('table', { class: 'table tracklist-table' },
        el('thead', {}, el('tr', {},
          el('th', { class: 'num-col' }, '#'), el('th', {}, 'Track'),
          el('th', {}, 'Featured'), el('th', { class: 'dur-col' }, 'Length'))),
        el('tbody', {}, ...(album.tracks || []).map((t) =>
          el('tr', {},
            el('td', { class: 'num-col' }, String(t.number || '')),
            el('td', {}, t.title || '', t.explicit ? el('span', { class: 'tag dim explicit-tag' }, 'E') : null),
            el('td', { class: 'featured-col' }, ...featuredCredits(t, album, artistLink)),
            el('td', { class: 'dur-col' }, duration(t.duration)))))),
    );
  }

  // ------------------------------------------------------------------ found

  // What earlier checks already established. A scan or a request check pays
  // tracker budget to learn "this exists on Deezer and is not on RED", and both
  // used to throw that away the moment you left the tab.
  async function loadFound() {
    const body = $('#found-body');
    body.replaceChildren(spinner('Loading'));
    try {
      const { found, blacklisted, held, held_count: heldCount, rule,
              held_groups: heldGroups = [], settled_count: settledCount = 0,
              settled_groups: settledGroups = [] } = await api('/api/found');
      state.found = found;
      state.foundHeld = held || [];
      state.foundRule = rule || '';
      state.selectedFound = new Set(found.map((f) => f.id));
      $('#found-restore').hidden = !blacklisted;
      $('#found-restore').textContent = `Clear blacklist (${blacklisted})`;

      state.heldGroups = heldGroups;

      // Releases dropped for good are not listed -- there is nothing to do
      // with them -- but the number is still said, so a queue that went quiet
      // is explained rather than just short.
      const dropped = settledCount
        ? `${settledCount} dropped: ${settledGroups.map((g) => `${g.count} ${g.label}`).join('; ')}.`
        : '';
      state.droppedNote = dropped;

      // What was kept out, and whether any of it can still be acted on. The
      // toggle only appears when there is a list behind it.
      const heldRow = $('#found-held-row');
      heldRow.hidden = !heldCount && !settledCount;
      $('#found-held-toggle').hidden = !heldCount;
      if (!heldCount && settledCount) $('#found-held').textContent = dropped;
      if (heldCount) {
        // Only one of these groups is a setting, and it is usually the small
        // one. Calling all of them "your queue rules" sent people to Settings
        // to widen a rule that had nothing to do with it.
        const parts = heldGroups.map((g) => {
          if (g.key === 'rules') return `${g.count} by your queue rules, which let through ${state.foundRule}`;
          return `${g.count} ${g.label}`;
        });
        $('#found-held').textContent = `${heldCount} excluded: ${parts.join('; ')}. ${dropped}`.trim();
        $('#found-held-toggle').textContent = state.showHeld ? 'Hide excluded' : 'Show excluded';
      }

      renderFound();
    } catch (e) {
      body.replaceChildren(empty(e.message));
    }
  }

  // The tracker options come from the rows themselves rather than a fixed
  // list, so a filter is never offered for a tracker this install does not use.
  // The list used to be filtered twice: a bar above the table with a search
  // box and two dropdowns, and then the table's own per-column filters. Two
  // controls for one job, and the bar could not say which column it narrowed.
  // The columns do it.

  /** The count line, from the rows the table is showing. */
  function updateFoundCount() {
    const shown = tableView('queue').shown || [];
    $('#found-count').textContent =
      `${countSelected(shown, state.selectedFound)} of ${shown.length} selected`;
  }

  function renderFound() {
    const body = $('#found-body');
    railCount('#found-count-rail', state.found.length);
    if (!state.found.length) {
      body.replaceChildren(
        state.foundHeld.length
          ? empty(`The queue is empty. ${state.foundHeld.length} release(s) were excluded — `
              + 'see the list below, or widen the queue rule in Settings.')
          : empty(state.droppedNote
              ? `The queue is empty. ${state.droppedNote}`
              : 'The queue is empty. Run a scan, or look up some requests.'),
      );
      $('#found-count').textContent = '';
      if (state.showHeld) body.append(heldTable());
      return;
    }
    const rows = state.found;
    // Counted from the rows, not the checkboxes, and against what is on
    // screen: the buttons act on what is on screen, so selecting all while
    // filtered and then downloading a hidden row would be indefensible.
    body.replaceChildren(dataTable({
      name: 'queue',
      rows,
      selection: { set: state.selectedFound, onChange: updateFoundCount },
      onShown: (shown) => {
        $('#found-count').textContent =
          `${countSelected(shown, state.selectedFound)} of ${shown.length} selected`;
      },
      empty: 'Nothing in the queue.',
      columns: [
        {
          label: 'Release',
          value: (f) => `${f.artist || ''} ${f.title || ''}`.trim(),
          filter: 'text',
          cell: (f) => el('a', {
            href: albumHref(f.album_id),
            onclick: (e) => { e.preventDefault(); goAlbum(f.album_id); },
          }, `${f.artist || ''} — ${f.title || ''}`),
        },
        {
          label: 'Trackers',
          class: 'found-trackers',
          value: trackerSummary,
          filter: 'choice',
          cell: (f) => el('span', {}, ...trackerTags(f)),
        },
        {
          label: 'Source',
          value: (f) => (f.sources || [f.kind]).join(', '),
          filter: 'choice',
          cell: (f) => el('span', {}, ...sourceTags(f)),
        },
        {
          label: 'Added',
          value: (f) => f.added_at || 0,
          filter: false,
          class: 'nowrap',
          cell: (f) => el('span', {}, f.added_at ? ago(f.added_at) : '—'),
        },
        {
          label: 'Last checked',
          value: (f) => f.checked_at || 0,
          filter: false,
          class: 'nowrap',
          cell: (f) => el('span', {}, f.checked_at ? ago(f.checked_at) : '—'),
        },
      ],
    }));
    if (state.showHeld) body.append(heldTable());
  }

  // Releases that were checked and are not in the queue, each saying what is
  // actually keeping it out.
  //
  // This used to be headed "Held back by your queue rules", which was wrong
  // for most of it: only one of the reasons is a setting, and a release that
  // every tracker already has is not being held by anything -- there is
  // nothing to upload. It also had no way to act on a row, so a list of
  // things you did not want was a list you could only look at.
  function heldPick() {
    // Counts come from the rows the table is showing, not from the boxes: a
    // row with no id used to add `undefined` to the set, so "17 selected" was
    // one more than the list held and clearing left that one behind.
    const n = countSelected(tableView('held').shown || state.foundHeld, state.selectedHeld);
    $$('.held-action').forEach((b) => { b.disabled = n === 0; });
    const label = $('#held-selected');
    if (label) label.textContent = n ? `${n} selected` : '';
  }

  async function heldAct(what) {
    const shown = tableView('held').shown || state.foundHeld;
    const ids = shown.filter((r) => state.selectedHeld.has(r.id)).map((r) => r.id);
    if (!ids.length) return;
    try {
      if (what === 'recheck') {
        const rows = shown.filter((r) => ids.includes(r.id));
        state.selectedHeld = new Set();
        await recheckReleases(rows);
        return;
      }
      await api('/api/found/dismiss', { method: 'POST', body: { ids, blacklist: what === 'blacklist' } });
      toast(what === 'blacklist'
        ? `${ids.length} blacklisted. No scan will list them again.`
        : `${ids.length} removed.`, 'ok');
      state.selectedHeld = new Set();
      await loadFound();
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  function heldTable() {
    if (!state.foundHeld.length) return empty('Every checked release is in the queue.');

    // What each group needs, said once rather than repeated down a column.
    const advice = (state.heldGroups || []).map((g) => {
      if (g.fix === 'recheck') {
        return el('li', {}, `${g.count} ${g.label}. Select them and re-check.`);
      }
      if (g.fix === 'settings') {
        return el('li', {}, `${g.count} excluded by your queue rules. Widen the rule in Settings to admit them.`);
      }
      return el('li', {}, `${g.count} ${g.label}.`);
    });

    return el('div', { class: 'held-block' },
      el('h3', { class: 'section-head-plain' },
         `Excluded from the queue (${state.foundHeld.length})`),
      advice.length ? el('ul', { class: 'held-why' }, ...advice) : null,
      el('div', { class: 'row held-actions' },
        el('button', { class: 'held-action', disabled: true, onclick: () => heldAct('recheck') },
           'Re-check on trackers'),
        el('button', { class: 'held-action', disabled: true, onclick: () => heldAct('remove') },
           'Remove'),
        el('button', { class: 'held-action danger', disabled: true, onclick: () => heldAct('blacklist') },
           'Blacklist'),
        el('span', { class: 'hint', id: 'held-selected' })),
      dataTable({
        name: 'held',
        rows: state.foundHeld,
        selection: { set: state.selectedHeld, onChange: heldPick },
        onShown: heldPick,
        empty: 'Every checked release is in the queue.',
        columns: [
          {
            label: 'Release',
            value: (f) => `${f.artist || ''} ${f.title || ''}`.trim(),
            filter: 'text',
            cell: (f) => el('a', {
              href: albumHref(f.album_id),
              onclick: (e) => { e.preventDefault(); goAlbum(f.album_id); },
            }, `${f.artist || ''} — ${f.title || ''}`),
          },
          {
            label: 'Trackers',
            class: 'found-trackers',
            value: (f) => trackerSummary(f),
            filter: 'choice',
            cell: (f) => el('span', {}, ...trackerTags(f)),
          },
          {
            label: 'Exclusion reason',
            value: (f) => f.held_reason || '',
            filter: 'choice',
            class: 'card-sub',
            cell: (f) => el('span', {}, f.held_reason || ''),
          },
        ],
      }));
  }

  // How a release got into the queue. Both can be true at once -- a scan found
  // it and a request check matched it -- which used to be two identical rows
  // with two different one-word labels. One row, both tags, and the request
  // tag is the link to the request.
  function sourceTags(row) {
    const sources = row.sources || [row.kind];
    const tags = [];
    if (sources.includes('scan')) tags.push(el('span', { class: 'tag dim' }, 'scan'));
    if (sources.includes('request')) {
      // "a OPS request" reads as badly as it looks. Tracker codes are said
      // as letters, so the article follows the first letter's sound.
      const code = row.tracker || '';
      const article = /^[AEIOU]/.test(code) ? 'an' : 'a';
      const text = code ? `fills ${article} ${code} request` : 'fills a request';
      tags.push(row.request_url
        ? el('a', { href: row.request_url, target: '_blank', rel: 'noopener', class: 'tag warn' }, text)
        : el('span', { class: 'tag warn' }, text));
    }
    return tags.length ? tags : [el('span', { class: 'tag dim' }, 'checked')];
  }

  // What the last check found, per tracker. Green means there is something to
  // upload there; dim means that one already has it.
  /**
   * The tracker state as one comparable string.
   *
   * The tags are pills, which sort and filter as markup. This is the same
   * fact in a form a column can group by, so "everything OPS is missing" is a
   * choice in the header rather than something to read row by row.
   *
   * @param {object} row - A queue row.
   * @returns {string} e.g. "OPS missing, RED has it".
   */
  function trackerSummary(row) {
    const missing = (row.missing_from || []).map((t) => `${t} missing`);
    const found = (row.found_on || []).map((t) => `${t} has it`);
    if (!missing.length && !found.length) return 'not checked on any tracker';
    return [...missing, ...found].join(', ');
  }

  function trackerTags(row) {
    const missing = row.missing_from || [];
    const found = row.found_on || [];
    if (!missing.length && !found.length) {
      return [el('span', { class: 'tag dim' }, 'not checked on any tracker')];
    }
    return [
      ...missing.map((t) => el('span', { class: 'tag ok' }, `${t} missing`)),
      ...found.map((t) => el('span', { class: 'tag dim' }, `${t} has it`)),
    ];
  }

  // Rows on screen only: a row you cannot see is a row you did not choose,
  // and "Download selected" acting on one would be indefensible. That means
  // the table's own column filters as well as the search above it, which is
  // why this asks the table rather than re-deriving the list.
  const foundSelection = () =>
    (tableView('queue').shown || state.found).filter((f) => state.selectedFound.has(f.id));

  // Off the list, one of two ways.
  //
  // "Remove" forgets the check result, so the next scan that turns the release
  // up puts it back -- which is what you want for something you are not doing
  // yet. "Blacklist" remembers it as unwanted, and no scan lists it again
  // until the blacklist is cleared.
  async function dismissFound(blacklist) {
    const picked = foundSelection();
    if (!picked.length) return toast('Nothing selected', 'bad');
    const what = `${picked.length} release${picked.length === 1 ? '' : 's'}`;
    if (blacklist && !confirm(`Blacklist ${what}?

They will not be listed again, even if a later scan finds them.`)) {
      return;
    }
    try {
      await api('/api/found/dismiss', {
        method: 'POST',
        body: { ids: picked.map((f) => f.id), blacklist },
      });
      toast(blacklist ? `Blacklisted ${what}` : `Removed ${what}`, 'ok');
      loadFound();
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  async function restoreFound() {
    if (!confirm('Clear the blacklist? Releases on it can be found by a scan again.')) return;
    try {
      const { restored } = await api('/api/found/restore', { method: 'POST', body: {} });
      toast(`Cleared ${restored} from the blacklist`, 'ok');
      loadFound();
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  // Ask the trackers again about the selection. Costs budget, so it is a button
  // rather than something that happens when the tab opens.
  async function recheckFound() {
    const picked = foundSelection();
    if (!picked.length) return toast('Nothing selected', 'bad');
    return recheckReleases(picked);
  }

  // Re-check a set of releases against the trackers.
  //
  // The same job whether the rows came from the queue or from the list of
  // things that did not make it into the queue -- and the second is where it
  // matters most, because "not checked against Deezer yet" is the one reason
  // on that list a re-check actually fixes.
  async function recheckReleases(picked) {
    const trackers = checkTrackers();
    if (!trackers.length) return toast('No tracker configured', 'bad');

    const candidates = picked.map((f) => ({
      album_id: f.album_id, title: f.title, artist: f.artist, source: 'found',
    }));
    try {
      const { job_id } = await api('/api/missing/check', { method: 'POST', body: { candidates, trackers } });
      const log = $('#found-log');
      log.hidden = false;
      log.textContent = 'Starting…';
      log.after(jobCancel(job_id, 'Stop re-checking'));
      followJob(job_id, {
        onUpdate: (job) => jobProgress(log, job),
        onDone: (job) => {
          refreshStatus();
          jobFinished(log, job.error || `Re-checked ${job.result_count} release(s).`);
          toast(job.error || `Re-checked ${job.result_count} release(s)`, job.error ? 'bad' : 'ok');
          loadFound();
        },
      });
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  // ---------------------------------------------------------------- uploads

  async function loadFolders() {
    const list = $('#folders-list');
    list.replaceChildren(spinner('Reading download folder'));
    try {
      const { folders, directory, linking, error: dirError } = await api('/api/folders');
      $('#uploads-dir').textContent = directory;
      state.linking = linking;
      $('#linking-note').textContent = linking
        ? 'Linking is on: each tracker gets its own hardlinked folder under the seeding directory, so the bytes exist once.'
        : 'Linking is off — every tracker will seed from the same folder. Set [linking] in your config for cross-seed style layout.';
      if (dirError) {
        // "No folders" and "cannot see the folder" look identical otherwise.
        list.replaceChildren(el('p', { class: 'empty bad' }, dirError));
        return;
      }
      if (!folders.length) {
        list.replaceChildren(empty('Nothing here yet. Finished downloads turn up ready to upload.'));
        return;
      }
      list.replaceChildren(
        el(
          'table',
          { class: 'table' },
          el('thead', {}, el('tr', {}, el('th', {}, 'Folder'), el('th', {}, 'Tracks'), el('th', {}, ''))),
          el(
            'tbody',
            {},
            ...folders.map((f) =>
              el(
                'tr',
                {},
                el('td', {}, f.name),
                el('td', {}, String(f.tracks)),
                el(
                  'td',
                  {},
                  el('button', { class: 'primary', onclick: () => startUpload(f.path) }, 'Upload'),
                  el('button', { class: 'danger', onclick: () => deleteFolder(f.path, f.name, loadFolders) },
                     'Delete'),
                ),
              ),
            ),
          ),
        ),
      );
    } catch (e) {
      list.replaceChildren(empty(e.message));
    }
  }

  // `albumId` is passed on when the upload came from a Deezer release, so a
  // successful one can take that release off the Found list. An upload started
  // from the Uploads tab has no id, and the server falls back to matching the
  // folder name.
  async function startUpload(folder, trackers, albumId) {
    const targets = trackers && trackers.length ? trackers : [...state.uploadTrackers];
    if (!targets.length) return toast('Pick at least one tracker', 'bad');

    try {
      const { flow_id, dry_run } = await api('/api/upload', {
        method: 'POST',
        body: { folder, trackers: targets, album_id: albumId ? String(albumId) : '' },
      });
      if (dry_run) toast('Dry run: nothing reaches the tracker or the download client.');
      state.flows.add(flow_id);
      return await followFlow(flow_id);
    } catch (e) {
      toast(e.message, 'bad');
      return null;
    }
  }

  // A flow is polled until it finishes. When it is waiting on a question the
  // question is rendered as real controls; answering resumes the pipeline.
  // Resolves when the flow stops, so a queued batch can wait for one upload --
  // including every question you answer during it -- before starting the next.
  function followFlow(flowId) {
    return new Promise((resolve) => {
      const tick = async () => {
        let flow;
        try {
          flow = await api(`/api/flows/${flowId}`);
        } catch (e) {
          toast(e.message, 'bad');
          return resolve(null);
        }
        renderFlow(flow);
        if (flow.state === 'running' || flow.state === 'waiting') {
          setTimeout(tick, flow.state === 'waiting' ? 900 : 500);
        } else {
          refreshStatus();
          loadFolders();
          resolve(flow);
        }
      };
      tick();
    });
  }

  // One track per row, captioned with its filename, the full spectral and its
  // zoom flush against each other -- the layout smoked-salmon's own spectral
  // page uses. Anything that boxes the two images independently letterboxes
  // them, and you end up judging a release from two small pictures adrift in a
  // lot of empty space.
  function spectralList(images) {
    return el(
      'div',
      { class: 'spectrals' },
      ...images.map((img) =>
        el(
          'div',
          { class: 'spectral-row' },
          el('div', { class: 'spectral-num', title: img.label || img.track || '' },
             img.label || img.track || ''),
          el(
            'div',
            { class: 'spectral-pair' },
            img.full
              ? el('a', { class: 'spectral-full', href: img.full, target: '_blank', rel: 'noopener',
                          title: 'Open full size' },
                  el('img', { src: img.full, loading: 'lazy', alt: `Full spectral, track ${img.track}` }))
              : null,
            img.zoom
              ? el('a', { class: 'spectral-zoom', href: img.zoom, target: '_blank', rel: 'noopener',
                          title: 'Open full size' },
                  el('img', { src: img.zoom, loading: 'lazy', alt: `Zoomed spectral, track ${img.track}` }))
              : null,
          ),
        ),
      ),
    );
  }

  // What is already in the group you are about to upload into. The question is
  // "does my release duplicate one of these", which is a comparison across
  // media, format and encoding -- so those are columns, sortable by eye,
  // rather than forty lines of slash-separated prose in a scroll box.
  function torrentTable(table) {
    const head = el('tr', {},
      ...['Year', 'Edition', 'Media', 'Format', 'Encoding'].map((h) => el('th', {}, h)));
    const rows = table.rows.map((r) =>
      el('tr', {},
        el('td', {}, r.year || ''),
        el('td', { class: 'torrent-edition' }, r.edition || '—'),
        el('td', {}, r.media || ''),
        el('td', {}, r.format || ''),
        el('td', {}, r.encoding || '')),
    );
    return el(
      'details',
      // Open, however many there are. What is already in the group is the whole
      // basis for "is this a duplicate" -- collapsing it past twelve rows hid
      // it in exactly the cases where there was most to check.
      { class: 'diff', open: true },
      el('summary', {}, table.title,
        el('span', { class: 'card-sub' }, ` — ${table.rows.length} existing torrent${table.rows.length === 1 ? '' : 's'}`)),
      el('div', { class: 'table-scroll' },
        el('table', { class: 'table torrent-table' }, el('thead', {}, head), el('tbody', {}, ...rows))),
    );
  }

  function stepTable(table) {
    if (table.kind === 'torrents') return torrentTable(table);
    // Metadata is a field-and-value list. Running it through the three-column
    // diff renderer gave it a before/after it does not have, and printed each
    // multi-value field twice -- once as an empty label, once as a group
    // header over its own values.
    if (table.kind === 'previous' || table.kind === 'pending') return metaTable(table);
    if (table.kind === 'renames' || table.kind === 'folder') return renameTable(table);
    // Rows can be grouped -- by filename for a tag diff, by field for a
    // metadata list -- so a group header is emitted whenever it changes.
    const rows = [];
    let group = null;
    const changed = table.rows.filter((r) => r.changed).length;

    table.rows.forEach((row) => {
      if (row.group && row.group !== group) {
        group = row.group;
        rows.push(el('tr', { class: 'diff-group' }, el('td', { colspan: '3' }, group)));
      }
      rows.push(
        el(
          'tr',
          { class: row.changed ? 'diff-changed' : '' },
          el('td', { class: 'diff-field' }, row.label || ''),
          el('td', { class: 'diff-before' }, row.before || ''),
          el('td', { class: 'diff-after' }, row.changed ? row.after : ''),
        ),
      );
    });

    return el(
      'details',
      { class: 'diff', open: table.rows.length <= 40 },
      el(
        'summary',
        {},
        table.title,
        el('span', { class: 'card-sub' },
          changed ? ` — ${changed} change${changed === 1 ? '' : 's'}` : ` — ${table.rows.length} rows`),
      ),
      el('table', { class: 'table diff-table' }, el('tbody', {}, ...rows)),
    );
  }

  // Two columns, one row per field, empties collapsed to a dash. Open by
  // default: it is the thing the next question is about.
  function metaTable(table) {
    const rows = table.rows.filter((r) => r.label || r.before);
    return el(
      'details',
      // Open. It is what the question is about, and having to click to see the
      // metadata before answering a question about the metadata is a click
      // that never had a reason. "Previous metadata" stays collapsed -- that
      // one really is background.
      { class: 'diff meta-block', open: table.kind !== 'previous' },
      el('summary', {}, table.title, el('span', { class: 'card-sub' }, ` — ${rows.length} fields`)),
      el(
        'table',
        { class: 'table meta-table' },
        el(
          'tbody',
          {},
          ...rows.map((r) =>
            el(
              'tr',
              { class: r.before ? '' : 'meta-empty' },
              el('td', { class: 'meta-field' }, r.label || ''),
              el('td', { class: 'meta-value' }, r.before || '—'),
            ),
          ),
        ),
      ),
    );
  }

  const ARTIST_ROLES = ['main', 'guest', 'composer', 'conductor', 'dj', 'remixer', 'producer', 'arranger'];

  // The whole release, editable at once.
  //
  // The pipeline does this as a menu: it prints the record, asks which single
  // field you want to change, opens an editor for that one field, prints the
  // record again and asks again. Four changes meant four round trips, and you
  // could never see the release while editing part of it. This is every field
  // with the control that suits it and one Save.
  function metadataForm(step, send) {
    // The server sends groups of fields; flatten for the answer, keep the
    // grouping for the layout.
    const groups = (step.options || []).map((g) => ({
      group: g.group || '',
      fields: (g.fields || []).map((f) => ({ ...f, rows: (f.rows || []).map((r) => ({ ...r })) })),
    }));
    const sections = groups.flatMap((g) => g.fields);

    // Credits, lists, the tracklist and the comment are as tall as their
    // content and read badly in a narrow column, so they take the full row.
    // Marked in the class as well as in CSS, so the layout does not depend on
    // :has() being available.
    const isWide = (section) => ['artists', 'list', 'tracks', 'textarea'].includes(section.kind);

    // A wide field owns its row, so its hint can sit beside the label where it
    // reads as part of the heading. A narrow one shares a row, and a hint long
    // enough to wrap there pushed its own input a line below its neighbours' --
    // which is why "Original release year" sat lower than "Edition year". So
    // for those the hint goes under the control, and every input in a row
    // starts at the same height whatever its label says.
    const label = (section) =>
      el('div', { class: 'meta-form-head' },
         el('label', {}, section.label),
         section.hint && isWide(section) ? el('span', { class: 'meta-form-hint' }, section.hint) : null);

    const body = el('div', { class: 'meta-groups' });

    const buildField = (section) => {
        const wide = isWide(section);
        const field = el('div', { class: `meta-form-field${wide ? ' meta-form-wide' : ''}` }, label(section));

        if (section.kind === 'artists') {
          const list = el('div', { class: 'editor-rows' });
          const paint = () => list.replaceChildren(
            ...section.rows.map((row, i) =>
              el('div', { class: 'editor-row' },
                el('input', {
                  type: 'text', value: row.name || '', placeholder: 'Artist name',
                  oninput: (e) => (row.name = e.target.value),
                }),
                el('select', { onchange: (e) => (row.role = e.target.value) },
                   ...ARTIST_ROLES.map((role) =>
                     el('option', { value: role, selected: (row.role || 'main') === role },
                        role === 'dj' ? 'DJ / Compiler' : role[0].toUpperCase() + role.slice(1)))),
                el('button', { type: 'button', class: 'danger row-drop', title: 'Remove this credit',
                               onclick: () => { section.rows.splice(i, 1); paint(); } }, '−'))),
            el('button', { type: 'button', class: 'ghost',
                           onclick: () => { section.rows.push({ name: '', role: 'main' }); paint(); } },
               '+ Add artist'),
          );
          paint();
          field.append(list);
          return field;
        }

        if (section.kind === 'list') {
          const list = el('div', { class: 'editor-rows' });
          const paint = () => list.replaceChildren(
            ...section.rows.map((row, i) =>
              el('div', { class: 'editor-row' },
                el('input', {
                  type: 'text', value: row.value || '',
                  oninput: (e) => (row.value = e.target.value),
                }),
                el('button', { type: 'button', class: 'danger row-drop', title: 'Remove',
                               onclick: () => { section.rows.splice(i, 1); paint(); } }, '−'))),
            el('button', { type: 'button', class: 'ghost',
                           onclick: () => { section.rows.push({ value: '' }); paint(); } }, '+ Add'),
          );
          paint();
          field.append(list);
          return field;
        }

        if (section.kind === 'tracks') {
          field.append(el('div', { class: 'meta-tracks' },
            ...section.rows.map((row) => {
              const input = el('input', { type: 'text' });
              input.value = row.value || '';
              input.addEventListener('input', () => (row.value = input.value));
              return el('div', { class: 'meta-track' },
                el('span', { class: 'meta-track-no' }, row.label || ''),
                input,
                row.artists ? el('span', { class: 'meta-track-artists' }, row.artists) : null);
            })));
          return field;
        }

        if (section.kind === 'select') {
          field.append(el('select', { onchange: (e) => (section.value = e.target.value) },
            ...(section.choices || []).map((choice) =>
              el('option', { value: choice, selected: section.value === choice }, choice))));
          return field;
        }

        const control = section.kind === 'textarea'
          ? el('textarea', { rows: '3' })
          : el('input', { type: section.kind === 'number' ? 'number' : 'text' });
        control.value = section.value ?? '';
        control.addEventListener('input', () => (section.value = control.value));
        field.append(control);
        return field;
    };

    const drawField = (section) => {
      const field = buildField(section);
      if (section.hint && !isWide(section)) {
        field.append(el('p', { class: 'meta-form-hint meta-form-note' }, section.hint));
      }
      return field;
    };

    const draw = () => {
      body.replaceChildren(...groups.map((group) =>
        el('section', { class: 'meta-group' },
           group.group ? el('h4', { class: 'meta-group-head' }, group.group) : null,
           el('div', { class: 'meta-form' }, ...group.fields.map(drawField)))));
    };
    draw();

    const answer = () => {
      const out = {};
      sections.forEach((section) => {
        if (section.kind === 'artists') {
          out.artists = section.rows.map((r) => ({ name: r.name, role: r.role }));
        } else if (section.kind === 'list') {
          out[section.key] = section.rows.map((r) => r.value);
        } else if (section.kind === 'tracks') {
          out.tracks = Object.fromEntries(section.rows.map((r) => [r.key, r.value ?? '']));
        } else {
          out[section.key] = section.value ?? '';
        }
      });
      return out;
    };

    return el(
      'div',
      { class: 'step' },
      el('div', { class: 'step-prompt' }, step.prompt),
      step.detail ? el('p', { class: 'hint' }, step.detail) : null,
      body,
      el('div', { class: 'row step-controls' },
         el('button', { type: 'button', class: 'primary', onclick: () => send(answer()) }, 'Save metadata'),
         // Null is what the pipeline reads as "leave it as it is".
         el('button', { type: 'button', onclick: () => send(null) }, 'Leave unchanged')),
    );
  }

  // Editing metadata as a form. The pipeline does this by opening $EDITOR,
  // which in a container is vim with no terminal -- it blocks forever. Each
  // thing it edits has a known shape, so each gets the controls it deserves:
  // credits are rows with a role, a genre list is a list, a title is a field.
  function editorStep(step, send) {
    if (step.edit_shape === 'metadata') return metadataForm(step, send);
    const rows = (step.options || []).map((r) => ({ ...r }));
    const body = el('div', { class: 'editor-rows' });

    const draw = () => {
      if (step.edit_shape === 'artists') {
        body.replaceChildren(
          ...rows.map((row, i) =>
            el(
              'div',
              { class: 'editor-row' },
              el('input', {
                type: 'text', value: row.name || '', placeholder: 'Artist name',
                oninput: (e) => (row.name = e.target.value),
              }),
              el(
                'select',
                { onchange: (e) => (row.role = e.target.value) },
                ...ARTIST_ROLES.map((role) =>
                  el('option', { value: role, selected: (row.role || 'main') === role },
                     role === 'dj' ? 'DJ / Compiler' : role[0].toUpperCase() + role.slice(1))),
              ),
              el('button', { class: 'danger row-drop', title: 'Remove',
                             onclick: () => { rows.splice(i, 1); draw(); } }, '−'),
            ),
          ),
          el('button', { class: 'ghost',
                         onclick: () => { rows.push({ name: '', role: 'main' }); draw(); } }, '+ Add artist'),
        );
        return;
      }

      if (step.edit_shape === 'aliases') {
        body.replaceChildren(
          ...rows.map((row) =>
            el(
              'div',
              { class: 'editor-row' },
              el('span', { class: 'editor-fixed' }, row.name || ''),
              el('span', { class: 'editor-arrow' }, '→'),
              el('input', {
                type: 'text', value: row.alias || '', placeholder: 'Leave blank to keep as is',
                oninput: (e) => (row.alias = e.target.value),
              }),
              el('label', { class: 'check' },
                 el('input', { type: 'checkbox', onchange: (e) => (row.drop = e.target.checked) }), 'Drop'),
            ),
          ),
        );
        return;
      }

      // A named set of fields: title, years, edition info, comment.
      if (step.edit_shape === 'form') {
        body.replaceChildren(
          ...rows.map((row) => {
            const control = row.kind === 'textarea'
              ? el('textarea', { rows: '4' })
              : el('input', { type: row.kind === 'number' ? 'number' : 'text' });
            control.value = row.value ?? '';
            control.addEventListener('input', () => (row.value = control.value));
            return el('div', { class: 'setting' }, el('label', {}, row.label || row.key), control);
          }),
        );
        return;
      }

      if (step.edit_shape === 'json') {
        const area = el('textarea', { rows: '14', spellcheck: 'false' });
        area.value = rows[0]?.value || '';
        area.addEventListener('input', () => (rows[0].value = area.value));
        body.replaceChildren(area);
        return;
      }

      if (step.edit_shape === 'title') {
        const field = el('input', { type: 'text', value: rows[0]?.value || '' });
        field.addEventListener('input', () => (rows[0].value = field.value));
        body.replaceChildren(field);
        return;
      }

      // A plain list: genres, urls.
      body.replaceChildren(
        ...rows.map((row, i) =>
          el(
            'div',
            { class: 'editor-row' },
            el('input', {
              type: 'text', value: row.value || '',
              oninput: (e) => (row.value = e.target.value),
            }),
            el('button', { class: 'danger row-drop', title: 'Remove',
                           onclick: () => { rows.splice(i, 1); draw(); } }, '−'),
          ),
        ),
        el('button', { class: 'ghost', onclick: () => { rows.push({ value: '' }); draw(); } }, '+ Add'),
      );
    };
    draw();

    const answer = () => {
      if (['json', 'title'].includes(step.edit_shape)) return rows[0]?.value ?? '';
      if (step.edit_shape === 'form') {
        return Object.fromEntries(rows.map((r) => [r.key, r.value ?? '']));
      }
      return rows;
    };

    return el(
      'div',
      { class: 'step' },
      el('div', { class: 'step-prompt' }, step.prompt),
      step.detail ? el('p', { class: 'hint' }, step.detail) : null,
      body,
      el(
        'div',
        { class: 'row step-controls' },
        el('button', { class: 'primary', onclick: () => send(answer()) }, 'Save'),
        // null is what click.edit returns when you quit without saving, and the
        // pipeline reads it as "leave this alone".
        el('button', { onclick: () => send(null) }, 'Cancel'),
      ),
    );
  }

  // What the files are about to be called. Two columns, open by default,
  // because the question that follows is whether to accept it.
  function renameTable(table) {
    return el(
      'details',
      { class: 'diff', open: true },
      el('summary', {}, table.title,
         el('span', { class: 'card-sub' },
            table.kind === 'folder'
              ? ''
              : ` — ${table.rows.length} file${table.rows.length === 1 ? '' : 's'}`)),
      el('div', { class: 'table-scroll' },
        el('table', { class: 'table diff-table' },
          el('thead', {}, el('tr', {}, el('th', {}, 'Now'), el('th', {}, 'After'))),
          el('tbody', {},
            ...table.rows.map((r) =>
              el('tr', {},
                el('td', { class: 'diff-before' }, r.before || ''),
                el('td', { class: 'diff-after' }, r.after || '')))))),
    );
  }

  // What the run did, or in a dry run would have done.
  //
  // A finished upload is a line, not a page. The full payload used to sit open
  // under every completed run, so three uploads pushed the one that still
  // needed you off the bottom of the screen. It is all still here, one click
  // away, and a dry run's descriptions are here in full -- the whole point of
  // rehearsing is reading what would have been posted, which "1179 chars" does
  // not let you do.
  function flowResult(result) {
    const parts = [];
    (result.outcomes || []).forEach((o) => {
      const rows = Object.entries(o.would_post || {});
      const detail = [];

      if (o.folder) {
        detail.push(el('p', { class: 'hint' },
          `${result.dry_run ? 'Would seed' : 'Seeded'} from ${o.folder}`));
      }

      const plain = rows.filter(([k]) => !DESCRIPTION_FIELDS.includes(k));
      if (plain.length) {
        detail.push(el('table', { class: 'table meta-table' },
          el('tbody', {}, ...plain.map(([k, v]) =>
            el('tr', {}, el('td', { class: 'meta-field' }, k), el('td', { class: 'meta-value' }, v))))));
      }
      DESCRIPTION_FIELDS.forEach((key) => {
        const body = (o.descriptions || {})[key];
        if (!body) return;
        detail.push(
          el('h4', { class: 'result-desc-head' }, key),
          el('pre', { class: 'result-desc' }, body),
        );
      });
      if (!detail.length) {
        detail.push(el('p', { class: 'hint' }, 'Nothing else to show — see the log.'));
      }

      parts.push(
        el(
          'details',
          { class: 'result-block' },
          el('summary', {},
             el('strong', {}, o.tracker),
             el('span', { class: `tag ${o.ok ? 'ok' : 'bad'}` },
                o.ok ? (result.dry_run ? 'would upload' : 'uploaded') : (o.error || 'failed')),
             el('span', { class: 'card-sub' }, o.folder ? basename(o.folder) : '')),
          ...detail,
        ),
      );
    });
    return el(
      'div',
      { class: 'flow-result' },
      ...[
        el('h3', { class: 'section-title' },
           result.dry_run ? 'Dry run — nothing was posted or seeded' : 'Result'),
        ...parts,
        leftovers(result),
      ].filter(Boolean),
    );
  }

  // What the run left on disk that nothing is going to use.
  //
  // A dry run posts nothing and adds nothing to a client, so everything it
  // produced is dead weight: the hardlinked per-tracker folders — FLAC and all
  // — and any downconversion it transcoded on the way. They are kept rather
  // than deleted, because rehearsing exists to be inspected and a transcode
  // deleted before you can look at it defeats the point. Clearing them is one
  // button each, or one button for the lot.
  //
  // After a real run the same files are what the torrents are seeding from, so
  // only the transcodes are listed and the label says what removing them costs.
  function leftovers(result) {
    const seen = new Set();
    const items = [];
    const add = (path, what) => {
      if (!path || seen.has(path)) return;
      seen.add(path);
      items.push({ path, what });
    };

    if (result.dry_run) {
      (result.outcomes || []).forEach((o) => {
        if (o.folder && o.folder !== result.folder) add(o.folder, `Seeding folder — ${o.tracker}`);
      });
    }
    (result.transcodes || []).forEach((p) => add(p, 'Downconversion'));
    if (!items.length) return null;

    const list = el('div', { class: 'leftover-list' });
    const block = el(
      'details',
      { class: 'result-block', open: true },
      el('summary', {},
         el('strong', {}, result.dry_run ? 'Files this rehearsal left behind' : 'Transcodes kept'),
         el('span', { class: 'card-sub' },
            `${items.length} folder${items.length === 1 ? '' : 's'}`)),
      el('p', { class: 'hint' },
         result.dry_run
           ? 'Nothing was posted or seeded, so none of this is in use. Delete what you do not want to keep.'
           : 'These were uploaded and are seeding. Deleting one stops that torrent.'),
      list,
    );

    const rows = new Map();
    const drop = (path) => {
      rows.get(path)?.remove();
      rows.delete(path);
      if (!rows.size) block.remove();
    };

    const remove = async (path) => {
      await api('/api/folders/delete', { method: 'POST', body: { folder: path } });
      drop(path);
    };

    items.forEach(({ path, what }) => {
      const row = el(
        'div',
        { class: 'row result-kept' },
        el('div', { class: 'leftover-what' },
           el('span', {}, what),
           el('span', { class: 'hint leftover-path' }, path)),
        el('button', {
          type: 'button', class: 'danger',
          onclick: async () => {
            if (!confirm(`Delete "${basename(path)}"?\n\n${path}\n\n`
                         + 'This removes the files from disk and cannot be undone.')) return;
            try {
              await remove(path);
              toast(`Deleted ${basename(path)}`, 'ok');
              loadFolders();
            } catch (e) {
              toast(e.message, 'bad');
            }
          },
        }, 'Delete'),
      );
      rows.set(path, row);
      list.append(row);
    });

    if (items.length > 1) {
      list.append(el(
        'div',
        { class: 'row leftover-all' },
        el('button', {
          type: 'button', class: 'danger',
          onclick: async (e) => {
            const paths = [...rows.keys()];
            if (!confirm(`Delete all ${paths.length} folders?\n\n${paths.join('\n')}\n\n`
                         + 'This removes the files from disk and cannot be undone.')) return;
            e.target.disabled = true;
            const failed = [];
            for (const path of paths) {
              try {
                await remove(path);
              } catch (err) {
                failed.push(`${basename(path)}: ${err.message}`);
              }
            }
            e.target.disabled = false;
            if (failed.length) toast(`Could not delete ${failed.join('; ')}`, 'bad');
            else toast(`Deleted ${paths.length} folders`, 'ok');
            loadFolders();
          },
        }, `Delete all ${items.length}`),
      ));
    }

    return block;
  }

  const DESCRIPTION_FIELDS = ['album description', 'release description'];

  const basename = (path) => String(path || '').split(/[\\/]/).filter(Boolean).pop() || '';

  function flowStep(flow) {
    const step = flow.step;
    const send = async (value) => {
      try {
        await api(`/api/flows/${flow.id}/answer`, {
          method: 'POST',
          body: { step_id: step.id, value },
        });
      } catch (e) {
        toast(e.message, 'bad');
      }
    };

    const controls = [];
    // Matches found on the tracker are the substance of the question, not one
    // more button. They get full-width cards above the actions, untruncated,
    // each with a link out so the group can be checked on the tracker itself.
    const matches = [];
    if (step.kind === 'confirm') {
      controls.push(
        el('button', { class: 'primary', onclick: () => send(true) }, 'Yes'),
        el('button', { onclick: () => send(false) }, 'No'),
      );
    } else if (step.kind === 'choice') {
      step.options.forEach((o) => {
        if (o.kind === 'group') {
          const sub = el('div', { class: 'match-sub' }, o.detail || '');
          // The pipeline's line carries no year for a Deezer candidate, and the
          // year is how you tell one edition from another. Deezer is free to
          // ask, so ask it rather than leaving the field out.
          const deezerAlbum = /deezer\.com\/album\/(\d+)/.exec(o.url || '');
          if (deezerAlbum) {
            api(`/api/album/${deezerAlbum[1]}`)
              .then((album) => {
                const facts = [
                  album.release_date ? album.release_date.slice(0, 4) : null,
                  album.record_type,
                  album.label,
                  o.detail,
                ].filter(Boolean);
                sub.textContent = facts.join(' · ');
              })
              .catch(() => {});
          }
          // The card is the answer, so the card is the button -- picking a
          // release should not mean aiming at a small control on the far side
          // of the row. The link out is the one thing inside it that means
          // something else, so it stops the click travelling.
          matches.push(
            el(
              'div',
              {
                class: 'match match-pick',
                role: 'button',
                tabindex: '0',
                title: 'Use this release',
                onclick: () => send(o.value),
                onkeydown: (e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    send(o.value);
                  }
                },
              },
              el('div', { class: 'match-body' },
                el('div', { class: 'match-title' }, o.label),
                sub),
              o.url
                ? el(
                    'div',
                    { class: 'match-actions' },
                    el(
                      'a',
                      {
                        class: 'filebtn',
                        href: o.url,
                        target: '_blank',
                        rel: 'noopener noreferrer',
                        onclick: (e) => e.stopPropagation(),
                      },
                      o.link_label || 'Open on tracker ↗',
                    ),
                  )
                : null,
            ),
          );
          return;
        }
        controls.push(
          el(
            'button',
            {
              class: o.danger ? 'danger' : o.value === step.default ? 'primary' : '',
              title: o.detail || '',
              onclick: () => send(o.value),
            },
            o.label,
          ),
        );
      });
    } else if (step.kind === 'multi') {
      const picked = new Set(step.default || []);
      step.options.forEach((o) =>
        controls.push(
          el(
            'label',
            { class: 'check' },
            el('input', {
              type: 'checkbox',
              checked: picked.has(o.value),
              onchange: (e) => (e.target.checked ? picked.add(o.value) : picked.delete(o.value)),
            }),
            o.label,
          ),
        ),
      );
      controls.push(el('button', { class: 'primary', onclick: () => send([...picked]) }, 'Continue'));
    } else if (step.kind === 'review') {
      controls.push(
        el('button', { class: 'primary', onclick: () => send(true) }, 'Looks right, continue'),
        el('button', { onclick: () => send(false) }, 'Stop'),
      );
    } else if (step.kind === 'edit') {
      return editorStep(step, send);
    } else {
      const field = el('input', { type: 'text', value: step.default ?? '', placeholder: 'Your answer' });
      controls.push(
        field,
        el('button', { class: 'primary', onclick: () => send(field.value) }, 'Send'),
      );
    }

    return el(
      'div',
      { class: 'step' },
      ...[
      el('div', { class: 'step-prompt' }, step.prompt),
      step.detail ? el('p', { class: 'hint' }, step.detail) : null,
      matches.length ? el('div', { class: 'matches' }, ...matches) : null,
      // The tag diff and metadata comparison are evidence for the answer, so
      // they are tables next to the question rather than prose above it.
      ...(step.tables || []).map(stepTable),
      // Spectrals ride along with the question they inform, so the lossy-master
      // call is made by looking rather than by trusting a filename.
      step.images && step.images.length ? spectralList(step.images) : null,
      step.kind === 'review' && step.options.length
        ? el(
            'dl',
            { class: 'meta' },
            ...step.options.flatMap((r) => [el('dt', {}, r.label), el('dd', {}, String(r.value))]),
          )
        : null,
      el('div', { class: 'row step-controls' }, ...controls),
      // A field beside the buttons, for the prompts that name pasting a URL as
      // one of their answers. Never instead of the buttons: replacing them
      // threw away the candidates the pipeline had just found, which are the
      // answer nearly every time.
      step.text_label ? urlAnswer(step, send) : null,
      ].filter((n) => n !== null && n !== undefined),
    );
  }

  // "Or paste a URL" — a text field and a Send button under the choices.
  function urlAnswer(step, send) {
    const field = el('input', {
      type: 'text',
      placeholder: 'https://…',
      onkeydown: (e) => { if (e.key === 'Enter' && field.value.trim()) send(field.value.trim()); },
    });
    return el(
      'div',
      { class: 'step-text-answer' },
      el('label', {}, step.text_label),
      el('div', { class: 'row' },
         field,
         el('button', { type: 'button',
                        onclick: () => field.value.trim() && send(field.value.trim()) }, 'Use this')),
    );
  }

  // Rebuilt in pieces, each only when that piece actually changed. Replacing the
  // whole card on every poll -- twice a second -- threw away your scroll
  // position, closed anything you had expanded and cleared anything you had
  // typed, which made a question you needed to read impossible to read.
  function renderFlow(flow) {
    const container = $('#upload-flows');
    if (!container) return;

    let card = $(`#flow-${flow.id}`);
    if (!card) {
      card = el(
        'div',
        { class: 'panel flow-card', id: `flow-${flow.id}` },
        el('div', { class: 'row flow-head' }),
        el('p', { class: 'hint flow-stage' }),
        el('div', { class: 'bar flow-bar', hidden: true }, el('div', { class: 'bar-fill' })),
        el('div', { class: 'flow-step' }),
        el('div', { class: 'flow-error' }),
        el('div', { class: 'flow-summary' }),
        // Behind a disclosure: the running commentary is for when something
        // looks wrong, not something to read past on every question.
        el('details', { class: 'flow-notes' },
           el('summary', {}, 'Log'),
           el('ul', { class: 'notelist' })),
      );
      container.prepend(card);
    }

    const stateTag = { waiting: 'warn', done: 'ok', failed: 'bad', cancelled: 'dim' }[flow.state] || 'dim';
    const head = card.querySelector('.flow-head');
    if (head.dataset.state !== flow.state) {
      head.dataset.state = flow.state;
      head.replaceChildren(
        ...[
          el('h2', {}, flow.label),
          el('span', { class: `tag ${stateTag}` }, flow.state === 'waiting' ? 'needs you' : flow.state),
          flow.state === 'running' || flow.state === 'waiting'
            ? el('button', { class: 'ghost', onclick: () => cancelFlow(flow.id) }, 'Cancel')
            : null,
        ].filter(Boolean),
      );
    }

    // Counted off the cards after this one has been updated, so it is right
    // whether a run just started, just answered, or was cancelled in another
    // tab. Counting before would have used this card's previous state.
    railNeedsYou($$('.flow-head[data-state="waiting"]').length);

    const stage = card.querySelector('.flow-stage');
    if (stage.textContent !== (flow.stage || '')) stage.textContent = flow.stage || '';
    stage.hidden = !flow.stage;

    const bar = card.querySelector('.flow-bar');
    const pct = flow.percent;
    bar.hidden = pct === null || pct === undefined;
    if (!bar.hidden) bar.firstElementChild.style.width = `${pct}%`;

    // The expensive part, and the one holding your scroll and your typing.
    // Only rebuilt when it becomes a different question.
    const stepBox = card.querySelector('.flow-step');
    const stepId = flow.step?.id || '';
    if (stepBox.dataset.step !== stepId) {
      stepBox.dataset.step = stepId;
      stepBox.replaceChildren(...(flow.step ? [flowStep(flow)] : []));
    }

    const errorBox = card.querySelector('.flow-error');
    if (errorBox.dataset.error !== (flow.error || '')) {
      errorBox.dataset.error = flow.error || '';
      errorBox.replaceChildren(...(flow.error ? [el('p', { class: 'test-result warn' }, flow.error)] : []));
    }

    // Notes append rather than redraw, and only follow the tail while you are
    // already at the bottom -- scrolling up to read something should not be
    // undone by the next line arriving.
    const notes = card.querySelector('.flow-notes');
    const list = notes.querySelector('.notelist');
    const shown = Number(notes.dataset.count || 0);
    const events = flow.events.slice(-25);
    if (events.length !== shown || list.childElementCount !== events.length) {
      const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 24;
      notes.dataset.count = String(events.length);
      list.replaceChildren(...events.map((n) => el('li', { class: n.level }, n.message)));
      if (atBottom) list.scrollTop = list.scrollHeight;
    }
    notes.hidden = !events.length;

    // What the flow produced, once it has produced it.
    const summary = card.querySelector('.flow-summary');
    if (flow.result && summary.dataset.done !== 'yes') {
      summary.dataset.done = 'yes';
      summary.replaceChildren(flowResult(flow.result));
    }
  }

  async function cancelFlow(flowId) {
    await api(`/api/flows/${flowId}/cancel`, { method: 'POST' });
  }

  async function resumeFlows() {
    try {
      const { flows } = await api('/api/flows?kind=upload');
      flows.filter((f) => f.state === 'running' || f.state === 'waiting').forEach((f) => followFlow(f.id));
    } catch {
      // nothing running
    }
  }

  // ---------------------------------------------------------------- settings

  // ---------------------------------------------------------------- settings

  async function loadSettings() {
    const body = $('#settings-body');
    body.replaceChildren(spinner('Loading settings'));
    try {
      const data = await api('/api/settings');
      state.settings = data;
      state.pending = {};
      renderSettings();
      applyTheme();
      loadDebug();
      loadSeedboxes();
    } catch (e) {
      body.replaceChildren(empty(e.message));
    }
  }

  const BYTE_UNITS = [
    { name: 'B', factor: 1 },
    { name: 'KB', factor: 1024 },
    { name: 'MB', factor: 1024 ** 2 },
    { name: 'GB', factor: 1024 ** 3 },
  ];

  // Show a stored byte count in the largest unit that divides it exactly, so
  // 8388608 comes back as "8 MB" rather than "8192 KB" or "8.0 MB".
  function splitBytes(bytes) {
    const total = Number(bytes) || 0;
    if (total <= 0) return [1, 'MB'];
    for (const unit of [...BYTE_UNITS].reverse()) {
      if (total >= unit.factor && total % unit.factor === 0) return [total / unit.factor, unit.name];
    }
    return [total, 'B'];
  }

  // Show a stored secret, on request.
  //
  // A field that says "•••••••• (saved)" cannot answer the question people
  // actually have, which is *which* key is in there. The value is fetched when
  // the eye is pressed rather than shipped with the page, so an unrevealed
  // secret is never sitting in the DOM.
  //
  // Revealing must not make the field dirty: the input event is what records a
  // change, and setting .value in code does not fire one. Re-masking puts the
  // box back exactly as it was, so a look costs nothing on save.
  function revealButton(input, fetchValue, placeholder) {
    let shown = false;
    const eye = el('button', {
      type: 'button',
      class: 'eye',
      title: 'Show what is stored',
      'aria-label': 'Show what is stored',
    });

    const paint = () => {
      eye.innerHTML = shown ? eyeOffIcon() : eyeIcon();
      eye.title = shown ? 'Hide it again' : 'Show what is stored';
      eye.setAttribute('aria-label', eye.title);
      eye.setAttribute('aria-pressed', String(shown));
    };
    paint();

    eye.addEventListener('click', async () => {
      if (shown) {
        shown = false;
        input.type = 'password';
        input.value = '';
        input.placeholder = placeholder;
        paint();
        return;
      }
      // Anything typed but not saved is what the user is looking at already.
      if (input.value) {
        shown = true;
        input.type = 'text';
        paint();
        return;
      }
      eye.disabled = true;
      try {
        const value = await fetchValue();
        if (!value) {
          toast('Nothing stored for that one yet', 'bad');
          return;
        }
        shown = true;
        input.type = 'text';
        input.value = value;
        paint();
      } catch (e) {
        toast(e.message, 'bad');
      } finally {
        eye.disabled = false;
      }
    });
    return eye;
  }

  // Drawn, not typed: a glyph out of whatever font carries it arrives at a
  // different weight and sits off the baseline.
  //
  // Functions, not constants. ICON() is declared further down this file, and a
  // const initialised up here would run before it exists -- a ReferenceError at
  // load, which for a script with no build step takes the whole UI with it.
  const eyeIcon = () => ICON('<path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12Z"/>'
    + '<circle cx="12" cy="12" r="2.6"/>');
  const eyeOffIcon = () => ICON('<path d="M4 4l16 16"/>'
    + '<path d="M9.9 5.7A10.6 10.6 0 0 1 12 5.5c6.4 0 10 6.5 10 6.5a18 18 0 0 1-3.3 4.1"/>'
    + '<path d="M6.6 7.6A17.7 17.7 0 0 0 2 12s3.6 6.5 10 6.5a10.8 10.8 0 0 0 4-.75"/>'
    + '<path d="M9.7 9.9a2.6 2.6 0 0 0 3.5 3.6"/>');

  function settingField(field, values, secretsSet) {
    const value = values[field.key];
    const isSecret = field.kind === 'secret';
    const configured = secretsSet.includes(field.key);

    const markDirty = () => {
      $('#settings-save').disabled = false;
      $('#settings-dirty').textContent = `${Object.keys(state.pending).length} unsaved change(s)`;
    };

    const onInput = (e) => {
      const target = e.target;
      state.pending[field.key] = target.type === 'checkbox' ? target.checked : target.value;
      markDirty();
    };

    // A size is a number and a unit. Storing bytes is right; asking you to type
    // 1073741824 and count the digits is not.
    if (field.kind === 'bytes') {
      const [amount, unit] = splitBytes(value);
      const size = el('input', { type: 'number', min: '1', step: '1', value: amount, class: 'bytes-amount' });
      const units = el(
        'select',
        { class: 'bytes-unit' },
        ...BYTE_UNITS.map((u) => el('option', { value: u.name, selected: u.name === unit }, u.name)),
      );
      const push = () => {
        const factor = BYTE_UNITS.find((u) => u.name === units.value).factor;
        state.pending[field.key] = Math.max(1, Number(size.value) || 0) * factor;
        markDirty();
      };
      size.addEventListener('input', push);
      units.addEventListener('change', push);
      return el(
        'div',
        { class: 'setting' },
        el('label', {}, field.label),
        el('div', { class: 'bytes-input' }, size, units),
        field.help ? el('p', { class: 'hint setting-help' }, field.help) : null,
      );
    }

    let input;
    if (field.kind === 'bool') {
      input = el('input', { type: 'checkbox', checked: !!value, onchange: onInput });
      return el(
        'div',
        { class: 'setting' },
        el('label', { class: 'check' }, input, field.label),
        field.help ? el('p', { class: 'hint setting-help' }, field.help) : null,
      );
    }

    if (field.kind === 'choice') {
      input = el(
        'select',
        { onchange: onInput },
        // The label is what you read, the value is what gets stored. Without
        // this a rule reads "only_missing_there" on screen.
        ...field.choices.map((c, i) =>
          el('option', { value: c, selected: c === value }, (field.labels || [])[i] || c)),
      );
    } else if (isSecret) {
      const placeholder = configured ? '•••••••• (saved — type to replace)' : 'Not set';
      input = el('input', {
        type: 'password',
        autocomplete: 'new-password',
        placeholder,
        oninput: onInput,
      });
      if (configured) {
        input = el('div', { class: 'secret-field' }, input,
          revealButton(input, async () => {
            const got = await api(`/api/settings/secret?key=${encodeURIComponent(field.key)}`);
            return got.value;
          }, placeholder));
      }
    } else if (field.kind === 'list') {
      // Stored as a list, edited as one per line, which is how anyone would
      // write a list of genres by hand.
      input = el('textarea', { rows: '4', placeholder: field.placeholder || '' });
      input.value = (value || []).join('\n');
      input.addEventListener('input', () => {
        state.pending[field.key] = input.value.split('\n').map((s) => s.trim()).filter(Boolean);
        markDirty();
      });
    } else {
      input = el('input', {
        type: field.kind === 'int' || field.kind === 'float' ? 'number' : 'text',
        step: field.kind === 'float' ? '0.01' : '1',
        min: field.min ?? null,
        max: field.max ?? null,
        value: value ?? '',
        placeholder: field.placeholder || '',
        oninput: onInput,
      });
    }

    // A credential that stands on its own gets its own test. One button at the
    // top of a section holding five independent tokens could only ever report
    // on one of them, which is what it was doing.
    // The box is found by element, not by id: a settings key contains dots, so
    // `#test-field-image.ptpimg_key` parses as an id plus a class and matched
    // nothing at all -- pressing Test did nothing, silently.
    const resultBox = field.test ? el('div', { class: 'test-result', hidden: true }) : null;
    const test = field.test
      ? el('button', { type: 'button', class: 'test-btn field-test',
                       onclick: (e) => runTest(field.test, e.target, resultBox) }, 'Test')
      : null;

    return el(
      'div',
      { class: 'setting' },
      el('div', { class: 'setting-head' },
         el('label', {}, field.label, configured ? el('span', { class: 'tag ok saved-tag' }, 'saved') : null),
         test),
      input,
      resultBox,
      field.help ? el('p', { class: 'hint setting-help' }, field.help) : null,
    );
  }

  const slug = (name) => String(name).toLowerCase().replace(/[^a-z0-9]+/g, '-');

  // Sections in category order, keeping the order they were declared in.
  function categoriesOf(sections) {
    const groups = new Map();
    sections.forEach((s) => {
      const name = s.category || 'General';
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(s);
    });
    return [...groups.entries()];
  }

  /**
   * A settings heading that is also its own address.
   *
   * The page is one long scroll, so a section is a place on it, and a place
   * you cannot name is one you cannot bookmark, link to, or be returned to on
   * a reload. The heading is the link because it is already the thing you
   * point at when you tell somebody where a setting lives.
   *
   * @param {string} tag - h2 or h3.
   * @param {string} name - What this part is called in an address.
   * @param {string} text - What it is called on screen.
   * @param {string} [cls] - A class for the heading itself.
   */
  function settingsHeading(tag, name, text, cls = '') {
    const target = addr(`/settings/${name}`);
    return el(tag, { class: cls || null, id: `settings-${name}` },
      el('a', {
        class: 'anchor',
        href: target,
        onclick: (event) => { event.preventDefault(); go(target); },
      }, text));
  }

  // The parts of the page that are not schema sections. They have their own
  // ids for other reasons, so the address names are mapped rather than
  // guessed -- and "users" rather than "accounts", which is already the name
  // of the category holding the tracker credentials.
  const SETTINGS_ELSEWHERE = { 'debug-log': '#debug-panel', users: '#accounts-panel' };

  /**
   * Bring the addressed part of the settings page into view.
   *
   * Landing halfway down a long page with nothing marked leaves you hunting
   * for the section you asked for, so it is flashed once.
   *
   * @param {boolean} [glide] - Scroll smoothly. Only for a heading pressed on
   *   a page that is already on screen and has stopped moving. Arriving is a
   *   jump: three of these panels fill themselves in afterwards, and each one
   *   replaces the node a smooth scroll is animating towards, which cancels
   *   it -- so landing on /settings/users stayed at the top of the page.
   */
  function revealSettingsSection(glide = false) {
    const name = state.settingsSection;
    if (!name) return;
    const named = $(`#settings-${name}`);
    // The save bar's own controls are #settings-save and #settings-dirty, so
    // the id alone is not enough: a place on this page is a heading.
    const heading = named && /^H[1-6]$/.test(named.tagName) ? named : null;
    const target = heading || $(SETTINGS_ELSEWHERE[name] || '#none');
    if (!target) return;
    const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
    target.scrollIntoView({ behavior: glide && !still ? 'smooth' : 'auto', block: 'start' });
    target.classList.add('addressed');
    setTimeout(() => target.classList.remove('addressed'), 1800);
  }

  function renderSettings() {
    const { sections, values, secrets_set: secretsSet, bootstrap, config_path: configPath } = state.settings;
    const body = $('#settings-body');

    body.replaceChildren(
      el(
        'div',
        { class: 'row settings-bar' },
        el('button', { class: 'primary', id: 'settings-save', disabled: true, onclick: saveSettings }, 'Save changes'),
        el('span', { class: 'hint', id: 'settings-dirty' }, 'No unsaved changes'),
      ),
      el(
        'p',
        { class: 'hint' },
        'Changes apply immediately, no restart. Tests use what is on screen, so a credential can be checked before it is saved.',
      ),
      ...categoriesOf(sections).flatMap(([name, group]) => [
        settingsHeading('h2', slug(name), name, 'settings-category'),
        ...group.map((section) =>
          el(
            'section',
            { class: 'panel settings-section' },
            el(
              'div',
              { class: 'row settings-head' },
              settingsHeading('h3', section.id, section.title),
              section.test
                ? el(
                    'button',
                    { type: 'button', class: 'test-btn', onclick: (e) => runTest(section.test, e.target) },
                    'Test connection',
                  )
                : null,
            ),
            section.blurb ? el('p', { class: 'hint' }, section.blurb) : null,
            el('div', { class: 'test-result', id: `test-${section.test || section.id}`, hidden: true }),
            section.fields.length
              ? el('div', { class: 'settings-grid' },
                   ...section.fields.map((f) => settingField(f, values, secretsSet)))
              : null,
            section.id === 'torrent' ? el('div', { id: 'seedbox-editor' }, spinner('Loading')) : null,
          ),
        ),
      ]),
      el(
        'section',
        { class: 'panel' },
        settingsHeading('h2', 'config-file', 'Set in config.toml'),
        el(
          'p',
          { class: 'hint' },
          `These are read before this page exists, so they cannot be edited here. From ${configPath || 'your config file'}.`,
        ),
        el('ul', { class: 'bootstrap-list' }, ...bootstrap.map((k) => el('li', {}, el('code', {}, k)))),
      ),
      el('section', { class: 'panel', id: 'debug-panel' },
         settingsHeading('h2', 'debug-log', 'Debug log'), spinner('Loading')),
      el(
        'section',
        { class: 'panel' },
        settingsHeading('h2', 'appearance', 'Appearance'),
        el(
          'div',
          { class: 'row' },
          el(
            'div',
            { class: 'segmented', id: 'theme-picker' },
            ...[['dark', 'Dark'], ['light', 'Light'], ['system', 'Auto']].map(([value, label]) =>
              el('button', { type: 'button', 'data-theme': value, onclick: () => applyTheme(value) }, label),
            ),
          ),
        ),
        el('p', { class: 'hint' }, 'Auto follows your operating system. The sidebar icon flips between dark and light.'),
        settingsHeading('h2', 'scan-history', 'Scan history'),
        el(
          'div',
          { class: 'row' },
          el('button', { onclick: () => clearHistory('albums') }, 'Clear album history'),
          el('button', { onclick: () => clearHistory('requests') }, 'Clear request history'),
        ),
        el('p', { class: 'hint' }, 'Clearing history makes the next scan re-check everything, costing tracker budget again.'),
      ),
      el('section', { class: 'panel', id: 'accounts-panel' },
         settingsHeading('h2', 'users', 'Accounts'), spinner('Loading')),
    );
    loadAccounts();
    // Whatever the address named, once there is a page to scroll through.
    revealSettingsSection();
  }

  // Who can sign in. Changing a password ends every session it had, including
  // this one on other devices, because the session signature is derived from
  // the stored hashes.
  async function loadAccounts() {
    const panel = $('#accounts-panel');
    if (!panel) return;
    let data;
    try {
      data = await api('/api/accounts');
    } catch (e) {
      panel.replaceChildren(settingsHeading('h2', 'users', 'Accounts'), empty(e.message));
      return;
    }

    const field = (id, placeholder, type = 'text') =>
      el('input', { type, id, placeholder, autocomplete: type === 'password' ? 'new-password' : 'off' });

    const current = field('acct-current', 'Current password', 'password');
    const next = field('acct-new', `New password (at least ${data.min_password})`, 'password');
    const newUser = field('acct-user', 'Username');
    const newPass = field('acct-pass', `Password (at least ${data.min_password})`, 'password');

    panel.replaceChildren(
      settingsHeading('h2', 'users', 'Accounts'),
      el('p', { class: 'hint' },
         data.you
           ? `Signed in as ${data.you}. Changing a password signs out every other browser using it.`
           : 'Signed in with the shared access token, so there is no password to change here.'),

      data.you ? el('h3', { class: 'accounts-head' }, 'Change your password') : null,
      data.you
        ? el('div', { class: 'row accounts-row' }, current, next,
             el('button', { class: 'primary', onclick: async (e) => {
               await accountAction(e.target, '/api/accounts/password',
                 { current: current.value, password: next.value },
                 'Password changed');
             } }, 'Change'))
        : null,

      el('h3', { class: 'accounts-head' }, 'Add an account'),
      el('div', { class: 'row accounts-row' }, newUser, newPass,
         el('button', { onclick: async (e) => {
           await accountAction(e.target, '/api/accounts',
             { username: newUser.value, password: newPass.value }, 'Account created');
         } }, 'Add')),

      el('h3', { class: 'accounts-head' }, `Existing (${data.accounts.length})`),
      el('div', { class: 'row' },
         ...data.accounts.map((name) =>
           el('span', { class: 'chip' }, name,
              data.accounts.length > 1
                ? el('button', { class: 'link chip-drop', title: `Remove ${name}`,
                                 onclick: async (e) => {
                                   if (!confirm(`Remove the account "${name}"?`)) return;
                                   await accountAction(e.target, '/api/accounts/delete',
                                     { username: name }, `Removed ${name}`);
                                 } }, '×')
                : null))),
      el('div', { class: 'row accounts-row' },
         el('button', { onclick: signOut }, 'Sign out of this browser')),
    );
    // Same as the debug log: this panel lands after the page around it, so an
    // address naming it has to be honoured once it is here.
    if (state.settingsSection === 'users') revealSettingsSection();
  }

  async function accountAction(button, path, body, done) {
    button.disabled = true;
    try {
      await api(path, { method: 'POST', body });
      toast(done, 'ok');
      loadAccounts();
    } catch (e) {
      toast(e.message, 'bad');
    } finally {
      button.disabled = false;
    }
  }

  async function loadDebug() {
    const panel = $('#debug-panel');
    if (!panel) return;
    try {
      const data = await api('/api/debug?limit=300');
      panel.replaceChildren(
        el(
          'div',
          { class: 'row' },
          settingsHeading('h2', 'debug-log', 'Debug log'),
          el('span', { class: `tag ${data.enabled ? 'ok' : 'dim'}` }, data.enabled ? 'debug on' : 'debug off'),
          el('button', { onclick: loadDebug }, 'Refresh'),
          el('button', { onclick: clearDebug }, 'Clear'),
          el('a', { class: 'linkbtn', href: '/api/debug/bundle' }, 'Download diagnostics'),
          el('a', { class: 'linkbtn', href: '/api/debug/logfile' }, 'Download log file'),
        ),
        el(
          'p',
          { class: 'hint' },
          `Rolling log at ${data.logfile.path} — ${(data.logfile.bytes / 1048576).toFixed(1)} MB, ` +
            `${(data.logfile.max_file_bytes / 1048576).toFixed(0)} MB per file, ` +
            `${(data.logfile.max_total_bytes / 1073741824).toFixed(1)} GB total before old files are dropped.`,
        ),
        el(
          'p',
          { class: 'hint' },
          data.enabled
            ? 'Credentials are redacted before anything is written, so this is safe to share.'
            : 'Turn on Debug mode above, reproduce the problem, then refresh.',
        ),
        el('pre', { class: 'console' }, data.log.join('\n') || '(nothing logged yet)'),
      );
      // This panel arrives after the page is drawn, so an address naming it
      // had nothing to scroll to when the rest of the page was ready.
      if (state.settingsSection === 'debug-log') revealSettingsSection();
    } catch (e) {
      panel.replaceChildren(settingsHeading('h2', 'debug-log', 'Debug log'), empty(e.message));
    }
  }

  // The torrent clients finished uploads are handed to.
  //
  // A list of connections rather than a set of single values, which is why it
  // was not on this page at all -- "inject into the torrent client" was a
  // toggle with nothing behind it, and the connection details only existed in
  // config.toml where the UI could not see or check them.
  async function loadSeedboxes() {
    const host = $('#seedbox-editor');
    if (!host) return;
    try {
      const { seedboxes, fields, clients } = await api('/api/settings/seedboxes');
      state.seedboxes = seedboxes;
      state.seedboxFields = fields;
      state.seedboxClients = clients || [];
      renderSeedboxes();
    } catch (e) {
      host.replaceChildren(empty(e.message));
    }
  }

  // One labelled control, in the shape the rest of the settings page uses.
  function settingBox(label, control, help) {
    return el(
      'div',
      { class: 'setting' },
      el('label', {}, label),
      control,
      help ? el('p', { class: 'hint setting-help' }, help) : null,
    );
  }

  // How to reach one torrent client.
  //
  // This was a single box asking for qbittorrent+http://user:pass@host:8080,
  // with the other three clients' shapes listed underneath as a hint. That is
  // a config file with a border drawn round it: the scheme is two schemes
  // joined by a plus, which one depends on the client, the password goes in the
  // middle of the host, and every way of getting it wrong reads as "could not
  // connect". So it is a dropdown and the boxes that dropdown implies, and the
  // server composes the URL.
  function connectionFields(box) {
    const conn = box.connection || (box.connection = {});
    const clients = state.seedboxClients || [];
    const spec = clients.find((c) => c.id === conn.client) || null;
    const grid = el('div', { class: 'settings-grid' });

    grid.append(settingBox(
      'Client',
      el('select', { onchange: (e) => { conn.client = e.target.value; renderSeedboxes(); } },
         el('option', { value: '', selected: !conn.client }, 'Choose…'),
         ...clients.map((c) => el('option', { value: c.id, selected: c.id === conn.client }, c.label))),
      spec ? spec.help : 'Which program the finished torrent is handed to.',
    ));
    if (!spec) return grid;

    grid.append(settingBox(
      'Host',
      el('input', { type: 'text', value: conn.host || '', placeholder: '192.168.1.10',
                    oninput: (e) => (conn.host = e.target.value) }),
      'A name or an IP. Pasting the whole address works — the rest is taken off.',
    ));
    grid.append(settingBox(
      'Port',
      el('input', { type: 'number', min: '1', max: '65535', value: conn.port ?? '',
                    placeholder: String(spec.port),
                    oninput: (e) => (conn.port = e.target.value) }),
      `Blank means ${spec.port}.`,
    ));
    grid.append(settingBox(
      'Username',
      el('input', { type: 'text', autocomplete: 'off', value: conn.username || '',
                    oninput: (e) => (conn.username = e.target.value) }),
    ));
    const pwPlaceholder = conn.password_set ? '•••••••• (saved — type to replace)' : 'Not set';
    const pw = el('input', { type: 'password', autocomplete: 'new-password',
                             placeholder: pwPlaceholder,
                             oninput: (e) => (conn.password = e.target.value) });
    grid.append(settingBox(
      'Password',
      conn.password_set
        ? el('div', { class: 'secret-field' }, pw,
             revealButton(pw, async () => {
               const got = await api(
                 `/api/settings/seedboxes/secret?name=${encodeURIComponent(box.name || '')}`);
               return got.value;
             }, pwPlaceholder))
        : pw,
    ));

    if (spec.secure) {
      grid.append(el('div', { class: 'setting' },
        el('label', { class: 'check' },
           el('input', { type: 'checkbox', checked: !!conn.secure,
                         onchange: (e) => (conn.secure = e.target.checked) }),
           'Reached over HTTPS')));
    }
    if (spec.path !== null && spec.path !== undefined) {
      grid.append(settingBox(
        'Path',
        el('input', { type: 'text', value: conn.path || '', placeholder: spec.path || '/',
                      oninput: (e) => (conn.path = e.target.value) }),
        spec.path_required
          ? 'Where the RPC plugin answers. The default is right for a stock install.'
          : 'Only if a reverse proxy serves it under a sub-path.',
      ));
    }
    return grid;
  }

  function renderSeedboxes() {
    const host = $('#seedbox-editor');
    const boxes = state.seedboxes;

    const otherFields = (box) => {
      const grid = el('div', { class: 'settings-grid' });
      state.seedboxFields.forEach((field) => {
        // A field that only applies in one mode is not shown in the others.
        // Three dead rclone boxes on a client that can already see the files
        // is how a settings page turns back into a copy of the config file.
        if (field.when && String(box[field.when.key] ?? '') !== field.when.value) return;
        const optionLabel = (value) =>
          (field.labels && field.labels[value] !== undefined ? field.labels[value] : value);

        if (field.kind === 'bool') {
          grid.append(el('div', { class: 'setting' },
            el('label', { class: 'check' },
               el('input', { type: 'checkbox', checked: !!box[field.key],
                             onchange: (e) => (box[field.key] = e.target.checked) }),
               field.label),
            field.help ? el('p', { class: 'hint setting-help' }, field.help) : null));
          return;
        }

        let input;
        if (field.kind === 'choice') {
          input = el('select', {
            onchange: (e) => {
              box[field.key] = e.target.value;
              // Another field may appear or disappear because of this one.
              if (state.seedboxFields.some((f) => f.when && f.when.key === field.key)) renderSeedboxes();
            },
          }, ...field.choices.map((c) =>
            el('option', { value: c, selected: (box[field.key] || '') === c }, optionLabel(c))));
        } else if (field.kind === 'list') {
          input = el('input', { type: 'text', value: (box[field.key] || []).join(' '),
                                placeholder: field.placeholder || '' });
          input.addEventListener('input', () => {
            box[field.key] = input.value.split(/\s+/).filter(Boolean);
          });
        } else {
          input = el('input', {
            type: 'text',
            value: box[field.key] ?? '',
            placeholder: field.placeholder || '',
            oninput: (e) => (box[field.key] = e.target.value),
          });
        }
        grid.append(settingBox(field.label, input, field.help));
      });
      return grid;
    };

    const card = (box, index) => {
      const spec = (state.seedboxClients || []).find((c) => c.id === box.connection?.client);
      return el('div', { class: 'seedbox-card' },
        el('div', { class: 'row settings-head' },
           el('strong', {}, box.name || `Client ${index + 1}`),
           spec ? el('span', { class: 'tag dim' }, spec.label) : null,
           el('button', { type: 'button', class: 'danger',
                          onclick: () => { boxes.splice(index, 1); renderSeedboxes(); } }, 'Remove')),
        connectionFields(box),
        el('hr', { class: 'seedbox-rule' }),
        otherFields(box));
    };

    host.replaceChildren(
      boxes.length
        ? el('div', { class: 'seedbox-list' }, ...boxes.map(card))
        : el('p', { class: 'hint' }, 'No torrent client yet, so finished uploads are not seeded automatically.'),
      el('div', { class: 'row' },
         el('button', { type: 'button', onclick: () => {
           boxes.push({ name: `client ${boxes.length + 1}`, enabled: true, type: 'local',
                        directory: '', label: '', extra_args: [], connection: {} });
           renderSeedboxes();
         } }, '+ Add a client'),
         el('button', { type: 'button', class: 'primary', onclick: saveSeedboxes }, 'Save clients')),
    );
  }

  async function saveSeedboxes() {
    try {
      await api('/api/settings/seedboxes', { method: 'PUT', body: { seedboxes: state.seedboxes } });
      toast('Torrent clients saved', 'ok');
      loadSeedboxes();
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  async function clearDebug() {
    await api('/api/debug/clear', { method: 'POST' });
    loadDebug();
  }

  async function saveSettings() {
    const changes = state.pending;
    if (!Object.keys(changes).length) return;
    const button = $('#settings-save');
    button.disabled = true;
    button.textContent = 'Saving…';
    try {
      const result = await api('/api/settings', { method: 'PUT', body: { changes } });
      toast(`Saved ${result.saved.length} setting(s)`, 'ok');
      if (result.unapplied?.length) toast(`Could not apply: ${result.unapplied.join(', ')}`, 'bad');
      state.pending = {};
      await refreshStatus();
      await loadSettings();
    } catch (e) {
      toast(e.message, 'bad');
      button.disabled = false;
    } finally {
      button.textContent = 'Save changes';
    }
  }

  async function runTest(target, button, boxOrId) {
    const box = typeof boxOrId === 'string' ? $(`#${boxOrId}`) : (boxOrId || $(`#test-${target}`));
    if (!box) return;
    const label = button.textContent;
    button.disabled = true;
    button.textContent = 'Testing…';
    box.hidden = false;
    box.className = 'test-result';
    box.replaceChildren(el('span', { class: 'spinner' }), ' Contacting…');

    try {
      // Send whatever is typed but not yet saved, so Test works before Save.
      // The server applies it for the call and rolls it back afterwards.
      const body = { values: state.pending };
      // The torrent clients are a list, not dotted keys, so they travel
      // separately — otherwise a connection could only be tested after saving
      // it, which is the wrong way round.
      if (target === 'qbittorrent') body.seedboxes = state.seedboxes || [];
      const result = await api(`/api/settings/test/${target}`, { method: 'POST', body });
      box.className = `test-result ${result.ok ? 'pass' : 'warn'}`;
      const detail = result.detail && Object.keys(result.detail).length
        ? el(
            'dl',
            { class: 'meta test-detail' },
            ...Object.entries(result.detail).flatMap(([k, v]) => [
              el('dt', {}, k),
              el('dd', {}, typeof v === 'object' ? JSON.stringify(v) : String(v)),
            ]),
          )
        : null;
      // replaceChildren stringifies non-Node arguments, so a null detail
      // would literally render the word "null".
      box.replaceChildren(
        ...[el('strong', {}, result.ok ? '✓ ' : '✕ '), result.message, detail].filter((n) => n !== null),
      );
    } catch (e) {
      box.className = 'test-result warn';
      box.textContent = `✕ ${e.message}`;
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }

  async function signOut() {
    try {
      await api('/api/auth/logout', { method: 'POST' });
    } catch {
      // Either way the cookie is gone or never existed; go to the login page.
    }
    location.replace('/login');
  }

  async function clearHistory(collection) {
    const { cleared } = await api(`/api/history/${collection}/clear`, { method: 'POST' });
    toast(`Cleared ${cleared} stored ${collection} entries`, 'ok');
  }

  // ---------------------------------------------------------------- wiring

  // Drawn rather than typed. The text glyphs these replaced (a crescent and a
  // sun) come from whatever font happens to have them, so they arrived at
  // different weights, different sizes and off the baseline. resolvedTheme only
  // ever answers dark or light, so those are the only two needed.
  const ICON = (paths) =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
          stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;

  const THEME_ICON = {
    dark: ICON('<path d="M20.5 14.8A8.5 8.5 0 1 1 9.2 3.5a7 7 0 0 0 11.3 11.3Z"/>'),
    light: ICON(
      '<circle cx="12" cy="12" r="4"/>' +
        '<path d="M12 2.5v2M12 19.5v2M4.4 4.4l1.4 1.4M18.2 18.2l1.4 1.4' +
        'M2.5 12h2M19.5 12h2M4.4 19.6l1.4-1.4M18.2 5.8l1.4-1.4"/>',
    ),
  };

  function resolvedTheme(theme) {
    if (theme !== 'system') return theme;
    return matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }

  function applyTheme(choice) {
    const theme = choice || localStorage.getItem('lox-theme') || 'system';
    localStorage.setItem('lox-theme', theme);
    if (theme === 'system') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', theme);

    const toggle = $('#theme-toggle');
    if (toggle) {
      toggle.innerHTML = THEME_ICON[resolvedTheme(theme)];
      toggle.title = `Theme: ${theme}. Click to switch.`;
    }
    $$('#theme-picker button').forEach((b) => b.classList.toggle('active', b.dataset.theme === theme));
  }

  // The topbar icon flips between the two you actually use; the three-way
  // choice including Auto lives in Settings.
  function toggleTheme() {
    applyTheme(resolvedTheme(localStorage.getItem('lox-theme') || 'system') === 'dark' ? 'light' : 'dark');
  }

  function init() {
    applyTheme();
    $('#theme-toggle')?.addEventListener('click', toggleTheme);

    $$('.nav-item').forEach((b) => {
      // The nav is buttons, not links, so give each one the address it goes
      // to. It is what a screen reader reads out and what the status bar
      // shows.
      const path = VIEW_PATHS[b.dataset.view];
      if (path) b.setAttribute('data-path', path);
      b.addEventListener('click', () => go(navAddr(b.dataset.view)));
    });
    // Searching is going somewhere, so it is a navigation like any other: the
    // results have an address, they survive a reload, and Back leaves them for
    // whatever you were looking at before rather than for the app's front door.
    $('#search-form').addEventListener('submit', (event) => {
      event.preventDefault();
      go(addr('/search', { q: $('#search-input').value.trim(), type: typeParam() }));
    });
    $$('#search-type button').forEach((b) =>
      b.addEventListener('click', () => selectSearchType(b.dataset.type)),
    );
    $$('#explore-tabs button').forEach((b) =>
      b.addEventListener('click', () => go(addr(BROWSE_PATHS[b.dataset.explore] || '/browse/channels', {
        // Channels has no genre filter, so carrying one there would put a
        // parameter in the address that nothing on screen answers to.
        genre: b.dataset.explore === 'channels' ? '' : genreParam(),
      }))),
    );
    $('#missing-scan').addEventListener('click', missingScan);
    // Re-checks whatever is ticked in the results, which is the only place a
    // subset makes sense.
    $('#missing-check').addEventListener('click', () => missingCheck());
    $$('#scan-tabs button').forEach((b) => {
      b.addEventListener('click', () =>
        go(addr(b.dataset.scantab === 'history' ? '/scan/history' : '/scan')));
    });
    $('#scanhistory-rerun').addEventListener('click', scanHistoryRerun);
    $('#scanhistory-forget').addEventListener('click', scanHistoryForget);
    $('#scanhistory-clear-filters').addEventListener('click', () => {
      const view = tableView('scanhistory');
      view.filters = {};
      view.sort = null;
      renderScanHistoryRows();
    });

    $$('#requests-tabs button').forEach((b) => {
      b.addEventListener('click', () => go(b.dataset.reqtab === 'history'
        ? addr('/requests/history')
        : addr('/requests', { tracker: state.requestsTracker || '' })));
    });
    $('#history-rerun').addEventListener('click', historyRerun);
    // The filters live in the columns now, so clearing them is clearing the
    // table's own state rather than emptying a form above it.
    $('#history-clear-filters').addEventListener('click', () => {
      const view = tableView('history');
      view.filters = {};
      view.sort = null;
      renderHistoryRows();
    });

    // The default does the whole job; the other one stops at the list.
    $('#requests-fetch-check').addEventListener('click', () => requestsFetch({ thenCheck: true }));
    $('#requests-fetch').addEventListener('click', () => requestsFetch());
    // Stops before the next page is paid for, rather than after all of them.
    // The check that may follow has its own Stop button, next to its log.
    $('#requests-cancel').addEventListener('click', () => state.requestsAbort?.abort());
    $('#requests-check-pasted').addEventListener('click', () => {
      const ids = idsFrom($('#requests-ids').value);
      if (!ids.length) return toast('Paste at least one request ID or URL', 'bad');
      requestsCheck(ids, { placeholders: true });
    });
    // Picking a file starts the check. Loading the ids into a box and waiting
    // for a second press was a step with nothing in it -- you chose the file
    // because you wanted it checked.
    $('#requests-file').addEventListener('change', async (e) => {
      const file = e.target.files?.[0];
      if (!file) return;
      const ids = idsFrom(await file.text());
      $('#requests-ids').value = ids.join('\n');
      e.target.value = '';
      if (!ids.length) return toast(`No request IDs found in ${file.name}`, 'bad');
      toast(`Checking ${ids.length} request(s) from ${file.name}`, 'ok');
      requestsCheck(ids, { placeholders: true });
    });
    $('#watchlist-form').addEventListener('submit', saveWatchlist);
    $('#downloads-clear').addEventListener('click', async () => {
      await api('/api/downloads/clear', { method: 'POST' });
      pollDownloads(true);
    });
    $('#found-download').addEventListener('click', () =>
      bulkDownload(foundSelection().map((f) => ({ id: f.album_id, item: f }))));
    $('#found-upload').addEventListener('click', () =>
      bulkDownloadAndUpload(foundSelection().map((f) => ({ id: f.album_id, item: f }))));
    $('#found-recheck').addEventListener('click', recheckFound);
    $('#found-dismiss').addEventListener('click', () => dismissFound(false));
    $('#found-blacklist').addEventListener('click', () => dismissFound(true));
    $('#found-restore').addEventListener('click', restoreFound);

    // Filtering is local: it never refetches, so it stays instant on a long
    // queue and costs no tracker budget.
    // Showing the excluded rows changes what the list is, so it is part of
    // where you are: a queue read with them shown survives a reload, and the
    // toggle is something Back can undo.
    $('#found-held-toggle').addEventListener('click', () =>
      go(addr('/queue', { held: state.showHeld ? '' : '1' })));
    $('#folders-refresh').addEventListener('click', loadFolders);
    $('#upload-dry-run').addEventListener('change', (e) =>
      setUploadFlag('upload.dry_run', e.target, 'Dry run'));
    $('#upload-yes-all').addEventListener('change', (e) =>
      setUploadFlag('upload.yes_all', e.target, 'Auto-answer prompts'));
    refreshStatus();
    setInterval(refreshStatus, 15000);

    // Back and Forward move through the app rather than out of it. The
    // address is already the browser's by the time this fires, so it is only
    // ever drawn, never pushed.
    window.addEventListener('popstate', () => renderRoute(here()));

    // Open on whatever the address says. Reloading anywhere used to land on
    // Search, because nothing had ever read the address. The first entry is
    // replaced rather than pushed, so Back from the page you opened on leaves
    // the app instead of stepping through a duplicate of it.
    go(location.pathname === '/' ? addr('/search') : here(), { replace: true });
  }

  document.addEventListener('DOMContentLoaded', init);
})();
