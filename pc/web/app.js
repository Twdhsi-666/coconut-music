/* 椰子音乐 · 前端逻辑 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const PLACEHOLDER =
  'data:image/svg+xml;utf8,' +
  encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="44" height="44">' +
    '<rect width="44" height="44" rx="7" fill="#cfd6de"/>' +
    '<path d="M20 12v12.6a3.6 3.6 0 1 0 1.8 3.1V16h5v-4z" fill="#8b939c"/></svg>');

const S = {
  view: 'discover',
  settings: { theme: 'light', volume: 0.85, playMode: 'list' },
  playlists: [],
  queue: [],
  qi: -1,
  current: null,
  noSource: new Set(),   // 这次运行里「自动匹配音源失败过」的歌：再遇到秒跳过，别反复搜 B 站（重复搜索会撞风控）
  autoSkips: 0,          // 本轮连续自动跳了几首，到 2 首就停下来让用户决定
  login: { logged_in: false, uname: '' },
  search: { kw: '', order: 'totalrank', page: 1, results: [] },
  imp: { songs: [], matches: [], running: false, info: null, cur: -1 },
  locateList: null,      // 当前视图的歌曲数组（存在时才显示「定位正在播放」圆点）
};

const audio = $('#audio');
let dragging = false;

/* ------------------------------------------------------------------ 工具 */
const esc = (t) => String(t == null ? '' : t)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const img = (u) => (u ? '/api/img?url=' + encodeURIComponent(u) : PLACEHOLDER);

/* ------------------------------------------------- 音源占位（QQ 歌单） */
/**
 * 这首歌是不是「还没匹配到 B 站音源」。
 *
 * QQ 音乐歌单是「秒导」进来的 —— 只搬目录、不逐首搜 B 站（759 首要是逐首搜，
 * 用户得等好几分钟，而且会直接撞上 B 站风控）。所以入库时 bvid 存的是
 * `qq_<mid>` 占位符 + unmatched:true，真正的音源等第一次播放时再补。
 * 判据写两遍（unmatched 标记 / qq_ 前缀）是因为老数据可能只有其中一个。
 */
function isUnmatchedSource(s) {
  return !!(s && (s.unmatched || String(s.bvid || '').indexOf('qq_') === 0));
}

/**
 * 把占位音源换成真实的 B 站音源；本来就正常的话原样返回。
 *
 * 后端匹配成功后会把**所有歌单 + 播放历史**里的这条一起换掉，所以同一首歌
 * 只会搜一次 —— 既快，也少触发 B 站风控（重复搜索会 csrf 校验失败）。
 * 匹配不到就抛错，让调用方决定怎么跟用户说（绝不硬塞一个可能不对的音源）。
 */
async function ensureSongSource(song) {
  if (!isUnmatchedSource(song)) return song;
  const r = await api('/api/source/auto', {
    bvid: song.bvid,
    title: song.name || song.title || '',
    artist: song.artist || song.author || '',
    duration: song.duration || 0,
  });
  if (!r.ok || !r.song) throw new Error(r.error || '没找到能播的音源');
  S.playlists = r.playlists || S.playlists;
  return r.song;
}

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const p = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
}
function fmtPlay(n) {
  n = Number(n || 0);
  if (n >= 100000000) return (n / 100000000).toFixed(1) + '亿';
  if (n >= 10000) return (n / 10000).toFixed(1) + '万';
  return String(n);
}

function toast(msg, kind) {
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = msg;
  $('#toastWrap').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 2200);
  setTimeout(() => el.remove(), 2600);
}

async function api(path, body, method) {
  const opt = { method: method || (body ? 'POST' : 'GET') };
  if (body) {
    opt.headers = { 'Content-Type': 'application/json' };
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  let js = {};
  try { js = await r.json(); } catch (e) { throw new Error('后端没有响应，请重启软件'); }
  if (!js.ok) throw new Error(js.error || '操作失败');
  return js;
}

function openModal(html) {
  $('#modalBox').innerHTML = html;
  $('#modalWrap').classList.remove('hidden');
}
function closeModal() { $('#modalWrap').classList.add('hidden'); $('#modalBox').innerHTML = ''; }

/* ------------------------------------------------------------------ 启动 */
async function init() {
  bindGlobal();
  try {
    const st = await api('/api/state');
    S.settings = Object.assign(S.settings, st.settings || {});
    S.playlists = st.playlists || [];
    S.login = st.login || S.login;
  } catch (e) {
    toast(e.message, 'err');
  }

  // 支持从启动参数带关键词/主题，例如 /?q=周杰伦&theme=dark
  const params = new URLSearchParams(location.search);
  if (params.get('theme')) S.settings.theme = params.get('theme');

  $('#volume').value = Math.round(Number(S.settings.volume ?? 0.85) * 100);
  applyVolume();          // 统一走 dB 映射，别在这里直接写 audio.volume
  applyTheme();
  applyLoudness(false);
  if (typeof bootEqualizer === 'function') bootEqualizer();
  if (typeof bootSettings === 'function') bootSettings();
  renderLoginState();
  renderPlaylists();
  setMode(S.settings.playMode || 'list');

  const autoQ = params.get('q');
  const autoView = params.get('view');
  if (autoQ) {
    $('#searchInput').value = autoQ;
    doSearch(autoQ);
  } else if (autoView) {
    // 深链：/?view=share&text=<urlencoded> 可以直达统一导入页并自动导入。
    // 旧的 ?view=qqimport 仍兼容（现在它就是统一导入页）。
    const view = autoView === 'qqimport' ? 'share' : autoView;
    showView(view);
    const preset = params.get('text') || params.get('qq');
    if (view === 'share' && preset) {
      setTimeout(() => {
        const inp = $('#shareInput');
        if (!inp) return;
        inp.value = preset;
        refreshKind();
        doShareImport();
      }, 200);
    }
  } else {
    showView('discover');
  }
}

/* ---------------------------------------------------------------- 主题 */
function applyTheme() {
  document.documentElement.dataset.theme = S.settings.theme;
  $('#themeBtn').textContent = S.settings.theme === 'dark' ? '☀️' : '🌙';
}
function toggleTheme() {
  S.settings.theme = S.settings.theme === 'dark' ? 'light' : 'dark';
  applyTheme();
  api('/api/settings', { key: 'theme', value: S.settings.theme }).catch(() => {});
}

/* ---------------------------------------------------------------- 登录 */
function renderLoginState() {
  const b = $('#loginBtn');
  if (S.login.logged_in) {
    b.textContent = S.login.uname ? S.login.uname : '已登录';
    b.classList.remove('ghost-btn');
  } else {
    b.textContent = '登录';
  }
}

async function openLogin() {
  if (S.login.logged_in) {
    openModal(`
      <h3>账号</h3>
      <div class="m-sub">已登录：${esc(S.login.uname || '已登录用户')}</div>
      <div class="hint">登录后可以拿到更高音质（Hi-Res 无损 / 杜比，需要大会员）。Cookie 只存在这台电脑上。</div>
      <div class="modal-foot">
        <button class="pill-btn plain" id="logoutBtn">退出登录</button>
        <button class="pill-btn" onclick="closeModal()">关闭</button>
      </div>`);
    $('#logoutBtn').onclick = async () => {
      await api('/api/login/logout', {});
      S.login = { logged_in: false, uname: '' };
      renderLoginState();
      closeModal();
      toast('已退出登录', 'ok');
    };
    return;
  }

  openModal(`
    <h3>扫码登录</h3>
    <div class="m-sub">用手机扫一下下面的二维码</div>
    <div class="qr-wrap"><div id="qrBox" class="qr-status">正在获取二维码…</div></div>
    <div class="qr-status" id="qrStatus">　</div>`);

  let key = null, timer = null;

  const stop = () => { if (timer) clearInterval(timer); timer = null; };

  try {
    const r = await api('/api/login/qrcode');
    key = r.key;
    $('#qrBox').outerHTML = `<img src="${r.qr}" alt="二维码">`;
    $('#qrStatus').textContent = '等待扫码…';
  } catch (e) {
    $('#qrStatus').textContent = '二维码获取失败：' + e.message;
    return;
  }

  timer = setInterval(async () => {
    if ($('#modalWrap').classList.contains('hidden')) { stop(); return; }
    try {
      const r = await api('/api/login/poll?key=' + encodeURIComponent(key), null, 'GET');
      const map = {
        86101: '等待扫码…',
        86090: '已扫码，请在手机上确认',
        86038: '二维码已过期，请重新打开登录',
      };
      if (r.logged_in) {
        stop();
        S.login = { logged_in: true, uname: r.uname || '' };
        renderLoginState();
        $('#qrStatus').className = 'qr-status ok';
        $('#qrStatus').textContent = '登录成功！欢迎 ' + (r.uname || '');
        toast('登录成功', 'ok');
        setTimeout(closeModal, 900);
      } else if (r.status !== 0) {
        $('#qrStatus').className = 'qr-status';
        $('#qrStatus').textContent = map[r.status] || '';
      }
    } catch (e) { /* 忽略轮询错误 */ }
  }, 2000);
  window.__stopQr = stop;
}

/* ------------------------------------------------------------ 侧栏歌单 */
function renderPlaylists() {
  const box = $('#playlistList');
  if (!S.playlists.length) {
    box.innerHTML = `<div class="pl-item" style="color:var(--text-3)">还没有歌单</div>`;
    return;
  }
  box.innerHTML = S.playlists.map((p) => `
    <div class="pl-item ${S.view === 'playlist:' + p.id ? 'active' : ''}" data-pl="${esc(p.id)}"
         data-initial="${esc(p.id === 'fav' ? '❤' : (p.name || '?').trim().slice(0, 1))}"
         title="${esc(p.name)}">
      <span>${p.id === 'fav' ? '❤️' : '🎵'}</span>
      <span class="name" title="${esc(p.name)}">${esc(p.name)}</span>
      <span class="cnt">${p.count}</span>
      ${p.id === 'fav' ? '' : '<span class="del" data-del="' + esc(p.id) + '" title="删除歌单">✕</span>'}
    </div>`).join('');

  $$('#playlistList .pl-item').forEach((el) => {
    el.onclick = (e) => {
      if (e.target.dataset.del) return;
      showView('playlist:' + el.dataset.pl);
    };
  });
  $$('#playlistList .del').forEach((el) => {
    el.onclick = async (e) => {
      e.stopPropagation();
      const id = el.dataset.del;
      const pl = S.playlists.find((p) => p.id === id);
      if (!confirm(`确定删除歌单「${pl ? pl.name : ''}」吗？`)) return;
      try {
        const r = await api('/api/playlist/delete', { id });
        S.playlists = r.playlists;
        renderPlaylists();
        if (S.view === 'playlist:' + id) showView('discover');
        toast('已删除', 'ok');
      } catch (err) { toast(err.message, 'err'); }
    };
  });
}

/* ---------------------------------------------------------------- 视图 */
function showView(v) {
  S.view = v;
  S.locateList = null;            // 换页了，定位圆点的归属列表作废
  $$('.nav-item').forEach((el) => {
    const target = el.dataset.view;
    // 旧深链 ?view=qqimport 归到统一导入页，侧栏也要跟着亮那一项
    const norm = v === 'qqimport' ? 'share' : v;
    el.classList.toggle('active', norm === target);
  });
  renderPlaylists();
  if (v === 'discover') return renderDiscover();
  if (v === 'search') return renderSearchView();
  if (v === 'share' || v === 'qqimport') return renderShareImportView();
  if (v === 'history') return renderHistoryView();
  if (v === 'download') return renderDownloadView();
  if (v.startsWith('playlist:')) return renderPlaylistView(v.slice(9));
}

/**
 * 内容页头部。
 *
 * 结构分两层：
 *   .head-top    —— 标题固定在左，右侧预留一个操作槽（定位按钮就放这儿）
 *   .head-sub    —— 副标题另起一行
 *
 * 定位按钮之所以要放头部的右上角，而不是像以前那样浮在 #content 右下角：
 * 列表长的时候右下角会被列表内容挤出去 / 被底部播放条压住，等于看不见。
 * 放在标题行里，位置固定、上下留白对称，永远都在。
 */
function contentHead(title, sub, actions) {
  return `<div class="content-head">
    <div class="head-top">
      <h1>${title}</h1>
      <span class="head-slot" id="headSlot"></span>
    </div>
    <div class="sub">${sub || ''}</div>
    ${actions ? `<div class="head-actions">${actions}</div>` : ''}
  </div>`;
}

/**
 * 「播放到哪儿了」定位圆点。
 *
 * 歌单长了以后，正在播的那一行往往滚出可视区，全靠自己翻很烦。
 * 这里在列表右下角浮一个小圆点，点一下就把正在播的那行滚到中间并闪一下。
 * 只有在「正在播的歌确实在这个列表里」时才显示，找不到就没有这个点，避免点了没反应。
 *
 * 圆点挂在 #content 上（position:absolute），所以不随列表滚动、始终停在右下角。
 * 归属列表存在 S.locateList，视图切换时会被清掉。
 */
function locateCurrentSong(sel) {
  const box = $(sel);
  const cur = S.current;
  if (!box || !cur) return;
  const row = $(`.song-row[data-bvid="${cssEsc(cur.bvid)}"]`, box);
  if (!row) return toast('这首歌不在这里');
  // 用滚动容器的可见高度算位置，比 scrollIntoView 稳（不会顺带把外层也滚走）
  const target = row.offsetTop - (box.clientHeight - row.offsetHeight) / 2;
  box.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
  row.classList.remove('locate-flash');
  void row.offsetWidth;                       // 强制重排，保证连点也能重放动画
  row.classList.add('locate-flash');
  setTimeout(() => row.classList.remove('locate-flash'), 1800);
}

/** 选择器里用 bvid，理论上都是 [A-Za-z0-9]，兜一层防止特殊字符把选择器弄坏 */
function cssEsc(t) {
  if (window.CSS && CSS.escape) return CSS.escape(String(t == null ? '' : t));
  return String(t == null ? '' : t).replace(/["\\]/g, '\\$&');
}

/**
 * 重画列表时保住滚动位置。
 *
 * 换源/自动匹配成功后整张列表要重画一遍（新音源、高亮、⇄ 都得跟上）。
 * renderPlaylistView 是把 #content 整块换掉的，列表容器随之变成一个全新节点，
 * 滚动位置天生从 0 开始：正在听第 80 首，列表却跳回第 1 行，用户得自己拖回去
 * 再点一次定位。所以重画前记下 top，重画后再查一次选择器把 top 还给新节点
 * （还回旧节点是无效的，旧节点已经不在文档里了）。
 */
async function keepScroll(sel, redraw) {
  const before = $(sel);
  const top = before ? before.scrollTop : 0;
  await redraw();
  const box = $(sel);
  if (box) box.scrollTop = top;
}

/** 重画当前视图的歌曲列表（换源/匹配失败后要刷新角标和高亮），并保住滚动位置。 */
async function redrawCurrentView() {
  if (S.view === 'discover') return keepScroll('#recentList', renderDiscover);
  if (S.view && S.view.indexOf('playlist:') === 0) {
    return keepScroll('#plList', () => renderPlaylistView(S.view.slice(9)));
  }
  return null;
}

/**
 * 当前视图里承载歌曲列表的容器。
 *
 * 三个视图都可能显示定位按钮，列表容器的 id 各不相同，统一在这里映射，
 * 免得每个调用点都写一遍 if/else 还容易漏。
 * 返回 null 表示当前视图没有可定位的列表（比如导入页、下载页）。
 */
function locateTarget() {
  if (S.view && S.view.startsWith('playlist:')) return { sel: '#plList' };
  if (S.view === 'discover') return { sel: '#recentList' };
  if (S.view === 'search')   return { sel: '#searchList' };
  return null;
}

/**
 * 把定位按钮同步到内容头部的右上角插槽（#headSlot）。
 *
 * 显示判据：正在播的那首歌确实在 S.locateList 里。不在就整个不显示，
 * 免得出现一个"点了没反应"的按钮。
 *
 * 之所以从「浮在 #content 右下角」改到这里：右下角会被长列表挤出可视区、
 * 还会被底部播放条压住，用户反馈"按钮不见了"。放进标题行的插槽后位置固定，
 * 用 flex 的 align-items:center 保证与标题同一中轴线、上下留白相等。
 */
function syncLocateDot(sel) {
  const slot = $('#headSlot');
  if (!slot) return;
  slot.innerHTML = '';                        // 先清掉旧的，避免视图切换后残留
  const list = S.locateList;
  if (!list || !S.current || !list.some((s) => s.bvid === S.current.bvid)) return;
  const btn = document.createElement('button');
  btn.className = 'locate-btn';
  btn.id = 'locateDot';
  btn.title = '定位正在播放的歌曲';
  btn.setAttribute('aria-label', '定位正在播放的歌曲');
  btn.innerHTML = '<span class="lb-dot"></span><span class="lb-text">定位</span>';
  btn.onclick = () => locateCurrentSong(sel);
  slot.appendChild(btn);
}

const LIST_HEAD = `<div class="list-head">
  <div>#</div><div>封面</div><div>音乐标题</div><div>歌手 / UP主</div><div>时长</div><div>操作</div>
</div>`;

function songRow(s, i) {
  const t1 = s.name || s.title || '';
  const t2 = (s.name && s.title && s.name !== s.title)
    ? s.title
    : (s.play ? fmtPlay(s.play) + ' 播放' : (s.album || ''));
  const who = s.artist || s.author || '';
  const playing = S.current && S.current.bvid === s.bvid;
  const unm = isUnmatchedSource(s);
  const stuck = S.noSource.has(s.bvid);
  return `<div class="song-row ${playing ? 'playing' : ''}${unm ? ' unmatched' : ''}${stuck ? ' no-source' : ''}" data-bvid="${esc(s.bvid)}" data-i="${i}">
    <div class="idx">${i + 1}</div>
    <img class="cov" src="${img(s.cover)}" data-fallback="${esc(s.cover)}" loading="lazy">
    <div class="cell-title">
      <div class="t1" title="${esc(t1)}">${esc(t1)}${unm ? '<span class="src-tag" title="导入的歌单，播放时会自动去找能播的版本">导入</span>' : ''}${stuck ? '<span class="src-tag warn" title="自动没找到能播的版本，点右边的 ⇄ 自己挑一个">待选</span>' : ''}</div>
      <div class="t2" title="${esc(t2)}">${esc(t2)}</div>
    </div>
    <div class="up" title="${esc(who)}">${esc(who)}</div>
    <div class="dur">${fmtTime(s.duration)}</div>
    <div class="ops">
      <button data-act="play" title="播放">▶</button>
      <button data-act="add" title="加入歌单">＋</button>
      <button data-act="dl" title="下载到本地">⬇</button>
      <button data-act="open" title="在浏览器打开">↗</button>
    </div>
  </div>`;
}

function bindSongList(rootSel, list, opts) {
  const root = $(rootSel);
  if (!root) return;
  root.onclick = (e) => {
    const row = e.target.closest('.song-row');
    if (!row) return;
    const i = Number(row.dataset.i);
    const song = list[i];
    if (!song) return;
    const act = e.target.dataset ? e.target.dataset.act : null;
    if (act === 'add') return openAddDialog(song);
    if (act === 'dl') return downloadSong(song, e.target);
    if (act === 'open') return openInBrowser(song);
    playSong(song, list, i);
  };
}

/**
 * 「在浏览器打开 ↗」。
 *
 * 占位音源拼不出 B 站视频地址：song.bvid 是 `qq_000ITtbG2Dys9Y` 这种，
 * 硬拼就是 bilibili.com/video/qq_000ITtbG2Dys9Y，B 站返回 404 —— 用户看到
 * 的是一片空白页，完全不知道发生了什么。所以这类歌改成打开 B 站搜索页，
 * 把歌名+歌手先搜出来让用户自己挑，至少不会白页。
 */
function openInBrowser(song) {
  if (isUnmatchedSource(song)) {
    const kw = cleanSwapQuery(song.name || song.title, song.artist || song.author)
      || song.name || song.title || '';
    toast('这首还没有能播的版本，先按歌名搜一下');
    return window.open('https://search.bilibili.com/all?keyword=' + encodeURIComponent(kw));
  }
  return window.open('https://www.bilibili.com/video/' + encodeURIComponent(song.bvid));
}

/* -------------------------------------------------------------- 发现页 */
async function renderDiscover() {
  const box = $('#content');
  const hist = await api('/api/history').then((r) => r.history).catch(() => []);
  const recent = hist.slice(0, 30);
  box.innerHTML = contentHead('发现音乐',
    '输入歌名或歌手开始搜索 · 想搬整张歌单就去「导入音乐与歌单」') +
    `<div class="discover">
      <div class="sec-title">🕘 最近播放</div>
      ${recent.length
        ? `<div class="song-list" id="recentList" style="flex:none;max-height:none">${LIST_HEAD}${recent.map(songRow).join('')}</div>`
        : `<div class="empty"><span class="big">🎵</span>还没有播放记录<br>在上面搜索框里搜一首歌开始吧</div>`}
     </div>`;
  if (recent.length) {
    bindSongList('#recentList', recent);
    // 最近播放里也能定位正在播的那首
    S.locateList = recent;
    syncLocateDot('#recentList');
  }
}

/* -------------------------------------------------------------- 搜索页 */
function renderSearchView() {
  const box = $('#content');
  const r = S.search;
  if (!r.results.length) {
    box.innerHTML = contentHead('搜索结果', '还没有搜索内容') +
      `<div class="empty"><span class="big">🔍</span>在上面的搜索框里输入歌名、歌手或 UP 主<br>回车或点「搜索」</div>`;
    return;
  }
  box.innerHTML = contentHead(`搜索「${esc(r.kw)}」`,
    `第 ${r.page} 页 · 共 ${r.results.length} 条结果`) +
    LIST_HEAD +
    `<div class="song-list" id="searchList">${r.results.map(songRow).join('')}</div>
     <div style="padding:10px 18px;text-align:center;border-top:1px solid var(--border)">
       <button class="pill-btn plain" id="moreBtn">加载更多</button>
     </div>`;
  bindSongList('#searchList', r.results);
  // 搜索结果里同样可以定位正在播的那首
  S.locateList = r.results;
  syncLocateDot('#searchList');
  $('#moreBtn').onclick = () => doSearch(r.kw, r.page + 1, true);
}

async function doSearch(kw, page, append) {
  kw = (kw || '').trim();
  if (!kw) return;
  if (!append) { S.search = { kw, order: $('#orderSel').value, page: 1, results: [] }; showView('search'); }
  $('#content').innerHTML = contentHead(`搜索「${esc(kw)}」`, '正在搜索…') +
    `<div class="empty"><span class="big">⏳</span>正在搜索…</div>`;
  try {
    const r = await api('/api/search?q=' + encodeURIComponent(kw) +
      '&page=' + (page || 1) + '&order=' + (S.search.order || 'totalrank'));
    let res = r.results || [];
    if (append) res = S.search.results.concat(res);
    S.search.results = res;
    S.search.page = page || 1;
    if (!res.length) {
      $('#content').innerHTML = contentHead(`搜索「${esc(kw)}」`, '没有结果') +
        `<div class="empty"><span class="big">😶</span>没搜到东西，换个关键词试试</div>`;
      return;
    }
    renderSearchView();
  } catch (e) {
    $('#content').innerHTML = contentHead('搜索失败', '') +
      `<div class="empty"><span class="big">⚠️</span>${esc(e.message)}</div>`;
  }
}

/* -------------------------------------------------------------- 歌单页 */
async function renderPlaylistView(pid) {
  const box = $('#content');
  let pl;
  try { pl = (await api('/api/playlist?id=' + encodeURIComponent(pid))).playlist; }
  catch (e) { box.innerHTML = contentHead('歌单不存在', ''); return; }

  const total = (pl.songs || []).reduce((a, s) => a + (s.duration || 0), 0);
  box.innerHTML = contentHead(esc(pl.name),
    `${pl.songs.length} 首歌 · 总时长 ${fmtTime(total)}` +
    (pl.source === 'qqmusic' ? ' · 导入的歌单' : ''),
    `<button class="pill-btn" id="playAllBtn">▶ 播放全部</button>
     <button class="pill-btn plain" id="renameBtn">重命名</button>
     <button class="pill-btn plain" id="exportBtn">导出列表</button>`) +
    (pl.songs.length ? LIST_HEAD + `<div class="song-list" id="plList">${pl.songs.map(songRow).join('')}</div>`
      : `<div class="empty"><span class="big">🎵</span>这个歌单还是空的<br>去搜索，或者到「导入音乐与歌单」搬一张过来</div>`);

  S.locateList = pl.songs;                      // 供定位圆点判断「正在播的歌在不在这个列表里」
  syncLocateDot('#plList');

  if (pl.songs.length) {
    bindSongList('#plList', pl.songs);
    $$('#plList .song-row').forEach((row) => {
      const bvid = row.dataset.bvid;
      const song = pl.songs.find((s) => s.bvid === bvid);
      const ops = $('.ops', row);

      // 换音源
      const swap = document.createElement('button');
      swap.textContent = '⇄';
      swap.title = '换一个音源（匹配不对时用）';
      swap.onclick = (e) => {
        e.stopPropagation();
        openSwapDialog(
          song || { bvid, title: '', duration: 0 },
          async (pick) => {
            const newSong = {
              bvid: pick.bvid,
              title: pick.title,
              name: song ? (song.name || song.title) : pick.title,
              artist: song ? (song.artist || pick.author) : pick.author,
              album: song ? song.album : '',
              duration: pick.duration,
              cover: pick.cover,
              play: pick.play,
              source: song ? song.source : 'bili',
            };
            try {
              const r = await api('/api/playlist/replace',
                { id: pid, old_bvid: bvid, song: newSong });
              toast('已换成这个版本', 'ok');

              // 换的如果正是当前这首歌，必须让它真的用新音源重新出声 ——
              // 只改 S.current 而不重载音频的话，你听到的还是旧的那条，
              // 看起来就像"换了没用 / 换源失败"。
              const isCurrent = S.current && S.current.bvid === bvid;
              if (isCurrent) {
                const wasPlaying = !audio.paused;
                // 队列里那条也要一起换掉，否则下一轮播放又回到旧音源
                const qi = S.queue.findIndex((x) => x.bvid === bvid);
                if (qi >= 0) S.queue[qi] = Object.assign({}, S.queue[qi], newSong);
                S.current = Object.assign({}, S.current, newSong);
                renderNowPlaying();
                renderQueue();
                // 关键：告诉 loadAndPlay 这是「同一首歌换音源重载」，
                // 失败时只弹提示、绝不自动跳下一首（旧版跳曲就是这里缺这个标记）。
                loadAndPlay(S.current, { reloadSame: true, keepPaused: !wasPlaying });
              }
              await keepScroll('#plList', () => renderPlaylistView(pid));
            } catch (err) { toast(err.message, 'err'); }
          });
      };
      ops.appendChild(swap);

      // 从歌单移除
      const del = document.createElement('button');
      del.textContent = '✕';
      del.title = '从歌单移除';
      del.onclick = async (e) => {
        e.stopPropagation();
        try {
          await api('/api/playlist/remove', { id: pid, bvid });
          renderPlaylistView(pid);
        } catch (err) { toast(err.message, 'err'); }
      };
      ops.appendChild(del);
    });
    $('#playAllBtn').onclick = () => {
      if (!pl.songs.length) return;
      playSong(pl.songs[0], pl.songs, 0);
    };
  }
  $('#renameBtn').onclick = async () => {
    const name = prompt('新名字：', pl.name);
    if (!name) return;
    try {
      const r = await api('/api/playlist/rename', { id: pid, name });
      S.playlists = r.playlists;
      renderPlaylists();
      renderPlaylistView(pid);
      toast('已重命名', 'ok');
    } catch (e) { toast(e.message, 'err'); }
  };
  $('#exportBtn').onclick = () => openShareDialog(pid, pl);
}

/* -------------------------------------------------------- 歌单分享（TXT） */
/*
 * 线协议与手机端（coconut-apk）完全一致，格式真相在 server 端 share.py 顶部注释里。
 * 前端只负责「拿到文本 / 交给后端写盘 / 收下来解析入库」这三件事，
 * 一切编解码都走服务端，保证 PC 与手机产出同一串字节。
 */

/** 分享弹窗：一个歌单 → 一份 TXT */
async function openShareDialog(pid, pl) {
  if (!pl || !(pl.songs || []).length) return toast('这个歌单是空的，没什么可分享', 'err');
  let info;
  try {
    info = await api('/api/playlist/share?id=' + encodeURIComponent(pid));
  } catch (e) { return toast(e.message, 'err'); }

  openModal(`
    <h2>分享歌单「${esc(info.name)}」</h2>
    <p class="hint" style="margin-bottom:12px">
      导出的 TXT 与手机版椰子音乐<b>完全通用</b>：发给对方，在手机 App 的「导入」里选这个文件就能整份搬过去。
    </p>
    <div class="share-preview" id="sharePreview">${esc(info.text)}</div>
    <div class="share-actions">
      <button class="pill-btn" id="shareSaveBtn">导出为 TXT 文件</button>
      <button class="pill-btn plain" id="shareOpenBtn">导出并打开文件</button>
      <button class="pill-btn plain" id="shareCopyBtn">复制分享文本</button>
      <button class="pill-btn plain" id="shareCloseBtn">关闭</button>
    </div>
    <div class="hint" id="shareTip" style="margin-top:10px">共 ${info.count} 首 · 文件名 ${esc(info.file)}</div>`);

  $('#shareCloseBtn').onclick = closeModal;
  $('#shareCopyBtn').onclick = async () => {
    try {
      await navigator.clipboard.writeText(info.text);
      toast('分享文本已复制，直接粘贴给对方也行', 'ok');
    } catch (e) { toast('复制不了，请手动全选上面那段文字', 'err'); }
  };
  $('#shareSaveBtn').onclick = () => saveShareFile(pid, false);
  $('#shareOpenBtn').onclick = () => saveShareFile(pid, true);
}

/** 交给后端写盘（UTF-8 无 BOM / LF / 末尾无换行 —— 与手机端逐字节一致） */
async function saveShareFile(pid, openAfter) {
  const btn = openAfter ? $('#shareOpenBtn') : $('#shareSaveBtn');
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '导出中…'; }
  try {
    const r = await api(openAfter ? '/api/playlist/share_open' : '/api/playlist/share_save', { id: pid });
    if (openAfter) {
      toast('已导出并用系统打开：' + r.name, 'ok');
      closeModal();
    } else {
      const tip = $('#shareTip');
      if (tip) tip.innerHTML = `已保存到：<b>${esc(r.path)}</b><br>去聊天窗口把这个文件发给对方即可。`;
      toast('已导出 ' + r.name, 'ok');
    }
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old; }
  }
}

/**
 * 统一导入：一个输入框，三种来源自动识别。
 *
 * 判据与手机版椰子音乐的 doImport()/checkClipboard() 完全一致（顺序也不能变）：
 *   1. COCONUTPL1:  → 椰子歌单分享（本地解码，最先认）
 *   2. qq.com 系 或 纯数字 ID → QQ 音乐歌单
 *   3. bilibili/b23.tv/BV 号/av 号 → B 站单曲
 * 认不出来就明确报错，不悄悄当成搜索词。
 *
 * 识别放在服务端（linkparse.detect），此处只负责"边打字边给出类型提示"。
 */
function renderShareImportView() {
  const box = $('#content');
  box.innerHTML = contentHead('导入音乐与歌单', '一个框搞定：椰子分享 / 外部歌单 / 视频链接') +
    `<div class="import-wrap"><div class="import-box">
      <p class="hint">
        <b>直接把东西粘进来，我来认：</b><br>
        · <b>椰子歌单分享</b> —— 手机版导出的 TXT 内容（或选那个 TXT 文件）<br>
        · <b>外部歌单</b> —— 歌单页链接 / 分享短链 / 纯歌单 ID<br>
        · <b>视频单曲</b> —— 视频链接 / 视频编号（支持 ?p=2 选分 P）<br>
        粘进去就会自动告诉你识别成了哪一种。
      </p>
      <div class="auto-kind" id="autoKind"><span class="ak-dot"></span><span class="ak-tx">等待输入…</span></div>
      <textarea id="shareInput" placeholder="在这里粘贴：COCONUTPL1:… / 歌单链接 / 视频链接或视频编号"></textarea>
      <div class="head-actions">
        <button class="pill-btn" id="shareParseBtn">开始导入</button>
        <button class="pill-btn plain" id="sharePickBtn">选择 TXT 文件</button>
        <button class="pill-btn plain" id="sharePasteBtn">从剪贴板粘贴</button>
      </div>
      <div id="sharePreview"></div>
    </div></div>`;

  const inp = $('#shareInput');
  $('#shareParseBtn').onclick = () => doShareImport();
  $('#sharePickBtn').onclick = pickShareFile;
  $('#sharePasteBtn').onclick = async () => {
    try {
      const t = await navigator.clipboard.readText();
      if (t) { inp.value = t; refreshKind(); toast('已粘贴，点「开始导入」'); }
      else toast('剪贴板是空的');
    } catch (e) {
      toast('读不到剪贴板，请手动 Ctrl+V 粘贴', 'err');
      inp.focus();
    }
  };

  // 输入时实时给类型提示（本地粗判，服务端再精确判一次）
  let kindTimer = null;
  inp.addEventListener('input', () => {
    clearTimeout(kindTimer);
    kindTimer = setTimeout(refreshKind, 180);
  });
  refreshKind();
}

/** 本地粗判来源 —— 正则与后端 linkparse.py / 手机版 app.js 完全同一套 */
function guessKind(t) {
  const s = String(t || '').trim();
  if (!s) return null;
  if (s.includes('COCONUTPL1:')) return { k: 'share', label: '椰子歌单分享', cls: 'ok' };
  if (/y\.qq\.com|qq\.com|qqmusic|qq\.music|musicu\.fcg/i.test(s) || /^\d{6,}$/.test(s))
    return { k: 'qq', label: '外部歌单', cls: 'ok' };
  if (/bilibili\.com|b23\.tv|BV1[0-9A-Za-z]{9}|av\d{5,}/i.test(s) || /BV[0-9A-Za-z]{10}/.test(s))
    return { k: 'bili', label: '视频链接', cls: 'ok' };
  return { k: 'unknown', label: '认不出来（不是椰子分享 / 歌单链接 / 视频链接）', cls: 'bad' };
}

function refreshKind() {
  const el = $('#autoKind');
  const inp = $('#shareInput');
  if (!el || !inp) return;
  const g = guessKind(inp.value);
  el.className = 'auto-kind' + (g ? (g.cls === 'ok' ? ' is-ok' : ' is-bad') : '');
  el.querySelector('.ak-tx').textContent = g ? ('识别为：' + g.label) : '等待输入…';
}

/** 系统「打开文件」对话框挑 TXT，服务端读出来回填 */
async function pickShareFile() {
  const btn = $('#sharePickBtn');
  if (btn) { btn.disabled = true; btn.textContent = '选择中…'; }
  try {
    const r = await api('/api/playlist/share_pick', {});
    if (!r.picked) return;                       // 用户取消，不打扰
    $('#shareInput').value = r.text || '';
    refreshKind();
    toast('已读取：' + (r.path || '').split(/[\\/]/).pop());
    doShareImport();
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '选择 TXT 文件'; }
  }
}

/**
 * 导入主流程 —— 交给 /api/import/auto，由后端按同一套判据分流。
 *
 * 失败提示刻意按来源分开：认不出 / 是分享但坏了 / 是 QQ 但读不到 / 是 B 站但没 BV 号，
 * 各自给能指导下一步的话。笼统报"导入失败"用户没法自救。
 */
async function doShareImport() {
  const raw = ($('#shareInput').value || '').trim();
  if (!raw) return toast('先把内容粘贴进来，或者选一个 TXT 文件', 'err');
  const pv = $('#sharePreview');
  const g = guessKind(raw);
  pv.innerHTML = `<div class="empty" style="padding:26px"><span class="big">⏳</span>${
    g ? '正在导入' + esc(g.label) + '…' : '正在识别并导入…'}</div>`;
  const btn = $('#shareParseBtn');
  if (btn) { btn.disabled = true; btn.textContent = '导入中…'; }
  try {
    const r = await api('/api/import/auto', { text: raw });
    S.playlists = r.playlists || S.playlists;
    renderPlaylists();
    const extra = r.skipped ? ` · 跳过 ${r.skipped} 条无音源信息` : '';
    const detail = r.total && r.added < r.total
      ? `共 ${r.total} 首，已导入 ${r.added} 首（其余是重复项）`
      : `已导入 ${r.added} 首`;
    pv.innerHTML = `
      <div class="imp-preview">
        <div class="ph">
          <div><b>${esc(r.name)}</b>
            <span>来源：${esc(r.label || '')} · ${detail}${extra}</span></div>
        </div>
        <div style="padding:14px 16px">
          <button class="pill-btn" id="shareGoBtn">打开这个歌单</button>
        </div>
      </div>`;
    $('#shareGoBtn').onclick = () => showView('playlist:' + r.playlist.id);
    toast(detail, 'ok');
    setTimeout(() => showView('playlist:' + r.playlist.id), 800);
  } catch (e) {
    pv.innerHTML = `<div class="empty" style="padding:26px"><span class="big">⚠️</span>${esc(e.message)}</div>`;
    toast(e.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '开始导入'; }
  }
}

/* -------------------------------------------------------------- 历史页 */
async function renderHistoryView() {
  const box = $('#content');
  const hist = await api('/api/history').then((r) => r.history).catch(() => []);
  box.innerHTML = contentHead('播放历史', `最近 ${hist.length} 首`,
    hist.length ? `<button class="pill-btn plain" id="clearHistBtn">清空历史</button>` : '') +
    (hist.length ? LIST_HEAD + `<div class="song-list" id="histList">${hist.map(songRow).join('')}</div>`
      : `<div class="empty"><span class="big">🕘</span>还没有播放记录</div>`);
  if (hist.length) {
    bindSongList('#histList', hist);
    $('#clearHistBtn').onclick = async () => {
      if (!confirm('清空全部播放历史？')) return;
      await api('/api/history/clear', {});
      renderHistoryView();
      toast('已清空', 'ok');
    };
  }
}

/* ------------------------------------------------------------ 本地下载 */
function buildFileName(song) {
  const artist = String(song.artist || song.author || '').trim();
  const name = String(song.name || song.title || '未命名').trim();
  const fn = ((artist && artist !== name) ? `${artist} - ${name}` : name)
    .replace(/[\\/:*?"<>|\r\n\t]/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 110);
  return (fn || '未命名') + '.m4a';
}

async function downloadSong(song, btnEl) {
  if (!song || !song.bvid) return;
  const old = btnEl ? btnEl.textContent : null;
  if (btnEl) { btnEl.disabled = true; btnEl.textContent = '…'; }
  try {
    // 占位音源直接拿去下载只会 404（bvid 是 qq_xxx），先补一个真实音源
    if (isUnmatchedSource(song)) {
      toast('正在给这首找能播的版本…');
      Object.assign(song, await ensureSongSource(song));
    }
    const filename = buildFileName(song);
    toast('正在下载并转成 MP3：' + filename);
    const r = await api('/api/download/song', {
      bvid: song.bvid,
      cid: song.cid,
      dir: S.settings.downloadDir || '',
      filename,
    });
    toast(r.converted
      ? `已存为 MP3：${r.name}（${(r.size / 1048576).toFixed(1)} MB）`
      : `已保存 ${r.name}（${(r.size / 1048576).toFixed(1)} MB · ${r.quality || ''}），但转 MP3 没成功，先留了原始格式`,
      r.converted ? 'ok' : 'err');
  } catch (e) {
    toast('下载失败：' + e.message, 'err');
  } finally {
    if (btnEl) { btnEl.disabled = false; btnEl.textContent = old || '⬇'; }
  }
}

async function renderDownloadView() {
  const box = $('#content');
  let st = {};
  try { st = await api('/api/download/status'); } catch (e) { /* 忽略 */ }
  const cur = st.path || '';
  const freeGB = st.free ? (st.free / 1073741824).toFixed(1) : '—';
  box.innerHTML = contentHead('下载音乐', '把喜欢的歌存到本地，离线也能听') +
    `<div class="dl-wrap">
      <div class="dl-card">
        <h3>保存位置</h3>
        <div class="dl-sub">默认存在桌面。改成任何你想要的文件夹都行，改完自动记住。</div>
        <div class="dl-path">
          <input id="dlDir" type="text" value="${esc(cur)}" placeholder="例如 D:\\音乐">
          <button class="pill-btn" id="dlPickBtn">浏览…</button>
          <button class="pill-btn plain" id="dlSaveBtn">就用这个位置</button>
          <button class="pill-btn plain" id="dlOpenBtn">打开文件夹</button>
        </div>
        <div class="dl-quick">
          <button class="chip" data-dir="Desktop">桌面</button>
          <button class="chip" data-dir="Downloads">下载</button>
          <button class="chip" data-dir="Music">音乐</button>
          <button class="chip" data-dir="Documents">文档</button>
        </div>
        <div class="dl-tip">${st.ok
          ? `当前可用 · 该磁盘剩余约 <b>${freeGB} GB</b>`
          : '<span style="color:var(--danger)">这个位置不可写，换一个吧</span>'}</div>
      </div>

      <div class="dl-card">
        <h3>单曲下载</h3>
        <div class="dl-sub">
          在<b>搜索结果</b>、<b>歌单</b>、<b>播放历史</b>里把鼠标移到任意一行，
          点右边的 <b>⬇</b> 就会存到上面这个位置。
        </div>
        <div class="dl-tip">
          • 文件名按「<b>歌手 - 歌名</b>」自动命名，重名会加序号，不会覆盖已有文件<br>
          • 格式为 <b>${esc(st.ext || '.mp3')}</b>（音源是 192K AAC，下完自动转成 320K MP3，手机 / 车机 / 剪辑软件都能直接吃）<br>
          • 转码一首约 2~5 秒，转完自动删掉中间的 .m4a<br>
          • 下载<b>不影响播放</b>，可以边听边下
        </div>
      </div>
    </div>`;

  const setPath = async (p) => {
    if (!p) return toast('先填一个文件夹路径', 'err');
    try {
      const r = await api('/api/download/check', { dir: p });
      if (!r.ok) return toast('这个位置用不了：' + (r.error || '未知原因'), 'err');
      S.settings.downloadDir = r.path;
      toast('保存位置已改为：' + r.path, 'ok');
      renderDownloadView();
    } catch (e) { toast(e.message, 'err'); }
  };

  $('#dlSaveBtn').onclick = () => setPath($('#dlDir').value.trim());
  $('#dlDir').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') setPath($('#dlDir').value.trim());
  });
  $('#dlOpenBtn').onclick = async () => {
    try { await api('/api/download/open', { dir: $('#dlDir').value.trim() }); }
    catch (e) { toast(e.message, 'err'); }
  };
  $$('.dl-quick .chip').forEach((c) => {
    c.onclick = () => setPath('~/' + c.dataset.dir);
  });

  $('#dlPickBtn').onclick = async () => {
    const b = $('#dlPickBtn');
    b.disabled = true;
    b.textContent = '请在弹出的窗口里选…';
    // 系统对话框有被挡住的风险，加个超时避免按钮一直卡着
    const req = fetch('/api/download/pick', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    }).then((r) => r.json());
    const timeout = new Promise((_, rej) => setTimeout(
      () => rej(new Error('等待超时（窗口可能被挡住了，直接在输入框里填路径也一样）')), 120000));
    try {
      const r = await Promise.race([req, timeout]);
      if (r.picked) { toast('保存位置已改为：' + r.path, 'ok'); renderDownloadView(); }
      else toast('没有选择新的文件夹');
    } catch (e) {
      toast(e.message, 'err');
    } finally {
      b.disabled = false;
      b.textContent = '浏览…';
    }
  };
}

/* -------------------------------------------------------------- 播放器 */
function playSong(song, list, index) {
  if (Array.isArray(list) && list.length) {
    S.queue = list.slice();
    S.qi = typeof index === 'number' ? index : 0;
  } else {
    if (!S.queue.length) { S.queue = [song]; S.qi = 0; }
    else {
      const i = S.queue.findIndex((x) => x.bvid === song.bvid);
      if (i >= 0) S.qi = i;
      else { S.queue.push(song); S.qi = S.queue.length - 1; }
    }
  }
  loadAndPlay(S.queue[S.qi]);
}

let audioAbort = null;      // 用来中断上一首还没下完的请求
let audioBlobUrl = null;    // 上一首的 blob 地址，换歌时释放

/**
 * 播放一首歌。
 *
 * @param song 要播的歌
 * @param opts.reloadSame  这次是「同一首歌换了音源后重载」，不是切歌。
 *   差别很重要：取流/播放失败时，切歌场景应该跳下一首（别卡死），
 *   但换源重载场景绝不能跳 —— 那会表现成「换源成功却立刻跳到下一首 + 弹红字」，
 *   而且换都换好了，跳走等于白换。所以这种情况只提示、不跳曲。
 * @param opts.keepPaused  重载后保持暂停（用户原本就没在播）
 * @param opts.auto  这次是「上一首播不出来/播完了」自动挪过来的，不是用户手动点的。
 *   手动的一律重新给连跳额度；自动的要留着计数，好知道什么时候该停下来问用户。
 */
function loadAndPlay(song, opts) {
  if (!song) return;
  const o = opts || {};
  if (!o.auto) S.autoSkips = 0;      // 用户自己点的/手动切歌，重新给一次连跳额度
  S.current = song;
  renderNowPlaying();
  renderQueue();
  $$('.song-row').forEach((r) => r.classList.toggle('playing', r.dataset.bvid === song.bvid));
  $$('.q-item').forEach((r) => r.classList.toggle('playing', r.dataset.bvid === song.bvid));
  // 切歌后重新判断要不要显示定位按钮；刚点播的那一行顺手闪一下。
  // 列表容器随视图变化（歌单页 #plList / 发现页 #recentList / 搜索页 #searchList），
  // 所以用 locateTarget() 统一取，别硬编码某一个 id。
  const lt = locateTarget();
  if (lt) {
    syncLocateDot(lt.sel);
    if (S.locateList && S.locateList.some((s) => s.bvid === song.bvid)) {
      const row = $(`.song-row[data-bvid="${cssEsc(song.bvid)}"]`, $(lt.sel));
      if (row) {
        row.classList.add('locate-flash');
        setTimeout(() => row.classList.remove('locate-flash'), 1800);
      }
    }
  }

  // 首次播放（用户手势）时建立 Web Audio 图并唤醒它，响度均衡才能生效
  const g = ensureAudioGraph();
  if (g && g.ctx.state === 'suspended') g.ctx.resume().catch(() => {});
  // 换歌时把增益拉回中性，免得上一首的增益（可能 2.2x）把新歌轰一下
  reapplyVolume();
  updateLoudBtn();

  // 占位音源（QQ 歌单「秒导」进来的）先去补一个真实的 B 站音源再播。
  // 换源重载（reloadSame）时音源已经是真的，不走这条路。
  if (isUnmatchedSource(song) && !o.reloadSame) {
    resolveSourceThenPlay(song, o);
  } else {
    startPlayback(song, o);
  }
}

/**
 * 占位音源第一次播放：先自动匹配 B 站音源，匹配到了才开始取流。
 *
 * 匹配不到就交给 autoNext 跳下一首（连跳上限见那里）—— 停在原地不出声最让人
 * 摸不着头脑，用户会以为软件卡死了。跳走时会点名是哪首没匹配上，不算偷偷摸摸。
 */
async function resolveSourceThenPlay(song, o) {
  const oldBvid = song.bvid;
  const tag = $('#qualityTag');
  if (tag) tag.textContent = '匹配音源…';
  // 自动续播碰上刚才已经搜过、没搜到的歌：直接跳，别再去刷一遍搜索接口。
  if (o.auto && S.noSource.has(oldBvid)) {
    if (tag) tag.textContent = '音质 —';
    setTimeout(autoNext, 250);
    return;
  }
  toast('这首来自导入的歌单，正在找能播的版本…');
  let matched;
  try {
    matched = await ensureSongSource(song);
  } catch (e) {
    if (tag) tag.textContent = '音质 —';
    S.noSource.add(oldBvid);
    toast(`《${song.name || song.title || '这首'}》没找到能播的版本，先跳下一首`, 'err');
    console.warn('自动匹配音源失败', oldBvid, e);
    await redrawCurrentView();      // 让「待选」角标马上出现在那一行
    setTimeout(autoNext, 1200);
    return;
  }
  if (S.current !== song) return;              // 用户已经切走了，这次结果丢掉
  const merged = Object.assign({}, song, matched);
  // 队列里那条也要换掉，否则下一轮播放又拿旧的占位音源去取流
  const qi = S.queue.findIndex((x) => x.bvid === oldBvid);
  if (qi >= 0) S.queue[qi] = merged;
  S.current = merged;
  renderNowPlaying();
  renderQueue();
  // 列表里那一行现在应该显示新音源（歌名/歌手仍是 QQ 那边的，不变），
  // 重画一次让 data-bvid、高亮、⇄ 都跟上，免得再点一次又去匹配。
  await redrawCurrentView();
  toast('找到能播的版本了', 'ok');
  startPlayback(merged, o);
}

/** 真正开始取流播放，并把右下角「音质」标签补上 */
function startPlayback(song, o) {
  loadAudioBlob(song, o);
  api('/api/history/add', { song }).catch(() => {});

  const tag = $('#qualityTag');
  if (tag) tag.textContent = '音质 …';
  // 慢请求回来时可能已经切歌了，按 bvid 核对一下再写标签
  const still = () => S.current && S.current.bvid === song.bvid;
  fetch('/api/stream?bvid=' + encodeURIComponent(song.bvid))
    .then((r) => r.json())
    .then((js) => {
      if (!still()) return;
      if (js.ok) {
        if (tag) tag.textContent = '音质 ' + js.quality;
        if (js.cid && !song.cid) song.cid = js.cid;
      } else if (tag) {
        tag.textContent = js.placeholder ? '音质 —' : '音质 未知';
      }
    })
    .catch(() => { if (still() && tag) tag.textContent = '音质 未知'; });
}

/**
 * 用 fetch 把整首音频取回来，再交给 <audio> 播放。
 *
 * 为什么不直接让 audio.src 指向 /api/audio：
 *   下载管理器（NDM / IDM 这类）的浏览器扩展会嗅探音频类请求，每次播放都弹一条下载浮条，
 *   在屏幕上闪来闪去很干扰。改成 fetch 拿 blob 后，请求由页面脚本发起、播放走 blob: 地址，
 *   扩展就看不见这个流了。
 * 代价：要缓冲 1~3 秒才开始播（一首歌 5 MB 左右），换来的是彻底安静。
 */
async function loadAudioBlob(song, opts) {
  const o = opts || {};
  if (audioAbort) { try { audioAbort.abort(); } catch (e) { /* 忽略 */ } }
  audioAbort = new AbortController();
  const url = '/api/audio?bvid=' + encodeURIComponent(song.bvid) +
    (song.cid ? '&cid=' + song.cid : '');
  const tag = $('#qualityTag');
  if (tag) tag.textContent = '缓冲中…';
  try {
    const resp = await fetch(url, { signal: audioAbort.signal });
    if (!resp.ok) {
      // 后端失败时回的是 {"ok":false,"error":"…中文…"}，把它读出来直接给用户看。
      // 只抛 `HTTP 500` 的话用户完全没有线索，也不知道下一步该干嘛。
      let msg = 'HTTP ' + resp.status;
      try {
        const j = await resp.json();
        if (j && j.error) msg = j.error;
      } catch (e2) { /* 不是 JSON（比如网关错误页），就用状态码 */ }
      throw new Error(msg);
    }
    const blob = await resp.blob();
    if (S.current !== song) return;              // 已经切歌了，这次结果丢掉
    if (audioBlobUrl) URL.revokeObjectURL(audioBlobUrl);
    audioBlobUrl = URL.createObjectURL(blob);
    audio.src = audioBlobUrl;
    if (o.keepPaused) {                          // 换源时用户本来就没在播，别擅自开声
      try { audio.pause(); } catch (e) { /* 忽略 */ }
      return;
    }
    // 注意：换源重载（reloadSame）时 audio 里还挂着上一版音源的 blob，
    // 换 src 的瞬间 play() 可能被中断而 reject。这时绝不能当成"播不了"去跳曲 ——
    // 音源其实已经换好了，跳走等于白换，用户看到的就是"换源成功却跳到下一首 + 弹红字"。
    const onFail = (e) => {
      if (o.reloadSame) {
        toast('刚换的这个版本放不出来，可以再换一个', 'err');
        console.warn('换源后播放失败', e);
      } else {
        toast('这首放不出来，正在试下一首', 'err');
        console.warn('播放失败', e);
        setTimeout(() => next(true), 1200);
      }
    };
    const p = audio.play();
    if (p && p.catch) p.catch(onFail);
  } catch (e) {
    if (e.name === 'AbortError') return;         // 主动取消，不是错误
    if (tag) tag.textContent = '音质 —';
    if (o.reloadSame) {
      // 换源后重载取流失败：音源已经写进歌单了，只提示，不跳曲
      toast('刚换的这个版本下载不下来，可以再换一个', 'err');
      console.warn('换源后取流失败', e);
    } else {
      toast('这首下载不下来，正在试下一首', 'err');
      console.warn('取流失败', e);
      setTimeout(() => next(true), 1500);
    }
  }
}

function renderNowPlaying() {
  const s = S.current;
  const cov = (s && s.cover) ? `<img src="${img(s.cover)}" onerror="this.style.display='none'">` : '<span class="ph">♪</span>';
  $('#nowCover').innerHTML = cov;
  $('#pCover').innerHTML = cov;
  $('#nowTitle').textContent = s ? (s.name || s.title) : '还没有播放歌曲';
  $('#pTitle').textContent = s ? (s.name || s.title) : '未在播放';
  $('#nowArtist').textContent = s ? (s.artist || s.author || '') : '从搜索开始吧';
  $('#pArtist').textContent = s ? (s.artist || s.author || '') : '—';
  $('#nowTags').innerHTML = s
    ? `<span class="tag">在线音源</span>` +
      (s.album ? `<span class="tag gray">${esc(s.album)}</span>` : '') +
      (s.duration ? `<span class="tag gray">${fmtTime(s.duration)}</span>` : '')
    : '';
  const faved = S.playlists.find((p) => p.id === 'fav');
  $('#favNowBtn').textContent = '收藏';
  document.title = s ? `${s.name || s.title} · 椰子音乐` : '椰子音乐';
}

function renderQueue() {
  const box = $('#queueList');
  if (!S.queue.length) { box.innerHTML = '<div style="padding:16px;color:var(--text-3);font-size:12px">队列是空的</div>'; return; }
  box.innerHTML = S.queue.map((s, i) => `
    <div class="q-item ${S.current && S.current.bvid === s.bvid ? 'playing' : ''}" data-i="${i}" data-bvid="${esc(s.bvid)}">
      <span style="width:16px;text-align:center;font-size:11px">${S.current && S.current.bvid === s.bvid ? '♪' : i + 1}</span>
      <span class="qt" title="${esc(s.name || s.title)}">${esc(s.name || s.title)}</span>
      <span class="qx" data-x="${i}">✕</span>
    </div>`).join('');
  box.onclick = (e) => {
    const x = e.target.dataset ? e.target.dataset.x : null;
    if (x !== null && x !== undefined) {
      const i = Number(x);
      S.queue.splice(i, 1);
      if (i < S.qi) S.qi--;
      else if (i === S.qi) { S.qi = Math.min(S.qi, S.queue.length - 1); }
      renderQueue();
      return;
    }
    const it = e.target.closest('.q-item');
    if (!it) return;
    S.qi = Number(it.dataset.i);
    loadAndPlay(S.queue[S.qi]);
  };
}

function next(auto) {
  if (!S.queue.length) return;
  const mode = S.settings.playMode;
  if (mode === 'shuffle') S.qi = Math.floor(Math.random() * S.queue.length);
  else if (S.qi + 1 < S.queue.length) S.qi++;
  else if (mode === 'list') S.qi = 0;
  else S.qi = auto ? 0 : S.qi;
  loadAndPlay(S.queue[S.qi], { auto: !!auto });
}

/**
 * 一首歌播不出来时往后挪一位，而不是停在原地不动。
 *
 * 上限是连跳 2 首：连着几首都取不到音源，多半是断网、B 站挂了或没登录，
 * 继续跳只会几十次地刷搜索接口（容易撞风控），不如停下来让人看一眼。
 * 计数器在一首歌真正播起来时（playing）和用户手动切歌时归零。
 */
function autoNext() {
  if (S.settings.playMode === 'single') return;   // 单曲循环本来就该停在这首
  if (S.autoSkips >= 2) {
    S.autoSkips = 0;
    toast('连着 2 首都播不出来，先停在这里。按 ⏭ 可以再往后试，或检查网络 / 登录状态', 'err');
    return;
  }
  S.autoSkips++;
  next(true);
}
function prev() {
  if (!S.queue.length) return;
  if (audio.currentTime > 4) { audio.currentTime = 0; return; }
  S.qi = S.qi > 0 ? S.qi - 1 : S.queue.length - 1;
  loadAndPlay(S.queue[S.qi]);
}
function togglePlay() {
  if (!S.current) {
    if (S.queue.length) { loadAndPlay(S.queue[S.qi < 0 ? 0 : S.qi]); }
    else toast('先从搜索结果里点一首歌吧');
    return;
  }
  if (audio.paused) audio.play().catch((e) => toast('播放失败：' + e.message, 'err'));
  else audio.pause();
}

function setMode(mode) {
  S.settings.playMode = mode;
  const txt = { list: '🔁', single: '🔂', shuffle: '🔀' };
  const tips = { list: '列表循环', single: '单曲循环', shuffle: '随机播放' };
  $('#modeBtn').textContent = txt[mode] || '🔁';
  $('#modeBtn').title = tips[mode] || '列表循环';
  api('/api/settings', { key: 'playMode', value: mode }).catch(() => {});
}

/* ---------------------------------------------------------- 换音源弹窗 */
/**
 * 把「歌名 + 歌手」整成干净的搜索关键词，供换源弹窗用。
 *
 * 必须和后端 bili.build_search_query 保持一致的清理规则，否则会出现
 * 「后端清理后能搜到、前端默认词却搜不到」的诡异现象。两个实测坑：
 *   1. 歌名里的撇号 / 引号（It's、Don't）会让 B 站搜不到 → 换成空格
 *   2. QQ 音乐带过来的歌手常是「高橋あず美、Lotus Juice、ATLUS...」多人串，
 *      全塞进去反而搜不准 → 只取第一位
 * 另外去掉 feat./ft. 之后的部分和各类括号。
 */
function cleanSwapQuery(title, artist) {
  let t = String(title || '')
    .replace(/['’‘"“”`]/g, ' ')
    .replace(/[（）()\[\]【】{}<>《》]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

  let a = String(artist || '').split(/[、,，/&＆|；;]/)[0].trim();
  a = a.replace(/\s*(?:feat|ft|with)\.?\s*.*$/i, '');
  a = a.replace(/['’‘"“”`]/g, ' ').trim();

  return `${t} ${a}`.trim();
}

/** 打开「换一个音源」：默认用歌名+歌手搜一次，也可以自己改关键词再搜 */
function openSwapDialog(song, onPick) {
  const nowTitle = song.title || song.name || '';
  const defaultQ = cleanSwapQuery(song.name || song.title, song.artist || song.author);

  openModal(`
    <h3>换一个音源</h3>
    <div class="m-sub swap-now">
      <div class="swap-now-row">
        <span class="swap-now-k">当前音源</span>
        <span class="swap-now-v" title="${esc(nowTitle)}">${esc(nowTitle.slice(0, 60)) || '（未知）'}</span>
      </div>
      ${song.name && song.name !== nowTitle ? `
      <div class="swap-now-row">
        <span class="swap-now-k">歌单里的歌</span>
        <span class="swap-now-v" title="${esc(song.name)}">${esc(song.name.slice(0, 60))}</span>
      </div>` : ''}
    </div>
    <div class="swap-search">
      <input id="swapQ" type="text" value="${esc(defaultQ)}"
             placeholder="歌名 或 歌名 歌手，搜到对的那条点一下就替换">
      <button class="pill-btn" id="swapGo">搜索</button>
    </div>
    <div class="swap-tip">搜不到就把关键词改短，只留歌名试试（去掉括号、副标题）</div>
    <div class="modal-list" id="swapList">
      <div class="empty" style="padding:26px">搜索中…</div>
    </div>
    <div class="modal-foot">
      <button class="pill-btn plain" onclick="closeModal()">取消</button>
    </div>`);

  const paint = (list) => {
    const box = $('#swapList');
    if (!list || !list.length) {
      box.innerHTML = `<div class="empty" style="padding:26px">
        没搜到<br><span style="font-size:12px">把关键词改短一点，只留歌名试试</span></div>`;
      return;
    }
    // 标出「就是当前这条」，免得在原音源和替代音源之间选错
    const curBvid = song.bvid || '';
    box.innerHTML = list.map((r, i) => {
      const isCur = curBvid && r.bvid === curBvid;
      return `
      <div class="modal-item${isCur ? ' swap-cur' : ''}" data-i="${i}">
        <img src="${img(r.cover)}" data-fallback="${esc(r.cover)}" loading="lazy">
        <div class="mi-body">
          <div class="mi-t" title="${esc(r.title)}">${esc(r.title)}</div>
          <div class="mi-s">${esc(r.author)} · ${fmtTime(r.duration)} · ${fmtPlay(r.play)} 播放</div>
        </div>
        ${isCur ? '<span class="swap-badge">当前</span>' : '<span class="swap-pick">用它</span>'}
      </div>`;
    }).join('');
    $$('#swapList .modal-item').forEach((el) => {
      el.onclick = () => {
        const pick = list[Number(el.dataset.i)];
        closeModal();
        onPick(pick);
      };
    });
  };

  const run = async (kw) => {
    kw = (kw || '').trim();
    if (!kw) return;
    $('#swapList').innerHTML = '<div class="empty" style="padding:26px">搜索中…</div>';
    try {
      const r = await api('/api/match?title=' + encodeURIComponent(kw) +
        '&artist=&duration=' + encodeURIComponent(song.duration || 0));
      paint(r.results || []);
    } catch (e) {
      $('#swapList').innerHTML =
        `<div class="empty" style="padding:26px">${esc(e.message)}</div>`;
    }
  };

  $('#swapGo').onclick = () => run($('#swapQ').value);
  $('#swapQ').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') run($('#swapQ').value);
  });
  run(defaultQ);
}

/** 重画导入列表里某一行的匹配结果 */
function paintImportRow(i, qq, match) {
  const row = $(`#impSongs .imp-song[data-i="${i}"]`);
  const mk = $('#mk' + i);
  const mt = $('#mt' + i);
  if (!match) {
    if (row) { row.classList.add('fail'); row.classList.remove('ok'); }
    if (mk) mk.innerHTML = '<span style="color:var(--danger)">没找到，点右边 ⇄ 换一个</span>';
    if (mt) mt.textContent = fmtTime(qq.duration);
    return;
  }
  if (row) { row.classList.add('ok'); row.classList.remove('fail'); }
  if (mt) mt.innerHTML = `<span style="color:var(--primary)">✓</span> ${fmtTime(match.duration)}`;
  if (mk) mk.innerHTML = `<span title="匹配到：${esc(match.title)}">${esc(match.title)}</span>`;
}

function refreshImportProgress() {
  const bar = $('#impBar');
  const txt = $('#impText');
  if (!bar || !txt) return;
  const total = (S.imp.songs || []).length || 1;
  const done = S.imp.done || 0;
  $('i', bar).style.width = Math.min(100, done / total * 100).toFixed(1) + '%';
  txt.textContent = `正在匹配 ${done} / ${total}　（已找到 ${S.imp.matches.filter(Boolean).length} 首）`;
}

/* ------------------------------------------------------------ 响度均衡 */
/**
 * B 站音源的响度差异很大：无损修复版普遍偏响，翻录/现场版普遍偏轻。
 * 这里用 Web Audio 做两件事：
 *   1) 轻压缩 —— 把过响的峰值压住，避免削波破音；
 *   2) 自动增益 —— 实时测 RMS，把偏轻的提上来、偏响的降下去（目标约 -18 dBFS）。
 * 增益是「慢速自适应」（约 3 秒收敛），所以不会有忽大忽小的抽气感。
 * 注意：AudioContext 必须等用户第一次点播放（手势）时再建，否则会被浏览器挂起、导致没声音。
 */
let ag = null;          // audio graph
let agTimer = null;

/**
 * 音量衰减的下限（dB）。滑条拖到最左边 = -45 dB，再往左就是静音。
 * 45 dB 差不多是「几乎听不见但还能分辨」的程度，足够应付半夜听歌的场景。
 */
const VOL_FLOOR_DB = -45;

/**
 * 滑条百分比（0~100）→ 线性增益（0~1）。
 *
 * 为什么不直接把百分比当音量用：
 *   线性音量在「小声」那一段分辨率极差 —— 0.1 到 0.05 听起来变化巨大，但那只是滑条上
 *   几格的距离；而 1.0 到 0.9 几乎听不出差别，却占了整整 10%。
 *   所以这里把滑条线性映射到 dB 域（对数刻度），每一格的听感变化都差不多均匀。
 *
 * 这个增益是加在压缩器 / AGC 之后的最后一个节点上，所以：
 *   不管响度均衡开着还是关着，它都能把声音一路压到没声，行为完全一致。
 */
function sliderToGain(pct) {
  pct = Math.max(0, Math.min(100, Number(pct) || 0));
  if (pct <= 0) return 0;
  return Math.pow(10, VOL_FLOOR_DB * (1 - pct / 100) / 20);
}

function gainToDb(g) {
  return g <= 0 ? -Infinity : 20 * Math.log10(g);
}

function ensureAudioGraph() {
  if (ag) return ag;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    const ctx = new AC();
    const src = ctx.createMediaElementSource(audio);   // 每个 audio 元素只能调一次
    const comp = ctx.createDynamicsCompressor();
    const gain = ctx.createGain();
    const volNode = ctx.createGain();   // 纯音量衰减：放在最末端，压过一切自动增益
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    gain.gain.value = 1;
    volNode.gain.value = 1;
    // 顺序很重要：analyser 必须在 volNode **之前**。
    // 否则它测到的是自己调过音量后的信号，AGC 会形成反馈环 ——
    // 用户把音量拉小，AGC 又把它提回去，就变成「怎么调都调不小」。
    //
    // 均衡器插在 comp 之后、gain（AGC）之前：
    //   src → comp → [EQ] → gain → analyser → volNode → destination
    // 这样 AGC 测的是「调过均衡后真正要播出去的电平」，
    // 而且音量滑条仍稳居最末端，压得过一切自动增益。
    src.connect(comp);
    buildEQChain(ctx);
    wireEQ(comp, gain);
    gain.connect(analyser);
    analyser.connect(volNode);
    volNode.connect(ctx.destination);
    ag = { ctx, src, comp, gain, volNode, analyser,
           buf: new Float32Array(analyser.fftSize), cur: 1 };
    startAGC();
    applyEQ();
    return ag;
  } catch (e) {
    console.warn('响度均衡初始化失败，将播放原始音量', e);
    return null;
  }
}

/**
 * 响度均衡的档位。
 *
 * 它的本意是「让所有歌听起来一样大」，做法应该是**把响的压下来**，
 * 而不是把所有歌都无脑中气十足地放大 —— 后者会让整体音量失控，
 * 用户会有「一开均衡声音就变好大、还调不小」的感觉。
 *
 * 所以：
 *   - 三档靠 threshold / ratio 真正区分开（阈值越低、压缩比越高 = 压得越狠）
 *   - 只允许很小幅度的向上补正（maxUp），而且只针对偏轻的歌
 *   - 音量滑条在最末端，永远压得过它（见 ensureAudioGraph 的节点顺序）
 */
const LOUD_PRESET = {
  light:    { threshold: -12, knee: 10, ratio: 2, maxUp: 1.3 },
  standard: { threshold: -18, knee: 14, ratio: 4, maxUp: 1.8 },
  strong:   { threshold: -24, knee: 20, ratio: 8, maxUp: 2.2 },
};
/** 补正目标电平：压完之后如果还比这个轻，才慢慢提一点，最多提到 maxUp */
const AGC_TARGET_DB = -20;

function loudPreset() {
  return LOUD_PRESET[S.settings.loudnessLevel || 'standard'] || LOUD_PRESET.standard;
}

function startAGC() {
  if (agTimer) return;
  agTimer = setInterval(() => {
    if (!ag || audio.paused) return;
    if (S.settings.loudness === false) {
      // 均衡关掉：增益平滑回到 1，纯靠音量滑条控制
      ag.cur += (1 - ag.cur) * 0.15;
      ag.gain.gain.value = ag.cur;
      return;
    }
    ag.analyser.getFloatTimeDomainData(ag.buf);
    let sum = 0;
    for (let i = 0; i < ag.buf.length; i++) sum += ag.buf[i] * ag.buf[i];
    const rms = Math.sqrt(sum / ag.buf.length);
    const db = 20 * Math.log10(rms + 1e-7);
    if (db < -48) return;                 // 静音 / 间奏，别乱调
    const p = loudPreset();
    // 压缩已经由 comp 节点实时完成，这里只做「偏轻时温和补正」，
    // 并且严格限制上限，避免把安静段落和底噪一起抬起来
    let target = 1;
    if (db < AGC_TARGET_DB) {
      target = Math.min(p.maxUp, Math.pow(10, (AGC_TARGET_DB - db) / 20));
    }
    ag.cur += (target - ag.cur) * 0.04;   // 慢速平滑，避免忽大忽小
    ag.gain.gain.value = ag.cur;
  }, 150);
}

/**
 * 把滑条的值应用到实际音量上。
 *
 * 设计目标：不管是开响度均衡还是关掉，都能一路调到几乎没声，而且手感一致。
 *
 * 做法：滑条 → dB 域 → 线性增益，同时写两个地方：
 *   1) audio.volume —— 均衡还没建立（第一次播放前）时的兜底
 *   2) volNode.gain —— 挂在压缩器 / AGC 之后的最末端，能压过一切自动增益
 * 两者同步设置，所以听不出接缝。
 */
function applyVolume() {
  const pct = Math.max(0, Math.min(100, Number(S.settings.volume ?? 0.85) * 100));
  const g = sliderToGain(pct);
  audio.volume = g;
  if (ag) ag.volNode.gain.value = g;
  updateVolLabel(pct, g);
}

/** 播放条右边那行小字：显示当前音量百分比 + 已衰减多少 dB */
function updateVolLabel(pct, g) {
  const hint = $('#volHint');
  const el = $('#volume');
  if (el) {
    el.title = g <= 0
      ? '音量：静音'
      : `音量 ${Math.round(pct)}%（衰减 ${gainToDb(g).toFixed(1)} dB）`;
  }
  if (hint) hint.textContent = g <= 0 ? '静音' : Math.round(pct) + '%';
}

/** 换歌 / 切换均衡时重新套用一遍音量设置 */
function reapplyVolume() {
  const g = ag;
  if (g) { g.cur = 1; g.gain.gain.value = 1; }
  applyVolume();
}

function updateLoudBtn() {
  const on = S.settings.loudness !== false;
  const lvName = { light: '轻', standard: '标准', strong: '强' };
  const b = $('#loudBtn');
  if (!b) return;
  b.classList.toggle('on', on);
  b.textContent = on ? '⚖ 均衡' : '⚖ 原声';
  b.title = on
    ? `响度均衡已开（强度：${lvName[S.settings.loudnessLevel] || '标准'}）\n` +
      '把响的歌压下来，让每首歌听起来差不多大（不会整体变吵）'
    : '响度均衡已关：播放原始音量';
}

function applyLoudness(force) {
  const on = S.settings.loudness !== false;
  const p = loudPreset();
  let g = ag;
  if (!g && (force || !audio.paused)) g = ensureAudioGraph();
  if (g) {
    if (on) {
      g.comp.threshold.value = p.threshold;
      g.comp.knee.value = p.knee;
      g.comp.ratio.value = p.ratio;
    } else {
      g.comp.threshold.value = 0;
      g.comp.knee.value = 0;
      g.comp.ratio.value = 1;
    }
    g.comp.attack.value = 0.003;
    g.comp.release.value = 0.25;
  }
  reapplyVolume();
  updateLoudBtn();
}

if ($('#loudBtn')) {
  $('#loudBtn').addEventListener('click', () => {
    S.settings.loudness = (S.settings.loudness === false);
    applyLoudness(true);
    api('/api/settings', { key: 'loudness', value: S.settings.loudness }).catch(() => {});
    toast(S.settings.loudness ? '响度均衡：已开启' : '响度均衡：已关闭（播放原声）');
  });
}

/* ---------------------------------------------------------- 加入歌单弹窗 */
function openAddDialog(song) {
  if (!song) return;
  openModal(`
    <h3>加入歌单</h3>
    <div class="m-sub" title="${esc(song.name || song.title)}">${esc((song.name || song.title || '').slice(0, 60))}</div>
    <div class="modal-list">
      ${S.playlists.map((p) => `<div class="modal-item" data-pl="${esc(p.id)}">
        <span style="font-size:20px">${p.id === 'fav' ? '❤️' : '🎵'}</span>
        <div class="mi-body"><div class="mi-t">${esc(p.name)}</div><div class="mi-s">${p.count} 首</div></div>
      </div>`).join('')}
    </div>
    <div class="modal-foot">
      <button class="pill-btn plain" id="mNewBtn">＋ 新建歌单</button>
      <button class="pill-btn plain" onclick="closeModal()">取消</button>
    </div>`);
  $$('#modalBox .modal-item').forEach((el) => {
    el.onclick = async () => {
      try {
        const r = await api('/api/playlist/add', { id: el.dataset.pl, song });
        S.playlists = r.playlists;
        renderPlaylists();
        toast(r.message || '已添加', 'ok');
        closeModal();
      } catch (e) { toast(e.message, 'err'); }
    };
  });
  $('#mNewBtn').onclick = async () => {
    const name = prompt('新歌单名字：', '我的歌单');
    if (!name) return;
    try {
      const r = await api('/api/playlist/create', { name });
      S.playlists = (await api('/api/state')).playlists;
      renderPlaylists();
      openAddDialog(song);
    } catch (e) { toast(e.message, 'err'); }
  };
}

/* -------------------------------------------------------------- 事件绑定 */
function bindGlobal() {
  // 图片兜底：后端代理失败 → 直连 B 站原图 → 占位图（捕获阶段监听，img 的 error 不冒泡）
  document.addEventListener('error', (e) => {
    const el = e.target;
    if (!el || el.tagName !== 'IMG') return;
    const fb = el.dataset.fallback;
    if (fb && el.src !== fb) {
      el.dataset.fallback = '';
      el.src = fb;
    } else if (!el.dataset.dead) {
      el.dataset.dead = '1';
      el.src = PLACEHOLDER;
    }
  }, true);

  $('#searchBtn').onclick = () => doSearch($('#searchInput').value);
  $('#searchInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch($('#searchInput').value); });
  $('#orderSel').onchange = () => { if (S.search.kw) doSearch(S.search.kw); };
  $('#themeBtn').onclick = toggleTheme;
  $('#loginBtn').onclick = openLogin;
  $('#newPlaylistBtn').onclick = async () => {
    const name = prompt('新歌单名字：', '我的歌单');
    if (!name) return;
    try {
      const r = await api('/api/playlist/create', { name });
      S.playlists = r.playlists;
      renderPlaylists();
      showView('playlist:' + r.playlist.id);
      toast('歌单已创建', 'ok');
    } catch (e) { toast(e.message, 'err'); }
  };

  $$('.nav-item').forEach((el) => { el.onclick = () => showView(el.dataset.view); });

  $('#playBtn').onclick = togglePlay;
  $('#nextBtn').onclick = () => next(false);
  $('#prevBtn').onclick = prev;
  $('#modeBtn').onclick = () => {
    const order = ['list', 'single', 'shuffle'];
    setMode(order[(order.indexOf(S.settings.playMode) + 1) % 3]);
    toast({ list: '列表循环', single: '单曲循环', shuffle: '随机播放' }[S.settings.playMode]);
  };
  $('#volBtn').onclick = () => {
    audio.muted = !audio.muted;
    $('#volBtn').textContent = audio.muted ? '🔇' : '🔊';
    if (!audio.muted) applyVolume();
  };
  $('#volume').oninput = (e) => {
    S.settings.volume = Number(e.target.value) / 100;
    audio.muted = false;
    $('#volBtn').textContent = '🔊';
    applyVolume();
  };
  $('#volume').onchange = () => {
    S.settings.volume = Number($('#volume').value) / 100;
    api('/api/settings', { key: 'volume', value: S.settings.volume }).catch(() => {});
  };

  $('#progress').oninput = (e) => {
    dragging = true;
    if (audio.duration) {
      audio.currentTime = Number(e.target.value) / 1000 * audio.duration;
      $('#curTime').textContent = fmtTime(audio.currentTime);
    }
  };
  $('#progress').onchange = () => { dragging = false; };

  // 收起 / 展开右侧播放队列。
  // 注意：窗口 <=1240px 时右侧面板本来就被 CSS 强制 display:none 了，
  // 这时候再切换 hide-now 是「点了没反应」。所以窄窗口下改成明确告知，
  // 而不是让用户对着一个没动静的按钮反复点。
  $('#queueBtn').onclick = () => {
    const app = document.querySelector('.app');
    const narrow = window.matchMedia('(max-width: 1240px)').matches;
    if (narrow) {
      toast('窗口太窄，播放队列已自动收起；把窗口拉宽就能看到');
      return;
    }
    app.classList.toggle('hide-now');
  };
  $('#clearQueueBtn').onclick = () => { S.queue = []; S.qi = -1; renderQueue(); };
  $('#favNowBtn').onclick = () => S.current && addToFav(S.current);
  $('#pFavBtn').onclick = () => S.current && addToFav(S.current);
  $('#addNowBtn').onclick = () => S.current ? openAddDialog(S.current) : toast('还没有播放歌曲');

  $('#modalWrap').querySelector('.modal-mask').onclick = () => {
    if (window.__stopQr) { window.__stopQr(); window.__stopQr = null; }
    closeModal();
  };

  audio.addEventListener('play', () => {
    $('#playIcon').innerHTML = '<path d="M6 5h4v14H6zm8 0h4v14h-4z"/>';
    // Web Audio 的上下文被浏览器挂起时会「没声音」，这里再兜一次
    if (ag && ag.ctx.state === 'suspended') ag.ctx.resume().catch(() => {});
  });
  audio.addEventListener('pause', () => {
    $('#playIcon').innerHTML = '<path d="M8 5v14l11-7z"/>';
  });
  audio.addEventListener('timeupdate', () => {
    if (!audio.duration) return;
    if (!dragging) $('#progress').value = Math.round(audio.currentTime / audio.duration * 1000);
    $('#curTime').textContent = fmtTime(audio.currentTime);
  });
  audio.addEventListener('loadedmetadata', () => {
    $('#totTime').textContent = fmtTime(audio.duration);
  });
  audio.addEventListener('ended', () => {
    if (S.settings.playMode === 'single') { audio.currentTime = 0; audio.play(); }
    else next(true);
  });
  audio.addEventListener('playing', () => {
    // 真的出声了：连续跳曲的计数清零，下一首播不出来还能再试两首
    S.autoSkips = 0;
  });
  audio.addEventListener('error', () => {
    // code 1 是我们自己换 src 打断上一首时发的，不是真坏了，别拿它去跳曲。
    const c = audio.error && audio.error.code;
    if (c !== 3 && c !== 4) return;
    if (audio.src) toast('这首歌播不出来，正在试下一首', 'err');
    setTimeout(autoNext, 800);
  });

  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
    if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
    if (e.code === 'ArrowRight' && e.ctrlKey) next(false);
    if (e.code === 'ArrowLeft' && e.ctrlKey) prev();
  });
}

async function addToFav(song) {
  try {
    const r = await api('/api/playlist/add', { id: 'fav', song });
    S.playlists = r.playlists;
    renderPlaylists();
    toast(r.message || '已收藏到「我喜欢的音乐」', 'ok');
    renderNowPlaying();
  } catch (e) { toast(e.message, 'err'); }
}

init();
