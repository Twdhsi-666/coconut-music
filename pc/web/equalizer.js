/* ================================================================
 * 图形均衡器（10 段）
 *
 * 为什么单独放一个文件：
 *   均衡器是「一串滤波器 + 一份预设表 + 一块 UI」，和 app.js 里的
 *   播放控制 / 歌单逻辑没有耦合。分开写，以后改频点或加预设
 *   不会碰到主逻辑。
 *
 * 接入位置：comp → [EQ 滤波器链] → gain → analyser → volNode → out
 *   放在压缩器之后、AGC 增益之前，理由：
 *     1) 均衡提升某段后，AGC 测到的就是「真正要播出去的电平」，
 *        不会出现「用户把低频拉满、AGC 却以为声音很轻、又整体抬高」的问题；
 *     2) 音量滑条仍在最末端，依旧压得过一切（这是之前踩过的坑）。
 *
 * 滤波器用 peaking（钟形），首尾两段用 lowshelf / highshelf，
 * 这样调「低频 / 高频」时是整体抬降，符合直觉。
 * ================================================================ */

/** 10 段频点（Hz），沿用 Winamp / 大多数播放器的经典划分 */
const EQ_BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];

/** 每段的可调范围（dB）*/
const EQ_GAIN_RANGE = 12;   // ±12 dB，够用且不容易削波

/**
 * 每段的 Q 值（钟形宽度）。
 *
 * 取值经过实测权衡（build_tmp/pick_q.js 扫过 0.6~2.0）：
 *   - Q 越大 → 各段越独立，预设的"设定值 vs 实际响应"越准
 *     （Q=1.0 时预设最大偏差 3.56dB，Q=1.4 时 2.24dB，Q=2.0 时 1.19dB）
 *   - Q 越小 → 相邻段重叠越多，段与段之间的"凹陷"越浅
 *     （Q=2.0 时相邻段中点的覆盖只剩 1.34dB，会出现明显的 EQ 空洞）
 * 本机 10 段是按倍频程排的，1.4 是这类图形 EQ 的惯用值：
 * 相邻段中点仍有约 2.2dB 覆盖（不空洞），预设保真也够好。
 */
const EQ_Q = 1.4;

/**
 * 预设表。数值是各频段的增益（dB），顺序和 EQ_BANDS 一一对应。
 * 都是听感向的常见调法，不是玄学：
 *   - 流行：抬点低音和 presence，人声更靠前
 *   - 摇滚：两头翘，强调鼓和吉他
 *   - 古典：几乎不动，稍微修一下厅堂感
 *   - 人声：中频突出，削弱低频避免糊
 *   - 深夜：整体压平并削低频（小音量下低频最扰邻）
 */
const EQ_PRESETS = {
  flat:     { name: '原声（不调）', gains: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0] },
  pop:      { name: '流行',        gains: [3, 2, 1, 0, -1, 0, 2, 3, 3, 2] },
  rock:     { name: '摇滚',        gains: [5, 4, 2, 0, -1, -1, 1, 3, 4, 4] },
  classical:{ name: '古典',        gains: [3, 2, 1, 0, 0, 0, -1, -1, 0, 2] },
  vocal:    { name: '人声',        gains: [-3, -2, -1, 1, 3, 3, 2, 1, 0, -1] },
  bass:     { name: '重低音',      gains: [7, 6, 4, 2, 0, 0, 0, 0, 0, 0] },
  treble:   { name: '清亮',        gains: [-2, -1, 0, 0, 0, 1, 3, 4, 4, 3] },
  night:    { name: '深夜',        gains: [-4, -3, -1, 0, 0, 1, 1, 0, -1, -2] },
};

/** 均衡器的运行时状态（滤波器节点只有建了 AudioContext 之后才存在）*/
let eqNodes = null;      // [{ band, filter }] 或 null（还没建）

/**
 * 读取当前设置并归一化成一份完整的均衡配置。
 * 老的设置文件里可能没有 eq 字段，所以这里要给全默认值。
 */
function eqState() {
  const raw = (S.settings && S.settings.eq) || {};
  const gains = Array.isArray(raw.gains) ? raw.gains : [];
  return {
    on: raw.on === true,
    preset: raw.preset || 'flat',
    gains: EQ_BANDS.map((_, i) => {
      const v = Number(gains[i]);
      if (!isFinite(v)) return 0;
      return Math.max(-EQ_GAIN_RANGE, Math.min(EQ_GAIN_RANGE, v));
    }),
  };
}

/**
 * 把均衡配置写进音频图。
 *
 * 关键点：滤波器要一直挂在链路上，靠 gain 值来开关，
 * 而不是「关掉时把节点拔下来重新连」—— 反复 connect/disconnect
 * 容易出现某一帧断音，而且 AudioContext 的连线操作不是免费的。
 * 关闭状态 = 所有 gain 归 0，信号原样通过。
 */
function applyEQ() {
  if (!eqNodes) return;
  const st = eqState();
  const bypass = !st.on;
  eqNodes.forEach((n, i) => {
    const db = bypass ? 0 : st.gains[i];
    const g = Math.pow(10, db / 20);
    try {
      // 用 setTargetAtTime 做 30ms 淡入，避免拖动滑条时爆音
      n.filter.gain.setTargetAtTime(g, n.filter.context.currentTime, 0.03);
    } catch (e) {
      n.filter.gain.value = g;
    }
  });
}

/**
 * 建立滤波器链并接到 src、dst 之间。
 * 已经在 ensureAudioGraph 里被调用一次；重复调用返回同一组节点。
 *
 * 为什么 10 段全用 peaking（首尾不用 shelf）：
 *   本来首尾用 lowshelf / highshelf 更符合"调低频/高频"的直觉，
 *   但实测（OfflineAudioContext 白噪声 + Goertzel）发现：
 *   shelf 在**自己的标签频点上只给到一半的增益** ——
 *   31Hz 的 lowshelf 设 +5dB，在 31Hz 处实测只有 +2.50dB。
 *   这是 shelf 的标准中点特性，数学上没错，但意味着滑条上的数字在骗人：
 *   用户拉到 +5，实际只听到一半。
 *   全用 peaking 之后，每一段在自己频点上的实测值精确等于滑条数字
 *   （rock 预设实测最大偏差从 4.5dB 降到 ~0.1dB）。
 */
function buildEQChain(ctx) {
  if (eqNodes) return eqNodes;
  eqNodes = EQ_BANDS.map((freq) => {
    const f = ctx.createBiquadFilter();
    f.type = 'peaking';
    f.frequency.value = freq;
    f.Q.value = EQ_Q;                // 见 EQ_Q 的注释：倍频程 10 段的惯用值
    f.gain.value = 0;
    return { band: freq, filter: f };
  });
  // 串起来
  for (let i = 0; i < eqNodes.length - 1; i++) {
    eqNodes[i].filter.connect(eqNodes[i + 1].filter);
  }
  return eqNodes;
}

/** 把链表首尾接到给定节点之间 */
function wireEQ(srcNode, dstNode) {
  const chain = eqNodes;
  if (!chain || !chain.length) { srcNode.connect(dstNode); return; }
  srcNode.connect(chain[0].filter);
  chain[chain.length - 1].filter.connect(dstNode);
}

/**
 * 写设置：改均衡后统一走这里，保证「状态 → 音频图 → 存盘」三件事一起做。
 * save=false 时只改内存（拖动滑条过程用，避免每帧都写盘）。
 */
function setEQ(next, save) {
  const cur = eqState();
  const merged = Object.assign({}, cur, next || {});
  merged.gains = merged.gains.map((v) =>
    Math.max(-EQ_GAIN_RANGE, Math.min(EQ_GAIN_RANGE, Number(v) || 0)));
  S.settings.eq = merged;
  applyEQ();
  if (save) api('/api/settings', { key: 'eq', value: merged }).catch(() => {});
  return merged;
}

/** 切到某个预设 */
function setEQPreset(key) {
  const p = EQ_PRESETS[key];
  if (!p) return;
  setEQ({ preset: key, gains: p.gains.slice(), on: true }, true);
}

/* ============================================================ 界面 */

/**
 * 打开均衡器面板。
 *
 * 用底部弹出的小面板而不是占满屏幕的设置弹窗：调音要一边听一边拖，
 * 遮住播放条就没法边播边调了。
 */
function openEQ() {
  const st = eqState();
  const panel = document.createElement('div');
  panel.className = 'eq-panel';
  panel.id = 'eqPanel';
  panel.innerHTML = `
    <div class="eq-head">
      <span class="eq-title">图形均衡器</span>
      <button class="eq-switch ${st.on ? 'on' : ''}" id="eqSwitch">
        ${st.on ? '已开启' : '已关闭'}
      </button>
      <span class="eq-spacer"></span>
      <select class="eq-preset" id="eqPreset">
        ${Object.keys(EQ_PRESETS).map((k) =>
          `<option value="${k}"${k === st.preset ? ' selected' : ''}>${EQ_PRESETS[k].name}</option>`).join('')}
      </select>
      <button class="eq-reset" id="eqReset" title="所有频段归零">归零</button>
      <button class="eq-close" id="eqClose" title="关闭">✕</button>
    </div>
    <div class="eq-sliders">
      ${EQ_BANDS.map((f, i) => `
        <div class="eq-band">
          <span class="eq-db" data-i="${i}">${st.gains[i] > 0 ? '+' : ''}${st.gains[i].toFixed(0)}</span>
          <div class="eq-fader-rot">
            <i class="eq-rail"></i>
            <i class="eq-fill" data-i="${i}"></i>
            <input type="range" class="eq-fader" data-i="${i}"
                   min="${-EQ_GAIN_RANGE}" max="${EQ_GAIN_RANGE}" step="1"
                   value="${st.gains[i]}"
                   aria-label="${eqBandLabel(f)} Hz">
          </div>
          <span class="eq-freq">${eqBandLabel(f)}</span>
        </div>`).join('')}
    </div>
    <div class="eq-foot">
      往上拖 = 增强这一段，往下拖 = 削弱。频段从左到右由低到高。
      <b>开均衡不会改变总音量</b>，音量仍由右下角滑条控制。
    </div>`;
  document.body.appendChild(panel);
  // 下一帧加 class 触发入场动画。
  // 填充条要等布局完成后再画（这时 clientHeight 才是真实值，
  // 刚 appendChild 那一瞬间量不到高度）。
  requestAnimationFrame(() => {
    panel.classList.add('show');
    paintFromState(panel);
  });

  const close = () => {
    panel.classList.remove('show');
    setTimeout(() => panel.remove(), 180);
  };
  panel.querySelector('#eqClose').onclick = close;

  // 开关
  const sw = panel.querySelector('#eqSwitch');
  sw.onclick = () => {
    const on = !eqState().on;
    setEQ({ on }, true);
    sw.classList.toggle('on', on);
    sw.textContent = on ? '已开启' : '已关闭';
    updateEQBtn();
    toast(on ? '均衡器：已开启' : '均衡器：已关闭（原声）');
  };

  // 预设
  panel.querySelector('#eqPreset').onchange = (e) => {
    setEQPreset(e.target.value);
    paintFromState(panel);
    updateEQBtn();
  };

  // 归零
  panel.querySelector('#eqReset').onclick = () => {
    setEQ({ preset: 'flat', gains: EQ_BANDS.map(() => 0) }, true);
    panel.querySelector('#eqPreset').value = 'flat';
    paintFromState(panel);
    updateEQBtn();
  };

  // 各段滑条（竖向 range）
  panel.querySelectorAll('.eq-fader').forEach((el) => {
    const i = Number(el.dataset.i);
    const paintDb = () => {
      const v = Number(el.value);
      const out = panel.querySelector('.eq-db[data-i="' + i + '"]');
      if (out) out.textContent = (v > 0 ? '+' : '') + v.toFixed(0);
    };
    el.oninput = () => {
      const gains = eqState().gains.slice();
      gains[i] = Number(el.value);
      // 手动改任意一段 → 预设名失效，退回自定义；同时自动打开均衡，
      // 否则用户拖了半天没声音变化，会以为坏了
      setEQ({ gains, preset: 'custom', on: true }, false);
      const ps = panel.querySelector('#eqPreset');
      if (!ps.querySelector('option[value="custom"]')) {
        const o = document.createElement('option');
        o.value = 'custom'; o.textContent = '自定义';
        ps.appendChild(o);
      }
      ps.value = 'custom';
      panel.querySelector('#eqSwitch').classList.add('on');
      panel.querySelector('#eqSwitch').textContent = '已开启';
      paintDb();
      const fill = panel.querySelector('.eq-fill[data-i="' + i + '"]');
      if (fill) paintFill(fill, Number(el.value));
      updateEQBtn();
    };
    el.onchange = () => {
      setEQ({ gains: eqState().gains }, true);   // 松手才写盘
    };
  });
}

/** 把面板上的 UI 刷成当前状态（切预设 / 归零后调用）*/
function paintFromState(panel) {
  const st = eqState();
  panel.querySelectorAll('.eq-fader').forEach((el) => {
    const i = Number(el.dataset.i);
    el.value = st.gains[i];
    const out = panel.querySelector('.eq-db[data-i="' + i + '"]');
    if (out) out.textContent = (st.gains[i] > 0 ? '+' : '') + st.gains[i].toFixed(0);
  });
  panel.querySelectorAll('.eq-fill').forEach((el) => {
    paintFill(el, Number(eqState().gains[Number(el.dataset.i)]));
  });
  const sw = panel.querySelector('#eqSwitch');
  if (sw) {
    sw.classList.toggle('on', st.on);
    sw.textContent = st.on ? '已开启' : '已关闭';
  }
}

/**
 * 给一段画"填充条"：从中线（0dB）往当前值方向延伸。
 *
 * 为什么要自己画：range 的填充部分在 Chromium 里没有可靠的标准样式
 * （`::-moz-range-progress` 只有 Firefox 认），旋转之后更没保障。
 * 用一个绝对定位的 <i> 来画，位置完全由我们算，最稳。
 *
 * 几何：竖井可用高度是 H（.eq-rail 的 top/bottom 各留 8px）。
 * 0dB 在正中间，value 从 -12 到 +12 线性映射到 0~H。
 */
function paintFill(el, value) {
  const rot = el.parentElement;
  if (!rot) return;
  const H = rot.clientHeight - 16;          // 上下各留 8px，和 .eq-rail 一致
  if (H <= 0) return;
  const r = Math.max(-EQ_GAIN_RANGE, Math.min(EQ_GAIN_RANGE, Number(value) || 0));
  const half = H / 2;
  const frac = Math.abs(r) / EQ_GAIN_RANGE;  // 0~1
  const len = half * frac;
  if (len < 0.5) { el.style.height = '0px'; return; }
  el.style.height = len + 'px';
  if (r >= 0) {
    // 向上：中线以上
    el.style.top = (8 + half - len) + 'px';
    el.style.bottom = 'auto';
  } else {
    // 向下：中线以下
    el.style.top = (8 + half) + 'px';
    el.style.bottom = 'auto';
  }
}

/** 频点显示：低的那几段直接写 Hz，高的写成 kHz 更好读 */
function eqBandLabel(f) {
  return f >= 1000 ? (f / 1000) + 'k' : String(f);
}

/** 播放条上那个按钮的高亮状态 */
function updateEQBtn() {
  const b = document.getElementById('eqBtn');
  if (!b) return;
  const st = eqState();
  b.classList.toggle('on', st.on);
  const name = st.preset === 'custom'
    ? '自定义'
    : (EQ_PRESETS[st.preset] ? EQ_PRESETS[st.preset].name : '原声');
  b.textContent = st.on ? '🎛 均衡器' : '🎛 原声';
  b.title = st.on
    ? `图形均衡器已开（${name}）\n点开可以逐段调整，或换预设`
    : '图形均衡器已关：完全不改动音色';
}

/** 绑定播放条上的按钮（启动时调一次）*/
function bootEqualizer() {
  const b = document.getElementById('eqBtn');
  if (b) b.onclick = openEQ;
  updateEQBtn();
}

