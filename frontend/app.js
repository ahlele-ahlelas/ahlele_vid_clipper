'use strict';

// ── State ──────────────────────────────────────────────────────────────────────
const state = {
  clipDuration: 30,               // number or 'custom'
  polls: new Map(),               // jobId -> intervalId
  queueRows: new Map(),           // jobId -> <tr>
  jobCards: new Map(),            // jobId -> <div.video-card>
  cookieSessionId: null,          // uploaded cookies.txt session id
};

// ── Init ───────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  checkFFmpeg();
  initPills();

  ['siteInput', 'searchQuery', 'context'].forEach(id => {
    document.getElementById(id).addEventListener('keydown', e => {
      if (e.key === 'Enter') findAndClip();
    });
  });
});

async function checkFFmpeg() {
  try {
    const res = await fetch('/api/ffmpeg-status');
    const d = await res.json();
    if (!d.available) {
      document.getElementById('ffmpegBanner').classList.remove('hidden');
    }
  } catch (_) {}
}

function initPills() {
  document.querySelectorAll('.pill').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.pill').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      state.clipDuration = btn.dataset.value === 'custom' ? 'custom' : parseInt(btn.dataset.value, 10);
      document.getElementById('customTime').classList.toggle('hidden', btn.dataset.value !== 'custom');
    });
  });
}

// ── Helpers ────────────────────────────────────────────────────────────────────
function isUrl(s) {
  return s.startsWith('http://') || s.startsWith('https://');
}

function _isFacebookPageUrl(url) {
  // Returns true if it's a page/profile URL, not a direct video URL
  try {
    const u = new URL(url);
    const p = u.pathname;
    // Direct video patterns → let yt-dlp handle
    if (u.searchParams.get('v')) return false;
    if (/^\/(watch|reel)\/?/.test(p)) return false;
    if (/\/videos\/\d+/.test(p)) return false;
    // Everything else is a page/profile
    return true;
  } catch (_) { return false; }
}

function hmsToSeconds(hms) {
  const parts = hms.trim().split(':').map(Number);
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0] || 0;
}

function fmtDuration(s) {
  if (!s) return '—';
  const total = Math.round(s);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  if (h) return `${h}:${pad(m)}:${pad(sec)}`;
  return `${m}:${pad(sec)}`;
}

function pad(n) { return String(n).padStart(2, '0'); }

function escHtml(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function setStatus(msg) {
  document.getElementById('statusBar').textContent = msg;
}

function setLoading(on) {
  const btn = document.getElementById('submitBtn');
  btn.disabled = on;
  document.querySelector('.btn-text').classList.toggle('hidden', on);
  document.getElementById('btnSpinner').classList.toggle('hidden', !on);
}

function getClipConfig() {
  const quality = document.getElementById('quality').value;
  if (state.clipDuration === 'custom') {
    const start = hmsToSeconds(document.getElementById('startTime').value || '0:0:0');
    const end   = hmsToSeconds(document.getElementById('endTime').value   || '0:0:30');
    return { quality, clip_duration: null, start_time: start, end_time: end };
  }
  return { quality, clip_duration: state.clipDuration, start_time: null, end_time: null };
}

// ── Site detection ─────────────────────────────────────────────────────────────
const _SITE_MAP = [
  { type: 'youtube',     labels: ['youtube', 'yt'],                       icon: '▶ YouTube',      urlOnly: false },
  { type: 'reddit',      labels: ['reddit', 'redd.it'],                   icon: '👽 Reddit',       urlOnly: false },
  { type: 'twitter',     labels: ['twitter', 'x.com', ' x '],            icon: '✦ X/Twitter',    urlOnly: false },
  { type: 'vimeo',       labels: ['vimeo'],                               icon: '🎞 Vimeo',        urlOnly: false },
  { type: 'facebook',    labels: ['facebook', 'fb.com', ' fb '],         icon: '📘 Facebook',     urlOnly: true  },
  { type: 'instagram',   labels: ['instagram', 'insta', ' ig '],         icon: '📷 Instagram',    urlOnly: true  },
  { type: 'tiktok',      labels: ['tiktok', 'tik tok'],                  icon: '🎵 TikTok',       urlOnly: true  },
  { type: 'twitch',      labels: ['twitch'],                              icon: '🟣 Twitch',       urlOnly: true  },
  { type: 'dailymotion', labels: ['dailymotion'],                         icon: '🎬 Dailymotion',  urlOnly: true  },
  { type: 'bilibili',    labels: ['bilibili', 'bili'],                    icon: '📺 Bilibili',     urlOnly: true  },
];

function detectSourceType(input) {
  if (!input) return 'auto';
  if (isUrl(input)) return 'url';
  const t = ' ' + input.toLowerCase().trim() + ' ';
  for (const { type, labels } of _SITE_MAP) {
    if (labels.some(l => t.includes(l))) return type;
  }
  // Unknown site name → browser crawler searches the site itself
  return 'site';
}

function _siteEntry(type) {
  return _SITE_MAP.find(s => s.type === type) || null;
}

function onSiteInputChange() {
  const val = document.getElementById('siteInput').value.trim();
  const badge = document.getElementById('siteDetectedBadge');
  const queryGroup = document.getElementById('queryGroup');
  const hint = document.getElementById('urlOnlyHint');
  const detected = detectSourceType(val);

  if (!val) {
    badge.classList.add('hidden');
    queryGroup.classList.remove('hidden');
    if (hint) hint.classList.add('hidden');
    return;
  }

  const entry = _siteEntry(detected);
  const isUrlOnly = entry && entry.urlOnly;
  const queryInput = document.getElementById('searchQuery');

  if (detected === 'url') {
    // Keep query visible: URL + query = search that site via crawler
    queryGroup.classList.remove('hidden');
    queryInput.placeholder = 'Optional — search this site for a topic (leave empty to clip the URL directly)';
    badge.textContent = '🔗 Direct URL';
    badge.className = 'site-badge badge-url';
    if (hint) hint.classList.add('hidden');
  } else if (isUrlOnly) {
    // Crawler can search these sites too — keep query visible
    queryGroup.classList.remove('hidden');
    queryInput.placeholder = 'What to search for on this site…';
    badge.textContent = entry.icon;
    badge.className = 'site-badge badge-site';
    if (hint) {
      const crawlable = entry.type === 'facebook';
      hint.textContent = crawlable
        ? `${entry.icon} — paste a page/profile URL to browse its videos, or paste a direct video URL to clip immediately.`
        : `${entry.icon} — paste a direct video URL, or enter a query to search the site via the browser crawler.`;
      hint.classList.remove('hidden');
    }
  } else {
    queryGroup.classList.remove('hidden');
    queryInput.placeholder = 'What to search for on this site…';
    if (hint) hint.classList.add('hidden');
    if (entry) {
      badge.textContent = entry.icon;
      badge.className = 'site-badge badge-site';
    } else {
      badge.textContent = '🌐 Site search (crawler)';
      badge.className = 'site-badge badge-auto';
    }
  }
}

// ── Main action ────────────────────────────────────────────────────────────────
async function findAndClip() {
  const siteVal   = document.getElementById('siteInput').value.trim();
  const queryVal  = document.getElementById('searchQuery').value.trim();
  const context   = document.getElementById('context').value.trim();

  if (!siteVal) { setStatus('Please enter a website name or URL.'); return; }

  setLoading(true);
  const detected = detectSourceType(siteVal);
  const entry = _siteEntry(detected);

  // URL-only sites: direct URL preferred, but a query routes to the crawler
  if (entry && entry.urlOnly) {
    if (!isUrl(siteVal)) {
      if (queryVal) {
        const combined = context ? `${queryVal} ${context}` : queryVal;
        await handleSiteSearch(siteVal, combined);
        setLoading(false);
        return;
      }
      setStatus(`${entry.icon} — paste a direct video URL, or enter a search query to crawl the site.`);
      setLoading(false);
      return;
    }
    // Facebook page/profile URL → crawl videos tab via browser
    if (detected === 'facebook' && _isFacebookPageUrl(siteVal)) {
      setLoading(false);
      openLoginModal('facebook_page', siteVal);
      return;
    }
    await handleDirectUrl(siteVal);
  } else if (detected === 'url') {
    if (queryVal) {
      // URL + query → search that site via the browser crawler
      const combined = context ? `${queryVal} ${context}` : queryVal;
      await handleSiteSearch(siteVal, combined);
    } else {
      await handleDirectUrl(siteVal);
    }
  } else if (!queryVal) {
    setStatus('Please enter a search query.');
    setLoading(false);
    return;
  } else {
    const combined = context ? `${queryVal} ${context}` : queryVal;

    if (['reddit', 'twitter'].includes(detected)) {
      setLoading(false);
      openLoginModal(detected, combined);
      return;
    } else if (detected === 'site') {
      // Unknown site name → open it, search the query, crawl results
      await handleSiteSearch(siteVal, combined);
    } else {
      await handleKeywords(combined, detected);
    }
  }

  setLoading(false);
}

async function handleDirectUrl(url) {
  setStatus('Fetching video info…');
  try {
    const res = await fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: url, source_type: 'auto', count: getResultCount(), ...getCookiePayload() }),
    });
    const d = await res.json();

    // yt-dlp doesn't know this URL — fall back to Playwright crawler
    if (d.error && /unsupported url|not supported/i.test(d.error)) {
      await handleCrawl(url, 'yt-dlp can\'t handle this site — crawling page for video files…');
      return;
    }

    if (d.error) { setStatus('Error: ' + d.error); return; }
    if (!d.results || !d.results.length) {
      await handleCrawl(url, 'No video found via yt-dlp — crawling page for video files…');
      return;
    }

    const video = d.results[0];
    if (!await confirmLong(video.duration)) { setStatus('Cancelled.'); return; }
    setStatus('Queueing clip job…');
    await createJobCard(video, getClipConfig());
    setStatus('Job started — processing…');
  } catch (e) {
    setStatus('Error: ' + e.message);
  }
}

async function handleSiteSearch(site, query) {
  setStatus(`Opening ${site} and searching "${query}"… (browser crawl, may take up to ~2 min)`);
  try {
    const res = await fetch('/api/crawl', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: site, query }),
    });
    const d = await res.json();
    if (d.error) { setStatus('Site search error: ' + d.error); return; }
    // Fallback: nothing found on the site → regular YouTube search for "query site"
    pollBrowserSearch(d.search_id, 'Site search', async () => {
      setStatus(`Nothing found on ${site} — searching YouTube instead…`);
      await handleKeywords(`${query} ${site}`, 'auto');
    });
  } catch (e) {
    setStatus('Site search failed: ' + e.message);
  }
}

async function handleCrawl(url, reason) {
  setStatus(reason || 'Crawling page for video files…');
  try {
    const res = await fetch('/api/crawl', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const d = await res.json();
    if (d.error) { setStatus('Crawl error: ' + d.error); return; }
    pollBrowserSearch(d.search_id, 'Crawl');
  } catch (e) {
    setStatus('Crawl failed: ' + e.message);
  }
}

async function handleKeywords(query, sourceType = 'auto') {
  setStatus(`Searching: "${query}"…`);
  try {
    const meta = await apiFetchMeta(query, sourceType);
    if (!meta) return;
    renderSearchResults(meta);
    setStatus(`Found ${meta.length} video${meta.length !== 1 ? 's' : ''}. Click "Clip" to start.`);
  } catch (e) {
    setStatus('Error: ' + e.message);
  }
}

function getBrowser() {
  const val = document.getElementById('browserCookies').value;
  return (val && val !== '__file__') ? val : '';
}

// ── Cookie file upload ─────────────────────────────────────────────────────────
function onBrowserChange() {
  const val = document.getElementById('browserCookies').value;
  document.getElementById('cookieFileRow').classList.toggle('hidden', val !== '__file__');
  if (val !== '__file__') {
    state.cookieSessionId = null;
    document.getElementById('cookieFileName').textContent = 'No file chosen';
    document.getElementById('cookieFileName').classList.remove('loaded');
  }
}

async function onCookieFileSelected(input) {
  const file = input.files[0];
  if (!file) return;

  const nameEl = document.getElementById('cookieFileName');
  nameEl.textContent = 'Uploading…';
  nameEl.classList.remove('loaded');

  const form = new FormData();
  form.append('file', file);

  try {
    const res = await fetch('/api/cookies', { method: 'POST', body: form });
    const d = await res.json();
    if (d.error) { nameEl.textContent = 'Error: ' + d.error; return; }
    state.cookieSessionId = d.session_id;
    nameEl.textContent = file.name;
    nameEl.classList.add('loaded');
  } catch (e) {
    nameEl.textContent = 'Upload failed';
  }
}

function clearCookieFile() {
  state.cookieSessionId = null;
  document.getElementById('cookieFile').value = '';
  document.getElementById('cookieFileName').textContent = 'No file chosen';
  document.getElementById('cookieFileName').classList.remove('loaded');
  document.getElementById('browserCookies').value = '';
  document.getElementById('cookieFileRow').classList.add('hidden');
}

function getCookiePayload() {
  const browser = getBrowser();
  const sessionId = state.cookieSessionId;
  return {
    browser: browser || null,
    cookie_session_id: sessionId || null,
  };
}

function getResultCount() {
  return parseInt(document.getElementById('resultCount').value, 10) || 5;
}

async function apiFetchMeta(query, sourceType = 'auto') {
  const res = await fetch('/api/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, source_type: sourceType, count: getResultCount(), ...getCookiePayload() }),
  });
  const d = await res.json();
  if (d.error) { setStatus('Error: ' + d.error); return null; }
  if (!d.results || !d.results.length) {
    setStatus(d.message || 'No compatible videos found.');
    showEmptyResults(d.message || 'No compatible videos found for this search.');
    return null;
  }
  return d.results;
}

async function confirmLong(duration) {
  if (duration && duration > 600) {
    return window.confirm(
      `This video is ${fmtDuration(duration)} long.\nDownloading may take a while. Continue?`
    );
  }
  return true;
}

// ── Job card (direct URL or after "Clip" click) ────────────────────────────────
async function createJobCard(video, config) {
  const card = buildJobCard(video);
  addCard(card);

  const res = await fetch('/api/clip', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url: video.url, ...config, ...getCookiePayload() }),
  });
  const d = await res.json();

  if (d.error) {
    card.querySelector('.card-status').textContent = '✗ ' + d.error;
    card.querySelector('.card-status').classList.add('error');
    return;
  }

  const jobId = d.job_id;
  card.dataset.jobId = jobId;
  addQueueRow(jobId, video);
  startPoll(jobId, card);
}

function buildJobCard(video) {
  const card = document.createElement('div');
  card.className = 'video-card fade-in';

  card.innerHTML = `
    <button class="card-close-btn" title="Remove" onclick="this.closest('.video-card').remove(); updateCount()">✕</button>
    <div class="card-thumb">
      ${video.thumbnail
        ? `<img src="${escHtml(video.thumbnail)}" alt="" loading="lazy" onerror="this.parentElement.innerHTML='<div class=\\'thumb-placeholder\\'>🎬</div>'">`
        : '<div class="thumb-placeholder">🎬</div>'}
    </div>
    <div class="card-body">
      <h3 class="card-title" title="${escHtml(video.title)}">${escHtml(video.title || 'Video')}</h3>
      <p class="card-url" title="${escHtml(video.url)}">${escHtml(truncate(video.url, 55))}</p>
      <p class="card-duration">Duration: ${fmtDuration(video.duration)}</p>
      <div class="card-progress">
        <div class="progress-bar"><div class="progress-fill" style="width:0%"></div></div>
        <span class="card-status">Queued</span>
      </div>
      <div class="card-clips"></div>
    </div>
  `;
  return card;
}

// ── Search result cards (keyword mode) ────────────────────────────────────────
function renderSearchResults(videos) {
  clearResults();
  videos.forEach(video => {
    const card = buildSearchCard(video);
    addCard(card);
  });
}

function buildSearchCard(video) {
  const card = document.createElement('div');
  card.className = 'video-card fade-in';

  card.innerHTML = `
    <button class="card-close-btn" title="Remove" onclick="this.closest('.video-card').remove(); updateCount()">✕</button>
    <div class="card-thumb">
      ${video.thumbnail
        ? `<img src="${escHtml(video.thumbnail)}" alt="" loading="lazy" onerror="this.parentElement.innerHTML='<div class=\\'thumb-placeholder\\'>🎬</div>'">`
        : '<div class="thumb-placeholder">🎬</div>'}
    </div>
    <div class="card-body">
      <h3 class="card-title" title="${escHtml(video.title)}">${escHtml(video.title || 'Video')}</h3>
      <p class="card-url" title="${escHtml(video.url)}">${escHtml(truncate(video.url, 55))}</p>
      <p class="card-duration">Duration: ${fmtDuration(video.duration)}</p>
      <button class="btn-accent" onclick="clipFromSearch(this)">✂️ Clip This Video</button>
    </div>
  `;

  // Attach video data to card for later use
  card._videoData = video;
  return card;
}

async function clipFromSearch(btn) {
  const card = btn.closest('.video-card');
  const video = card._videoData;
  if (!video) return;

  if (!await confirmLong(video.duration)) return;

  btn.disabled = true;
  btn.textContent = 'Starting…';

  // Inject progress UI
  const body = card.querySelector('.card-body');
  btn.remove();
  body.insertAdjacentHTML('beforeend', `
    <div class="card-progress">
      <div class="progress-bar"><div class="progress-fill" style="width:0%"></div></div>
      <span class="card-status">Queued</span>
    </div>
    <div class="card-clips"></div>
  `);

  const res = await fetch('/api/clip', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url: video.url, ...getClipConfig(), ...getCookiePayload() }),
  });
  const d = await res.json();

  if (d.error) {
    card.querySelector('.card-status').textContent = '✗ ' + d.error;
    card.querySelector('.card-status').classList.add('error');
    return;
  }

  const jobId = d.job_id;
  card.dataset.jobId = jobId;
  addQueueRow(jobId, video);
  startPoll(jobId, card);
}

// ── Results DOM ────────────────────────────────────────────────────────────────
function addCard(card) {
  const grid = document.getElementById('resultsGrid');
  const empty = document.getElementById('emptyResults');
  if (empty) empty.remove();
  grid.appendChild(card);
  updateCount();
}

function clearResults() {
  document.getElementById('resultsGrid').innerHTML = '';
  updateCount();
}

function showEmptyResults(msg) {
  document.getElementById('resultsGrid').innerHTML = `
    <div class="empty-state">
      <div class="empty-icon">🔍</div>
      <p>${escHtml(msg)}</p>
    </div>`;
  updateCount();
}

function updateCount() {
  const n = document.querySelectorAll('#resultsGrid .video-card').length;
  document.getElementById('resultsCount').textContent = n > 0 ? `${n} video${n !== 1 ? 's' : ''}` : '';
}

// ── Queue ──────────────────────────────────────────────────────────────────────
function addQueueRow(jobId, video) {
  const tbody = document.getElementById('queueBody');
  const emptyRow = document.getElementById('emptyQueue');
  if (emptyRow) emptyRow.remove();

  const tr = document.createElement('tr');
  tr.id = `row-${jobId}`;

  tr.innerHTML = `
    <td class="cell-title">${escHtml(truncate(video.title || 'Video', 40))}</td>
    <td class="cell-source"><a href="${escHtml(video.url || '#')}" target="_blank" rel="noopener noreferrer">source ↗</a></td>
    <td class="cell-duration">${fmtDuration(video.duration)}</td>
    <td class="cell-status"><span class="badge badge-queued">Queued</span></td>
    <td class="cell-download">—</td>
  `;

  tbody.appendChild(tr);
  state.queueRows.set(jobId, tr);
}

function updateQueueRow(jobId, job) {
  const tr = state.queueRows.get(jobId);
  if (!tr) return;

  const badge = tr.querySelector('.badge');
  badge.className = `badge badge-${job.status}`;
  badge.textContent = capitalize(job.status);

  const dlCell = tr.querySelector('.cell-download');
  if (job.status === 'ready' && job.clips && job.clips.length) {
    dlCell.innerHTML = job.clips.map(clip =>
      `<a class="download-link" href="/api/download/${encodeURIComponent(jobId)}/${encodeURIComponent(clip)}" download="${escHtml(clip)}">${escHtml(clip)}</a>`
    ).join('');
  } else if (job.status === 'ready' && job.clips_pending && job.clips_pending.length) {
    dlCell.innerHTML = `<span class="text-muted" style="font-size:0.8rem">Click clips to render</span>`;
  } else if (job.status === 'failed') {
    dlCell.innerHTML = `<span class="error-text">${escHtml(truncate(job.error || 'Failed', 50))}</span>`;
  }
}

// ── Download All / Clear Completed ────────────────────────────────────────────
function downloadAll() {
  state.queueRows.forEach((tr) => {
    tr.querySelectorAll('.download-link').forEach(a => a.click());
  });
}

function clearCompleted() {
  const toDelete = [];
  state.queueRows.forEach((tr, jobId) => {
    const badge = tr.querySelector('.badge');
    if (badge && (badge.classList.contains('badge-ready') || badge.classList.contains('badge-failed'))) {
      tr.remove();
      toDelete.push(jobId);
    }
  });
  toDelete.forEach(id => {
    state.queueRows.delete(id);
    fetch(`/api/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => {});
  });

  if (state.queueRows.size === 0) {
    document.getElementById('queueBody').innerHTML =
      '<tr id="emptyQueue"><td colspan="5" class="empty-queue">No jobs in queue.</td></tr>';
  }
}

function _renderAllClips(jobId) {
  const card = document.querySelector(`.video-card[data-job-id="${CSS.escape(jobId)}"]`);
  if (!card) return;
  card.querySelectorAll('.clip-item:not([data-rendered]) [data-action="download"]')
    .forEach(b => { if (!b.disabled) b.click(); });
}

async function _downloadFullVideo(jobId, btn) {
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = 'Preparing…';
  try {
    const res = await fetch('/api/render-full', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({job_id: jobId}),
    });
    const d = await res.json();
    if (d.error) {
      btn.textContent = '✗ ' + d.error;
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 4000);
      return;
    }
    const renderId = d.render_id;
    const poll = setInterval(async () => {
      try {
        const s = await (await fetch(`/api/render/${encodeURIComponent(renderId)}`)).json();
        if (s.status === 'ready') {
          clearInterval(poll);
          window.location.href = `/api/download/${encodeURIComponent(jobId)}/${encodeURIComponent(s.output)}`;
          btn.textContent = orig;
          btn.disabled = false;
        } else if (s.status === 'failed' || s.status === 'cancelled') {
          clearInterval(poll);
          btn.textContent = '✗ Failed';
          setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 4000);
        }
      } catch (_) { clearInterval(poll); btn.textContent = orig; btn.disabled = false; }
    }, 2000);
  } catch (e) {
    btn.textContent = orig;
    btn.disabled = false;
  }
}

// ── Polling ────────────────────────────────────────────────────────────────────
function startPoll(jobId, card) {
  const id = setInterval(async () => {
    try {
      const res = await fetch(`/api/status/${encodeURIComponent(jobId)}`);
      if (!res.ok) { clearInterval(id); return; }
      const job = await res.json();

      updateCard(card, job);
      updateQueueRow(jobId, job);

      if (job.status === 'ready' || job.status === 'failed') {
        clearInterval(id);
        state.polls.delete(jobId);
      }
    } catch (_) {}
  }, 2000);
  state.polls.set(jobId, id);
}

function updateCard(card, job) {
  card.dataset.jobId = job.id;
  const fill        = card.querySelector('.progress-fill');
  const statusEl    = card.querySelector('.card-status');
  const clipsEl     = card.querySelector('.card-clips');
  const progressWrap = card.querySelector('.card-progress');

  if (fill) fill.style.width = (job.progress || 0) + '%';
  if (statusEl) {
    statusEl.textContent = job.message || capitalize(job.status);
    statusEl.classList.toggle('error', job.status === 'failed');
  }

  if (job.status !== 'ready' || !clipsEl) return;
  if (progressWrap) progressWrap.classList.add('done');

  const pending  = job.clips_pending || [];
  const rendered = new Set(job.clips || []);

  // Build clips UI once
  if (!clipsEl.dataset.rendered) {
    clipsEl.dataset.rendered = '1';

    if (pending.length > 0) {
      // Lazy-render mode: show all segments, download/render on demand
      const jid = escHtml(job.id);
      clipsEl.innerHTML = `
        <div class="clips-header-row">
          <span class="clips-label">Clips (${pending.length})</span>
          <div class="clips-bulk-actions">
            <button class="btn-ghost btn-xs" onclick="_renderAllClips('${jid}')">⟳ Render All</button>
            <a class="btn-ghost btn-xs" href="/api/download-zip/${encodeURIComponent(job.id)}" download>⬇ All ZIP</a>
            <button class="btn-ghost btn-xs" id="fullvid-${jid}" onclick="_downloadFullVideo('${jid}', this)">↓ Full Video</button>
          </div>
        </div>
        <div class="clips-list"></div>`;
      const list = clipsEl.querySelector('.clips-list');
      pending.forEach((seg, idx) => {
        list.appendChild(_buildClipItem(job.id, seg, idx, rendered.has(seg.key)));
      });
    } else if (job.clips && job.clips.length) {
      // Backward-compat: pre-rendered clips (old jobs)
      clipsEl.innerHTML = `<div class="clips-label">Generated clips (${job.clips.length})</div><div class="clips-list"></div>`;
      const list = clipsEl.querySelector('.clips-list');
      job.clips.forEach(clip => list.appendChild(_buildRenderedClipItem(job.id, clip)));
    }
  }

  // Upgrade any newly-rendered clips
  if (pending.length) {
    pending.forEach((seg, idx) => {
      if (rendered.has(seg.key)) {
        const item = clipsEl.querySelector(`[data-clip-idx="${idx}"]`);
        if (item && !item.dataset.rendered) _upgradeClipItem(item, job.id, seg);
      }
    });
  }
}

function _buildClipItem(jobId, seg, idx, isRendered) {
  const item = document.createElement('div');
  item.className = 'clip-item';
  item.dataset.clipIdx = idx;
  item.dataset.clipKey = seg.key;
  if (isRendered) item.dataset.rendered = '1';

  const topRow = document.createElement('div');
  topRow.className = 'clip-top-row';

  const nameEl = document.createElement('span');
  nameEl.className = 'clip-name';
  nameEl.title = seg.key;
  nameEl.textContent = seg.label;
  topRow.appendChild(nameEl);

  const actionsEl = document.createElement('div');
  actionsEl.className = 'clip-actions';
  topRow.appendChild(actionsEl);
  item.appendChild(topRow);

  // Ratio row + conv-results (hidden until rendered)
  const ratioRow = document.createElement('div');
  ratioRow.className = 'ratio-row hidden';
  item.appendChild(ratioRow);

  const convResults = document.createElement('div');
  convResults.className = 'conv-results';
  item.appendChild(convResults);

  if (isRendered) {
    _fillRenderedActions(actionsEl, jobId, seg.key);
    _fillRatioRow(ratioRow, jobId, seg.key);
    ratioRow.classList.remove('hidden');
  } else {
    _fillPendingActions(actionsEl, jobId, seg, idx, item);
  }

  return item;
}

function _buildRenderedClipItem(jobId, clipName) {
  const seg = { key: clipName, label: clipName };
  return _buildClipItem(jobId, seg, clipName, true);
}

function _fillPendingActions(actionsEl, jobId, seg, idx, item) {
  const previewBtn = document.createElement('button');
  previewBtn.className = 'btn-ghost render-action-btn';
  previewBtn.dataset.action = 'preview';
  previewBtn.textContent = '▶ Preview';
  // preview_only=true: temp render, no save to clips list
  previewBtn.addEventListener('click', () => _triggerRender(jobId, seg, item, 'preview', true));

  const dlBtn = document.createElement('button');
  dlBtn.className = 'btn-ghost render-action-btn';
  dlBtn.dataset.action = 'download';
  dlBtn.textContent = '↓ Download';
  // preview_only=false: permanent render, saved to clips list
  dlBtn.addEventListener('click', () => _triggerRender(jobId, seg, item, 'download', false));

  actionsEl.appendChild(previewBtn);
  actionsEl.appendChild(dlBtn);
}

function _fillRenderedActions(actionsEl, jobId, clipName) {
  actionsEl.innerHTML = '';
  const prevBtn = document.createElement('button');
  prevBtn.className = 'btn-ghost';
  prevBtn.textContent = '▶';
  prevBtn.addEventListener('click', () => previewClip(jobId, clipName));

  const dlLink = document.createElement('a');
  dlLink.className = 'btn-ghost';
  dlLink.href = `/api/download/${encodeURIComponent(jobId)}/${encodeURIComponent(clipName)}`;
  dlLink.download = clipName;
  dlLink.textContent = '↓';

  actionsEl.appendChild(prevBtn);
  actionsEl.appendChild(dlLink);
}

function _fillRatioRow(ratioRow, jobId, clipName) {
  ratioRow.innerHTML = `<span class="ratio-label">Convert:</span>` +
    ['9:16','4:5','1:1','16:9','4:3'].map(r =>
      `<button class="ratio-btn" data-ratio="${r}" onclick="convertClip(this,'${escHtml(jobId)}','${escHtml(clipName)}')">${r}</button>`
    ).join('');
}

function _upgradeClipItem(item, jobId, seg, fromPreview = false) {
  item.dataset.rendered = fromPreview ? 'preview' : '1';
  const actionsEl = item.querySelector('.clip-actions');
  if (actionsEl) {
    actionsEl.innerHTML = '';
    if (fromPreview) {
      // Replay preview + permanent download button
      const replayBtn = document.createElement('button');
      replayBtn.className = 'btn-ghost';
      replayBtn.textContent = '▶';
      replayBtn.addEventListener('click', () => previewClip(jobId, seg.key));

      const dlBtn = document.createElement('button');
      dlBtn.className = 'btn-ghost render-action-btn';
      dlBtn.dataset.action = 'download';
      dlBtn.textContent = '↓ Download';
      dlBtn.addEventListener('click', () => _triggerRender(jobId, seg, item, 'download', false));

      actionsEl.appendChild(replayBtn);
      actionsEl.appendChild(dlBtn);
    } else {
      _fillRenderedActions(actionsEl, jobId, seg.key);
    }
  }
  const ratioRow = item.querySelector('.ratio-row');
  if (ratioRow) {
    _fillRatioRow(ratioRow, jobId, seg.key);
    ratioRow.classList.remove('hidden');
  }
}

async function _triggerRender(jobId, seg, item, action, previewOnly = false) {
  const btns = item.querySelectorAll('.render-action-btn');
  btns.forEach(b => {
    b.disabled = true;
    if (b.dataset.action === action) b.textContent = 'Rendering…';
  });

  // Add cancel button
  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn-ghost render-cancel-btn';
  cancelBtn.textContent = '✕ Cancel';
  item.querySelector('.clip-actions').appendChild(cancelBtn);

  try {
    const res = await fetch('/api/render', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: jobId, start: seg.start, end: seg.end, preview_only: previewOnly }),
    });
    const d = await res.json();
    if (d.error) {
      _resetPendingBtns(item);
      _showClipError(item, d.error);
      return;
    }

    cancelBtn.addEventListener('click', () => _cancelRender(d.render_id, item));
    _pollRender(d.render_id, jobId, seg, item, action, cancelBtn, previewOnly);
  } catch (e) {
    _resetPendingBtns(item);
  }
}

async function _cancelRender(renderId, item) {
  try {
    await fetch(`/api/render/${encodeURIComponent(renderId)}`, { method: 'DELETE' });
  } catch (_) {}
  _resetPendingBtns(item);
}

function _resetPendingBtns(item) {
  item.querySelectorAll('.render-action-btn').forEach(b => {
    b.disabled = false;
    b.textContent = b.dataset.action === 'preview' ? '▶ Preview' : '↓ Download';
  });
  item.querySelectorAll('.render-cancel-btn').forEach(b => b.remove());
}

function _pollRender(renderId, jobId, seg, item, pendingAction, cancelBtn, previewOnly = false) {
  const iv = setInterval(async () => {
    try {
      const res = await fetch(`/api/render/${encodeURIComponent(renderId)}`);
      const d = await res.json();

      if (d.status === 'ready') {
        clearInterval(iv);
        if (cancelBtn) cancelBtn.remove();
        _upgradeClipItem(item, jobId, seg, previewOnly);
        if (pendingAction === 'preview') {
          previewClip(jobId, d.output);
        } else {
          const a = document.createElement('a');
          a.href = `/api/download/${encodeURIComponent(jobId)}/${encodeURIComponent(d.output)}`;
          a.download = d.output;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
        }
      } else if (d.status === 'failed') {
        clearInterval(iv);
        _resetPendingBtns(item);
        _showClipError(item, d.error || 'Render failed');
      } else if (d.status === 'cancelled') {
        clearInterval(iv);
        _resetPendingBtns(item);
      }
    } catch (_) {}
  }, 2000);
}

function _showClipError(item, msg) {
  let errEl = item.querySelector('.clip-error');
  if (!errEl) {
    errEl = document.createElement('span');
    errEl.className = 'clip-error error-text';
    item.querySelector('.clip-top-row').appendChild(errEl);
  }
  errEl.textContent = msg;
}

// ── Browser search (Reddit / Twitter login) ────────────────────────────────────
let _bsQuery = '';
let _bsSource = '';

function openLoginModal(source, query) {
  _bsSource = source;
  _bsQuery  = query;
  const siteLabels = { twitter: 'Twitter / X', facebook_page: 'Facebook (login optional)' };
  document.getElementById('loginSiteName').textContent =
    siteLabels[source] || (source.charAt(0).toUpperCase() + source.slice(1));
  document.getElementById('loginUsername').value = '';
  document.getElementById('loginPassword').value = '';
  document.getElementById('loginError').classList.add('hidden');
  document.getElementById('loginModal').classList.remove('hidden');
  setTimeout(() => document.getElementById('loginUsername').focus(), 100);
}

function closeLoginModal() {
  document.getElementById('loginModal').classList.add('hidden');
}

async function submitBrowserSearch(anonymous = false) {
  const username = anonymous ? null : document.getElementById('loginUsername').value.trim();
  const password = anonymous ? null : document.getElementById('loginPassword').value.trim();

  const errEl = document.getElementById('loginError');
  errEl.classList.add('hidden');

  const submitBtn = document.getElementById('loginSubmitBtn');
  submitBtn.disabled = true;
  document.querySelector('#loginSubmitBtn .btn-text').classList.add('hidden');
  document.getElementById('loginSpinner').classList.remove('hidden');

  try {
    const res = await fetch('/api/browser-search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: _bsSource, query: _bsQuery, username, password }),
    });
    const d = await res.json();
    if (d.error) { showLoginError(d.error); return; }

    closeLoginModal();
    setStatus(`Searching ${_bsSource} in browser…`);
    pollBrowserSearch(d.search_id);
  } catch (e) {
    showLoginError(e.message);
  } finally {
    submitBtn.disabled = false;
    document.querySelector('#loginSubmitBtn .btn-text').classList.remove('hidden');
    document.getElementById('loginSpinner').classList.add('hidden');
  }
}

function showLoginError(msg) {
  const el = document.getElementById('loginError');
  el.textContent = msg;
  el.classList.remove('hidden');
}

function pollBrowserSearch(searchId, label = 'Browser search', onEmpty = null) {
  const iv = setInterval(async () => {
    try {
      const res = await fetch(`/api/browser-search/${encodeURIComponent(searchId)}`);
      const d = await res.json();
      if (d.status === 'ready') {
        clearInterval(iv);
        if (d.cookie_session_id) state.cookieSessionId = d.cookie_session_id;
        if (!d.results || !d.results.length) {
          if (onEmpty) { onEmpty(); return; }
          setStatus('No videos found.');
          showEmptyResults('No videos found on this page.');
          return;
        }
        renderSearchResults(d.results);
        setStatus(`Found ${d.results.length} video${d.results.length !== 1 ? 's' : ''}. Click "Clip" to start.`);
      } else if (d.status === 'failed') {
        clearInterval(iv);
        if (onEmpty) { onEmpty(); return; }
        setStatus(`${label} failed: ` + (d.error || 'Unknown error'));
      }
    } catch (_) {}
  }, 2500);
}

// Close login modal on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeLoginModal(); closeModal(); }
});

// ── Aspect ratio conversion ────────────────────────────────────────────────────
async function convertClip(btn, jobId, clipName) {
  btn.disabled = true;
  const ratio = btn.dataset.ratio;
  btn.textContent = '…';
  btn.classList.add('ratio-btn-loading');

  try {
    const res = await fetch('/api/convert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: jobId, clip_name: clipName, aspect_ratio: ratio, preview_only: true }),
    });
    const d = await res.json();
    if (d.error) { btn.textContent = '✗'; btn.title = d.error; return; }
    pollConversion(d.conv_id, btn, jobId);
  } catch (e) {
    btn.textContent = '✗'; btn.title = e.message;
  }
}

function pollConversion(convId, btn, jobId) {
  const iv = setInterval(async () => {
    try {
      const res = await fetch(`/api/convert/${encodeURIComponent(convId)}`);
      const d = await res.json();

      if (d.status === 'ready') {
        clearInterval(iv);
        btn.classList.remove('ratio-btn-loading');
        const clipItem = btn.closest('.clip-item');
        const convResults = clipItem && clipItem.querySelector('.conv-results');
        if (convResults) {
          const ratio = btn.dataset.ratio;
          const clipName = btn.closest('.clip-item') && btn.closest('.clip-item').dataset.clipKey;
          const wrap = document.createElement('div');
          wrap.className = 'conv-result-row';

          const previewBtn = document.createElement('button');
          previewBtn.className = 'btn-ghost conv-preview-btn';
          previewBtn.textContent = '▶';
          previewBtn.title = `Preview ${ratio}`;
          previewBtn.onclick = () => previewClip(jobId, d.output);

          const saveBtn = document.createElement('button');
          saveBtn.className = 'btn-ghost conv-save-btn';
          saveBtn.textContent = `↓ Save`;
          saveBtn.title = `Save ${ratio} permanently`;
          saveBtn.onclick = () => _saveConversion(saveBtn, jobId, clipName || d.output, ratio);

          wrap.appendChild(previewBtn);
          wrap.appendChild(saveBtn);
          convResults.appendChild(wrap);
        }
        btn.textContent = btn.dataset.ratio;
        btn.classList.add('ratio-btn-done');
      } else if (d.status === 'failed') {
        clearInterval(iv);
        btn.classList.remove('ratio-btn-loading');
        btn.textContent = '✗';
        btn.classList.add('ratio-btn-failed');
        const errMsg = d.error || 'Conversion failed';
        btn.title = errMsg;
        const clipItem = btn.closest('.clip-item');
        if (clipItem) {
          let errEl = clipItem.querySelector('.conv-error');
          if (!errEl) { errEl = document.createElement('div'); errEl.className = 'conv-error'; clipItem.appendChild(errEl); }
          errEl.textContent = errMsg.slice(0, 200);
        }
      }
    } catch (_) {}
  }, 2000);
}

async function _saveConversion(btn, jobId, clipName, ratio) {
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const res = await fetch('/api/convert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: jobId, clip_name: clipName, aspect_ratio: ratio, preview_only: false }),
    });
    const d = await res.json();
    if (d.error) { btn.textContent = '✗ Save failed'; btn.title = d.error; btn.disabled = false; return; }
    // Poll until permanent file is ready, then trigger browser download
    const iv = setInterval(async () => {
      try {
        const r = await fetch(`/api/convert/${encodeURIComponent(d.conv_id)}`);
        const c = await r.json();
        if (c.status === 'ready') {
          clearInterval(iv);
          const a = document.createElement('a');
          a.href = `/api/download/${encodeURIComponent(jobId)}/${encodeURIComponent(c.output)}`;
          a.download = c.output;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          btn.textContent = '✓ Saved';
        } else if (c.status === 'failed') {
          clearInterval(iv);
          btn.textContent = '✗ Save failed';
          btn.title = c.error || 'Save failed';
          btn.disabled = false;
        }
      } catch (_) {}
    }, 2000);
  } catch (e) {
    btn.textContent = '✗ Save failed';
    btn.title = e.message;
    btn.disabled = false;
  }
}

// ── Modal ──────────────────────────────────────────────────────────────────────
function previewClip(jobId, clipName) {
  const modal = document.getElementById('previewModal');
  const video = document.getElementById('modalVideo');
  document.getElementById('modalTitle').textContent = clipName;
  video.src = `/api/preview/${encodeURIComponent(jobId)}/${encodeURIComponent(clipName)}?t=${Date.now()}`;
  modal.classList.remove('hidden');
}

function closeModal() {
  const modal = document.getElementById('previewModal');
  const video = document.getElementById('modalVideo');
  video.pause();
  video.src = '';
  modal.classList.add('hidden');
}

document.addEventListener('keydown', e => {
  // handled above
});

// ── Misc ───────────────────────────────────────────────────────────────────────
function truncate(s, n) {
  if (!s) return '';
  return s.length > n ? s.slice(0, n) + '…' : s;
}

function capitalize(s) {
  if (!s) return '';
  return s.charAt(0).toUpperCase() + s.slice(1);
}
