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
    // What a check said about each collected album, by album id.
    scanResults: new Map(),
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
    // Narrowing what is on screen. Not persisted: it is a way of reading the
    // list, not a setting.
    // Releases ticked for a batch action, by album id.
    picked: new Map(),
    // Which trackers an upload goes to, in the order it goes to them. A
    // list rather than a set: uploading to two trackers is two uploads, one
    // after the other, and which of them goes first is a decision -- the
    // first one to post owns the group the second one adds to.
    uploadTrackers: [],
    // The download folder as it was last read, and what is ticked in it.
    folders: [],
    // The tracker chip being dragged into a new position, if any.
    draggingTarget: null,
    // Folders waiting their turn, and the one being uploaded now.
    uploadQueue: [],
    uploadCurrent: null,
    selectedFolders: new Set(),
    albumCheck: null,
    watchlists: [],
    // Which saved searches are ticked, so several can be scanned as one job.
    watchSelected: new Set(),
    queueTab: 'queue',
    blacklist: [],
    blacklistSelected: new Set(),
    // Downloads whose quality has already been queried, so a poll every second
    // does not ask the same question every second.
    qualityAsked: new Set(),
    // Flow steps whose answer is on its way, so a second press cannot send a
    // second one and be told the first had already arrived.
    answering: new Set(),
    // When each upload switch was last written from this page, so a status
    // poll carrying the value from before the click cannot undo it.
    flagWrittenAt: {},
    // The one being renamed, so a re-render does not close the box you are
    // typing in.
    watchEditing: null,
    linking: false,
    requestRows: [],
    selectedRequests: new Set(),
    // The running search, so Cancel has something to stop and a second click
    // on Search cannot start a parallel one.
    requestsAbort: null,
    requestTab: 'find',
    scanTab: 'run',
    uploadTab: 'folders',
    uploads: [],
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
    // Every check result, matched or not, so the Result column survives a
    // sort, a filter or any other re-render. It used to live only in the
    // cell, so narrowing the list threw away everything already looked up.
    requestResults: new Map(),
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

  /**
   * Replace a node's children, dropping the ones that are not there.
   *
   * `replaceChildren` is not `el`: handed a null it appends a text node
   * reading "null", so a `condition ? node : null` argument prints the word
   * on the page. It has done exactly that three times now -- under a search
   * summary, between the tracker chips, and on the accounts panel -- so the
   * filtering lives here rather than being remembered at each call.
   *
   * @param {Element} node - What to fill.
   * @param {...(Node|null|undefined|false)} children - What to fill it with.
   */
  function fill(node, ...children) {
    node.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
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
  /**
   * What a stored choice filter selected.
   *
   * Older views stored a single string; a filter is a set now, so both shapes
   * are read rather than one of them silently meaning "nothing selected".
   *
   * @param {*} stored - Whatever the view had.
   * @returns {string[]} The selected options.
   */
  function pickedChoices(stored) {
    if (Array.isArray(stored)) return stored.filter(Boolean);
    return stored ? [String(stored)] : [];
  }

  /**
   * The individual facts a choice column says about one row.
   *
   * A column may set `parts` when its value is several things joined together
   * -- the tracker verdicts, the sources a release came from. Without it the
   * whole value is the one fact, which is right for a column like Tracker
   * whose value is a single code.
   *
   * @param {object} column - The column definition.
   * @param {object} row - The row.
   * @returns {string[]} Non-empty facts.
   */
  function choiceParts(column, row) {
    const parts = column.parts
      ? column.parts(row)
      : [column.value ? column.value(row) : ''];
    return (parts || []).map((p) => String(p ?? '').trim()).filter(Boolean);
  }

  function dataTable({ name, rows, columns, selection = null, onShown = null, rowAttrs = null,
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
      // A date column filters the way anybody asks about one: "in the last
      // six hours", "older than three months". Both ends are a number of
      // something, so the column says which something -- a window measured in
      // days is not the same question as one measured in hours, and a pair of
      // boxes with no unit on them is not a question at all.
      if (column.filter === 'range' || column.filter === 'days') {
        // Two limits, either of which may be left empty: "1990 to blank"
        // means everything from 1990 on, which is how people ask.
        const scale = column.filter === 'days' ? unitSize(wanted && wanted.unit) : 1;
        const low = wanted && wanted.low !== '' ? Number(wanted.low) * scale : null;
        const high = wanted && wanted.high !== '' ? Number(wanted.high) * scale : null;
        if (low === null && high === null) continue;
        shown = shown.filter((row) => {
          const value = Number(valueOf(column, row));
          if (!Number.isFinite(value)) return false;
          if (low !== null && value < low) return false;
          return !(high !== null && value > high);
        });
        continue;
      }
      // A choice column is a set of facts about a row, not one string. The
      // Trackers cell says "RED missing", "OPS has it" AND "already on
      // tracker"; the filter offered whole joined values, so the only way to
      // ask for one fact was to find a row that happened to have exactly that
      // combination -- and "already on tracker" was not in the list at all,
      // because it was drawn in the cell and never went into the value.
      if (column.filter === 'choice') {
        const chosen = pickedChoices(view.filters[column.label]);
        if (!chosen.length) continue;
        const set = new Set(chosen.map((c) => c.toLowerCase()));
        shown = shown.filter((row) =>
          choiceParts(column, row).some((part) => set.has(part.toLowerCase())));
        continue;
      }
      if (!wanted) continue;
      shown = shown.filter((row) =>
        String(valueOf(column, row) ?? '').toLowerCase().includes(String(wanted).toLowerCase()));
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
      host.replaceWith(
        dataTable({ name, rows, columns, selection, onShown, rowAttrs, idOf, empty: emptyText }));
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
          const options = [...new Set(rows.flatMap((r) => choiceParts(column, r)))].sort();
          const chosen = pickedChoices(view.filters[column.label]);
          const set = new Set(chosen);
          const apply = (next) => {
            view.filters[column.label] = next.length ? next : null;
            rerender();
          };
          const summary = !chosen.length
            ? 'all'
            : chosen.length === 1 ? chosen[0] : `${chosen.length} of ${options.length}`;
          const panel = el('div', { class: 'th-choices' },
            el('div', { class: 'th-choices-head' },
              el('button', { type: 'button', class: 'linkbtn',
                             onclick: () => apply([...options]) }, 'All'),
              el('button', { type: 'button', class: 'linkbtn',
                             onclick: () => apply([]) }, 'None')),
            ...options.map((option) => el('label', { class: 'th-choice' },
              el('input', {
                type: 'checkbox',
                checked: set.has(option),
                onchange: (e) => {
                  const next = new Set(set);
                  if (e.target.checked) next.add(option); else next.delete(option);
                  // Every box ticked is the same question as none, and reads
                  // better as "all" than as "7 of 7".
                  apply(next.size === options.length ? [] : [...next]);
                },
              }),
              el('span', {}, option))));
          // A details/summary rather than a popover library: it opens in place,
          // closes on the next click anywhere, and needs no script to do it.
          control = el('details', { class: 'th-choicebox' },
            el('summary', { class: 'th-filter', title: `Filter by ${column.label.toLowerCase()}` },
               summary),
            panel);
        } else if (column.filter === 'range' || column.filter === 'days') {
          const isDays = column.filter === 'days';
          const blank = { low: '', high: '', unit: DEFAULT_UNIT };
          const current = { ...blank, ...(view.filters[column.label] || {}) };
          const put = (patch, wait) => {
            view.filters[column.label] = { ...current, ...patch };
            clearTimeout(view.timer);
            if (wait) view.timer = setTimeout(rerender, 260);
            else rerender();
          };
          const limit = (which, placeholder, title) => el('input', {
            class: 'th-filter th-range',
            type: 'number',
            min: isDays ? '0' : null,
            placeholder,
            title,
            value: current[which],
            oninput: (e) => put({ [which]: e.target.value }, true),
          });
          control = el('div', { class: 'th-range-pair' },
            limit('low', column.lowLabel || 'min',
                  isDays ? 'At least this long ago' : 'No less than this'),
            limit('high', column.highLabel || 'max',
                  isDays ? 'At most this long ago' : 'No more than this'),
            // Which unit those two numbers are in. Without it the boxes were a
            // pair of numbers with nothing saying what they measured, and the
            // answer was always days whether or not days was the useful window.
            isDays
              ? el('select', {
                  class: 'th-filter th-unit',
                  title: 'What the two numbers are measured in',
                  onchange: (e) => put({ unit: e.target.value }, false),
                },
                ...TIME_UNITS.map(([name]) =>
                  el('option', { value: name, selected: current.unit === name }, name)))
              : null);
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

        // A column is a label, the filter that narrows it and the values --
        // one stack on one centre line -- so the header has to know which
        // column it is heading. Only the alignment is shared: the column's own
        // class still dresses the DATA cell alone, because putting all of it
        // on the header put `display: flex` on the trackers header and laid
        // the label out beside its filter while every other one stacked them.
        return el('th', { class: column.text ? 'col-text' : null },
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
      el('tr', rowAttrs ? rowAttrs(row) : {},
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
        ...columns.map((column) =>
          el('td', { class: `${column.class || ''}${column.text ? ' col-text' : ''}`.trim() },
             column.cell(row)))));

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

  //: What a date filter's two numbers can be measured in, and how many days
  //: one of each is. "Checked in the last six hours" and "added more than
  //: three months ago" are both questions people ask about the same column.
  const TIME_UNITS = [['hours', 1 / 24], ['days', 1], ['weeks', 7], ['months', 30], ['years', 365]];
  const DEFAULT_UNIT = 'days';

  /** How many days one of a unit is. Unknown units count as days. */
  const unitSize = (unit) => (TIME_UNITS.find(([name]) => name === unit) || [null, 1])[1];

  /**
   * How many days ago a stored timestamp was.
   *
   * @param {number} stamp - Epoch seconds, or falsy when nothing is recorded.
   * @returns {number} Whole-ish days, or -1 for "never" -- which sorts before
   *   everything and is excluded by any range filter, both of which are what
   *   you want from a row nothing has happened to.
   */
  function daysAgo(stamp) {
    const seconds = Number(stamp);
    if (!seconds) return -1;
    return Math.max(0, (Date.now() / 1000 - seconds) / 86400);
  }

  /**
   * Whether a queue row is old enough to be confirmed again.
   *
   * The confirmation happens by itself, in the background, whenever nothing
   * else is running -- so this is not a chore being handed to the reader, it
   * is the row saying which side of the line it is on.
   *
   * @param {number} stamp - When it was last checked.
   * @returns {string} A note for the cell, or "".
   */
  function staleNote(stamp) {
    const window_ = state.queueRecheck?.after_days || 0;
    if (!window_ || !stamp) return '';
    return daysAgo(stamp) >= window_ ? 'due a re-check' : '';
  }

  /** A when-column cell: how long ago on top, the date itself underneath. */
  function whenCell(stamp, note = '') {
    if (!stamp) return el('span', {}, '\u2014');
    return el('span', {},
      el('div', {}, ago(stamp)),
      el('span', { class: 'hint' }, [checkedOn(stamp), note].filter(Boolean).join(' \u00b7 ')));
  }

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
  // search, a Browse tab, a genre, a channel, a request and a place on the
  // settings page all carried the address of whichever screen you had arrived
  // from. Back skipped every one of them at
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
  const ROUTE_KEYS = ['q', 'type', 'genre', 'tracker'];

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
  /**
   * Keep the address in step with the tab that is actually showing.
   *
   * These two screens put their tab in the address, and a couple of places
   * switched the tab directly -- "Show me what they said" on the skipped
   * note, for one. The content moved and the address did not, so the tab
   * buttons pointed at where you already were: pressing "Find requests" asked
   * to go to /requests while the address still said /requests, go() saw the
   * same address and did nothing, and the only way out was to press the other
   * tab first. Replacing rather than pushing, because switching tab in place
   * is not a second entry in the history.
   *
   * @param {string} path - The address for the tab now showing.
   */
  function keepAddress(path) {
    const target = addr(path);
    const [now] = splitAddr(here());
    if (splitAddr(target)[0] === now) return;
    history.replaceState({ url: target }, '', target);
    showingUrl = target;
  }

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
      case '/queue': showQueue(); return;
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
    if (view === 'found') return addr('/queue');
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
   * The Uploading screen, on one of its two tabs.
   *
   * @param {string} tab - folders or history.
   */
  function showUploadTab(name) {
    const wanted = name === 'history' ? 'history' : 'folders';
    state.uploadTab = wanted;
    $('#upload-tab-folders').hidden = wanted !== 'folders';
    $('#upload-tab-history').hidden = wanted !== 'history';
    $$('#upload-tabs button').forEach((b) => {
      b.classList.toggle('active', b.dataset.uptab === wanted);
    });
    if (wanted === 'history') loadUploadHistory();
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

  /** The Queue, on one of its two tabs. */
  function showQueue() {
    // Already here: re-draw rather than re-fetch, so nothing you have ticked
    // is thrown away by arriving at the address you are already at.
    if (state.view === 'found') { renderFound(); setTitle(); return; }
    setView('found');
  }

  /**
   * Which half of the Queue screen is showing.
   *
   * @param {string} name - queue or blacklist.
   */
  function showQueueTab(name) {
    const wanted = name === 'blacklist' ? 'blacklist' : 'queue';
    state.queueTab = wanted;
    $('#queue-tab-queue').hidden = wanted !== 'queue';
    $('#queue-tab-blacklist').hidden = wanted !== 'blacklist';
    $$('#queue-tabs button').forEach((b) => {
      b.classList.toggle('active', b.dataset.queuetab === wanted);
    });
    if (wanted === 'blacklist') loadBlacklist();
  }

  // --------------------------------------------------------- blacklist
  //
  // Saying "never show me this again" was a one-way door: the id went into a
  // file nothing could read back, the album record it came from was deleted in
  // the same breath, and the only way out was to clear the whole list. It is a
  // list now, with names on it, and one can be let back in without letting all
  // of them back in.

  function blacklistPick() {
    const shown = tableView('blacklist').shown || state.blacklist;
    const n = countSelected(shown, state.blacklistSelected);
    const button = $('#blacklist-restore');
    if (button) {
      button.disabled = n === 0;
      button.textContent = n ? `Take ${n} off the blacklist` : 'Take off the blacklist';
    }
  }

  async function loadBlacklist() {
    const host = $('#blacklist-results');
    host.replaceChildren(spinner('Loading'));
    try {
      const { blacklisted, total } = await api('/api/blacklist');
      state.blacklist = blacklisted;
      state.blacklistSelected = new Set();
      $('#blacklist-count').textContent = total
        ? `${total} release${total === 1 ? '' : 's'} refused`
        : 'nothing refused';
      renderBlacklist();
    } catch (e) {
      host.replaceChildren(empty(e.message));
    }
  }

  function renderBlacklist() {
    $('#blacklist-results').replaceChildren(dataTable({
      name: 'blacklist',
      rows: state.blacklist,
      selection: { set: state.blacklistSelected, onChange: blacklistPick },
      onShown: blacklistPick,
      empty: 'Nothing has been blacklisted.',
      columns: [
        {
          label: 'Release',
          text: true,
          value: (r) => `${r.artist || ''} ${r.title || ''} ${r.album_id}`.trim(),
          filter: 'text',
          cell: (r) => el('a', {
            href: albumHref(r.album_id),
            onclick: (e) => { e.preventDefault(); goAlbum(r.album_id); },
          }, r.artist || r.title
            ? `${r.artist || '?'} — ${r.title || r.album_id}`
            : `Release ${r.album_id}`),
        },
        {
          label: 'Refused',
          value: (r) => daysAgo(r.at),
          filter: 'days',
          class: 'nowrap',
          cell: (r) => whenCell(r.at),
        },
      ],
    }));
    blacklistPick();
  }

  /** Let the ticked releases be found again. */
  async function restoreBlacklisted() {
    const shown = tableView('blacklist').shown || state.blacklist;
    const ids = shown.filter((r) => state.blacklistSelected.has(r.id)).map((r) => r.id);
    if (!ids.length) return;
    try {
      const { restored } = await api('/api/found/restore', { method: 'POST', body: { ids } });
      toast(`${restored} taken off the blacklist. A scan can find them again.`, 'ok');
      state.found = [];
      await loadBlacklist();
    } catch (e) {
      toast(e.message, 'bad');
    }
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
    state.queueRecheck = status.queue_recheck || null;
    if (state.view === 'found') renderQueueUpkeep();
    renderBudgets();
    if (!state.missingTrackers.size) state.trackers.forEach((t) => state.missingTrackers.add(t.code));
    renderTrackerPickers();

    railCount('#dl-badge', status.downloads.active);
    // From the poll, not from the queue drawing itself. The number beside
    // Queue only moved when that tab was open, so a scan running elsewhere
    // filled the queue and the rail went on showing whatever it last said.
    if (status.queue) railCount('#found-count-rail', status.queue.size);
    $('#downloads-dir').textContent = `Saving to ${status.downloads.directory} as ${status.downloads.format}`;
    $('#uploads-dir').textContent = status.downloads.directory;
    renderProblems(status.problems);
    syncUploadToggles(status.upload);
  }

  // Reflect the stored setting, unless it has just been changed here.
  //
  // A status poll every fifteen seconds can be in flight while the box is
  // clicked, and it carries the value from before the click. Landing after the
  // save, it put the box back -- so the toast said "Auto-answer prompts on"
  // and the box was off, which is the app calling itself a liar. A box that
  // was written to locally is left alone until the answer to that write has
  // been round the loop.
  const SETTLE_MS = 4000;

  function syncUploadToggles(upload) {
    if (!upload) return;
    const now = Date.now();
    for (const [id, value] of [['upload-dry-run', upload.dry_run], ['upload-yes-all', upload.yes_all]]) {
      const box = $(`#${id}`);
      if (!box || box === document.activeElement) continue;
      if (now - (state.flagWrittenAt[id] || 0) < SETTLE_MS) continue;
      box.checked = !!value;
    }
  }

  // Writes through to the one setting rather than keeping a per-page copy.
  async function setUploadFlag(key, box, label) {
    const value = box.checked;
    box.disabled = true;
    state.flagWrittenAt[box.id] = Date.now();
    try {
      await api('/api/settings', { method: 'PUT', body: { changes: { [key]: value } } });
      // Read back rather than assumed. The toast used to fire on the strength
      // of the request not throwing, which says nothing about what was stored.
      const status = await api('/api/status');
      const stored = key === 'upload.dry_run' ? status.upload?.dry_run : status.upload?.yes_all;
      box.checked = !!stored;
      state.flagWrittenAt[box.id] = Date.now();
      if (!!stored === value) {
        toast(`${label} ${value ? 'on' : 'off'}`, 'ok');
      } else {
        toast(`${label} did not change — it is still ${stored ? 'on' : 'off'}`, 'bad');
      }
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

    renderUploadTargets();
    requestsCost();
  }

  /**
   * Which trackers an upload goes to, and in what order.
   *
   * Uploading to two trackers is two posts in sequence, and which goes first
   * decides who owns the group the second one adds to -- so the order is a
   * real decision and the list is a real list. You drag it into the order you
   * want, which is what everyone tries first; the arrows this replaces were
   * two more buttons on a row that already had too many.
   *
   * Clicking a tracker turns it on or off. Turning off the last one you had on
   * used to empty the list, and the next render quietly refilled it with the
   * FIRST configured tracker -- so with only OPS on, pressing OPS gave you
   * RED, while with only RED on, pressing RED gave you RED again. One
   * direction worked and the other did nothing, for no visible reason. There
   * has to be a tracker selected, so the last one off hands over to the next.
   */
  //: The order trackers run in when nothing has been dragged. Anything not
  //: named here keeps its configured position, after the ones that are.
  const UPLOAD_ORDER = ['OPS', 'RED'];
  const uploadRank = (code) => {
    const at = UPLOAD_ORDER.indexOf(code);
    return at < 0 ? UPLOAD_ORDER.length : at;
  };

  function renderUploadTargets() {
    const host = $('#upload-tracker');
    if (!host) return;
    const codes = state.trackers.map((t) => t.code);
    // A tracker whose credentials were removed is not somewhere to upload to.
    state.uploadTrackers = state.uploadTrackers.filter((c) => codes.includes(c));
    // Everything configured, OPS first. Posting to one tracker and then
    // remembering the other is a second pass over the same release, so the
    // default is the thing you almost always want; deselecting is one click
    // and is remembered from then on.
    if (!state.uploadTrackers.length && codes.length) {
      state.uploadTrackers = [...codes].sort(
        (a, b) => uploadRank(a) - uploadRank(b) || codes.indexOf(a) - codes.indexOf(b),
      );
    }

    const toggle = (code) => {
      const at = state.uploadTrackers.indexOf(code);
      if (at < 0) {
        state.uploadTrackers.push(code);
      } else if (state.uploadTrackers.length > 1) {
        state.uploadTrackers.splice(at, 1);
      } else {
        // The last one: hand over rather than leaving nothing selected.
        state.uploadTrackers = [codes[(codes.indexOf(code) + 1) % codes.length]];
      }
      renderUploadTargets();
    };

    /**
     * Move one tracker to another's position.
     *
     * By index rather than by "insert before that one": inserting before the
     * chip to your right puts you back exactly where you started, so dragging
     * RED onto OPS did nothing at all while dragging OPS onto RED worked.
     * Taking the target's index means everything between shuffles up, which is
     * what dragging a thing onto another thing means everywhere else.
     *
     * @param {string} code - The tracker being moved.
     * @param {string|null} target - The one it was dropped on, or null for the
     *   end of the row.
     */
    const moveTo = (code, target) => {
      const from = state.uploadTrackers.indexOf(code);
      if (from < 0) return;
      const to = target ? state.uploadTrackers.indexOf(target) : state.uploadTrackers.length - 1;
      if (to < 0 || to === from) return;
      const order = [...state.uploadTrackers];
      order.splice(from, 1);
      order.splice(to, 0, code);
      state.uploadTrackers = order;
      renderUploadTargets();
    };

    // Selected first, in their running order, then the rest.
    const ordered = [...state.uploadTrackers, ...codes.filter((c) => !state.uploadTrackers.includes(c))];
    const several = state.uploadTrackers.length > 1;

    const chip = (code) => {
      const at = state.uploadTrackers.indexOf(code);
      const on = at >= 0;
      const node = el('button', {
        type: 'button',
        class: `upload-target${on ? ' on' : ''}${several && on ? ' draggable' : ''}`,
        // Only a selected tracker has a position, so only a selected tracker
        // has anything to drag.
        //
        // The string, not the boolean: `draggable` is an enumerated attribute,
        // and el() writes a `true` as an empty value -- which is not one of
        // the words it accepts, so the browser fell back to the default and
        // nothing could be picked up.
        draggable: several && on ? 'true' : 'false',
        'data-code': code,
        title: on
          ? (several
              ? `${code} runs ${at === 0 ? 'first' : `${at + 1} of ${state.uploadTrackers.length}`}. Drag to reorder.`
              : `Uploading to ${code}`)
          : `Also upload to ${code}`,
        onclick: () => toggle(code),
        ondragstart: (e) => {
          state.draggingTarget = code;
          node.classList.add('dragging');
          e.dataTransfer.effectAllowed = 'move';
          // Firefox will not start a drag without something on the transfer.
          e.dataTransfer.setData('text/plain', code);
        },
        ondragend: () => {
          state.draggingTarget = null;
          $$('.upload-target').forEach((n) => n.classList.remove('dragging', 'drop-before'));
        },
        ondragover: (e) => {
          if (!state.draggingTarget || state.draggingTarget === code) return;
          if (!state.uploadTrackers.includes(code)) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = 'move';
          node.classList.add('drop-before');
        },
        ondragleave: () => node.classList.remove('drop-before'),
        ondrop: (e) => {
          e.preventDefault();
          node.classList.remove('drop-before');
          const dragged = state.draggingTarget || e.dataTransfer.getData('text/plain');
          if (dragged && dragged !== code) moveTo(dragged, code);
        },
      },
      several && on ? el('span', { class: 'target-order' }, String(at + 1)) : null,
      code);
      return node;
    };

    fill(host,
      ...ordered.map(chip),
      // The drop target for "put it last": without one, dragging past the end
      // of the row has nowhere to land.
      several
        ? el('span', {
            class: 'target-end',
            ondragover: (e) => {
              if (!state.draggingTarget) return;
              e.preventDefault();
              e.currentTarget.classList.add('drop-before');
            },
            ondragleave: (e) => e.currentTarget.classList.remove('drop-before'),
            ondrop: (e) => {
              e.preventDefault();
              e.currentTarget.classList.remove('drop-before');
              const dragged = state.draggingTarget || e.dataTransfer.getData('text/plain');
              if (dragged) moveTo(dragged, null);
            },
          })
        : null,
    );
    const note = $('#upload-order-note');
    if (note) note.textContent = several ? 'drag to reorder' : '';
  }

  /**
   * Where one release should actually go.
   *
   * The toggles above say where uploads go in general. A release that has been
   * checked says something more specific: it is missing from RED and OPS
   * already has it, and uploading it to OPS would be a duplicate somebody else
   * has to report. So the check wins where there is one, and the toggles only
   * decide the order it runs in.
   *
   * @param {object} [item] - A queue row, or anything carrying missing_from.
   * @returns {string[]} Tracker codes, in the order set on the Uploading tab.
   */
  function uploadTargets(item) {
    const codes = state.trackers.map((t) => t.code);
    const order = [...state.uploadTrackers, ...codes.filter((c) => !state.uploadTrackers.includes(c))];
    const missing = ((item && item.missing_from) || []).filter((c) => codes.includes(c));
    if (!missing.length) return [...state.uploadTrackers];
    return order.filter((c) => missing.includes(c));
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
    fill(bar,
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
    if (!state.uploadTrackers.length) return toast('Pick a tracker to upload to first', 'bad');
    clearPicks();
    go(addr('/downloading'));
    toast(`Downloading ${entries.length} together. Uploads start as each one lands.`);

    const started = await Promise.all(entries.map(async ({ id, item }) => ({
      id,
      item,
      label: `${item?.artist || ''} - ${item?.title || ''}`.trim() || String(id),
      queued: await download(id),
    })));

    // One at a time from here, in the order they were picked.
    for (const { id, item, label, queued } of started) {
      if (!queued) continue;
      const job = await waitForDownload(queued.id);
      if (!job || job.status !== 'done' || !job.folder) {
        toast(`${label}: ${job?.error || 'download did not finish'}`, 'bad');
        continue;
      }
      go(addr('/uploading'));
      // Where this one release is missing from, not where uploads go in
      // general: a release OPS already has should not be offered to OPS.
      await startUpload(job.folder, uploadTargets(item), id);
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
    // Picked one at a time, so the scan's filters do not apply to them: they
    // exist to stop a sweep of a channel module spending budget on four
    // hundred singles, and you have already made that decision by ticking a
    // release and pressing the button.
    //
    // And then actually check them. This used to stop here, having moved you
    // to another tab and pasted some URLs, with the button you had already
    // pressed -- "Check trackers" -- waiting to be pressed again under a
    // different name.
    await missingScan({ manual: true });
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

  // Browse is where you go when you do not already know what you are looking
  // for, and it is the one place in the app that can start a release moving
  // without you having typed anything. It could not do that: the Channels grid
  // led to "Invalid channel slug: 'genre:132'" on every card, New releases was
  // blank for every genre outside a handful of markets, and nothing on any of
  // the three tabs could be sent anywhere. All three are dealt with below and
  // in lox/deezer/explore.py, and every list here can now be sent to a scan.

  /** The albums on a list of cards, as Deezer links. */
  const albumLinks = (items) => (items || [])
    .filter((i) => (i.type === 'album' || i.album_id) && (i.id || i.album_id))
    .map((i) => `https://www.deezer.com/album/${i.album_id || i.id}`);

  /**
   * A button that sends a list of releases to the Scan tab.
   *
   * Browse without this is a picture gallery: you can see that Deezer has
   * forty new rap records and you have no way of asking which of them your
   * trackers are missing without opening forty pages.
   *
   * @param {Array} items - Cards, of which the albums are taken.
   * @param {string} label - What the button says.
   */
  function scanTheseButton(items, label = 'Send to Scan') {
    const links = albumLinks(items);
    if (!links.length) return null;
    return el('button', {
      class: 'ghost',
      title: `Add ${links.length} release(s) to the Scan tab`,
      onclick: () => sendToMissing(links),
    }, `${label} (${links.length})`);
  }

  /** A section heading with whatever can be done to the section beside it. */
  function browseSection(title, items, extra = null) {
    return el('div', { class: 'row browse-head' },
      el('h2', { class: 'section-title' }, title),
      extra,
      scanTheseButton(items));
  }

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
        return await renderChannels(body);
      }

      await renderGenreFilter(filters);
      if (state.exploreTab === 'charts') return await renderCharts(body);
      return await renderReleases(body);
    } catch (e) {
      body.replaceChildren(empty(e.message));
      return undefined;
    }
  }

  /** The Channels grid, or the genres that stand in for it. */
  async function renderChannels(body) {
    const { channels } = await api('/api/explore/channels');
    if (!channels.length) {
      body.replaceChildren(empty('Deezer returned no channels and no genres. Check the ARL in Settings.'));
      return;
    }
    // Deezer only hands channels to an authenticated session. Without one the
    // grid is the editorial genres instead, which is a real page rather than
    // an apology -- but it should say which of the two you are looking at.
    const fallback = channels.every((c) => c.kind === 'genre');

    // Deezer gives most of its channels a colour and no picture. A grid of
    // plain rectangles is a grid you have to read the captions of, so the ones
    // with nothing get their initial drawn on their own colour: not artwork,
    // but something to aim at and something to tell them apart by.
    const initialOf = (title) => String(title || '?')
      .split(/[\s&/-]+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((word) => word[0])
      .join('')
      .toUpperCase() || '?';

    const channelCard = (c) => el(
      'div',
      {
        class: 'card',
        title: c.title,
        onclick: () => go(addr(`/browse/channel/${encodeURIComponent(c.slug)}`)),
      },
      c.image
        ? el('div', { class: 'card-art', style: `background-image:url('${c.image}')` })
        : el('div', {
            class: 'card-art card-initial',
            style: `background:${c.colour || 'var(--bg-input)'}`,
          }, initialOf(c.title)),
      el('div', { class: 'card-title' }, c.title),
    );

    // Deezer serves close to a hundred of these -- genres, moods and thirty-five
    // podcast categories -- and one flat grid of ninety-eight is not somewhere
    // anybody browses. They arrive in the strips Deezer groups them into, so
    // the page keeps those.
    const groups = new Map();
    channels.forEach((c) => {
      const name = c.group || '';
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(c);
    });

    body.replaceChildren(
      ...[
        fallback
          ? el('p', { class: 'hint' },
               'Deezer is not serving channels to this account, so these are its genres. '
               + 'Each one opens what is new and what is charting in it.')
          : null,
        ...[...groups.entries()].flatMap(([name, group]) => [
          name ? el('h2', { class: 'section-title' }, name) : null,
          el('div', { class: 'grid' }, ...group.map(channelCard)),
        ]),
      ].filter(Boolean),
    );
  }

  /** The Charts tab: albums, tracks and artists for the chosen genre. */
  async function renderCharts(body) {
    const chart = await api(`/api/explore/charts?genre=${state.exploreGenre}`);
    body.replaceChildren();
    for (const [label, key] of [['Albums', 'albums'], ['Tracks', 'tracks'], ['Artists', 'artists']]) {
      if (!chart[key]?.length) continue;
      const grid = el('div', { class: 'grid' });
      grid.append(...chart[key].map(card));
      body.append(browseSection(label, chart[key]), grid);
    }
    if (!body.children.length) body.replaceChildren(empty('Deezer has no chart for this selection.'));
  }

  /** The New releases tab, saying which of its three sources answered. */
  async function renderReleases(body) {
    const data = await api(`/api/explore/releases?genre=${state.exploreGenre}`);
    if (!data.results.length) {
      body.replaceChildren(empty(data.note || 'No new releases.'));
      return;
    }
    const grid = el('div', { class: 'grid' });
    grid.append(...data.results.map(card));
    body.replaceChildren(
      ...[
        browseSection(data.source === 'chart' ? 'Recent releases' : 'New releases', data.results),
        // Never silent about where these came from. A chart is popularity, not
        // recency, and passing one off as this week's records is how a 1993
        // record ended up under "New releases".
        data.note ? el('p', { class: 'hint' }, data.note) : null,
        grid,
      ].filter(Boolean),
    );
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
    body.replaceChildren(spinner('Loading'));
    try {
      const channel = await api(`/api/explore/channel/${encodeURIComponent(slug)}`);
      if (state.exploreChannel === slug) setTitle(channel.title || slug);
      const everything = channel.sections.flatMap((section) => section.items);
      body.replaceChildren(
        el('div', { class: 'row toolbar' },
           el('button', { class: 'ghost', onclick: () => go(addr('/browse/channels')) }, '← Channels'),
           scanTheseButton(everything, 'Send everything here to Scan')),
        el('h2', { class: 'section-title' }, channel.title),
        ...(channel.note ? [el('p', { class: 'hint' }, channel.note)] : []),
      );
      for (const section of channel.sections) {
        const grid = el('div', { class: 'grid' });
        grid.append(...section.items.map(card));
        body.append(
          browseSection(
            section.title || 'Selection',
            section.items,
            // A module has an address of its own, so a scan can re-run it
            // later rather than being handed a frozen list of what is in it
            // today. A genre section has no module, and its albums are the
            // only thing there is to send.
            section.id
              ? el('button', {
                  class: 'ghost',
                  title: 'Scan this module, so a later scan picks up whatever Deezer has added to it',
                  onclick: () => sendToMissing(`https://www.deezer.com/en/channels/module/${section.id}`),
                }, 'Scan module')
              : null,
          ),
          grid,
        );
      }
      if (!channel.sections.length) body.append(empty('Deezer sent nothing back for this channel.'));
    } catch (e) {
      body.replaceChildren(
        el('div', { class: 'row toolbar' },
           el('button', { class: 'ghost', onclick: () => go(addr('/browse/channels')) }, '← Channels')),
        empty(e.message));
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

  /**
   * Put one or more Deezer links in the Scan box and go there.
   *
   * @param {string|string[]} urls - A link, or a list of them.
   */
  function sendToMissing(urls) {
    const list = (Array.isArray(urls) ? urls : [urls]).filter(Boolean);
    if (!list.length) return;
    const box = $('#missing-sources');
    // Whatever is already in the box stays: sending a second selection should
    // add to the scan you are building, not replace it. Duplicates are dropped
    // so pressing the same button twice does not queue the same album twice.
    const already = box.value.split('\n').map((l) => l.trim()).filter(Boolean);
    const merged = [...new Set([...already, ...list])];
    box.value = merged.join('\n');
    go(addr('/scan'));
    toast(list.length === 1
      ? 'Added to the Scan tab.'
      : `${list.length} releases added to the Scan tab.`);
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
               'Everything above comes from Deezer. Check trackers asks RED and OPS whether they '
               + 'already have this release, and puts the answer below.'),
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
    state.uploadTrackers = [...trackers];
    renderUploadTargets();

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
      // With the release id, so a successful upload can take this release off
      // the queue. Without it the server had only the folder name to go on,
      // and a release uploaded from its own page stayed in the queue.
      startUpload(existing.path, trackers, album.id);
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
      // A search deleted elsewhere should not stay ticked and counted.
      const alive = new Set(watchlists.map((w) => w.id));
      state.watchSelected.forEach((id) => alive.has(id) || state.watchSelected.delete(id));
      renderWatchlists();
    } catch (e) {
      container.replaceChildren(empty(e.message));
      watchlistPick();
    }
  }

  /** How many are ticked, and what that lets you press. */
  function watchlistPick() {
    const shown = tableView('watchlists').shown || state.watchlists;
    const n = countSelected(shown, state.watchSelected);
    $('#watchlist-scan').disabled = n === 0;
    $('#watchlist-delete').disabled = n === 0;
    $('#watchlist-scan-all').disabled = state.watchlists.length === 0;
    $('#watchlist-count').textContent = shown.length ? `${n} of ${shown.length} selected` : '';
  }

  function renderWatchlists() {
    const container = $('#watchlists');
    if (!state.watchlists.length) {
      container.replaceChildren(el('p', { class: 'hint' },
        'Nothing saved yet. Paste a link above and press Save for later.'));
      watchlistPick();
      return;
    }

    // The same table as every other list here. Thirty saved playlists in a
    // column of hand-rolled rows had no way to find one by name, and no way
    // to see which of them had gone unscanned longest.
    container.replaceChildren(dataTable({
      name: 'watchlists',
      rows: state.watchlists,
      selection: { set: state.watchSelected, onChange: watchlistPick },
      onShown: watchlistPick,
      empty: 'Nothing saved yet.',
      columns: [
        {
          label: 'Saved search',
          // Read down rather than compared across, so it keeps its left edge.
          text: true,
          value: (w) => w.name || '',
          filter: 'text',
          cell: (w) => (state.watchEditing === w.id
            ? renameField(w)
            : el('span', {},
                el('div', {}, el('strong', {}, w.name)),
                el('span', { class: 'hint' }, w.holds || ''))),
        },
        {
          label: 'Kind',
          value: (w) => w.kind_label || '',
          filter: 'choice',
          class: 'nowrap',
          cell: (w) => el('span', { class: 'tag dim' }, w.kind_label || ''),
        },
        {
          label: 'Last scanned',
          value: (w) => daysAgo(w.last_run),
          filter: 'days',
          class: 'nowrap',
          cell: (w) => (w.last_run ? whenCell(w.last_run) : el('span', { class: 'hint' }, 'never')),
        },
        {
          label: '',
          filter: false,
          class: 'row-actions',
          cell: (w) => el('span', {},
            w.url
              ? el('a', { class: 'linkbtn', href: w.url, target: '_blank', rel: 'noreferrer' }, 'Open \u2197')
              : null,
            el('button', {
              class: 'ghost',
              onclick: () => { state.watchEditing = w.id; renderWatchlists(); },
            }, 'Rename'),
            el('button', { class: 'ghost', onclick: () => deleteWatchlists([w.id]) }, 'Delete')),
        },
      ],
    }));
    watchlistPick();
  }

  /** The rename box, in place of the name it is replacing. */
  function renameField(w) {
    const box = el('input', {
      type: 'text',
      value: w.name,
      'aria-label': 'Name',
      onkeydown: (e) => {
        if (e.key === 'Enter') { e.preventDefault(); save(); }
        if (e.key === 'Escape') { e.preventDefault(); cancel(); }
      },
    });
    const cancel = () => { state.watchEditing = null; renderWatchlists(); };
    const save = async () => {
      const name = box.value.trim();
      if (!name) return cancel();
      try {
        await api(`/api/watchlists/${w.id}`, { method: 'PATCH', body: { name } });
      } catch (err) {
        return toast(err.message, 'bad');
      }
      state.watchEditing = null;
      return loadWatchlists();
    };
    // Focused after it is in the document, which is the next frame.
    setTimeout(() => { box.focus(); box.select(); }, 0);
    return el('div', { class: 'watch-rename' },
      box,
      el('button', { class: 'primary', onclick: save }, 'Save'),
      el('button', { class: 'ghost', onclick: cancel }, 'Cancel'));
  }

  /**
   * Save whatever links are in the scan box.
   *
   * The same box that scans them, because they are the same links: a search
   * you want to keep is one you were about to run. Deezer is asked what each
   * one is, so there is no name to invent and no id to go and find.
   */
  async function saveSources() {
    const urls = sourceLines();
    if (!urls.length) return toast('Paste a Deezer link first', 'bad');

    const button = $('#missing-save');
    button.disabled = true;
    try {
      const { saved, failed } = await api('/api/watchlists', { method: 'POST', body: { urls } });
      const added = saved.filter((w) => !w.already_saved);
      // Named rather than counted when there is one, because the name is the
      // thing being confirmed -- it came from Deezer, not from you.
      if (added.length === 1) toast(`Saved “${added[0].name}”`, 'ok');
      else if (added.length) toast(`Saved ${added.length} searches`, 'ok');
      if (saved.length > added.length) {
        toast(`${saved.length - added.length} already saved`, '');
      }
      failed.forEach((f) => toast(`${f.url}: ${f.error}`, 'bad'));
      if (added.length) loadWatchlists();
    } catch (e) {
      toast(e.message, 'bad');
    } finally {
      button.disabled = false;
    }
  }

  async function deleteWatchlists(ids) {
    if (!ids.length) return;
    const names = state.watchlists.filter((w) => ids.includes(w.id)).map((w) => w.name);
    const what = names.length === 1 ? `“${names[0]}”` : `${names.length} saved searches`;
    if (!confirm(`Delete ${what}?\n\nThis only forgets the link. Nothing already scanned is affected.`)) return;
    await Promise.all(ids.map((id) => api(`/api/watchlists/${id}`, { method: 'DELETE' })));
    ids.forEach((id) => state.watchSelected.delete(id));
    loadWatchlists();
  }

  /**
   * Scan a set of saved searches as one job.
   *
   * Their links go into the box above rather than being scanned invisibly, so
   * what is about to be looked at is on screen and can be edited before, or
   * pasted somewhere else after.
   *
   * @param {string[]} ids - Which to scan. Empty means every saved search.
   */
  async function scanWatchlists(ids) {
    let payload;
    try {
      payload = await api('/api/watchlists/sources', { method: 'POST', body: { ids } });
    } catch (e) {
      return toast(e.message, 'bad');
    }
    payload.problems.forEach((p) => toast(`${p.name || 'A saved search'}: ${p.error}`, 'bad'));
    if (!payload.sources.length) return toast('Nothing to scan', 'bad');
    $('#missing-sources').value = payload.sources.join('\n');
    return missingScan();
  }

  // ---------------------------------------------------------------- downloads

  /**
   * Queue one album.
   *
   * @param {string} albumId - Deezer album id.
   * @param {boolean} [options.allowLossy] - Take whatever quality Deezer will
   *   serve for this one release, whatever the fallback setting says. This is
   *   the "download it anyway" answer to a release Deezer has no FLAC for.
   */
  async function download(albumId, { allowLossy = false } = {}) {
    try {
      const result = await api('/api/download', {
        method: 'POST',
        body: { album_id: String(albumId), allow_lossy: allowLossy },
      });
      if (result.failed?.length) {
        const problem = result.failed[0].error;
        // The one failure with an answer: the account cannot have this release
        // as FLAC and lower qualities are switched off. Rather than a dead
        // end, offer the release at the quality Deezer will actually serve.
        if (!allowLossy && /flac|format|quality|no media|not available/i.test(problem)) {
          if (confirm(`${problem}\n\nDownload it at whatever quality Deezer will serve instead?`)) {
            return download(albumId, { allowLossy: true });
          }
          return null;
        }
        toast(problem, 'bad');
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

  /**
   * Ask what to do about a download that came back below FLAC.
   *
   * Asked once per job, the moment the first lossy track lands, so there is
   * still a download to stop. Keeping it is a real answer -- sometimes lossy
   * is what the request wanted -- so this is a question and not a refusal.
   *
   * @param {object} job - The download job as the API reports it.
   */
  async function askAboutQuality(job) {
    if (state.qualityAsked.has(job.id)) return;
    state.qualityAsked.add(job.id);
    const name = `${job.artist} — ${job.title}`;
    const quality = (job.quality || '').replace('_', ' ') || 'a lower quality';
    const keep = confirm(
      `${name} is not FLAC.\n\n`
      + `Deezer is serving it as ${quality}. Trackers reject a lossy release posted as lossless, `
      + `and lox has named the folder for what is actually in it.\n\n`
      + 'OK keeps the download. Cancel stops it and deletes the folder.');
    try {
      const result = await api(`/api/downloads/${job.id}/quality`, { method: 'POST', body: { keep } });
      toast(keep
        ? `Keeping ${name} at ${quality}.`
        : `${name} stopped${result.deleted ? ' and the folder deleted' : ''}.`, keep ? 'ok' : '');
    } catch (e) {
      toast(e.message, 'bad');
    }
    pollDownloads(true);
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
    const trackers = uploadTargets(item);
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
        const { jobs, confirm_lower_quality: askFirst } = await api('/api/downloads');
        renderDownloads(jobs);
        // A release Deezer will not serve as FLAC used to land in the download
        // folder looking exactly like one it would, and the first thing to
        // notice was a tracker rejecting the upload.
        if (askFirst) {
          const surprise = jobs.find((j) => j.lossy && !j.decision && !j.allow_lossy);
          if (surprise) askAboutQuality(surprise);
        }
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

  /**
   * A filter bar for a list that is not a table.
   *
   * The downloads are cards with progress bars rather than rows, so they
   * cannot carry the per-column filters every table here has -- but a list of
   * forty downloads still needs narrowing, and "which of these failed" was
   * something you could only answer by scrolling.
   *
   * @param {string} name - Keys the retained filter state.
   * @param {Array} rows - What is being filtered.
   * @param {Function} textOf - The searchable text of one row.
   * @param {Function} statusOf - The status of one row, for the dropdown.
   * @param {Function} rerender - Called when a filter changes.
   * @returns {object} `{ bar, shown }`.
   */
  function listFilter(name, rows, textOf, statusOf, rerender) {
    const view = tableView(name);
    const wanted = String(view.filters.text || '').toLowerCase();
    const status = view.filters.status || '';
    const shown = rows.filter((row) =>
      (!wanted || String(textOf(row)).toLowerCase().includes(wanted))
      && (!status || statusOf(row) === status));

    const options = [...new Set(rows.map(statusOf).filter(Boolean))].sort();
    const bar = el('div', { class: 'row listfilter' },
      el('input', {
        class: 'th-filter',
        type: 'search',
        placeholder: 'filter',
        value: view.filters.text || '',
        oninput: (e) => {
          view.filters.text = e.target.value;
          clearTimeout(view.timer);
          view.timer = setTimeout(rerender, 220);
        },
      }),
      el('select', {
        class: 'th-filter',
        onchange: (e) => { view.filters.status = e.target.value; rerender(); },
      },
      el('option', { value: '', selected: !status }, 'all'),
      ...options.map((o) => el('option', { value: o, selected: status === o }, o))),
      el('span', { class: 'hint' },
         shown.length === rows.length
           ? `${rows.length} download${rows.length === 1 ? '' : 's'}`
           : `${shown.length} of ${rows.length}`));
    return { bar, shown };
  }

  function renderDownloads(jobs) {
    const list = $('#downloads-list');
    if (!jobs.length) {
      list.replaceChildren(empty('Nothing downloading. Add something from Search or Browse.'));
      return;
    }
    const { bar, shown } = listFilter(
      'downloads', jobs,
      (job) => `${job.artist} ${job.title}`,
      (job) => job.status,
      () => renderDownloads(jobs),
    );
    if (!shown.length) {
      list.replaceChildren(bar, empty('Nothing matches these filters.'));
      return;
    }
    list.replaceChildren(
      bar,
      ...shown.map((job) => {
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
            // What actually came back. Silent for a FLAC download, which is
            // the expected case and needs no announcement.
            job.lossy
              ? el('div', { class: 'dl-quality' },
                  el('span', { class: 'tag warn' }, (job.quality || 'lossy').replace('_', ' ')),
                  ' ',
                  job.decision === 'discarded'
                    ? el('span', { class: 'hint' }, 'discarded')
                    : job.decision === 'kept'
                      ? el('span', { class: 'hint' }, 'kept anyway')
                      : el('span', {},
                          el('button', {
                            class: 'linkbtn',
                            onclick: async () => {
                              await api(`/api/downloads/${job.id}/quality`,
                                        { method: 'POST', body: { keep: true } });
                              pollDownloads(true);
                            },
                          }, 'keep it'),
                          ' · ',
                          el('button', {
                            class: 'linkbtn danger-link',
                            onclick: async () => {
                              await api(`/api/downloads/${job.id}/quality`,
                                        { method: 'POST', body: { keep: false } });
                              toast('Stopped, and the folder deleted.', 'ok');
                              pollDownloads(true);
                            },
                          }, 'delete it')))
              : null,
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
            // Plenty of downloads fail for reasons that do not last -- one
            // track timing out, the gateway briefly refusing -- and trying
            // again meant finding the release and starting it by hand.
            job.status === 'failed'
              ? el('button', {
                  title: 'Delete what was fetched and start over',
                  onclick: async () => {
                    try {
                      await api(`/api/downloads/${job.id}/retry`, { method: 'POST' });
                      toast('Downloading again', 'ok');
                    } catch (e) { toast(e.message, 'bad'); }
                    pollDownloads(true);
                  },
                }, 'Retry')
              : null,
            // Whenever there is something on disk to remove, however the
            // download ended. This was done-only, so a download that failed
            // partway -- nine of ten tracks, the folder sitting there named
            // [WEB FLAC] with a hole in it -- had no way off the page at all:
            // Cancel was gone, Delete never appeared, and Clear finished drops
            // the row while leaving the folder behind. A failure is the case
            // you most need this for.
            job.folder && job.status !== 'queued' && job.status !== 'running'
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
  /** The links in the scan box, one per line, blanks dropped. */
  function sourceLines() {
    return $('#missing-sources').value.split('\n').map((s) => s.trim()).filter(Boolean);
  }

  /**
   * Expand the links in the box, then check what comes out.
   *
   * @param {boolean} [options.manual] - These were picked one at a time from
   *   Search or Browse, so the scan's own filters and its "already looked up"
   *   skip do not apply to them.
   */
  async function missingScan({ manual = false } = {}) {
    const sources = sourceLines();
    if (!sources.length) return toast('Paste at least one Deezer link', 'bad');

    const log = $('#missing-collect-log');
    log.hidden = false;
    log.textContent = 'Expanding sources…';
    $('#missing-scan').disabled = true;
    state.candidates = [];

    try {
      // Whether an album already looked up is looked at again is the recheck
      // window's business, up in the filters. The tickbox that used to sit
      // beside this button said the same thing in fewer words, so one decision
      // had two controls and they could contradict each other.
      const { job_id } = await api('/api/missing/collect', {
        method: 'POST',
        body: { sources, manual },
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

  /** What identifies a collected album. */
  const candidateKey = (c) => c.album_id;

  function candidatesPick() {
    const shown = tableView('candidates').shown || state.candidates;
    const n = countSelected(shown, state.selectedCandidates, candidateKey);
    const button = $('#missing-check');
    if (button) {
      button.disabled = n === 0;
      button.textContent = n ? `Check ${n} again` : 'Check again';
    }
    const label = $('#missing-selected');
    if (label) label.textContent = `${n} of ${shown.length} shown`;
  }

  /** What a check said about one collected album, or null. */
  const scanResultOf = (c) => state.scanResults.get(String(c.album_id)) || null;

  /** The result column as one phrase, so it can be filtered and sorted on. */
  function scanOutcome(c) {
    const result = scanResultOf(c);
    if (!result) return 'not checked';
    const parts = [
      ...(result.missing_from || []).map((t) => `${t} missing`),
      ...(result.found_on || []).map((t) => `${t} has it`),
      ...Object.keys(result.errors || {}).map((t) => `${t} error`),
    ];
    return parts.join(', ') || 'no result';
  }

  /** The result cell, with every verdict as somewhere to go and check. */
  function scanResultCell(c) {
    const result = scanResultOf(c);
    if (!result) return el('span', { class: 'tag dim' }, 'not checked');
    const links = trackerLinks({ ...result, artist: c.artist, title: c.title });
    const parts = [
      ...(result.missing_from || []).map((t) =>
        trackerTag(t, 'ok', `${t} missing`, links[t], `Open what ${c.artist || 'this artist'} has on ${t}`)),
      ...(result.found_on || []).map((t) =>
        trackerTag(t, 'dim', `${t} has it`, links[t], `Open the release on ${t}`)),
      ...Object.entries(result.errors || {}).map(([t, e]) =>
        el('span', { class: 'tag warn', title: e }, `${t} error`)),
    ];
    return el('span', {}, ...(parts.length ? parts : [el('span', { class: 'tag dim' }, 'no result')]));
  }

  function renderCandidates() {
    const panel = $('#missing-candidates-panel');
    panel.hidden = state.candidates.length === 0;
    if (!state.candidates.length) return;

    $('#missing-cost').textContent =
      `${state.candidates.length} album(s) collected. Nothing here has been looked up yet.`;

    $('#missing-table').replaceChildren(dataTable({
      name: 'candidates',
      rows: state.candidates,
      selection: { set: state.selectedCandidates, onChange: candidatesPick },
      onShown: candidatesPick,
      idOf: candidateKey,
      rowAttrs: (c) => ({ 'data-album': c.album_id }),
      empty: 'Nothing collected.',
      columns: [
        {
          label: 'Album',
          // Read down rather than compared across, so it keeps its left edge.
          text: true,
          value: (c) => `${c.artist || ''} ${c.title || ''}`.trim(),
          filter: 'text',
          cell: (c) => el('a', {
            href: albumHref(c.album_id),
            onclick: (e) => { e.preventDefault(); goAlbum(c.album_id); },
          }, `${c.artist} — ${c.title}`),
        },
        {
          label: 'Year',
          value: (c) => Number(String(c.year || '').slice(0, 4)) || 0,
          filter: 'range',
          lowLabel: 'from',
          highLabel: 'to',
          class: 'nowrap',
          cell: (c) => el('span', {}, c.year || ''),
        },
        {
          label: 'Tracks',
          value: (c) => Number(c.tracks) || 0,
          filter: 'range',
          class: 'nowrap',
          cell: (c) => el('span', {}, String(c.tracks)),
        },
        {
          label: 'Source',
          value: (c) => c.source || '',
          filter: 'choice',
          class: 'card-sub',
          cell: (c) => el('span', {}, c.source || ''),
        },
        {
          label: 'Result',
          value: scanOutcome,
          filter: 'text',
          class: 'result',
          cell: scanResultCell,
        },
      ],
    }));
    candidatesPick();
  }

  // ``all`` checks everything that was just collected; without it the ticked
  // rows are checked, which is what "Check again" on the results is for.
  async function missingCheck({ all = false } = {}) {
    const trackers = [...state.missingTrackers];
    if (!trackers.length) return toast('Pick at least one tracker', 'bad');
    const shown = tableView('candidates').shown || state.candidates;
    const candidates = all
      ? state.candidates
      : shown.filter((c) => state.selectedCandidates.has(c.album_id));
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
              ? `Stopped after ${stopped.checked}: the tracker will not answer any more just yet. `
                + `${stopped.remaining ?? '?'} still to check — run it again shortly.`
              : `Done. ${job.result_count} album(s) checked.`);
            if (job.error) toast(job.error, 'bad');
            releasesChanged();
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
    const id = String(result.album_id);
    // Kept as data, so sorting or filtering the list redraws the answers
    // rather than blanking every row that had one.
    state.scanResults.set(id, result);
    const cell = $(`#missing-table tr[data-album="${CSS.escape(id)}"] .result`);
    const candidate = state.candidates.find((c) => String(c.album_id) === id);
    if (cell && candidate) cell.replaceChildren(scanResultCell(candidate));
  }

  // ---------------------------------------------------------------- requests

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
         `${state.requestsTracker} sends ${spec.page_size} requests per page`)));

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
    // The one thing the sidebar cannot tell you, because it is about what you
    // have just typed: this search is asking for more than the tracker will
    // answer right now, and it will stop short.
    cost.textContent = over
      ? `${budget.code} will only answer ${budget.remaining} more page${budget.remaining === 1 ? '' : 's'} `
        + 'right now, so the search will stop there.'
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
    fill(host,
      ...[
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
          : null,
      ].filter(Boolean));
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
    // The lookup is one job per page, but it is one lookup as far as anybody
    // watching is concerned. Reporting each job's own progress meant the line
    // read "2/25" on a search that had already found three hundred requests
    // and restarted at zero every time a page landed -- so the count is kept
    // here, across the jobs, and grows as the search does.
    let queuedForLookup = 0;
    let lookedUp = 0;

    if (pipeline) {
      log.hidden = false;
      log.textContent = 'Starting…';
      $$('.requests-check-btn').forEach((b) => { b.disabled = true; });
    }

    const lookUpLater = (ids) => {
      if (!ids.length) return;
      queuedForLookup += ids.length;
      chain = chain
        .then(async () => {
          if (cancelled) return;
          const startedAt = lookedUp;
          const job = await runCheckJob(tracker, ids, {
            onSkipped: (note) => {
              // Collected rather than shown per page: twenty pages would be
              // twenty panels saying the same thing.
              pipelineSkipped.push(...(note.requests || []));
              pipelineWindow = note.recheck_after_days;
            },
            onProgress: (j) => {
              const done = startedAt + ((j.progress && j.progress.current) || 0);
              jobProgress(log, {
                ...j,
                progress: { phase: 'looking up', current: Math.min(done, queuedForLookup),
                            total: queuedForLookup, album: (j.progress || {}).album },
              }, queuedForLookup < rows.length ? `${rows.length} found so far` : '');
            },
          });
          lookedUp = startedAt + ids.length;
          pipelineChecked += job.result_count || 0;
        })
        .catch((e) => { if (!cancelled) toast(e.message, 'bad'); });
    };

    // --- reading the pages -------------------------------------------------
    container.replaceChildren(spinner(`Reading page 1 of ${pages}`));
    requestsProgress(0, pages, `Page 1 of ${pages}`);
    requestsSummary({ shown: null });
    // Whatever the last run skipped is not what this one is doing.
    clearSkipped();

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
      requestsPick();
      jobFinished(log, `Done. ${pipelineChecked} request(s) checked.`);
      refreshStatus();
      return;
    }

    if (!cancelled && rows.length && thenCheck) {
      await requestsCheck(rows.map((r) => r.id));
    }
  }

  //: Suffix to multiplier, so a bounty written the way the tracker writes it
  //: can be compared as a number. "900 MB" sorts above "1 TB" as text.
  const BOUNTY_UNITS = { B: 1, KB: 1024, MB: 1024 ** 2, GB: 1024 ** 3, TB: 1024 ** 4, PB: 1024 ** 5 };

  function bountyBytes(text) {
    const parts = String(text || '').trim().split(/\s+/);
    if (parts.length !== 2) return 0;
    const size = Number(parts[0]);
    return Number.isFinite(size) ? size * (BOUNTY_UNITS[parts[1].toUpperCase()] || 0) : 0;
  }

  /**
   * What identifies one request's answer.
   *
   * Tracker and id together, never the id alone: request 80755 exists on both
   * trackers and is a different release on each, so keying on the number would
   * show RED's answer against an OPS row.
   */
  const resultKey = (tracker, id) => `${tracker || state.requestsTracker || ''}:${id}`;

  /** What a check said about one request, or null while nothing has. */
  const requestResult = (row) => state.requestResults.get(resultKey(row.tracker, row.id)) || null;

  /**
   * The result column as one comparable phrase.
   *
   * Kept out of the cell renderer so the column can be filtered and sorted on
   * the same words it displays -- "show me the ones that matched" should be
   * something you can type into the column rather than something you scroll
   * looking for.
   */
  function requestOutcome(row) {
    const match = requestResult(row);
    if (!match) return 'not checked';
    if (match.status === 'filled') return match.reason || 'already filled';
    if (!match.fillable) return match.reason || match.status || 'nothing usable';
    return `${(match.confidence * 100).toFixed(0)}% match`;
  }

  /** The result cell: the outcome, and whatever can be done about it. */
  function requestResultCell(row) {
    const match = requestResult(row);
    if (!match) return el('span', { class: 'tag dim' }, 'not checked');
    if (match.status === 'filled') {
      return el('span', { class: 'tag warn', title: match.reason || '' }, match.reason || 'already filled');
    }
    if (!match.fillable) {
      return el('span', { class: 'tag dim', title: match.reason || '' },
                match.reason || match.status || 'nothing usable');
    }
    return el('span', {},
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
      el('button', { class: 'ghost', onclick: () => download(match.deezer_id) }, 'Download'));
  }

  function requestsPick() {
    const shown = tableView('requests').shown || state.requestRows;
    const n = countSelected(shown, state.selectedRequests);
    $$('.requests-check-btn').forEach((b) => {
      b.disabled = n === 0;
      b.textContent = n ? `Check ${n} selected` : 'Check selected';
    });
    const label = $('#requests-selected');
    if (label) label.textContent = `${n} of ${shown.length} shown`;
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
          onclick: () => requestsCheck(
            (tableView('requests').shown || state.requestRows)
              .filter((r) => state.selectedRequests.has(r.id))
              .map((r) => ({ id: r.id, tracker: r.tracker || state.requestsTracker })),
          ),
        }, state.selectedRequests.size ? `Check ${state.selectedRequests.size} selected` : 'Check selected'),
        el('span', { class: 'hint', id: 'requests-selected' }),
      ),
      // The same table as every other list, so the filters sit in the columns
      // they filter. This one had none at all: a search that came back with
      // three hundred requests could only be read top to bottom.
      dataTable({
        name: 'requests',
        rows: state.requestRows,
        selection: { set: state.selectedRequests, onChange: requestsPick },
        onShown: requestsPick,
        // The live result cells find their row through this, so a check can
        // fill them in without rebuilding the table under the reader.
        rowAttrs: (r) => ({ 'data-request': r.id }),
        empty: 'Nothing matched. Widen the filters, or fetch more pages.',
        columns: [
          {
            label: 'Request',
            // Read down rather than compared across, so it keeps its left edge.
            text: true,
            value: (r) => `${r.artist || ''} ${r.title || ''} ${r.id}`.trim(),
            filter: 'text',
            class: 'req-name',
            cell: (r) => el('a', {
              class: 'rowlink',
              href: r.url,
              title: 'Open the request beside the Deezer release',
              onclick: (e) => {
                e.preventDefault();
                go(addr(`/requests/${encodeURIComponent(r.tracker || state.requestsTracker || '')}`
                        + `/${encodeURIComponent(r.id)}`));
              },
            }, `${r.artist || '?'} — ${r.title || `Request ${r.id}`}`),
          },
          {
            label: 'Year',
            value: (r) => Number(String(r.year || '').slice(0, 4)) || 0,
            filter: 'range',
            lowLabel: 'from',
            highLabel: 'to',
            class: 'nowrap req-year',
            cell: (r) => el('span', {}, r.year || ''),
          },
          {
            // Compared in GB, which is the unit the tracker writes it in.
            label: 'Bounty (GB)',
            value: (r) => bountyBytes(r.bounty) / (1024 ** 3),
            filter: 'range',
            class: 'nowrap req-bounty',
            cell: (r) => el('span', {}, r.bounty || ''),
          },
          {
            // How long it has sat open. A request from 2019 that nothing has
            // filled is a different proposition from one raised yesterday.
            label: 'Added',
            value: (r) => (r.created ? daysAgo(Date.parse(r.created) / 1000) : -1),
            filter: 'days',
            class: 'nowrap',
            cell: (r) => el('span', { title: r.created || '' }, r.age || '—'),
          },
          {
            // Filled or not, stated rather than inferred. A filled request
            // cannot be filled again.
            label: 'Filled',
            value: (r) => (r.filled ? 'filled' : 'open'),
            filter: 'choice',
            class: 'req-filled',
            cell: (r) => (r.filled
              ? el('span', { class: 'tag dim', title: r.filled_by ? `by ${r.filled_by}` : '' }, 'filled')
              : el('span', { class: 'tag ok' }, 'open')),
          },
          {
            label: 'Result',
            value: requestOutcome,
            filter: 'text',
            class: 'result req-result',
            cell: requestResultCell,
          },
          {
            // Saying no to a match. The matcher is confident about a wrong
            // one and stays confident, so taking the release off the queue
            // lasted until the next check put it straight back.
            label: '',
            filter: false,
            class: 'row-actions',
            cell: (r) => el('span', {},
              r.deezer_id
                ? el('button', {
                    class: 'danger',
                    title: 'This release does not fill this request. It leaves the queue and '
                           + 'will not be matched to it again.',
                    onclick: (e) => { e.stopPropagation(); rejectMatch(r); },
                  }, 'Not this')
                : null),
          },
        ],
      }),
    );
    requestsPick();
  }

  /**
   * Say that a release does not fill a request.
   *
   * The release leaves the queue, the request goes back to having no match,
   * and the next check will not propose the same pairing again -- which is
   * what made removing it from the queue by hand pointless. The request stays
   * open for something that does fill it, and the refusal is kept: it is the
   * only evidence there is of the matcher being wrong.
   *
   * @param {object} row - A request row carrying a deezer_id.
   */
  async function rejectMatch(row) {
    const named = [row.deezer_artist, row.deezer_title].filter(Boolean).join(' — ')
      || `release ${row.deezer_id}`;
    if (!confirm(`"${named}" does not fill request ${row.id}?

It leaves the queue and will not be matched to this request again. The request stays open.`)) {
      return;
    }
    try {
      await api('/api/requests/reject', {
        method: 'POST',
        body: {
          tracker: row.tracker || state.requestsTracker,
          request_id: row.id,
          deezer_id: row.deezer_id,
        },
      });
      toast('Noted. It will not be offered for this request again.', 'ok');
      releasesChanged();
      if (state.requestTab === 'history') loadHistory();
    } catch (e) {
      toast(e.message, 'bad');
    }
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
  async function runCheckJob(tracker, ids,
                             { recheck = false, prefix = '', onSkipped = null, onProgress = null,
                               logSel = '#requests-log' } = {}) {
    const log = $(logSel) || $('#requests-log');
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
          if (onProgress) onProgress(j);
          else jobProgress(log, j, prefix);
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

  async function requestsCheck(entries, { placeholders = false, recheck = false,
                                          logSel = '#requests-log' } = {}) {
    // Callers used to hand over bare ids; they hand over {id, tracker} now,
    // and a bare id still works so a caller that has only an id is not forced
    // to invent a tracker for it.
    const items = entries.map((e) => (typeof e === 'object' ? e : { id: String(e), tracker: null }));
    if (!items.length) return toast('Nothing to check', 'bad');

    const log = $(logSel) || $('#requests-log');
    log.hidden = false;
    log.textContent = 'Starting…';
    clearSkipped();
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

    const done = () => requestsPick();

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
          logSel,
          prefix: groups.size > 1 ? `${tracker}: ` : '',
          onSkipped: (note) => showSkipped(tracker, note),
        });
        checked += job.result_count || 0;
        stoppedEarly = stoppedEarly || job.events.find((e) => e.event === 'budget_exhausted');
        if (stoppedEarly) break;
      }
      done();
      refreshStatus();
      releasesChanged();
      // What the run actually produced, at the top, before the hundreds of
      // rows that produced nothing. A check of two hundred requests that finds
      // three worth filling used to say "Done. 200 request(s) checked" and
      // leave the three to be found by scrolling -- and they are the whole
      // reason the check was run.
      const qualified = liftQualifying();
      jobFinished(log, stoppedEarly
        ? `Stopped after ${stoppedEarly.checked} request(s): the tracker will not answer any more just yet. `
          + 'Run it again shortly.'
        : `Done. ${checked} request(s) checked.`
          + (qualified ? ` ${qualified} can be filled — they are at the top.` : ' None can be filled.'));
    } catch (e) {
      done();
      toast(e.message, 'bad');
    }
  }

  /**
   * Put the requests worth acting on at the top of the list.
   *
   * A check of two hundred requests that finds three worth filling left the
   * three wherever the tracker's own ordering had put them. They are what the
   * run was for, and they are also the ones that want looking at by hand --
   * a wrong match is refused from this list.
   *
   * A stable partition rather than a sort, so within each half the order the
   * tracker gave is kept.
   *
   * @returns {number} How many qualified.
   */
  function liftQualifying() {
    const canFill = (row) => Boolean(requestResult(row)?.fillable);
    const yes = state.requestRows.filter(canFill);
    if (!yes.length || yes.length === state.requestRows.length) return yes.length;
    state.requestRows = [...yes, ...state.requestRows.filter((r) => !canFill(r))];
    renderRequestRows();
    return yes.length;
  }

  /**
   * Clear the skipped panel.
   *
   * Called at the start of every run, because the panel is about the run that
   * put it there. It used to be appended after the log with nothing ever
   * taking it away, so a second search stacked a second panel on the first,
   * a third on those, and the top of the results was a pile of notes about
   * searches that had already finished.
   */
  function clearSkipped() {
    const host = $('#requests-skipped');
    if (host) { host.replaceChildren(); host.hidden = true; }
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
    const host = $('#requests-skipped');
    if (!host) return;
    const window_ = note.recheck_after_days;
    const summary = window_
      ? `${note.count} already checked in the last ${window_} day${window_ === 1 ? '' : 's'} — skipped.`
      : `${note.count} already checked — skipped.`;

    // One panel, replaced rather than added to: this is the state of the run
    // you are watching, not a log of every run this session.
    host.hidden = false;
    host.replaceChildren(el('div', { class: 'panel skipped-note' },
      el('div', { class: 'row' },
        el('strong', {}, summary),
        el('button', {
          onclick: async () => {
            clearSkipped();
            await requestsCheck(rows.map((r) => ({ id: r.id, tracker: r.tracker })),
                                { placeholders: true, recheck: true });
          },
        }, 'Check them anyway'),
        el('button', {
          onclick: () => {
            clearSkipped();
            showRequestTab('history');
          },
        }, 'Show me what they said'),
        // Somewhere to put it that is not "do one of the two things it
        // suggests". A note you have read should be dismissable.
        el('button', {
          class: 'ghost note-dismiss',
          title: 'Dismiss',
          'aria-label': 'Dismiss',
          onclick: clearSkipped,
        }, '\u00d7')),
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
    keepAddress(name === 'history' ? '/scan/history' : '/scan');
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
      return toast(e.message, 'bad');
    }
    // Read back rather than assumed: clearing a date puts the rolling default
    // in the box, and the note under it has to change with it.
    return loadScanFilters();
  }

  // The filters a scan applies before contacting a tracker. They govern
  // scanning and nothing else -- the request checker, the album page and the
  // search results never consult them -- so they sit with the scan rather
  // than on the settings page, where they read as rules the whole app obeys.
  /**
   * One filter: its name, the control, and the note under it.
   *
   * Not the banded row the request form uses. That layout earns its width on a
   * group of fifty checkboxes; spent on a single number it puts one short
   * answer per screen-wide line, and four of them filled the top of the page
   * with rules and whitespace.
   *
   * @param {string} label - What the filter is.
   * @param {Node[]} controls - The control, or the pair a duration takes.
   * @param {string} hint - What a blank or a zero means.
   */
  function scanField(label, controls, hint) {
    return el('div', { class: 'scanfield' },
      el('div', { class: 'reqlabel' }, label),
      el('div', { class: 'scancontrol' }, ...controls),
      hint ? el('span', { class: 'hint reqhint' }, hint) : null);
  }

  /**
   * One date filter.
   *
   * A real date input, so the answer is picked off a calendar rather than
   * typed in a format you have to be told. Both of these default to something
   * relative to today -- last January, and two days out -- so the box shows
   * that date whether or not one has been set, and the note underneath says
   * which of the two you are looking at and offers the way back.
   *
   * @param {Object} filters - The filter payload from the server.
   * @param {string} key - min_date or max_date.
   * @param {string} label - What it is called on screen.
   */
  function dateFilter(filters, key, label) {
    const set = filters[key] || '';
    const fallback = filters[`${key}_default`] || '';
    return scanField(label, [
      el('input', {
        id: `scan-${key}`,
        type: 'date',
        value: set || filters[`${key}_effective`] || fallback,
        onchange: (e) => saveScanFilter(key, e.target.value.trim()),
      }),
    ], set
      ? el('button', {
          type: 'button',
          class: 'linkbtn filter-reset',
          title: `Go back to ${fallback}, which moves with the calendar`,
          onclick: () => saveScanFilter(key, ''),
        }, `default is ${fallback}`)
      : 'the default, and it rolls forward');
  }

  function renderScanFilters(filters, window_) {
    const host = $('#scan-filters');
    if (!host) return;

    host.replaceChildren(
      scanField('Fewer tracks than', [
        el('input', {
          id: 'scan-min-tracks',
          type: 'number',
          min: '0',
          step: '1',
          value: String(filters.min_tracks ?? 0),
          onchange: (e) => saveScanFilter('min_tracks', e.target.value || '0'),
        }),
      ], '0 checks every album'),
      dateFilter(filters, 'min_date', 'Released before'),
      dateFilter(filters, 'max_date', 'Released after'),
      // The ceiling over every reason a scan has for asking again. "never"
      // keeps an answer for good.
      scanField('Looked up more than', durationControl({
        id: 'scan-recheck',
        days: window_,
        never: true,
        onChange: (days) => saveScanFilter('album_recheck_after_days', days),
      }), 'ago \u2014 anything older is looked up again'),
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
      // Passed through whole: the server sends what is set, what the default
      // currently is, and which of the two a scan will use.
      renderScanFilters(checker, checker.album_recheck_after_days ?? 365);
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
          // Read down rather than compared across, so it keeps its left edge.
          text: true,
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
          parts: (r) => [r.uploaded_at ? 'uploaded' : r.outcome],
          cell: (r) => el('span', {},
            uploadedPill(r) || el('span', { class: 'pill' }, r.outcome),
            r.reason && !r.uploaded_at ? el('div', { class: 'hint' }, r.reason) : null),
        },
        {
          label: 'Trackers',
          class: 'found-trackers',
          value: trackerSummary,
          parts: trackerParts,
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
          value: (r) => daysAgo(r.added_at),
          filter: 'days',
          class: 'nowrap',
          cell: (r) => whenCell(r.added_at),
        },
        {
          // The same column as Added, about a different event, so it reads the
          // same way. It was days only -- "0d ago" for something checked four
          // minutes ago, and "94d ago" where "3 months" is the thing being
          // judged -- while Added beside it had the units all along.
          label: 'Latest tracker check',
          value: (r) => daysAgo(r.checked_at),
          filter: 'days',
          class: 'nowrap',
          cell: (r) => {
            const days = r.checked_days_ago;
            const stale = state.scanWindow > 0 && days !== null && days >= state.scanWindow;
            return whenCell(r.checked_at, stale ? 'due a re-check' : '');
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
    // Stays here. It used to switch to the Scan sub-tab, which is neither
    // where the button was nor where the progress went.
    await recheckReleases(picked, '#scanhistory-log');
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
    keepAddress(name === 'history' ? '/requests/history' : '/requests');
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
          // Read down rather than compared across, so it keeps its left edge.
          text: true,
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
          // What the check found on each tracker, as links -- the same column
          // the queue and the scan history carry. A row that says a request
          // can be filled and that the tracker already has the release is
          // saying something you want to go and look at.
          label: 'Trackers',
          class: 'found-trackers',
          value: (r) => (r.found_on || r.missing_from ? trackerSummary(r) : ''),
          parts: trackerParts,
          filter: 'choice',
          cell: (r) => {
            const tags = (r.found_on || []).length || (r.missing_from || []).length
              ? trackerTags(r)
              : [];
            if (r.already_on_tracker === true && r.tracker_group_url) {
              tags.push(trackerTag(r.tracker, 'warn', 'already on tracker', r.tracker_group_url,
                                   'Open the release the tracker already has'));
            }
            return el('span', {}, ...(tags.length ? tags : [el('span', { class: 'tag dim' }, '—')]));
          },
        },
        {
          label: 'Outcome',
          value: (r) => (r.uploaded_at
            ? 'Uploaded'
            : (HISTORY_STATUS[r.status] || [r.status || 'Unknown'])[0]),
          filter: 'choice',
          cell: (r) => {
            const pair = HISTORY_STATUS[r.status] || [r.status || 'Unknown', ''];
            return el('span', {},
              uploadedPill(r) || el('span', { class: `pill ${pair[1]}` }, pair[0]),
              r.reason && !r.uploaded_at ? el('div', { class: 'hint' }, r.reason) : null);
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
          // What the tracker calls the moment a request was raised. "Opened"
          // was the odd one out: every other list in the app calls this
          // column Added, and two names for one idea is one too many.
          label: 'Added',
          value: (r) => (r.created ? daysAgo(Date.parse(r.created) / 1000) : -1),
          filter: 'days',
          class: 'nowrap',
          cell: (r) => (r.created_age
            ? dateCell(`${r.created_age} ago`, (r.created || '').slice(0, 10), '')
            : el('span', {}, '—')),
        },
        {
          label: 'Latest tracker check',
          value: (r) => daysAgo(r.checked_at),
          filter: 'days',
          class: 'nowrap',
          cell: (r) => {
            const days = r.checked_days_ago;
            const window_ = state.historyWindow;
            const stale = window_ > 0 && days !== null && days >= window_;
            return dateCell(
              r.checked_at ? ago(r.checked_at) : 'unknown',
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
    // Reported here rather than on the Find sub-tab this used to jump to.
    // Placeholders belong to a search's own result list, so they are not stood
    // up over a history table that already has the rows.
    for (const [tracker, ids] of byTracker) {
      // eslint-disable-next-line no-await-in-loop -- serial on purpose: two
      // trackers at once race the same budget guard.
      await requestsCheck(ids.map((id) => ({ id, tracker })),
                          { recheck: true, logSel: '#history-log' });
    }
    await loadHistory();
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
    if (!row) return;

    const learned = {
      artist: match.artist || '',
      title: match.album || '',
      year: match.year || '',
      bounty: match.bounty || '',
      created: match.created || '',
      url: match.request_url || '',
      tracker: match.tracker || row.tracker || '',
    };
    let touched = false;
    Object.entries(learned).forEach(([key, value]) => {
      const blank = !row[key] || (key === 'url' && row.url === '#')
        || (key === 'title' && row.title === `Request ${id}`);
      if (value && blank) { row[key] = value; touched = true; }
    });
    if (!touched) return;

    // Patched by class rather than by column position. The cells used to be
    // found as tr.children[2] and tr.children[3], which is a promise about the
    // order of the columns that the table can no longer keep -- sorting or
    // filtering rearranges nothing, but adding one column would have written
    // the year into the bounty.
    const tr = $(`#requests-results tr[data-request="${CSS.escape(id)}"]`);
    if (!tr) return;
    const link = tr.querySelector('.req-name a');
    if (link) {
      link.textContent = row.artist || row.title ? `${row.artist} — ${row.title}` : `Request ${id}`;
      if (row.url && row.url !== '#') link.href = row.url;
    }
    const year = tr.querySelector('.req-year');
    if (year) year.textContent = row.year || '';
    const bounty = tr.querySelector('.req-bounty');
    if (bounty) bounty.textContent = row.bounty || '';
  }

  function applyRequestResult(match) {
    const id = String(match.request_id);
    // Kept as data first. The cell is a rendering of it, so a sort or a filter
    // afterwards redraws the answer rather than losing it.
    state.requestResults.set(resultKey(match.tracker, id), match);
    if (match.fillable) state.requestMatches.set(id, match);

    // A pasted request arrives as a placeholder that knows nothing but its own
    // id, and the check is what learns the rest. Without this the row stayed
    // "— Request 80755" with an empty year and bounty even after the tracker
    // had answered with all of it.
    fillPastedRequestRow(match);

    const tr = $(`#requests-results tr[data-request="${CSS.escape(id)}"]`);
    if (!tr) return;
    const row = state.requestRows.find((r) => String(r.id) === id);
    const cell = tr.querySelector('.result');
    if (cell && row) cell.replaceChildren(requestResultCell(row));
    // A request that was already filled is not a failed check, it is a closed
    // request -- so the Filled column is corrected in place for a row that was
    // fetched before somebody filled it.
    if (match.status === 'filled' && row) {
      row.filled = true;
      row.filled_by = match.filled_by || row.filled_by || '';
      const filled = tr.querySelector('.req-filled');
      if (filled) {
        filled.replaceChildren(
          el('span', { class: 'tag dim', title: row.filled_by ? `by ${row.filled_by}` : '' }, 'filled'));
      }
    }
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

    // Rebuilt on a refresh, so the head can say how old the copy is.
    const drawHead = (detail) => el('div', { class: 'row split-head' },
      el('h3', { class: 'section-title' }, `Request on ${code || 'the tracker'}`),
      // What the terms are was settled when the request was posted, so the
      // stored copy is the answer and opening one is instant. The vote count
      // and the comments do move, which is why the age is on screen and there
      // is a button to ask again.
      detail && detail.cached_at
        ? el('span', { class: 'hint' }, `as of ${ago(detail.cached_at)}`)
        : null,
      detail
        ? el('button', {
            class: 'ghost',
            title: 'Ask the tracker again. Takes about a minute.',
            onclick: () => loadRequestSide(true),
          }, 'Refresh')
        : null,
      url ? el('a', { class: 'filebtn', href: url, target: '_blank', rel: 'noopener noreferrer' },
               'Open in a tab ↗') : null);

    left.replaceChildren(drawHead(null), spinner('Loading the request'));

    const deezerSide = (async () => {
      if (!match?.deezer_id) {
        right.replaceChildren(
          el('h3', { class: 'section-title' }, 'Deezer release'),
          empty('No Deezer match yet. Check this request to find one.'),
        );
        return;
      }
      right.replaceChildren(spinner('Loading release'));
      // The side-by-side view is where the two are actually compared, so it is
      // where "these are not the same record" is decided. Saying so needed the
      // queue, three tabs away, and did not stick.
      const notThis = el('div', { class: 'row split-head' },
        el('h3', { class: 'section-title' }, 'Deezer release'),
        el('button', {
          class: 'danger',
          title: 'This release does not fill this request. It leaves the queue and '
                 + 'will not be matched to it again.',
          onclick: () => rejectMatch({
            id: String(id),
            tracker: code,
            deezer_id: match.deezer_id,
            deezer_artist: match.deezer_artist,
            deezer_title: match.deezer_title,
          }),
        }, 'Not this release'));
      try {
        const album = await api(`/api/album/${match.deezer_id}`);
        right.replaceChildren(notThis, albumPanel(album));
      } catch (e) {
        right.replaceChildren(notThis, empty(e.message));
      }
    })();

    async function loadRequestSide(refresh = false) {
      if (!code || !id) {
        left.replaceChildren(drawHead(null), empty('This request has no tracker or id to look up.'));
        return;
      }
      if (refresh) {
        left.replaceChildren(drawHead(null), spinner('Asking the tracker again'));
      }
      try {
        const detail = await api(
          `/api/requests/detail?tracker=${encodeURIComponent(code)}&id=${encodeURIComponent(id)}`
          + (refresh ? '&refresh=1' : ''));
        left.replaceChildren(drawHead(detail), requestPanel(detail));
        // Somebody who arrived on the link had nothing to name the page with
        // until now. The tracker's own record has it, so use it.
        const title = [detail.artist, detail.title].filter(Boolean).join(' — ');
        if (title) {
          const crumb = $('.crumb.current', pane);
          if (crumb) crumb.textContent = title;
          setTitle(title);
        }
      } catch (e) {
        left.replaceChildren(drawHead(null), empty(e.message));
      }
    }

    await loadRequestSide();
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
      const { found } = await api('/api/found');
      state.found = found;
      state.selectedFound = new Set(found.map((f) => f.id));

      // What did not make the queue is not the queue's business.
      //
      // This was a table you could open, and then a line saying how many had
      // been kept out and why. Both were the same mistake: everything counted
      // there is something nobody wanted -- releases every tracker already
      // has, releases Deezer cannot supply, releases a rule the operator set
      // deliberately excludes -- and reporting the total made a working queue
      // look like it was withholding eighty-nine things. A queue is a list of
      // work; what is not work does not belong on it in any form.
      renderQueueUpkeep();
      renderFound();
    } catch (e) {
      body.replaceChildren(empty(e.message));
    }
  }

  /**
   * Whether the queue is keeping itself honest, and when it last checked.
   *
   * A queue row claims nobody has uploaded the release yet, and that stops
   * being true without anyone telling you: somebody else posts it, or the
   * request behind the row gets filled, and the row sits there for months
   * looking like work. Rows past the window are confirmed again in the
   * background, one at a time, and only while nothing else is running.
   *
   * The line is here rather than left to be inferred, because a queue that
   * silently drops rows is as confusing as one that silently keeps stale ones.
   */
  function renderQueueUpkeep() {
    const host = $('#found-upkeep');
    if (!host) return;
    const upkeep = state.queueRecheck;
    if (!upkeep || !upkeep.enabled) {
      host.textContent = 'Rows are only confirmed again when you ask. '
        + 'Settings → What reaches the queue can do it on a schedule.';
      return;
    }
    const days = upkeep.after_days;
    const when = upkeep.last_run ? `Last confirmed ${ago(upkeep.last_run)}.` : '';
    host.textContent =
      `Rows older than ${days} day${days === 1 ? '' : 's'} are confirmed again in the background, `
      + `while nothing else is running. ${when}`.trim();
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
      body.replaceChildren(empty('The queue is empty. Run a scan, or look up some requests.'));
      $('#found-count').textContent = '';
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
          // Read down rather than compared across, so it keeps its left edge.
          text: true,
          value: (f) => `${f.artist || ''} ${f.title || ''}`.trim(),
          filter: 'text',
          cell: (f) => el('a', {
            href: albumHref(f.album_id),
            onclick: (e) => { e.preventDefault(); goAlbum(f.album_id); },
          }, `${f.artist || ''} — ${f.title || ''}`),
        },
        {
          // Which pressing this is. Two editions of the same record are two
          // different uploads, and the year is how anybody tells them apart.
          label: 'Year',
          value: (f) => Number(String(f.year || '').slice(0, 4)) || 0,
          filter: 'range',
          lowLabel: 'from',
          highLabel: 'to',
          class: 'nowrap',
          cell: (f) => el('span', {}, String(f.year || '—')),
        },
        {
          // How much release this is. Four tracks and forty are different
          // propositions -- for the download, for the upload, and for
          // deciding which of them to do first -- and the queue was the one
          // list that would not say.
          label: 'Tracks',
          value: (f) => Number(f.deezer_tracks) || 0,
          filter: 'range',
          class: 'nowrap',
          cell: (f) => el('span', {}, f.deezer_tracks ? String(f.deezer_tracks) : '—'),
        },
        {
          label: 'Trackers',
          class: 'found-trackers',
          value: trackerSummary,
          parts: trackerParts,
          filter: 'choice',
          cell: (f) => el('span', {}, ...trackerTags(f)),
        },
        {
          label: 'Source',
          value: (f) => (f.sources || [f.kind]).join(', '),
          parts: (f) => f.sources || [f.kind],
          filter: 'choice',
          cell: (f) => el('span', {}, ...sourceTags(f)),
        },
        {
          label: 'Added',
          value: (f) => daysAgo(f.added_at),
          filter: 'days',
          class: 'nowrap',
          cell: (f) => whenCell(f.added_at),
        },
        {
          label: 'Last checked',
          value: (f) => daysAgo(f.checked_at),
          filter: 'days',
          class: 'nowrap',
          cell: (f) => whenCell(f.checked_at, staleNote(f.checked_at)),
        },
        {
          // Deciding about one release is the common case, and it was a
          // two-step: tick the box, travel to the toolbar, press. The toolbar
          // still does several at once, which is what it is for.
          label: '',
          filter: false,
          class: 'row-actions',
          cell: (f) => el('span', {},
            el('button', { class: 'ghost', title: 'Take it off the queue. A later scan can find it again.',
                           onclick: (e) => { e.stopPropagation(); dismissRows([f], false); } }, 'Remove'),
            el('button', { class: 'danger', title: 'Never list this release again.',
                           onclick: (e) => { e.stopPropagation(); dismissRows([f], true); } }, 'Blocklist')),
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
  /**
   * The tracker verdicts on a row, one string each.
   *
   * What trackerSummary joins together. The filter needs them apart: ticking
   * "OPS missing" should find every row OPS is missing, not only the rows
   * whose whole verdict happens to read "OPS missing, RED has it".
   *
   * @param {object} row - Anything with found_on / missing_from.
   * @returns {string[]} One phrase per tracker verdict.
   */
  /**
   * The "posted" pill, for a release that has been uploaded.
   *
   * The stamp was written on every successful upload and only ever read to
   * keep the row out of the queue, so both lookup histories went on saying
   * "missing from RED" about something posted to RED an hour earlier. It is
   * the most settled answer either page has and it was the one they would not
   * give.
   *
   * @param {object} row - A history row.
   * @returns {Element|null} The pill, or null when it has not been uploaded.
   */
  function uploadedPill(row) {
    if (!row.uploaded_at) return null;
    const where = (row.uploaded_to || []).join(', ');
    return el('span', {
      class: 'pill ok',
      title: `Uploaded ${where ? `to ${where} ` : ''}${ago(row.uploaded_at)}`,
    }, where ? `uploaded to ${where}` : 'uploaded');
  }

  function trackerParts(row) {
    const parts = [
      ...(row.missing_from || []).map((t) => `${t} missing`),
      ...(row.found_on || []).map((t) => `${t} has it`),
    ];
    // Drawn as a tag in the cell and never present in the value, so it could
    // not be filtered for at all.
    if (row.already_on_tracker === true) parts.push('already on tracker');
    return parts.length ? parts : ['not checked on any tracker'];
  }

  function trackerSummary(row) {
    const missing = (row.missing_from || []).map((t) => `${t} missing`);
    const found = (row.found_on || []).map((t) => `${t} has it`);
    if (!missing.length && !found.length) return 'not checked on any tracker';
    return [...missing, ...found].join(', ');
  }

  /**
   * One tracker verdict, as somewhere to go.
   *
   * "OPS is missing this" and "RED has it" are both claims about a page that
   * exists, and both used to be dead text -- so confirming either meant
   * copying the artist into the tracker's own search by hand. A tracker that
   * has it links to the group it matched; one that does not links to what that
   * artist already has there, which is where you would have gone anyway.
   *
   * @param {string} code - Tracker code.
   * @param {string} cls - Tag colour class.
   * @param {string} text - What the tag says.
   * @param {string} href - Where it goes, or "" for a tag with no page behind it.
   * @param {string} title - The tooltip.
   */
  function trackerTag(code, cls, text, href, title) {
    if (!href) return el('span', { class: `tag ${cls}` }, text);
    return el('a', {
      class: `tag ${cls} tag-link`,
      href,
      target: '_blank',
      rel: 'noopener',
      title,
      // A tag inside a row that opens something else on click: the link is the
      // more specific intent, so it wins.
      onclick: (e) => e.stopPropagation(),
    }, text);
  }

  /** Where a tracker lives, from the status the sidebar already polls. */
  const trackerHome = (code) => (state.trackers.find((t) => t.code === code) || {}).url || '';

  /**
   * Where each tracker verdict on a row leads.
   *
   * The stored rows carry this from the server, because it knows which group
   * matched. A result that has only just arrived over a job poll does not, so
   * it is worked out from the same three facts here rather than being the one
   * place in the app where a verdict is not a link.
   *
   * @param {object} row - Anything with found_on, missing_from and a name.
   * @returns {Object<string,string>} Tracker code to URL.
   */
  function trackerLinks(row) {
    if (row.tracker_links && Object.keys(row.tracker_links).length) return row.tracker_links;
    const groups = row.group_ids || {};
    const terms = encodeURIComponent([row.artist, row.title].filter(Boolean).join(' '));
    const out = {};
    for (const code of row.found_on || []) {
      const home = trackerHome(code);
      if (!home) continue;
      out[code] = groups[code]
        ? `${home}/torrents.php?id=${groups[code]}`
        : `${home}/torrents.php?searchstr=${terms}`;
    }
    for (const code of row.missing_from || []) {
      const home = trackerHome(code);
      if (!home || out[code]) continue;
      out[code] = row.artist
        ? `${home}/artist.php?artistname=${encodeURIComponent(row.artist)}`
        : `${home}/torrents.php?searchstr=${terms}`;
    }
    return out;
  }

  function trackerTags(row) {
    const missing = row.missing_from || [];
    const found = row.found_on || [];
    const links = trackerLinks(row);
    if (!missing.length && !found.length) {
      return [el('span', { class: 'tag dim' }, 'not checked on any tracker')];
    }
    return [
      ...missing.map((t) => trackerTag(t, 'ok', `${t} missing`, links[t],
                                       `Open what ${row.artist || 'this artist'} already has on ${t}`)),
      ...found.map((t) => trackerTag(t, 'dim', `${t} has it`, links[t],
                                     `Open the release on ${t}`)),
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
    return dismissRows(picked, blacklist);
  }

  /**
   * Take releases off the queue, optionally for good.
   *
   * Split out of dismissFound so a row can offer the same two decisions
   * without first being ticked. Deciding about one release is the common case
   * and it was a two-step: tick the box, travel to the toolbar, press.
   *
   * @param {object[]} picked - The rows to act on.
   * @param {boolean} blacklist - Never list them again, rather than just now.
   */
  async function dismissRows(picked, blacklist) {
    const what = picked.length === 1
      ? `${picked[0].artist || ''} — ${picked[0].title || ''}`.trim() || 'this release'
      : `${picked.length} releases`;
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
      releasesChanged();
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  /**
   * Something was checked, so every list that reads a check is out of date.
   *
   * The store is already one store -- a scan, a request check and a re-check
   * from the queue all write the same album records -- but each screen keeps
   * its own copy of what it last read. Re-checking from the queue therefore
   * dropped the row from the queue and left the scan's Lookup History showing
   * the answer from before, which reads as two databases disagreeing.
   *
   * The cached copies are dropped here, and whichever screen is on screen
   * re-reads at once.
   */
  function releasesChanged() {
    state.scanHistory = [];
    state.history = [];
    state.blacklist = [];
    if (state.view === 'found') {
      if (state.queueTab === 'blacklist') loadBlacklist();
      else loadFound();
    }
    if (state.view === 'missing' && state.scanTab === 'history') loadScanHistory();
    if (state.view === 'requests' && state.requestTab === 'history') loadHistory();
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
  async function recheckReleases(picked, boxSel = '#found-log') {
    const trackers = checkTrackers();
    if (!trackers.length) return toast('No tracker configured', 'bad');

    const candidates = picked.map((f) => ({
      album_id: f.album_id, title: f.title, artist: f.artist, source: 'found',
    }));
    try {
      const { job_id } = await api('/api/missing/check', { method: 'POST', body: { candidates, trackers } });
      // Whichever page asked. This was always the queue's log, so a re-check
      // started from the scan's lookup history reported onto a page the user
      // was not on: the button looked dead and the work was invisible.
      const log = $(boxSel) || $('#found-log');
      log.hidden = false;
      log.textContent = 'Starting…';
      log.after(jobCancel(job_id, 'Stop re-checking'));
      followJob(job_id, {
        onUpdate: (job) => jobProgress(log, job),
        onDone: (job) => {
          refreshStatus();
          jobFinished(log, job.error || `Re-checked ${job.result_count} release(s).`);
          toast(job.error || `Re-checked ${job.result_count} release(s)`, job.error ? 'bad' : 'ok');
          releasesChanged();
        },
      });
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  // ---------------------------------------------------------------- uploads

  const folderKey = (f) => f.path;

  /** A byte count as the folder list says it. */
  function fileSize(bytes) {
    const n = Number(bytes) || 0;
    if (!n) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = n;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
    return `${value >= 10 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
  }

  function foldersPick() {
    const shown = tableView('folders').shown || state.folders || [];
    const n = countSelected(shown, state.selectedFolders, folderKey);
    const button = $('#folders-upload');
    if (button) {
      button.disabled = n === 0;
      button.textContent = n > 1 ? `Upload ${n} in turn` : 'Upload selected';
    }
    const remove = $('#folders-delete');
    if (remove) {
      remove.disabled = n === 0;
      remove.textContent = n > 1 ? `Delete ${n}` : 'Delete selected';
    }
    const label = $('#folders-selected');
    if (label) label.textContent = n ? `${n} of ${shown.length} selected` : '';
  }

  /**
   * Delete every folder that is ticked.
   *
   * Named rather than counted where there are few of them: "delete 3 folders"
   * and "delete these three releases" are different amounts of information,
   * and this one cannot be undone.
   */
  async function deleteSelectedFolders() {
    const shown = tableView('folders').shown || state.folders || [];
    const picked = shown.filter((f) => state.selectedFolders.has(f.path));
    if (!picked.length) return;
    const names = picked.slice(0, 6).map((f) => f.name).join('\n');
    const rest = picked.length > 6 ? `\n…and ${picked.length - 6} more` : '';
    if (!confirm(`Delete ${picked.length} release${picked.length === 1 ? '' : 's'}?\n\n${names}${rest}`
                 + '\n\nThis removes the files from disk and cannot be undone.')) {
      return;
    }
    let gone = 0;
    for (const folder of picked) {
      try {
        // eslint-disable-next-line no-await-in-loop -- one at a time so a
        // failure names the folder it failed on.
        await api('/api/folders/delete', { method: 'POST', body: { folder: folder.path } });
        gone += 1;
        state.selectedFolders.delete(folder.path);
      } catch (e) {
        toast(`${folder.name}: ${e.message}`, 'bad');
      }
    }
    if (gone) toast(`Deleted ${gone} release${gone === 1 ? '' : 's'}`, 'ok');
    await loadFolders();
  }

  async function loadFolders() {
    const list = $('#folders-list');
    list.replaceChildren(spinner('Reading download folder'));
    try {
      const { folders, directory, linking, error: dirError } = await api('/api/folders');
      $('#uploads-dir').textContent = directory;
      state.linking = linking;
      state.folders = folders;
      // A folder that has been uploaded or deleted since the last read should
      // not still be ticked and counted.
      const alive = new Set(folders.map(folderKey));
      [...state.selectedFolders].forEach((k) => alive.has(k) || state.selectedFolders.delete(k));

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
        foldersPick();
        return;
      }
      list.replaceChildren(dataTable({
        name: 'folders',
        rows: folders,
        selection: { set: state.selectedFolders, onChange: foldersPick },
        onShown: foldersPick,
        idOf: folderKey,
        empty: 'Nothing here yet.',
        columns: [
          {
            label: 'Folder',
            // Read down rather than compared across, so it keeps its left edge.
            text: true,
            value: (f) => f.name,
            filter: 'text',
            cell: (f) => el('span', {},
              el('div', {}, f.name),
              queuePositionOf(f) !== null
                ? el('span', { class: 'hint' },
                     queuePositionOf(f) === 0 ? 'uploading now' : `queued, #${queuePositionOf(f)}`)
                : null),
          },
          {
            label: 'Tracks',
            value: (f) => Number(f.tracks) || 0,
            filter: 'range',
            class: 'nowrap',
            cell: (f) => el('span', {}, String(f.tracks)),
          },
          {
            label: 'Size',
            value: (f) => (Number(f.bytes) || 0) / (1024 ** 3),
            filter: 'range',
            class: 'nowrap',
            cell: (f) => el('span', {}, fileSize(f.bytes)),
          },
          {
            label: 'Finished',
            value: (f) => daysAgo(f.modified),
            filter: 'days',
            class: 'nowrap',
            cell: (f) => whenCell(f.modified),
          },
          {
            label: '',
            filter: false,
            class: 'row-actions',
            cell: (f) => el('span', {},
              el('button', { class: 'primary', onclick: () => queueUploads([f]) }, 'Upload'),
              el('button', { class: 'danger', onclick: () => deleteFolder(f.path, f.name, loadFolders) },
                 'Delete')),
          },
        ],
      }));
      foldersPick();
    } catch (e) {
      list.replaceChildren(empty(e.message));
    }
  }

  // -------------------------------------------------------- upload history
  //
  // An upload is the one thing in here that cannot be repeated to find out
  // what happened. The torrent is posted, the flow is dropped when the page is
  // closed, and everything it printed goes with it -- so afterwards there was
  // nowhere to see what a release had been posted as, which tracker took it,
  // or why one of the two refused.

  async function loadUploadHistory() {
    const host = $('#uphistory-results');
    host.replaceChildren(spinner('Loading'));
    try {
      const { uploads, total } = await api('/api/uploads/history');
      state.uploads = uploads;
      $('#uphistory-count').textContent = total
        ? `${total} upload${total === 1 ? '' : 's'}`
        : 'nothing uploaded yet';
      renderUploadHistory();
    } catch (e) {
      host.replaceChildren(empty(e.message));
    }
  }

  /** What one upload did, per tracker, as links where there are any. */
  function uploadOutcomeTags(row) {
    const tags = (row.outcomes || []).map((o) => {
      if (!o.ok) return el('span', { class: 'tag bad', title: o.error || '' }, `${o.tracker} failed`);
      // The torrent it posted where there is one, the group it went into
      // otherwise. A tracker name on this page is a thing that now exists on
      // that tracker, so it should open it.
      const href = o.url || trackerLinks({ found_on: [o.tracker], group_ids: row.group_ids || {},
                                           artist: row.artist || '', title: row.release || '' })[o.tracker];
      return trackerTag(o.tracker, 'ok', o.tracker, href, `Open what was uploaded to ${o.tracker}`);
    });
    if (row.dry_run) tags.push(el('span', { class: 'tag warn' }, 'dry run'));
    return tags.length ? tags : [el('span', { class: 'tag dim' }, '—')];
  }

  /** Everything one upload printed, and what it was posted as. */
  function uploadDetail(row) {
    const fields = Object.entries(row.fields || {});
    const descriptions = Object.entries(row.descriptions || {});
    return el('div', { class: 'upload-detail' },
      fields.length
        ? el('details', { class: 'diff meta-block' },
            el('summary', {}, 'What it was posted as',
               el('span', { class: 'card-sub' }, ` — ${fields.length} fields`)),
            el('table', { class: 'table meta-table' },
              el('tbody', {},
                ...fields.map(([key, value]) =>
                  el('tr', {},
                    el('td', { class: 'meta-field' }, key),
                    el('td', {}, String(value)))))))
        : null,
      ...descriptions.map(([name, text]) =>
        el('details', { class: 'diff meta-block' },
          el('summary', {}, name),
          el('pre', { class: 'upload-desc' }, text))),
      el('details', { class: 'diff meta-block', open: !fields.length },
        el('summary', {}, 'Log',
           el('span', { class: 'card-sub' }, ` — ${(row.log || []).length} lines`)),
        el('div', { class: 'joblog upload-log' },
          ...(row.log || []).map((line) =>
            el('div', { class: `joblog-line ${line.level === 'info' ? '' : line.level}` },
               line.message)))));
  }

  function renderUploadHistory() {
    $('#uphistory-results').replaceChildren(dataTable({
      name: 'uploads',
      rows: state.uploads,
      idOf: (r) => r.id,
      empty: 'Nothing has been uploaded yet.',
      columns: [
        {
          label: 'Release',
          text: true,
          value: (r) => r.release || '',
          filter: 'text',
          cell: (r) => el('details', { class: 'upload-row' },
            el('summary', {}, r.release || '(unnamed)'),
            uploadDetail(r)),
        },
        {
          label: 'Trackers',
          class: 'found-trackers',
          value: (r) => (r.outcomes || []).map((o) => `${o.tracker} ${o.ok ? 'ok' : 'failed'}`).join(', '),
          parts: (r) => (r.outcomes || []).map((o) => `${o.tracker} ${o.ok ? 'ok' : 'failed'}`),
          filter: 'choice',
          cell: (r) => el('span', {}, ...uploadOutcomeTags(r)),
        },
        {
          label: 'Result',
          value: (r) => (r.error ? 'failed' : r.succeeded?.length ? 'uploaded' : 'nothing posted'),
          filter: 'choice',
          cell: (r) => (r.error
            ? el('span', { class: 'tag bad', title: r.error }, 'failed')
            : r.succeeded?.length
              ? el('span', { class: 'tag ok' }, `${r.succeeded.length} posted`)
              : el('span', { class: 'tag dim' }, 'nothing posted')),
        },
        {
          label: 'When',
          value: (r) => daysAgo(r.finished),
          filter: 'days',
          class: 'nowrap',
          cell: (r) => whenCell(r.finished),
        },
      ],
    }));
  }

  // ------------------------------------------------------------ the queue
  //
  // Uploading was one folder at a time and only ever the one you had just
  // pressed: a night's worth of releases meant sitting at the page pressing
  // Upload, waiting, pressing Upload. They queue now, and the queue runs
  // itself -- each one starts when the one before it finishes or is called
  // off, and the questions an upload asks are still asked one release at a
  // time, which is the reason it is a queue rather than a free-for-all.

  /** Where a folder sits in the queue: 0 for the one running, or null. */
  function queuePositionOf(folder) {
    if (state.uploadCurrent && state.uploadCurrent.path === folder.path) return 0;
    const at = state.uploadQueue.findIndex((q) => q.path === folder.path);
    return at < 0 ? null : at + 1;
  }

  function renderUploadQueue() {
    const host = $('#upload-queue');
    if (!host) return;
    const waiting = state.uploadQueue.length;
    if (!state.uploadCurrent && !waiting) { host.hidden = true; host.replaceChildren(); return; }

    host.hidden = false;
    host.replaceChildren(el('div', { class: 'panel upload-queue' },
      el('div', { class: 'row' },
        el('strong', {}, state.uploadCurrent
          ? `Uploading ${state.uploadCurrent.name}`
          : 'Starting the next upload'),
        el('span', { class: 'hint' },
           waiting ? `${waiting} waiting behind it` : 'last one in the queue'),
        // Cancelling the one on screen only ever stopped that one, and the
        // rest started immediately afterwards -- which is not what anybody
        // pressing Cancel on a batch means.
        waiting
          ? el('button', { class: 'danger', onclick: cancelQueuedUploads },
               `Cancel the other ${waiting}`)
          : null),
      waiting
        ? el('ol', { class: 'queue-list' },
            ...state.uploadQueue.map((q) =>
              el('li', {},
                q.name,
                el('button', {
                  class: 'linkbtn',
                  title: 'Take this one out of the queue',
                  onclick: () => {
                    state.uploadQueue = state.uploadQueue.filter((other) => other.path !== q.path);
                    renderUploadQueue();
                    loadFolders();
                  },
                }, 'remove'))))
        : null));
  }

  /**
   * Add folders to the upload queue and start it if it is idle.
   *
   * @param {Array} folders - Rows from the folder list.
   */
  function queueUploads(folders) {
    if (!state.uploadTrackers.length) return toast('Pick at least one tracker to upload to', 'bad');
    const known = new Set([
      ...state.uploadQueue.map((q) => q.path),
      state.uploadCurrent ? state.uploadCurrent.path : '',
    ]);
    const fresh = folders.filter((f) => !known.has(f.path));
    if (!fresh.length) return toast('Already queued', '');

    state.uploadQueue.push(...fresh.map((f) => ({ path: f.path, name: f.name })));
    state.selectedFolders.clear();
    renderUploadQueue();
    foldersPick();
    if (fresh.length > 1) toast(`${fresh.length} queued. They upload one after another.`, 'ok');
    return runUploadQueue();
  }

  /** Empty the queue, leaving whatever is running to finish or be cancelled. */
  function cancelQueuedUploads() {
    const dropped = state.uploadQueue.length;
    state.uploadQueue = [];
    renderUploadQueue();
    loadFolders();
    if (dropped) toast(`${dropped} removed from the queue. The one running was left alone.`, 'ok');
  }

  /** Work through the queue, one upload at a time. */
  async function runUploadQueue() {
    if (state.uploadCurrent) return;
    while (state.uploadQueue.length) {
      const next = state.uploadQueue.shift();
      state.uploadCurrent = next;
      renderUploadQueue();
      // eslint-disable-next-line no-await-in-loop -- the point of a queue: an
      // upload asks questions, and two of them asking at once gives you a
      // prompt you cannot tell the release for.
      await startUpload(next.path, null, next.albumId);
      state.uploadCurrent = null;
      renderUploadQueue();
    }
    loadFolders();
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
          if (state.uploadTab === 'history') loadUploadHistory();
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

    // A tag diff is a receipt, not a question: it is what the tagger already
    // decided, one row per field per file, and open by default it pushed the
    // question you are actually being asked off the bottom of the screen. The
    // summary still says how many fields changed, which is the part worth
    // reading at a glance; opening it is one click when a number looks wrong.
    const receipt = table.kind === 'tags' || table.kind === 'album_tags';
    return el(
      'details',
      { class: 'diff', open: !receipt && table.rows.length <= 40 },
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
          // The artists were printed here, greyed. The trackers refuse a track
          // with no main artist, so the form could show that error and offer
          // no field that answered it -- Save could only ask the same question
          // again, which is exactly what it looked like from the outside.
          field.append(el('div', { class: 'meta-tracks' },
            ...section.rows.map((row) => {
              const input = el('input', { type: 'text' });
              input.value = row.value || '';
              input.addEventListener('input', () => (row.value = input.value));
              const who = el('input', { type: 'text', class: 'meta-track-artists',
                                        placeholder: 'Artists, separated by commas' });
              who.value = row.artists || '';
              who.addEventListener('input', () => (row.artists = who.value));
              return el('div', { class: 'meta-track' },
                el('span', { class: 'meta-track-no' }, row.label || ''),
                input,
                who);
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
      // The field the validator refused. The message used to go into the prose
      // above thirteen fields and leave the reader to work out which one it
      // meant: "You must specify at least one genre" with the genre list four
      // screens down reads as a form that will not save and will not say why.
      if (section.error) {
        field.classList.add('meta-form-bad');
        field.append(el('p', { class: 'meta-form-hint meta-form-error' }, step.detail || 'Fix this to carry on.'));
      }
      return field;
    };

    const draw = () => {
      body.replaceChildren(...groups.map((group) =>
        el('section', { class: 'meta-group' },
           group.group ? el('h4', { class: 'meta-group-head' }, group.group) : null,
           el('div', { class: 'meta-form' }, ...group.fields.map(drawField)))));
      // Scrolled to, because the form is taller than the screen and a field
      // highlighted below the fold is a field nobody has been shown.
      const bad = body.querySelector('.meta-form-bad');
      if (bad) requestAnimationFrame(() => bad.scrollIntoView({ block: 'center', behavior: 'smooth' }));
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
          out.tracks = Object.fromEntries(
            section.rows.map((r) => [r.key, { title: r.value ?? '', artists: r.artists ?? '' }]));
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
             // The torrent that now exists, where there is one. A real run
             // knows its exact id, so "uploaded" opens the upload rather than
             // being a word about it.
             o.ok && o.url && !result.dry_run
               ? trackerTag(o.tracker, 'ok', 'uploaded', o.url, `Open it on ${o.tracker}`)
               : el('span', { class: `tag ${o.ok ? 'ok' : 'bad'}` },
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
  // What a rehearsal left on disk, and only a rehearsal.
  //
  // A real run's downconversions were posted and are seeding: offering to
  // delete them on the page that says the upload succeeded invites you to
  // break four torrents you just made. This panel exists because a dry run
  // hardlinks and transcodes without posting anything, so what it leaves is
  // genuinely litter -- which is why the server sends nothing here for a run
  // that was not one.
  function leftovers(result) {
    if (!result.dry_run) return null;
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
         el('strong', {}, 'Files this rehearsal left behind'),
         el('span', { class: 'card-sub' },
            `${items.length} folder${items.length === 1 ? '' : 's'}`)),
      el('p', { class: 'hint' },
         'Nothing was posted or seeded, so none of this is in use. '
         + 'Delete what you do not want to keep.'),
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

  // Take a card out of the "sent, waiting" state. The class was only ever
  // removed when sending FAILED, so a card that answered successfully stayed
  // greyed for the rest of the run: every later question -- the spectrals
  // among them -- was asked through a faded form that read as disabled.
  function unbusy(card) {
    if (!card) return;
    card.classList.remove('answering');
    $$('button, input, select, textarea', card).forEach((c) => { c.disabled = false; });
    $$('.answering-note', card).forEach((note) => note.remove());
  }

  function flowStep(flow) {
    const step = flow.step;
    // Answering is not instant: the pipeline picks the answer up on its own
    // schedule and may spend a while before it publishes the next question.
    // Nothing said so, so Save appeared to do nothing, and pressing it again
    // was answered with "that question has already been answered" -- an error
    // about having been too patient the first time.
    const send = async (value) => {
      const card = $(`#flow-${CSS.escape(flow.id)}`);
      if (state.answering.has(step.id)) return;
      state.answering.add(step.id);
      if (card) {
        card.classList.add('answering');
        // Only the question's own controls. Disabling the whole card took
        // Cancel with it, and the header is not redrawn unless the run changes
        // state -- so Cancel stayed dead for the rest of the upload.
        $$('button, input, select, textarea', $('.flow-step', card) || card)
          .forEach((c) => { c.disabled = true; });
        const controls = $('.step-controls', card);
        if (controls) controls.append(el('span', { class: 'hint answering-note' },
                                          'Sent. Waiting for the upload to carry on…'));
      }
      try {
        await api(`/api/flows/${flow.id}/answer`, {
          method: 'POST',
          body: { step_id: step.id, value },
        });
      } catch (e) {
        // A 409 means the question had already moved on -- the answer is in,
        // and the next poll will draw whatever came next. That is not a
        // failure worth putting in front of anybody.
        if (!/already been answered/i.test(e.message)) {
          state.answering.delete(step.id);
          unbusy(card);
          toast(e.message, 'bad');
        }
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

    // A question this page has not answered yet means the last one arrived and
    // was acted on, so the guard that stopped it being sent twice is spent.
    if (flow.step && !state.answering.has(flow.step.id)) state.answering.clear();

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
    // The head carries two things that change independently of the run's
    // state: how many uploads are still queued behind this one, and whether
    // this one is filling a request. Keyed on all three so neither is stale.
    const waiting = state.uploadQueue.length;
    const context = flow.context || {};
    const headKey = `${flow.state}|${waiting}|${context.request_url || ''}`;
    // data-state stays the plain state, because the rail counts the cards
    // waiting on somebody with it. The composite goes in its own attribute.
    head.dataset.state = flow.state;
    if (head.dataset.head !== headKey) {
      head.dataset.head = headKey;
      head.replaceChildren(
        ...[
          el('h2', {}, flow.label),
          el('span', { class: `tag ${stateTag}` }, flow.state === 'waiting' ? 'needs you' : flow.state),
          // Filling a request is the one upload where posting the wrong
          // release cannot be undone, and the card said only which folder it
          // was reading. The request it is answering is one click now.
          context.request_url
            ? el('a', {
                class: 'tag warn tag-link',
                href: context.request_url,
                target: '_blank',
                rel: 'noopener',
                title: 'Open the request this fills',
              }, `fills ${context.request_tracker || ''} #${context.request_id || ''}`.replace('  ', ' '))
            : null,
          // What is still to come. It was on a panel of its own above the
          // cards, which is not where you are looking while answering one.
          (flow.state === 'running' || flow.state === 'waiting') && waiting
            ? el('span', { class: 'tag dim', title: 'Uploads queued behind this one' },
                 `${waiting} more waiting`)
            : null,
          flow.state === 'running' || flow.state === 'waiting'
            ? el('button', { class: 'ghost', onclick: () => cancelFlow(flow.id) }, 'Cancel')
            // Finished, so the way off the page is dismissing it rather than
            // cancelling something that is not running. The record of it is in
            // Upload History either way.
            : el('button', { class: 'ghost', title: 'Kept in Upload History',
                             onclick: () => dismissFlow(flow.id) }, 'Dismiss'),
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
      // A new question means the last answer landed and the run moved on, so
      // whatever it left disabled comes back. Done before the rebuild, so the
      // controls outside the step box are reached too.
      unbusy(card);
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

  /**
   * Take a finished run off the page.
   *
   * A done card sat there until the browser was reloaded, so the tab that says
   * what is uploading went on showing what already had. What it did is not
   * lost by dismissing it -- Upload History keeps every run, with its log.
   *
   * @param {string} flowId - The finished run.
   */
  async function dismissFlow(flowId) {
    try {
      await api(`/api/flows/${flowId}/dismiss`, { method: 'POST' });
    } catch (e) {
      return toast(e.message, 'bad');
    }
    state.flows.delete(flowId);
    $(`#flow-${CSS.escape(flowId)}`)?.remove();
    railNeedsYou($$('.flow-head[data-state="waiting"]').length);
    return undefined;
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

    /**
     * One setting, in three parts, always in the same order.
     *
     * The page lays these out in a grid, and a grid can only line up a row of
     * settings if every one of them has the same parts in the same places.
     * They did not: a checkbox had no control row at all and sat level with
     * its neighbours' labels, a field with a long note pushed the one beside
     * it out of line, and a two-line label shunted its own box down while the
     * box next to it stayed put. Nothing on the page shared a baseline with
     * anything else.
     *
     * The help slot is present even when empty, because an absent row is a
     * row the grid cannot reserve.
     *
     * @param {Node|null} head - The label row, or null for a control that
     *   carries its own label (a checkbox).
     * @param {Node|Node[]} control - The control row.
     * @returns {HTMLElement} The setting.
     */
    const setting = (head, ...control) => el(
      'div',
      { class: 'setting' },
      el('div', { class: 'setting-head' }, head),
      el('div', { class: 'setting-control' }, ...control.filter(Boolean)),
      el('p', { class: 'hint setting-help' }, field.help || ''),
    );

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
      return setting(el('label', {}, field.label), el('div', { class: 'bytes-input' }, size, units));
    }

    let input;
    if (field.kind === 'bool') {
      input = el('input', { type: 'checkbox', checked: !!value, onchange: onInput });
      // The checkbox carries its own label, so the head row is empty and the
      // tick lines up with the boxes and dropdowns beside it rather than with
      // their labels.
      return setting(null, el('label', { class: 'check' }, input, field.label));
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

    return setting(
      [
        el('label', {}, field.label, configured ? el('span', { class: 'tag ok saved-tag' }, 'saved') : null),
        test,
      ],
      input,
      resultBox,
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
        el('p', { class: 'hint' },
           'Clearing history makes the next scan look everything up again from scratch, which takes '
           + 'as long as the first scan did and uses the same tracker allowance.'),
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

    fill(panel,
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
  /**
   * One field on the torrent-client editor.
   *
   * Three parts in three rows, matching settingField() on the settings page,
   * because both of them are laid out on .settings-grid and a grid can only
   * line a row up if every cell in it has the same shape.
   *
   * @param {string|Node|null} label - The label, or null for a control that
   *   carries its own (a checkbox).
   * @param {Node} control - The control.
   * @param {string} [help] - The note under it.
   */
  function settingBox(label, control, help) {
    return el(
      'div',
      { class: 'setting' },
      el('div', { class: 'setting-head' }, label ? el('label', {}, label) : null),
      el('div', { class: 'setting-control' }, control),
      el('p', { class: 'hint setting-help' }, help || ''),
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
      grid.append(settingBox(
        null,
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
          grid.append(settingBox(
            null,
            el('label', { class: 'check' },
               el('input', { type: 'checkbox', checked: !!box[field.key],
                             onchange: (e) => (box[field.key] = e.target.checked) }),
               field.label),
            field.help));
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
    $('#missing-scan').addEventListener('click', () => missingScan());
    // A reload used to bring back whatever was in the box when you left, which
    // for anyone who had just run a scan was that scan's list -- sitting there
    // looking like the next thing they were about to do. The browser restores
    // it; this is what does not.
    $('#missing-sources').value = '';
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
    // Saving is the same links the Scan button reads, so it hangs off the
    // same box rather than a form of its own underneath it.
    $('#missing-save').addEventListener('click', saveSources);
    $('#watchlist-scan').addEventListener('click', () => scanWatchlists([...state.watchSelected]));
    // Everything saved, in one scan. An empty list means all of them.
    $('#watchlist-scan-all').addEventListener('click', () => scanWatchlists([]));
    $('#watchlist-delete').addEventListener('click', () => deleteWatchlists([...state.watchSelected]));
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
    $$('#queue-tabs button').forEach((b) => {
      b.addEventListener('click', () => showQueueTab(b.dataset.queuetab));
    });
    $('#blacklist-restore').addEventListener('click', restoreBlacklisted);
    $('#blacklist-clear').addEventListener('click', async () => {
      if (!confirm('Clear the whole blacklist?\n\nEvery release on it can be found by a scan again.')) {
        return;
      }
      const { restored } = await api('/api/found/restore', { method: 'POST', body: {} });
      toast(`Cleared ${restored} from the blacklist`, 'ok');
      loadBlacklist();
    });

    $$('#upload-tabs button').forEach((b) => {
      b.addEventListener('click', () => showUploadTab(b.dataset.uptab));
    });
    $('#uphistory-refresh').addEventListener('click', loadUploadHistory);
    $('#uphistory-clear').addEventListener('click', async () => {
      if (!confirm('Forget every upload in this list?\n\nNothing already posted is affected.')) return;
      await api('/api/uploads/history/clear', { method: 'POST' });
      loadUploadHistory();
    });
    $('#folders-delete').addEventListener('click', deleteSelectedFolders);
    $('#folders-refresh').addEventListener('click', loadFolders);
    // Several at once, uploaded one after another. Pressing Upload on each of
    // eight folders and waiting between them was the whole evening.
    $('#folders-upload').addEventListener('click', () => {
      const shown = tableView('folders').shown || state.folders || [];
      queueUploads(shown.filter((f) => state.selectedFolders.has(f.path)));
    });
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
