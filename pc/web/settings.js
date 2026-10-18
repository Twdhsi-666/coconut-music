/* 椰子音乐 · 设置面板 / 主题色 / 全局快捷键
 *
 * 依赖 app.js 里已有的全局内容：S / audio / api / toast / openModal / closeModal
 *                              $ / $$ / esc / prev / next / applyLoudness
 */

/* ================================================================ 主题色 */
const PRESET_COLORS = [
  { name: '椰子绿（默认）', value: '#31c27c' },
  { name: '天空蓝', value: '#3b82f6' },
  { name: '葡萄紫', value: '#8b5cf6' },
  { name: '蜜桃粉', value: '#ec4899' },
  { name: '落日橙', value: '#f59e0b' },
  { name: '青碧', value: '#06b6d4' },
  { name: '朱砂红', value: '#ef4444' },
  { name: '极简白', value: '#e8eaed' },
];

function hexToRgb(hex) {
  let h = String(hex || '').trim().replace('#', '');
  if (h.length === 3) h = h.split('').map((c) => c + c).join('');
  if (!/^[0-9a-fA-F]{6}$/.test(h)) return null;
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

function shade(hex, percent) {
  const c = hexToRgb(hex);
  if (!c) return hex;
  const t = percent < 0 ? 0 : 255;
  const p = Math.abs(percent) / 100;
  return '#' + c.map((v) => Math.round((t - v) * p + v).toString(16).padStart(2, '0')).join('');
}

/** 把主色写进 CSS 变量，同时派生 hover 色、渐变亮色、柔和背景色，
 *  以及「主色上的文字色」—— 不然选了白色主题色就会白字白底看不见。 */
function applyPrimary(hex) {
  const c = hexToRgb(hex);
  if (!c) return;
  const root = document.documentElement;
  root.style.setProperty('--primary', hex);
  root.style.setProperty('--primary-2', shade(hex, -14));
  root.style.setProperty('--primary-light', shade(hex, 24));
  root.style.setProperty('--primary-soft', `rgba(${c[0]}, ${c[1]}, ${c[2]}, .14)`);
  const lum = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2];
  root.style.setProperty('--on-primary', lum > 168 ? '#1b1e24' : '#ffffff');
}

/* ============================================================ 响度强度 */
const LOUD_LEVELS = [
  { key: 'light', name: '轻' },
  { key: 'standard', name: '标准' },
  { key: 'strong', name: '强' },
];

/* ============================================================== 快捷键 */
const HOTKEY_ACTIONS = [
  { key: 'prev', label: '上一首' },
  { key: 'next', label: '下一首' },
  { key: 'volUp', label: '调大音量' },
  { key: 'volDown', label: '降低音量' },
];

function normalizeKey(e) {
  const k = e.key;
  if (/^Arrow(Left|Up|Right|Down)$/.test(k)) return k.slice(5);
  if (/^[a-zA-Z]$/.test(k)) return k.toUpperCase();
  if (/^[0-9]$/.test(k)) return k;
  if (/^F([1-9]|1[0-2])$/.test(k)) return k;
  const map = {
    ' ': 'Space', Enter: 'Enter', Escape: 'Escape', Tab: 'Tab',
    Backspace: 'Backspace', Delete: 'Delete', Insert: 'Insert',
    Home: 'Home', End: 'End', PageUp: 'PageUp', PageDown: 'PageDown',
    '-': '-', '=': '=', '[': '[', ']': ']', '\\': '\\', ';': ';',
    "'": "'", ',': ',', '.': '.', '/': '/', '`': '`',
  };
  return map[k] || null;
}

function prettyCombo(combo) {
  return String(combo || '未设置').split('+').join(' + ');
}

let recordingBtn = null;

function startRecord(btn) {
  if (recordingBtn && recordingBtn !== btn) {
    recordingBtn.classList.remove('recording');
    recordingBtn.textContent = prettyCombo(recordingBtn.dataset.old);
  }
  recordingBtn = btn;
  btn.dataset.old = (S.settings.hotkeys || {})[btn.dataset.act] || '';
  btn.classList.add('recording');
  btn.textContent = '按下组合键…（Esc 取消）';
}

function finishRecord(e) {
  if (!recordingBtn) return;
  e.preventDefault();
  e.stopPropagation();
  if (e.key === 'Escape') {
    recordingBtn.classList.remove('recording');
    recordingBtn.textContent = prettyCombo(recordingBtn.dataset.old);
    recordingBtn = null;
    return;
  }
  if (['Control', 'Alt', 'Shift', 'Meta'].includes(e.key)) return;   // 只按了修饰键，继续等
  const key = normalizeKey(e);
  if (!key) return;
  const mods = [];
  if (e.ctrlKey) mods.push('Ctrl');
  if (e.altKey) mods.push('Alt');
  if (e.shiftKey) mods.push('Shift');
  if (!mods.length) {
    recordingBtn.textContent = '至少要带 Ctrl / Alt / Shift';
    return;
  }
  const combo = mods.concat(key).join('+');
  const act = recordingBtn.dataset.act;
  recordingBtn.classList.remove('recording');
  recordingBtn.textContent = prettyCombo(combo);
  recordingBtn = null;
  saveHotkeys(act, combo);
}

async function saveHotkeys(act, combo) {
  const hk = Object.assign({}, S.settings.hotkeys || {});
  if (act) hk[act] = combo;
  S.settings.hotkeys = hk;
  try {
    const r = await api('/api/settings', { key: 'hotkeys', value: hk });
    paintHotkeyNote(r.hotkeyStatus);
    toast(act === 'enabled' ? '快捷键设置已保存' : '快捷键已更新', 'ok');
  } catch (e) {
    toast(e.message, 'err');
  }
}

/** 后端遇到「组合键被系统占用」时会自动换一组并把设置改写过去。
 *  这里把界面上的按键文本同步成真正生效的那组 —— 显示的就是实际用的，
 *  不再额外写一行解释，界面保持干净。 */
function syncHotkeysFromServer(st) {
  if (!st || !st.raw) return;
  const hk = Object.assign({}, S.settings.hotkeys || {});
  let changed = false;
  for (const action of Object.keys(st.raw)) {
    if (hk[action] !== st.raw[action]) { hk[action] = st.raw[action]; changed = true; }
  }
  if (!changed) return;
  S.settings.hotkeys = hk;
  $$('#modalBox .hk-key').forEach((b) => {
    b.textContent = prettyCombo(hk[b.dataset.act]);
  });
}

function paintHotkeyNote(st) {
  const box = $('#setHkNote');
  if (!box || !st) return;
  syncHotkeysFromServer(st);
  const fails = st.failed || [];
  if (fails.length) {
    box.className = 'hk-note warn';
    box.innerHTML = fails.map((f) => `「${f.combo}」注册失败，换一组吧`).join('<br>');
  } else {
    box.className = 'hk-note';
    box.textContent = '快捷键已生效，软件缩到后台也能用';
  }
}

/* 全局热键 → 播放动作（后端注册，前端每 350ms 取一次指令） */
let hkTimer = null;

function runHotkeyAction(action) {
  if (action === 'prev') prev();
  else if (action === 'next') next(false);
  else if (action === 'volUp') nudgeVolume(0.06);
  else if (action === 'volDown') nudgeVolume(-0.06);
}

let volToastEl = null;

function nudgeVolume(delta) {
  const v = Math.max(0, Math.min(1, (audio.volume || 0) + delta));
  audio.volume = v;
  audio.muted = false;
  const bar = $('#volume');
  if (bar) bar.value = Math.round(v * 100);
  const btn = $('#volBtn');
  if (btn) btn.textContent = '🔊';
  S.settings.volume = v;
  api('/api/settings', { key: 'volume', value: v }).catch(() => {});
  if (!volToastEl) {
    volToastEl = document.createElement('div');
    volToastEl.className = 'toast';
    $('#toastWrap').appendChild(volToastEl);
  }
  volToastEl.textContent = '音量 ' + Math.round(v * 100) + '%';
  clearTimeout(volToastEl._t);
  volToastEl._t = setTimeout(() => { volToastEl.remove(); volToastEl = null; }, 1300);
}

function startHotkeyPolling() {
  if (hkTimer) return;
  hkTimer = setInterval(async () => {
    try {
      const r = await api('/api/hotkey/poll', null, 'GET');
      (r.actions || []).forEach(runHotkeyAction);
    } catch (e) { /* 后端没起来时忽略 */ }
  }, 350);
}

/* ============================================================ 设置面板 */
function openSettings() {
  const s = S.settings || {};
  const hk = s.hotkeys || {};
  const curColor = (s.primaryColor || '#31c27c').toLowerCase();
  const curLevel = s.loudnessLevel || 'standard';

  openModal(`
    <h3>设置</h3>
    <div class="m-sub">改完立即生效，并且会记住</div>
    <div class="set-wrap">

      <div class="set-sec">
        <h4>外观</h4>
        <div class="set-row">
          <span class="lbl">明暗</span>
          <div class="seg" id="setTheme">
            <button data-v="light" class="${s.theme !== 'dark' ? 'on' : ''}">浅色</button>
            <button data-v="dark" class="${s.theme === 'dark' ? 'on' : ''}">深色</button>
          </div>
        </div>
        <div class="set-row">
          <span class="lbl">主题色</span>
          <div class="swatches" id="setColors">
            ${PRESET_COLORS.map((c) => `
              <span class="swatch ${curColor === c.value ? 'on' : ''}" data-v="${c.value}"
                    title="${c.name}" style="background:${c.value}"></span>`).join('')}
            <span class="swatch custom" title="自定义颜色">
              🎨<input type="color" id="setCustomColor" value="${esc(s.primaryColor || '#31c27c')}">
            </span>
          </div>
        </div>
      </div>

      <div class="set-sec">
        <h4>音量</h4>
        <div class="set-row vol-row">
          <span class="lbl">音量大小</span>
          <div class="range-row vol-range">
            <button class="vol-step" id="setVolDown" title="小一点">−</button>
            <input type="range" id="setVolume" min="0" max="100" step="1"
                   value="${Math.round(Number(s.volume ?? 0.85) * 100)}">
            <button class="vol-step" id="setVolUp" title="大一点">+</button>
            <span class="val" id="setVolVal">${Math.round(Number(s.volume ?? 0.85) * 100)}%</span>
          </div>
        </div>
        <div class="hk-note">
          这是「你想听多大声」，和响度均衡互不干扰。<br>
          滑条左边更小声：越往左衰减越快（最左为静音），下面能看到具体的 dB 衰减量。
        </div>
        <div class="vol-meter"><span id="setVolHint">衰减 0.0 dB</span></div>
      </div>

      <div class="set-sec">
        <h4>响度均衡</h4>
        <div class="set-row">
          <span class="lbl">响度均衡</span>
          <div class="seg" id="setLoud">
            <button data-v="off" class="${s.loudness === false ? 'on' : ''}">关闭</button>
            <button data-v="on" class="${s.loudness !== false ? 'on' : ''}">开启</button>
          </div>
        </div>
        <div class="set-row">
          <span class="lbl">均衡强度</span>
          <div class="range-row">
            <input type="range" id="setLoudLevel" min="0" max="2" step="1"
                   value="${Math.max(0, LOUD_LEVELS.findIndex((x) => x.key === curLevel))}">
            <span class="val" id="setLoudVal">${(LOUD_LEVELS.find((x) => x.key === curLevel) || LOUD_LEVELS[1]).name}</span>
          </div>
        </div>
        <div class="hk-note">
          它只做一件事：<b>让每首歌听起来差不多大</b>。<br>
          做法是把偏响的压下来（不是把整体变吵），所以开它<b>不会</b>让声音变大，
          也不会影响上面那个音量滑条。调得越强，歌与歌的响度差越小，但动态会被压得更平；
          听古典、现场时建议调轻或关掉。
        </div>
      </div>

      <div class="set-sec">
        <h4>全局快捷键（软件在后台时也生效）</h4>
        <div class="set-row">
          <span class="lbl">启用</span>
          <div class="seg" id="setHkEnabled">
            <button data-v="off" class="${hk.enabled === false ? 'on' : ''}">关闭</button>
            <button data-v="on" class="${hk.enabled !== false ? 'on' : ''}">开启</button>
          </div>
        </div>
        ${HOTKEY_ACTIONS.map((a) => `
          <div class="hk-row">
            <span class="hk-name">${a.label}</span>
            <button class="hk-key" data-act="${a.key}">${prettyCombo(hk[a.key])}</button>
          </div>`).join('')}
        <div class="hk-note" id="setHkNote">正在读取快捷键状态…</div>
      </div>

    </div>
    <div class="modal-foot">
      <button class="pill-btn plain" onclick="closeModal()">关闭</button>
    </div>`);

  // ---- 明暗 ----
  $$('#setTheme button').forEach((b) => {
    b.onclick = () => {
      S.settings.theme = b.dataset.v;
      applyTheme();
      $$('#setTheme button').forEach((x) => x.classList.toggle('on', x === b));
      api('/api/settings', { key: 'theme', value: b.dataset.v }).catch(() => {});
    };
  });

  // ---- 主题色 ----
  const pickColor = (hex) => {
    S.settings.primaryColor = hex;
    applyPrimary(hex);
    $$('#setColors .swatch[data-v]').forEach((el) => {
      el.classList.toggle('on', el.dataset.v.toLowerCase() === hex.toLowerCase());
    });
    api('/api/settings', { key: 'primaryColor', value: hex }).catch(() => {});
  };
  $$('#setColors .swatch[data-v]').forEach((el) => {
    el.onclick = () => pickColor(el.dataset.v);
  });
  const custom = $('#setCustomColor');
  if (custom) {
    custom.oninput = () => pickColor(custom.value);       // 拖动取色实时预览
    custom.onchange = () => toast('主题色已改为 ' + custom.value, 'ok');
  }

  // ---- 音量（独立于响度均衡）----
  const volEl = $('#setVolume');
  const volVal = $('#setVolVal');
  const volHint = $('#setVolHint');
  const paintVol = (pct) => {
    const g = sliderToGain(pct);
    if (volVal) volVal.textContent = g <= 0 ? '静音' : Math.round(pct) + '%';
    if (volHint) {
      volHint.textContent = g <= 0 ? '已静音' : '衰减 ' + Math.abs(gainToDb(g)).toFixed(1) + ' dB';
    }
  };
  const commitVol = (pct, save) => {
    pct = Math.max(0, Math.min(100, Math.round(pct)));
    S.settings.volume = pct / 100;
    if (volEl) volEl.value = pct;
    applyVolume();               // 全局：播放条和这里共用同一套取值
    const bar = $('#volume');
    if (bar) bar.value = pct;    // 同步播放条上的滑条
    paintVol(pct);
    if (save) api('/api/settings', { key: 'volume', value: S.settings.volume }).catch(() => {});
  };
  if (volEl) {
    paintVol(Number(volEl.value));
    volEl.oninput = () => commitVol(Number(volEl.value), false);
    volEl.onchange = () => commitVol(Number(volEl.value), true);
  }
  const stepVol = (d) => commitVol(Number($('#setVolume').value) + d, true);
  if ($('#setVolDown')) $('#setVolDown').onclick = () => stepVol(-5);
  if ($('#setVolUp')) $('#setVolUp').onclick = () => stepVol(5);

  // ---- 响度 ----
  $$('#setLoud button').forEach((b) => {
    b.onclick = () => {
      S.settings.loudness = (b.dataset.v === 'on');
      $$('#setLoud button').forEach((x) => x.classList.toggle('on', x === b));
      applyLoudness(true);
      api('/api/settings', { key: 'loudness', value: S.settings.loudness }).catch(() => {});
      toast(S.settings.loudness ? '响度均衡：已开启' : '响度均衡：已关闭（播放原声）');
    };
  });
  const lv = $('#setLoudLevel');
  if (lv) {
    lv.oninput = () => {
      const level = LOUD_LEVELS[Number(lv.value)].key;
      S.settings.loudnessLevel = level;
      $('#setLoudVal').textContent = LOUD_LEVELS[Number(lv.value)].name;
      applyLoudness(true);
    };
    lv.onchange = () => {
      api('/api/settings', { key: 'loudnessLevel', value: S.settings.loudnessLevel }).catch(() => {});
      toast('响度均衡强度：' + LOUD_LEVELS[Number(lv.value)].name, 'ok');
    };
  }

  // ---- 快捷键 ----
  $$('#setHkEnabled button').forEach((b) => {
    b.onclick = () => {
      const on = (b.dataset.v === 'on');
      $$('#setHkEnabled button').forEach((x) => x.classList.toggle('on', x === b));
      const hk2 = Object.assign({}, S.settings.hotkeys || {});
      hk2.enabled = on;
      S.settings.hotkeys = hk2;
      saveHotkeys(null, null);
    };
  });
  $$('#modalBox .hk-key').forEach((btn) => {
    btn.onclick = () => startRecord(btn);
  });

  api('/api/hotkey/status').then((st) => paintHotkeyNote(st)).catch(() => {});
}

/* ============================================================== 启动 */
function bootSettings() {
  applyPrimary(S.settings.primaryColor || '#31c27c');
  const btn = $('#settingsBtn');
  if (btn) btn.onclick = openSettings;
  startHotkeyPolling();
  // 启动时同步一次热键状态：后端可能已因「组合键被占用」自动换过一组
  api('/api/hotkey/status').then((st) => {
    if (st && st.raw) {
      S.settings.hotkeys = Object.assign({}, S.settings.hotkeys || {}, st.raw);
    }
  }).catch(() => {});
  // 直接打开设置面板： /?settings=1
  if (new URLSearchParams(location.search).get('settings')) {
    setTimeout(openSettings, 400);
  }
}

document.addEventListener('keydown', finishRecord, true);
