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
    // Releases ticked for a batch action, by album id.
    picked: new Map(),
    uploadTrackers: new Set(),
    albumCheck: null,
    watchlists: [],
    linking: false,
    requestRows: [],
    selectedRequests: new Set(),
    trackers: [],
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

  const duration = (seconds) => {
    if (!seconds) return '';
    const m = Math.floor(seconds / 60);
    const s = String(Math.floor(seconds % 60)).padStart(2, '0');
    return `${m}:${s}`;
  };

  const empty = (message) => el('p', { class: 'empty' }, message);
  const spinner = (label) => el('p', { class: 'empty' }, el('span', { class: 'spinner' }), ' ' + label);

  // ---------------------------------------------------------------- routing

  function setView(view) {
    state.view = view;
    $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
    $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${view}`));
    $('#view-title').textContent = $(`.nav-item[data-view="${view}"]`).textContent.trim();
    if (view === 'explore') loadExplore();
    if (view === 'missing') loadWatchlists();
    if (view === 'requests' && state.requestFiltersFor !== state.requestsTracker) loadRequestFilters();
    if (view === 'downloads') pollDownloads(true);
    if (view === 'uploads') { loadFolders(); resumeFlows(); }
    if (view === 'settings') loadSettings();
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

    const badge = $('#dl-badge');
    badge.hidden = !status.downloads.active;
    badge.textContent = status.downloads.active;
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
      el('button', { class: 'link', onclick: () => setView('settings') }, 'Open settings'),
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

    if (!state.requestsTracker && state.trackers.length) state.requestsTracker = state.trackers[0].code;
    $('#requests-tracker').replaceChildren(
      ...state.trackers.map((t) =>
        el(
          'button',
          {
            type: 'button',
            class: state.requestsTracker === t.code ? 'active' : '',
            onclick: () => {
              state.requestsTracker = t.code;
              renderTrackerPickers();
              loadRequestFilters();
            },
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
    const node = el(
      'div',
      {
        class: `card ${item.type}`,
        onclick: () => {
          if (item.type === 'artist') openArtist(item.id);
          else if (albumId) openAlbum(albumId);
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
                checked: state.picked.has(albumId),
                onchange: (e) => togglePick(albumId, item, e.target.checked, node),
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
              href: '#',
              onclick: (e) => { e.preventDefault(); e.stopPropagation(); openArtist(item.artist_id); },
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
    if (state.picked.has(albumId)) node.classList.add('picked');
    return node;
  }

  // ------------------------------------------------------------ selection

  function togglePick(albumId, item, on, node) {
    if (on) state.picked.set(albumId, item);
    else state.picked.delete(albumId);
    node?.classList.toggle('picked', on);
    renderPickBar();
  }

  function clearPicks() {
    state.picked.clear();
    $$('.card.picked').forEach((c) => {
      c.classList.remove('picked');
      const box = c.querySelector('.card-pick input');
      if (box) box.checked = false;
    });
    renderPickBar();
  }

  // A bar that only exists while something is selected, so the page is not
  // permanently carrying controls for a thing you are usually not doing.
  function renderPickBar() {
    const bar = $('#pick-bar');
    const count = state.picked.size;
    bar.hidden = !count;
    if (!count) return;
    const items = () => [...state.picked.entries()].map(([id, item]) => ({ id, item }));
    bar.replaceChildren(
      el('strong', {}, `${count} selected`),
      el('button', { onclick: () => bulkDownload(items()) }, 'Download'),
      el('button', { onclick: () => bulkDownloadAndUpload(items()) }, 'Download & upload'),
      el('button', { onclick: () => bulkCheck(items()) }, 'Check trackers'),
      el('button', { class: 'ghost', onclick: clearPicks }, 'Clear'),
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
      setView('downloads');
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  // Uploads run one at a time. Several at once would interleave their
  // questions, and a prompt you cannot tell the release for is worse than
  // waiting.
  async function bulkDownloadAndUpload(entries) {
    const trackers = [...state.uploadTrackers];
    if (!trackers.length) return toast('Pick a tracker to upload to first', 'bad');
    clearPicks();
    setView('uploads');
    toast(`Queued ${entries.length} for download and upload, one at a time.`);
    for (const { id, item } of entries) {
      // Sequential on purpose: each waits for the one before it to finish.
      await downloadAndUpload(id, item, { quiet: true });
    }
  }

  async function bulkCheck(entries) {
    const trackers = checkTrackers();
    if (!trackers.length) return toast('No tracker configured', 'bad');
    setView('missing');
    const box = $('#missing-sources');
    const urls = entries
      .map(({ item }) => item.url || (item.id ? `https://www.deezer.com/album/${item.id}` : ''))
      .filter(Boolean);
    box.value = [box.value.trim(), ...urls].filter(Boolean).join('\n');
    clearPicks();
    toast(`${urls.length} added. Press Check to spend budget on them.`);
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
  function pushPane() {
    const pane = $('#search-results');
    if (!pane.childNodes.length) return;
    state.paneStack.push({ cls: pane.className, nodes: [...pane.childNodes], scroll: window.scrollY });
    if (state.paneStack.length > 12) state.paneStack.shift();
  }

  function popPane() {
    const previous = state.paneStack.pop();
    if (!previous) return;
    const pane = $('#search-results');
    pane.className = previous.cls;
    pane.replaceChildren(...previous.nodes);
    window.scrollTo(0, previous.scroll);
  }

  // Shown only when there is somewhere to go back to.
  function backButton() {
    if (!state.paneStack.length) return null;
    return el('button', { class: 'ghost back-btn', onclick: popPane }, '← Back');
  }

  // ---------------------------------------------------------------- search

  const SECTION_LABEL = { album: 'Albums', track: 'Tracks', artist: 'Artists' };

  async function runSearch(event) {
    event?.preventDefault();
    const query = $('#search-input').value.trim();
    if (!query) return;
    // Unfiltered results stack as sections, each holding its own grid; a single
    // kind is just a grid.
    const single = state.searchType !== 'all';
    // A new search is a new root, so there is nothing behind it any more.
    state.paneStack.length = 0;
    const results = searchPane(single ? 'grid' : 'search-sections');
    results.replaceChildren(spinner('Searching Deezer'));
    try {
      const data = await api(`/api/search?q=${encodeURIComponent(query)}&type=${state.searchType}`);
      if (single) {
        renderGrid(results, data.results, 'Nothing found.');
        return;
      }

      const sections = Object.entries(data.sections || {}).filter(([, rows]) => rows.length);
      if (!sections.length) {
        results.replaceChildren(empty('Nothing found.'));
        return;
      }
      results.replaceChildren(
        ...sections.flatMap(([kind, rows]) => [
          el(
            'div',
            { class: 'section-head' },
            el('h3', { class: 'section-title' }, `${SECTION_LABEL[kind] || kind} (${rows.length})`),
            // Straight to that kind on its own, which is what the filter is for.
            el('button', { class: 'link', onclick: () => selectSearchType(kind) }, 'Only these'),
          ),
          el('div', { class: 'grid' }, ...rows.map(card)),
        ]),
      );
    } catch (e) {
      results.replaceChildren(empty(e.message));
    }
  }

  function selectSearchType(type) {
    state.searchType = type;
    $$('#search-type button').forEach((b) => b.classList.toggle('active', b.dataset.type === type));
    runSearch();
  }

  // ---------------------------------------------------------------- explore

  async function loadExplore() {
    const body = $('#explore-body');
    const filters = $('#explore-filters');
    body.replaceChildren(spinner('Loading'));

    try {
      if (state.exploreTab === 'channels') {
        filters.replaceChildren();
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
              { class: 'card', onclick: () => openChannel(c.slug) },
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
        if (!body.children.length) body.replaceChildren(empty('No chart data.'));
      } else {
        const data = await api(`/api/explore/releases?genre=${state.exploreGenre}`);
        const grid = el('div', { class: 'grid' });
        renderGrid(grid, data.results, data.note || 'No new releases.');
        body.replaceChildren(
          ...[data.note ? el('p', { class: 'hint' }, data.note) : null, grid].filter(Boolean),
        );
      }
    } catch (e) {
      body.replaceChildren(empty(e.message));
    }
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
            onclick: () => {
              state.exploreGenre = g.id;
              loadExplore();
            },
          },
          g.title,
        ),
      ),
    );
  }

  async function openChannel(slug) {
    const body = $('#explore-body');
    body.replaceChildren(spinner(`Loading ${slug}`));
    try {
      const channel = await api(`/api/explore/channel/${encodeURIComponent(slug)}`);
      body.replaceChildren(
        el('div', { class: 'row toolbar' }, el('button', { class: 'ghost', onclick: loadExplore }, '← Channels')),
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
      if (!channel.sections.length) body.append(empty('This channel returned no modules.'));
    } catch (e) {
      body.replaceChildren(empty(e.message));
    }
  }

  async function openArtist(artistId) {
    setView('search');
    pushPane();
    const back = backButton();
    // Its own sections, each with an inner grid, so the pane itself is a plain
    // block here.
    const results = searchPane('artist-page');
    results.replaceChildren(spinner('Loading artist'));
    try {
      const artist = await api(`/api/artist/${artistId}`);
      const total = artist.groups.reduce((n, g) => n + g.albums.length, 0);

      results.replaceChildren(
        back,
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
      results.replaceChildren(...[back, empty(e.message)].filter(Boolean));
    }
  }

  function sendToMissing(url) {
    const box = $('#missing-sources');
    box.value = box.value ? `${box.value.trim()}\n${url}` : url;
    setView('missing');
    toast('Added to the Scan tab. Nothing has touched a tracker yet.');
  }

  // ---------------------------------------------------------------- detail

  // A page, not a drawer. A release is the thing you are deciding about, and
  // deciding needs the tracklist, the credits and the tracker verdict side by
  // side rather than a 380px column you scroll through a slot at a time.
  async function openAlbum(albumId) {
    setView('search');
    pushPane();
    const back = backButton();
    const pane = searchPane('album-page');
    pane.replaceChildren(spinner('Loading album'));

    try {
      const album = await api(`/api/album/${albumId}`);
      state.album = album;
      const availability = album.availability;
      const verdict = availability
        ? availability.uploadable
          ? el('span', { class: 'tag ok' }, 'All FLAC, all streamable')
          : el('span', { class: 'tag bad' }, availability.reason || 'Not uploadable')
        : el('span', { class: 'tag dim' }, album.availability_error || 'Availability needs an ARL');

      const artistLink = (id, name) =>
        id
          ? el('a', { href: '#', onclick: (e) => { e.preventDefault(); openArtist(id); } }, name)
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
        back,
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
                  {},
                  el('td', { class: 'num-col' }, String(tr.number || '')),
                  el(
                    'td',
                    {},
                    el('span', { class: 'track-title' }, tr.title || ''),
                    tr.explicit ? el('span', { class: 'tag dim explicit-tag' }, 'E') : null,
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
      pane.replaceChildren(...[back, empty(e.message)].filter(Boolean));
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
      target.replaceChildren(workingOn(`Asking ${trackers.join(' and ')}`, job_id));
      followJob(job_id, {
        onUpdate: (job) => {
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
      setView('uploads');
      startUpload(existing.path, trackers);
      return;
    }

    await download(album.id);
    setView('downloads');
    toast(`Downloading first. When it finishes, upload it from the Uploads tab — ${trackers.join(' and ')} are preselected.`);
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
      setView('downloads');
    }

    const label = `${item?.artist || ''} - ${item?.title || ''}`.trim() || String(albumId);
    const job = await waitForDownload(queued.id);
    if (!job || job.status !== 'done' || !job.folder) {
      return toast(`${label}: ${job?.error || 'download did not finish'}`, 'bad');
    }
    setView('uploads');
    await startUpload(job.folder, trackers);
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
        const badge = $('#dl-badge');
        badge.hidden = !active;
        badge.textContent = jobs.filter((j) => ['queued', 'running'].includes(j.status)).length;
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
      list.replaceChildren(empty('Nothing downloaded yet. Queue something from Search or Explore.'));
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
          log.textContent = [jobLine(job), ...events.map((e) => `${e.source}: ${e.error || `${e.albums} albums`}`)].join('\n');
        },
        onDone: (job) => {
          if (job.error) {
            $('#missing-scan').disabled = false;
            return toast(job.error, 'bad');
          }
          log.textContent = `${state.candidates.length} album(s) after the Deezer-side filters.`;
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

  function renderCandidates() {
    const panel = $('#missing-candidates-panel');
    panel.hidden = state.candidates.length === 0;
    if (!state.candidates.length) return;

    state.selectedCandidates = new Set(
      state.selectedCandidates.size ? state.selectedCandidates : state.candidates.map((c) => c.album_id),
    );

    const trackers = [...state.missingTrackers];
    $('#missing-cost').textContent =
      `${state.candidates.length} album(s) to check: about ${state.candidates.length * 3} call(s) ` +
      `per tracker on ${trackers.join(', ') || 'no tracker'}. The scan stops rather than overdraw a budget.`;

    const table = $('#missing-table');
    table.replaceChildren(
      el(
        'thead',
        {},
        el('tr', {}, el('th', {}, ''), el('th', {}, 'Album'), el('th', {}, 'Year'), el('th', {}, 'Tracks'), el('th', {}, 'Source'), el('th', {}, 'Result')),
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
                onchange: (e) => {
                  e.target.checked ? state.selectedCandidates.add(c.album_id) : state.selectedCandidates.delete(c.album_id);
                  renderCandidates();
                },
              }),
            ),
            el(
              'td',
              {},
              el('a', { href: '#', onclick: (e) => { e.preventDefault(); openAlbum(c.album_id); } }, `${c.artist} — ${c.title}`),
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
            log.textContent = jobLine(job);
            job.results.forEach(applyScanResult);
          },
          onDone: (job) => {
            $('#missing-check').disabled = false;
            refreshStatus();
            const stopped = job.events.find((e) => e.event === 'budget_exhausted');
            log.textContent = stopped
              ? `Stopped early to protect the budget: ${stopped.checked} checked, ${stopped.remaining ?? '?'} left. Run again when the window rolls over.`
              : `Done. ${job.result_count} album(s) checked.`;
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

  // The options come from the tracker, not from us. `strictId` adds that
  // group's own "Only specified", which the tracker keeps per group and off by
  // default -- a request that accepts any media names no media at all, so
  // turning it on hides every one of those rather than narrowing the list.
  function filterChoices(id, label, options, strictId) {
    const box = el(
      'div',
      { class: 'checkgroup', id },
      ...options.map((name) =>
        el('label', { class: 'check' }, el('input', { type: 'checkbox', value: name, onchange: requestsCost }), name),
      ),
    );
    return el(
      'div',
      { class: 'setting' },
      el('label', { for: id }, label),
      box,
      strictId
        ? el(
            'label',
            { class: 'check strict-check', title: 'Exclude requests that leave this open to anything' },
            el('input', { type: 'checkbox', id: strictId }),
            'Only these',
          )
        : null,
    );
  }

  const chosen = (id) => [...$$(`#${id} input:checked`)].map((i) => i.value);

  // Rebuilt whenever the tracker changes: RED and OPS do not offer the same
  // filters, and showing one tracker's options while the other is selected
  // would be showing something that cannot be sent.
  async function loadRequestFilters() {
    const host = $('#requests-filters');
    if (!state.requestsTracker) {
      host.replaceChildren(empty('No tracker configured.'));
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

    const fields = [
      el(
        'div',
        { class: 'setting' },
        el('label', { for: 'requests-search' }, 'Search'),
        el('input', { type: 'search', id: 'requests-search', placeholder: 'Artist, album or both' }),
        // RED keeps this beside its own search box, and it only widens what the
        // search string is matched against -- with the box empty it does
        // nothing at all, which is impossible to guess from a lone toggle
        // sitting under "Options".
        spec.descriptions
          ? el(
              'label',
              { class: 'check strict-check', title: 'Only affects what the search text above matches' },
              el('input', { type: 'checkbox', id: 'requests-descriptions' }),
              'Also search descriptions and comments',
            )
          : null,
        el('p', { class: 'hint setting-help' },
           'Matched against artist and title. Blank lists everything open.'),
      ),
      filterField(
        'requests-tags',
        'Tags',
        el('input', { type: 'search', id: 'requests-tags', placeholder: 'hip.hop, jazz' }),
        "Comma separated, in the tracker's own spelling — dots, not spaces.",
      ),
      filterField(
        'requests-tags-mode',
        'Tag match',
        el(
          'select',
          { id: 'requests-tags-mode' },
          el('option', { value: 'any' }, 'Any of these tags'),
          el('option', { value: 'all' }, 'All of these tags'),
        ),
      ),
    ];

    if (spec.formats.length) {
      fields.push(filterChoices('requests-format', 'Format', spec.formats, 'requests-strict-format'));
    }
    if (spec.media.length) {
      fields.push(filterChoices('requests-media', 'Media', spec.media, 'requests-strict-media'));
    }
    if (spec.encodings.length) {
      fields.push(filterChoices('requests-encoding', 'Encoding', spec.encodings, 'requests-strict-encoding'));
    }
    if (spec.release_types.length) {
      fields.push(filterChoices('requests-release-type', 'Release type', spec.release_types, ''));
    }
    if (spec.bounty) {
      fields.push(
        filterField(
          'requests-bounty-min',
          'Bounty (GiB)',
          el(
            'div',
            { class: 'row' },
            el('input', { type: 'text', id: 'requests-bounty-min', placeholder: 'min' }),
            el('input', { type: 'text', id: 'requests-bounty-max', placeholder: 'max' }),
          ),
          'In GiB. Add M or T for MiB or TiB.',
        ),
      );
    }

    fields.push(
      filterField(
        'requests-limit',
        'How many to fetch',
        el(
          'select',
          { id: 'requests-limit', onchange: requestsCost },
          ...[25, 50, 100, 200, 500].map((n) =>
            el(
              'option',
              { value: String(n), selected: n === 100 },
              `${n} — ${Math.ceil(n / (spec.page_size || 25))} page${n > spec.page_size ? 's' : ''}`,
            ),
          ),
        ),
        `${state.requestsTracker} serves ${spec.page_size} per page, and each page is one call.`,
      ),
    );

    const toggles = [
      el('label', { class: 'check' }, el('input', { type: 'checkbox', id: 'requests-show-filled' }), 'Include filled'),
    ];
    if (spec.include_old) {
      toggles.push(
        el('label', { class: 'check' }, el('input', { type: 'checkbox', id: 'requests-include-old' }), 'Include old'),
      );
    }

    host.replaceChildren(...fields, el('div', { class: 'setting filter-toggles' }, el('label', {}, 'Options'),
      el('div', { class: 'row' }, ...toggles)));
    if (!spec.mapped && spec.note) {
      host.append(el('p', { class: 'hint setting-help filter-note' }, spec.note));
    }
    for (const id of ['requests-search', 'requests-tags']) {
      $(`#${id}`).addEventListener('keydown', (e) => e.key === 'Enter' && requestsFetch());
    }
    state.requestFiltersFor = state.requestsTracker;
    requestsCost();
  }

  // What a fetch will cost, before you spend it.
  function requestsCost() {
    const limitEl = $('#requests-limit');
    if (!limitEl) return;
    const size = state.requestFilters?.page_size || 25;
    const pages = Math.ceil((Number(limitEl.value) || size) / size);
    const budget = state.trackers.find((t) => t.code === state.requestsTracker);
    const note = `Costs up to ${pages} call${pages === 1 ? '' : 's'}`;
    $('#requests-cost').textContent = budget ? `${note} of ${budget.remaining} left on ${budget.code}.` : `${note}.`;
  }

  const ticked = (id) => !!$(`#${id}`)?.checked;

  async function requestsFetch() {
    if (!state.requestsTracker) return toast('No tracker configured', 'bad');
    const limit = Number($('#requests-limit')?.value) || 25;
    const container = $('#requests-results');
    container.replaceChildren(spinner(`Fetching up to ${limit} open requests`));
    try {
      const params = new URLSearchParams({
        tracker: state.requestsTracker,
        search: $('#requests-search').value,
        tags: $('#requests-tags').value,
        tags_all: $('#requests-tags-mode').value === 'all' ? '1' : '0',
        show_filled: ticked('requests-show-filled') ? '1' : '0',
        strict_format: ticked('requests-strict-format') ? '1' : '0',
        strict_media: ticked('requests-strict-media') ? '1' : '0',
        strict_encoding: ticked('requests-strict-encoding') ? '1' : '0',
        include_old: ticked('requests-include-old') ? '1' : '0',
        descriptions: ticked('requests-descriptions') ? '1' : '0',
        bounty_min: $('#requests-bounty-min')?.value || '',
        bounty_max: $('#requests-bounty-max')?.value || '',
        limit: String(limit),
      });
      // Repeated keys, one per ticked box.
      for (const [key, id] of [
        ['format', 'requests-format'],
        ['media', 'requests-media'],
        ['encoding', 'requests-encoding'],
        ['release_type', 'requests-release-type'],
      ]) {
        for (const value of chosen(id)) params.append(key, value);
      }
      const { requests, calls, complete } = await api(`/api/requests/list?${params}`);
      state.requestRows = requests;
      state.selectedRequests = new Set(requests.map((r) => r.id));
      renderRequestRows();
      // Say what was actually spent and whether the tracker ran dry, so a short
      // list is not mistaken for a failed fetch.
      const spent = `${requests.length} request(s) from ${calls} call${calls === 1 ? '' : 's'}`;
      toast(complete ? spent : `${spent} — that is everything matching`, 'ok');
      refreshStatus();
    } catch (e) {
      container.replaceChildren(empty(e.message));
    }
  }

  function renderRequestRows() {
    const container = $('#requests-results');
    if (!state.requestRows.length) {
      container.replaceChildren(empty('No open requests found.'));
      return;
    }
    $('#requests-selected-count').textContent = `${state.selectedRequests.size} selected`;
    container.replaceChildren(
      el(
        'table',
        { class: 'table' },
        el('thead', {}, el('tr', {}, el('th', {}, ''), el('th', {}, 'Request'), el('th', {}, 'Year'), el('th', {}, 'Bounty'), el('th', {}, 'Result'))),
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
                  onchange: (e) => {
                    e.target.checked ? state.selectedRequests.add(r.id) : state.selectedRequests.delete(r.id);
                    $('#requests-selected-count').textContent = `${state.selectedRequests.size} selected`;
                  },
                }),
              ),
              el('td', {}, el('a', { href: r.url, target: '_blank', rel: 'noopener' }, `${r.artist} — ${r.title}`)),
              el('td', {}, r.year || ''),
              el('td', {}, r.bounty || ''),
              el('td', { class: 'result' }, el('span', { class: 'tag dim' }, 'not checked')),
            ),
          ),
        ),
      ),
    );
  }

  async function requestsCheck() {
    const pasted = $('#requests-ids').value
      .split('\n')
      .map((line) => (line.match(/(\d+)/) || [])[1])
      .filter(Boolean);
    const ids = pasted.length ? pasted : [...state.selectedRequests];
    if (!ids.length) return toast('Select or paste at least one request', 'bad');

    const log = $('#requests-log');
    log.hidden = false;
    log.textContent = 'Starting…';
    $('#requests-check').disabled = true;

    if (pasted.length) {
      state.requestRows = pasted.map((id) => ({ id, artist: '', title: `Request ${id}`, year: '', bounty: '', url: '#' }));
      state.selectedRequests = new Set(pasted);
      renderRequestRows();
    }

    try {
      const { job_id } = await api('/api/requests/check', {
        method: 'POST',
        body: { tracker: state.requestsTracker, request_ids: ids },
      });
      log.after(jobCancel(job_id, 'Stop checking'));
      followJob(job_id, {
        onUpdate: (job) => {
          log.textContent = jobLine(job);
          job.results.forEach(applyRequestResult);
        },
        onDone: (job) => {
          $('#requests-check').disabled = false;
          refreshStatus();
          const stopped = job.events.find((e) => e.event === 'budget_exhausted');
          log.textContent = stopped
            ? `Stopped early to protect the budget after ${stopped.checked} request(s).`
            : `Done. ${job.result_count} request(s) checked.`;
        },
      });
    } catch (e) {
      $('#requests-check').disabled = false;
      toast(e.message, 'bad');
    }
  }

  function applyRequestResult(match) {
    const cell = $(`#requests-results tr[data-request="${match.request_id}"] .result`);
    if (!cell) return;
    if (!match.fillable) {
      cell.replaceChildren(el('span', { class: 'tag dim', title: match.reason || '' }, match.reason || match.status));
      return;
    }
    cell.replaceChildren(
      el('span', { class: 'tag ok' }, `${(match.confidence * 100).toFixed(0)}% match`),
      ' ',
      el('a', { href: match.deezer_url, target: '_blank', rel: 'noopener' }, match.deezer_title || 'Deezer'),
      ' ',
      el('button', { class: 'ghost', onclick: () => download(match.deezer_id) }, 'Download'),
    );
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
        list.replaceChildren(empty('No release folders yet.'));
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

  async function startUpload(folder, trackers) {
    const targets = trackers && trackers.length ? trackers : [...state.uploadTrackers];
    if (!targets.length) return toast('Pick at least one tracker', 'bad');

    try {
      const { flow_id, dry_run } = await api('/api/upload', {
        method: 'POST',
        body: { folder, trackers: targets },
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

  // One row per track: the full spectral wide enough to read, its zoom beside
  // it, and the track number. The old grid of 180px thumbnails in a 420px box
  // showed twenty-four unlabelled squares you could not judge anything from,
  // which defeated the point of looking at them.
  function spectralList(images) {
    return el(
      'div',
      { class: 'spectrals' },
      ...images.map((img) =>
        el(
          'div',
          { class: 'spectral-row' },
          el('div', { class: 'spectral-num' }, img.track || ''),
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
      { class: 'diff', open: table.rows.length <= 12 },
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
      { class: 'diff meta-block', open: true },
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
          matches.push(
            el(
              'div',
              { class: 'match' },
              el('div', { class: 'match-body' },
                el('div', { class: 'match-title' }, o.label),
                el('div', { class: 'match-sub' }, o.detail || '')),
              el(
                'div',
                { class: 'match-actions' },
                o.url
                  ? el('a', { class: 'filebtn', href: o.url, target: '_blank', rel: 'noopener noreferrer' },
                      'Open on tracker ↗')
                  : null,
                el('button', { class: 'primary', onclick: () => send(o.value) }, 'Use this group'),
              ),
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
      ].filter((n) => n !== null && n !== undefined),
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
        el('div', { class: 'flow-notes' }, el('ul', { class: 'notelist' })),
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
      const atBottom = notes.scrollHeight - notes.scrollTop - notes.clientHeight < 24;
      notes.dataset.count = String(events.length);
      list.replaceChildren(...events.map((n) => el('li', { class: n.level }, n.message)));
      if (atBottom) notes.scrollTop = notes.scrollHeight;
    }
    notes.hidden = !events.length;
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
        ...field.choices.map((c) => el('option', { value: c, selected: c === value }, c)),
      );
    } else if (isSecret) {
      input = el('input', {
        type: 'password',
        autocomplete: 'new-password',
        placeholder: configured ? '•••••••• (saved — type to replace)' : 'Not set',
        oninput: onInput,
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

    return el(
      'div',
      { class: 'setting' },
      el('label', {}, field.label, configured ? el('span', { class: 'tag ok saved-tag' }, 'saved') : null),
      input,
      field.help ? el('p', { class: 'hint setting-help' }, field.help) : null,
    );
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
        'Changes apply immediately, no restart. Save before running a test — tests read what is stored, not what is typed.',
      ),
      ...sections.map((section) =>
        el(
          'section',
          { class: 'panel settings-section' },
          el(
            'div',
            { class: 'row' },
            el('h2', {}, section.title),
            section.test
              ? el(
                  'button',
                  { class: 'test-btn', onclick: (e) => runTest(section.test, e.target) },
                  'Test connection',
                )
              : null,
          ),
          section.blurb ? el('p', { class: 'hint' }, section.blurb) : null,
          el('div', { class: 'test-result', id: `test-${section.test || section.id}`, hidden: true }),
          el('div', { class: 'settings-grid' }, ...section.fields.map((f) => settingField(f, values, secretsSet))),
        ),
      ),
      el(
        'section',
        { class: 'panel' },
        el('h2', {}, 'Set in config.toml'),
        el(
          'p',
          { class: 'hint' },
          `These are read before this page exists, so they cannot be edited here. From ${configPath || 'your config file'}.`,
        ),
        el('ul', { class: 'bootstrap-list' }, ...bootstrap.map((k) => el('li', {}, el('code', {}, k)))),
      ),
      el('section', { class: 'panel', id: 'debug-panel' }, el('h2', {}, 'Debug log'), spinner('Loading')),
      el(
        'section',
        { class: 'panel' },
        el('h2', {}, 'Appearance'),
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
        el('h2', {}, 'Scan history'),
        el(
          'div',
          { class: 'row' },
          el('button', { onclick: () => clearHistory('albums') }, 'Clear album history'),
          el('button', { onclick: () => clearHistory('requests') }, 'Clear request history'),
          el('button', { onclick: signOut }, 'Sign out of this browser'),
        ),
        el('p', { class: 'hint' }, 'Clearing history makes the next scan re-check everything, costing tracker budget again.'),
      ),
    );
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
          el('h2', {}, 'Debug log'),
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
    } catch (e) {
      panel.replaceChildren(el('h2', {}, 'Debug log'), empty(e.message));
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

  async function runTest(target, button) {
    const box = $(`#test-${target}`);
    if (!box) return;
    button.disabled = true;
    button.textContent = 'Testing…';
    box.hidden = false;
    box.className = 'test-result';
    box.replaceChildren(el('span', { class: 'spinner' }), ' Contacting…');

    try {
      // Send whatever is typed but not yet saved, so Test works before Save.
      const result = await api(`/api/settings/test/${target}`, {
        method: 'POST',
        body: { values: state.pending },
      });
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
      button.textContent = 'Test connection';
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

    $$('.nav-item').forEach((b) => b.addEventListener('click', () => setView(b.dataset.view)));
    $('#search-form').addEventListener('submit', runSearch);
    $$('#search-type button').forEach((b) =>
      b.addEventListener('click', () => selectSearchType(b.dataset.type)),
    );
    $$('#explore-tabs button').forEach((b) =>
      b.addEventListener('click', () => {
        state.exploreTab = b.dataset.explore;
        $$('#explore-tabs button').forEach((x) => x.classList.toggle('active', x === b));
        loadExplore();
      }),
    );
    $('#missing-scan').addEventListener('click', missingScan);
    // Re-checks whatever is ticked in the results, which is the only place a
    // subset makes sense.
    $('#missing-check').addEventListener('click', () => missingCheck());
    $('#missing-select-all').addEventListener('change', (e) => {
      state.selectedCandidates = e.target.checked ? new Set(state.candidates.map((c) => c.album_id)) : new Set();
      renderCandidates();
    });
    $('#requests-fetch').addEventListener('click', requestsFetch);
    $('#requests-check').addEventListener('click', requestsCheck);
    $('#requests-file').addEventListener('change', async (e) => {
      const file = e.target.files?.[0];
      if (!file) return;
      const text = await file.text();
      const ids = text
        .split(/\r?\n/)
        .map((line) => (line.match(/(\d+)/) || [])[1])
        .filter(Boolean);
      $('#requests-ids').value = ids.join('\n');
      e.target.value = '';
      toast(`${ids.length} request ID(s) loaded from ${file.name}`, ids.length ? 'ok' : 'bad');
    });
    $('#watchlist-form').addEventListener('submit', saveWatchlist);
    $('#downloads-clear').addEventListener('click', async () => {
      await api('/api/downloads/clear', { method: 'POST' });
      pollDownloads(true);
    });
    $('#folders-refresh').addEventListener('click', loadFolders);
    $('#upload-dry-run').addEventListener('change', (e) =>
      setUploadFlag('upload.dry_run', e.target, 'Dry run'));
    $('#upload-yes-all').addEventListener('change', (e) =>
      setUploadFlag('upload.yes_all', e.target, 'Auto-answer prompts'));
    refreshStatus();
    setInterval(refreshStatus, 15000);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
