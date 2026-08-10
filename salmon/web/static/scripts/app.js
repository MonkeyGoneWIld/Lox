/* deezer-upload web UI.
 *
 * No build step and no framework on purpose: this ships inside a Python package
 * and has to stay editable without a Node toolchain.
 *
 * The one rule worth remembering while reading: nothing here calls a tracker
 * except missingCheck(), requestsFetch() and requestsCheck(). Everything else
 * is Deezer-only and free.
 */

(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const state = {
    view: 'search',
    searchType: 'album',
    exploreTab: 'channels',
    exploreGenre: '0',
    candidates: [],
    selectedCandidates: new Set(),
    missingTrackers: new Set(),
    requestsTracker: null,
    uploadTrackers: new Set(),
    albumCheck: null,
    watchlists: [],
    linking: false,
    requestRows: [],
    selectedRequests: new Set(),
    trackers: [],
    uploadJob: null,
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
    if (view === 'downloads') pollDownloads(true);
    if (view === 'uploads') loadFolders();
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
  }

  // ---------------------------------------------------------------- cards

  function card(item) {
    const isAlbum = item.type === 'album' || item.album_id;
    const albumId = item.type === 'album' ? item.id : item.album_id;
    return el(
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
            )
          : null,
      ),
      el('div', { class: 'card-title', title: item.title }, item.title || ''),
      el('div', { class: 'card-sub', title: item.artist }, item.artist || (item.albums ? `${item.albums} albums` : '')),
    );
  }

  function renderGrid(container, items, emptyMessage) {
    container.replaceChildren(...(items.length ? items.map(card) : [empty(emptyMessage)]));
  }

  // ---------------------------------------------------------------- search

  async function runSearch(event) {
    event?.preventDefault();
    const query = $('#search-input').value.trim();
    if (!query) return;
    const results = $('#search-results');
    results.replaceChildren(spinner('Searching Deezer'));
    try {
      const data = await api(`/api/search?q=${encodeURIComponent(query)}&type=${state.searchType}`);
      renderGrid(results, data.results, 'Nothing found.');
    } catch (e) {
      results.replaceChildren(empty(e.message));
    }
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
        const { results } = await api(`/api/explore/releases?genre=${state.exploreGenre}`);
        const grid = el('div', { class: 'grid' });
        renderGrid(grid, results, 'No new releases.');
        body.replaceChildren(grid);
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
                    title: 'Send this module to the Missing tab',
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
    const results = $('#search-results');
    setView('search');
    results.replaceChildren(spinner('Loading discography'));
    try {
      const { results: albums } = await api(`/api/artist/${artistId}/albums`);
      renderGrid(results, albums, 'No albums.');
    } catch (e) {
      results.replaceChildren(empty(e.message));
    }
  }

  function sendToMissing(url) {
    const box = $('#missing-sources');
    box.value = box.value ? `${box.value.trim()}\n${url}` : url;
    setView('missing');
    toast('Added to the Missing tab. Nothing has touched a tracker yet.');
  }

  // ---------------------------------------------------------------- detail

  async function openAlbum(albumId) {
    const panel = $('#detail');
    const body = $('#detail-body');
    panel.hidden = false;
    body.replaceChildren(spinner('Loading album'));

    try {
      const album = await api(`/api/album/${albumId}`);
      const availability = album.availability;
      const verdict = availability
        ? availability.uploadable
          ? el('span', { class: 'tag ok' }, 'All FLAC, all streamable')
          : el('span', { class: 'tag bad' }, availability.reason || 'Not uploadable')
        : el('span', { class: 'tag dim' }, album.availability_error || 'Availability needs an ARL');

      body.replaceChildren(
        album.cover ? el('img', { class: 'detail-art', src: album.cover, alt: '' }) : el('div', { class: 'detail-art' }),
        el('div', { class: 'detail-title' }, album.title || ''),
        el('div', { class: 'detail-artist' }, album.artist || ''),
        el(
          'div',
          { class: 'row' },
          el('button', { class: 'primary', onclick: () => download(album.id) }, 'Download'),
          album.url ? el('a', { class: 'linkbtn', href: album.url, target: '_blank', rel: 'noopener' }, 'Open on Deezer') : null,
        ),
        el('p', {}, verdict),
        el(
          'div',
          { class: 'checkbox-panel', id: 'album-check' },
          el(
            'div',
            { class: 'row' },
            el('strong', {}, 'Trackers'),
            ...state.trackers.map((t) =>
              el('button', { onclick: (e) => checkAlbum(album, [t.code], e.target) }, `Check ${t.code}`),
            ),
            state.trackers.length > 1
              ? el(
                  'button',
                  { class: 'primary', onclick: (e) => checkAlbum(album, state.trackers.map((t) => t.code), e.target) },
                  'Check all',
                )
              : null,
          ),
          el('div', { id: 'album-check-body' }, el('p', { class: 'hint' }, 'Nothing has been asked of a tracker yet.')),
        ),
        el(
          'dl',
          { class: 'meta' },
          el('dt', {}, 'Released'), el('dd', {}, album.release_date || '?'),
          el('dt', {}, 'Type'), el('dd', {}, album.record_type || '?'),
          el('dt', {}, 'Tracks'), el('dd', {}, String(album.nb_tracks ?? '?')),
          el('dt', {}, 'Label'), el('dd', {}, album.label || '?'),
          el('dt', {}, 'UPC'), el('dd', {}, album.upc || '?'),
          el('dt', {}, 'Genres'), el('dd', {}, (album.genres || []).join(', ') || '?'),
          availability ? el('dt', {}, 'FLAC') : null,
          availability ? el('dd', {}, `${availability.flac_count}/${availability.total}`) : null,
        ),
        el(
          'ul',
          { class: 'tracklist' },
          ...(album.tracks || []).map((t) =>
            el(
              'li',
              {},
              el('span', { class: 'num' }, String(t.number || '')),
              el('span', {}, t.title || ''),
              el('span', { class: 'dur' }, duration(t.duration)),
            ),
          ),
        ),
      );
    } catch (e) {
      body.replaceChildren(empty(e.message));
    }
  }

  // ------------------------------------------------------ per-album check

  async function checkAlbum(album, trackers, button) {
    const target = $('#album-check-body');
    if (!target) return;
    if (button) button.disabled = true;
    target.replaceChildren(spinner(`Asking ${trackers.join(' and ')}`));

    try {
      const { job_id } = await api(`/api/album/${album.id}/check`, { method: 'POST', body: { trackers } });
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

  function renderAlbumCheck(album, check) {
    const target = $('#album-check-body');
    if (!target || !check) return;

    const blocks = check.verdicts.map((v) => {
      const head = el(
        'div',
        { class: 'row verdict-head' },
        el('strong', {}, v.tracker),
        el(
          'span',
          { class: `tag ${v.status === 'missing' ? 'ok' : v.status === 'found' ? 'dim' : 'warn'}` },
          v.status === 'missing' ? 'not on tracker' : v.status,
        ),
        el('span', { class: 'card-sub' }, `${v.calls_used} call(s), ${v.queries.length} search(es)`),
        v.artist_url ? el('a', { href: v.artist_url, target: '_blank', rel: 'noopener' }, 'artist page') : null,
      );

      const inspected = v.inspected.length
        ? el(
            'ul',
            { class: 'hitlist' },
            ...v.inspected.map((h) =>
              el(
                'li',
                { class: h.matched ? 'hit matched' : 'hit' },
                el('a', { href: h.url, target: '_blank', rel: 'noopener' }, `${h.artist} — ${h.name}${h.year ? ` (${h.year})` : ''}`),
                el('span', { class: 'card-sub' }, h.matched ? 'matched' : h.reason),
                h.formats.length ? el('span', { class: 'card-sub' }, h.formats.join(' · ')) : null,
              ),
            ),
          )
        : el('p', { class: 'hint' }, v.error || 'No groups came back from the search.');

      return el('div', { class: 'verdict' }, head, inspected);
    });

    const uploadable = check.uploadable_to || [];
    const actions = el(
      'div',
      { class: 'row' },
      uploadable.length
        ? el(
            'button',
            { class: 'primary', onclick: () => uploadAlbum(album, uploadable) },
            `Download & upload to ${uploadable.join(' + ')}`,
          )
        : el('span', { class: 'hint' }, 'Nothing to upload — every checked tracker already has it, or the check did not finish.'),
    );

    target.replaceChildren(
      el(
        'p',
        { class: 'hint' },
        'Open the links and confirm the checker got it right before uploading. Rejected groups are listed with the reason.',
      ),
      ...blocks,
      actions,
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

    $('#detail').hidden = true;
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
      if (result.failed?.length) toast(result.failed[0].error, 'bad');
      else {
        toast('Queued for download', 'ok');
        pollDownloads(true);
      }
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
            {},
            job.status === 'queued'
              ? el('button', { class: 'ghost', onclick: () => cancelDownload(job.id) }, 'Cancel')
              : el('span', { class: `tag ${cls === 'done' ? 'ok' : cls === 'failed' ? 'bad' : 'dim'}` }, `${job.percent}%`),
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
      else onDone?.(job);
    };
    tick();
  }

  function jobLine(job) {
    const p = job.progress || {};
    if (p.total) return `${p.phase || 'working'} ${p.current}/${p.total}${p.album ? ` · ${p.album}` : ''}`;
    return job.status;
  }

  // ---------------------------------------------------------------- missing

  async function missingCollect() {
    const sources = $('#missing-sources').value.split('\n').map((s) => s.trim()).filter(Boolean);
    if (!sources.length) return toast('Add at least one URL', 'bad');

    const log = $('#missing-collect-log');
    log.hidden = false;
    log.textContent = 'Collecting…';
    $('#missing-collect').disabled = true;
    state.candidates = [];

    try {
      const { job_id } = await api('/api/missing/collect', {
        method: 'POST',
        body: { sources, skip_known: $('#missing-skip-known').checked },
      });
      followJob(job_id, {
        onUpdate: (job) => {
          state.candidates.push(...job.results);
          const events = job.events.filter((e) => e.event.startsWith('source')).slice(-4);
          log.textContent = [jobLine(job), ...events.map((e) => `${e.source}: ${e.error || `${e.albums} albums`}`)].join('\n');
        },
        onDone: (job) => {
          $('#missing-collect').disabled = false;
          if (job.error) return toast(job.error, 'bad');
          log.textContent = `Collected ${state.candidates.length} album(s) that pass every Deezer-side filter. No tracker was contacted.`;
          renderCandidates();
        },
      });
    } catch (e) {
      $('#missing-collect').disabled = false;
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
      `${state.selectedCandidates.size} album(s) selected. Estimated cost: about ` +
      `${state.selectedCandidates.size * 3} call(s) per tracker on ${trackers.join(', ') || 'no tracker'}. ` +
      `The scan stops rather than overdraw a budget.`;

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

  async function missingCheck() {
    const trackers = [...state.missingTrackers];
    if (!trackers.length) return toast('Pick at least one tracker', 'bad');
    const candidates = state.candidates.filter((c) => state.selectedCandidates.has(c.album_id));
    if (!candidates.length) return toast('Select at least one album', 'bad');

    const log = $('#missing-check-log');
    log.hidden = false;
    log.textContent = 'Starting…';
    $('#missing-check').disabled = true;

    try {
      const { job_id } = await api('/api/missing/check', { method: 'POST', body: { candidates, trackers } });
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
        },
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

  async function requestsFetch() {
    if (!state.requestsTracker) return toast('No tracker configured', 'bad');
    const container = $('#requests-results');
    container.replaceChildren(spinner('Fetching open requests'));
    try {
      const params = new URLSearchParams({ tracker: state.requestsTracker, search: $('#requests-search').value });
      const { requests } = await api(`/api/requests/list?${params}`);
      state.requestRows = requests;
      state.selectedRequests = new Set(requests.map((r) => r.id));
      renderRequestRows();
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
      const { folders, directory, linking } = await api('/api/folders');
      $('#uploads-dir').textContent = directory;
      state.linking = linking;
      $('#linking-note').textContent = linking
        ? 'Linking is on: each tracker gets its own hardlinked folder under the seeding directory, so the bytes exist once.'
        : 'Linking is off — every tracker will seed from the same folder. Set [linking] in your config for cross-seed style layout.';
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

    const dryRun = $('#upload-dry-run').checked;
    $('#upload-console-panel').hidden = false;
    $('#upload-console').textContent = '';
    try {
      const { job_id, linking } = await api('/api/upload', {
        method: 'POST',
        body: { folder, trackers: targets, dry_run: dryRun },
      });
      if (dryRun) toast('Dry run: nothing will reach the tracker or the download client.');
      else if (linking && targets.length > 1) toast(`Hardlinking one folder per tracker: ${targets.join(', ')}`);
      state.uploadJob = job_id;
      followJob(job_id, {
        interval: 700,
        onUpdate: (job) => {
          const console_ = $('#upload-console');
          console_.textContent = job.log.join('\n');
          console_.scrollTop = console_.scrollHeight;
          $('#upload-input').disabled = !job.accepts_input;
        },
        onDone: (job) => {
          state.uploadJob = null;
          $('#upload-input').disabled = true;
          toast(job.status === 'done' ? 'Upload finished' : job.error || 'Upload failed', job.status === 'done' ? 'ok' : 'bad');
        },
      });
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  async function sendUploadInput(event) {
    event.preventDefault();
    if (!state.uploadJob) return;
    const input = $('#upload-input');
    try {
      await api(`/api/jobs/${state.uploadJob}/input`, { method: 'POST', body: { line: input.value } });
      input.value = '';
    } catch (e) {
      toast(e.message, 'bad');
    }
  }

  // ---------------------------------------------------------------- settings

  async function loadSettings() {
    const body = $('#settings-body');
    body.replaceChildren(spinner('Loading'));
    try {
      const config = await api('/api/config');
      body.replaceChildren(
        el('h2', {}, 'Configuration'),
        el('p', { class: 'hint' }, 'Read-only. Everything here comes from your config.toml, which is gitignored and never leaves this machine.'),
        el(
          'dl',
          { class: 'meta' },
          el('dt', {}, 'Download directory'), el('dd', {}, config.download_directory),
          el('dt', {}, 'Preferred format'), el('dd', {}, config.preferred_format),
          el('dt', {}, 'Trackers'), el('dd', {}, config.trackers.join(', ') || 'none configured'),
          el('dt', {}, 'Deezer ARL'), el('dd', {}, config.arl_set ? 'set' : 'not set'),
          el('dt', {}, 'Discogs token'), el('dd', {}, config.discogs_set ? 'set' : 'not set'),
          el('dt', {}, 'Tracker budget'), el('dd', {}, `${config.checker.tracker_budget} calls per ${config.checker.tracker_budget_window}s`),
          el('dt', {}, 'Minimum tracks'), el('dd', {}, String(config.checker.min_tracks || 'no minimum')),
          el('dt', {}, 'Date range'), el('dd', {}, `${config.checker.min_date || 'any'} to ${config.checker.max_date || 'any'}`),
          el('dt', {}, 'Match confidence'), el('dd', {}, String(config.checker.min_confidence)),
        ),
        el('h2', {}, 'Scan history'),
        el(
          'div',
          { class: 'row' },
          el('button', { onclick: () => clearHistory('albums') }, 'Clear album history'),
          el('button', { onclick: () => clearHistory('requests') }, 'Clear request history'),
        ),
        el('p', { class: 'hint' }, 'Clearing history makes the next scan re-check everything, which costs tracker budget again.'),
        el('h2', {}, 'Session'),
        el(
          'div',
          { class: 'row' },
          el('button', { onclick: signOut }, 'Sign out of this browser'),
        ),
        el('p', { class: 'hint' }, 'Clears the session cookie. You will be asked for the access token again.'),
      );
    } catch (e) {
      body.replaceChildren(empty(e.message));
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

  function init() {
    $$('.nav-item').forEach((b) => b.addEventListener('click', () => setView(b.dataset.view)));
    $('#search-form').addEventListener('submit', runSearch);
    $$('#search-type button').forEach((b) =>
      b.addEventListener('click', () => {
        state.searchType = b.dataset.type;
        $$('#search-type button').forEach((x) => x.classList.toggle('active', x === b));
        runSearch();
      }),
    );
    $$('#explore-tabs button').forEach((b) =>
      b.addEventListener('click', () => {
        state.exploreTab = b.dataset.explore;
        $$('#explore-tabs button').forEach((x) => x.classList.toggle('active', x === b));
        loadExplore();
      }),
    );
    $('#missing-collect').addEventListener('click', missingCollect);
    $('#missing-check').addEventListener('click', missingCheck);
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
    $('#upload-input-form').addEventListener('submit', sendUploadInput);
    $('#detail-close').addEventListener('click', () => ($('#detail').hidden = true));
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') $('#detail').hidden = true;
    });

    refreshStatus();
    setInterval(refreshStatus, 15000);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
