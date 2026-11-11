'use strict';

// はるみバス アプリ本体。
// 役割は時刻計算と表示のみ。パースの複雑さは pipeline 側(timetable.json)に寄せてある。

// 本番(Pages)では app/ がルート → ./data、ローカルはリポジトリ直下配信で /app/ を開く → ../data。
// 両方を順に試すことで配置差を吸収する。
const DATA_URLS = ['./data/timetable.json', '../data/timetable.json'];

const SCHEDULE_LABEL = { weekday: '平日ダイヤ', saturday: '土曜ダイヤ', holiday: '休日ダイヤ' };
const STORE_KEY = 'harumi-bus:enabled-stops';
const SHOW_PER_ROUTE = 3;   // 各系統・方面で先読みする便数
const OVERVIEW_COUNT = 4;   // 3社横断「次に来るバス」で区分ごとに出す便数

// 事業者キー → 表示名(timetable.json に operator_name が無い旧データ用のフォールバック)
const OPERATOR_LABEL = { toei: '都バス', brt: '東京BRT', harumi: '晴海ライナー' };
const SOURCE_ORDER = ['toei', 'brt', 'harumi'];

// 鮮度の段階。pipeline は毎日 03:00 JST に 1 回動くので、
// 30 時間を超えたら「1回更新が飛んだ」、3 日を超えたら「明らかに古い」とみなす。
const FRESH_HOURS = 30;
const OLD_HOURS = 72;

const GROUP_SECTIONS = [
  { key: 'depart', label: 'おでかけ', dest: '都心方面' },
  { key: 'return', label: '帰宅', dest: '晴海方面' },
];

// ---- 純粋関数(時刻ロジック)------------------------------------------------
function pad2(n) { return String(n).padStart(2, '0'); }

function toISODate(date) {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

// その日のダイヤ区分を決める。祝日は holidays 配列で休日ダイヤに割り当てる。
function scheduleTypeFor(date, holidays) {
  if (holidays && holidays.indexOf(toISODate(date)) !== -1) return 'holiday';
  const dow = date.getDay(); // 0=日 .. 6=土
  if (dow === 0) return 'holiday';
  if (dow === 6) return 'saturday';
  return 'weekday';
}

function hhmmToMin(s) {
  const parts = s.split(':');
  return parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10);
}

// times("HH:MM"配列) のうち now(分) 以降で最も近い便を count 件返す。
function upcoming(times, nowMin, count) {
  const up = times
    .map((t) => ({ time: t, min: hhmmToMin(t) }))
    .filter((x) => x.min >= nowMin)
    .sort((a, b) => a.min - b.min);
  return count ? up.slice(0, count) : up;
}

// data(timetable.json) + 対象バス停名 → その停を通る {routeName,direction,times} の配列。
function routesAtStop(data, stopName, scheduleType) {
  const out = [];
  for (const r of data.routes || []) {
    for (const s of r.stops || []) {
      if (s.stop_name !== stopName) continue;
      out.push({
        routeName: r.route_name,
        direction: r.direction,
        operator: r.operator,
        operatorName: r.operator_name || OPERATOR_LABEL[r.operator] || r.operator,
        times: s[scheduleType] || [],
      });
    }
  }
  return out;
}

function stopNames(data) {
  const seen = [];
  for (const r of data.routes || []) {
    for (const s of r.stops || []) {
      if (seen.indexOf(s.stop_name) === -1) seen.push(s.stop_name);
    }
  }
  return seen;
}

// 停留所名 → 区分け(depart=おでかけ / return=帰宅)。timetable.json の stops[].group から。
function stopGroups(data) {
  const map = {};
  for (const r of data.routes || []) {
    for (const s of r.stops || []) {
      if (s.group && !(s.stop_name in map)) map[s.stop_name] = s.group;
    }
  }
  return map;
}

// 3社横断: 複数バス停の全系統から「次に来る便」を時刻順に count 件返す。
// 戻り値は {min,time,stopName,routeName,direction,operator,operatorName} の配列。
function nextAcross(data, names, scheduleType, nowMin, count) {
  const all = [];
  for (const stopName of names) {
    for (const r of routesAtStop(data, stopName, scheduleType)) {
      for (const x of upcoming(r.times, nowMin, count)) {
        all.push({
          min: x.min, time: x.time, stopName,
          routeName: r.routeName, direction: r.direction,
          operator: r.operator, operatorName: r.operatorName,
        });
      }
    }
  }
  all.sort((a, b) => a.min - b.min || a.stopName.localeCompare(b.stopName, 'ja'));
  return count ? all.slice(0, count) : all;
}

// generated_at(ISO文字列) → {ageMin, label, level}。
// level: fresh(通常) / stale(更新が1回以上飛んだ) / old(3日超) / unknown(解釈不能)
function freshnessOf(generatedAt, now) {
  const d = new Date(generatedAt);
  if (!generatedAt || isNaN(d)) return { ageMin: null, label: '更新日時不明', level: 'unknown' };
  const ageMin = Math.max(0, Math.round((now - d) / 60000));
  let label;
  if (ageMin < 1) label = 'たった今更新';
  else if (ageMin < 60) label = `${ageMin}分前に更新`;
  else if (ageMin < 24 * 60) label = `${Math.floor(ageMin / 60)}時間前に更新`;
  else label = `${Math.floor(ageMin / (24 * 60))}日前に更新`;
  const hours = ageMin / 60;
  const level = hours > OLD_HOURS ? 'old' : hours > FRESH_HOURS ? 'stale' : 'fresh';
  return { ageMin, label, level };
}

// sources(timetable.json) → 事業者ごとの取得状態の一覧(表示順は SOURCE_ORDER)。
function sourceSummary(data) {
  const sources = (data && data.sources) || {};
  const keys = SOURCE_ORDER.filter((k) => k in sources)
    .concat(Object.keys(sources).filter((k) => SOURCE_ORDER.indexOf(k) === -1));
  return keys.map((key) => {
    const s = sources[key] || {};
    return {
      key,
      name: OPERATOR_LABEL[key] || key,
      status: s.status || 'unknown',
      fetchedAt: s.fetched_at || '',
      note: s.note || '',
    };
  });
}

// ---- 以降はブラウザ専用(DOM)。node からの require では実行しない。----------
if (typeof document !== 'undefined') {
  let cachedData = null;
  let tickTimer = null;

  const $ = (sel) => document.querySelector(sel);

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function loadEnabledStops() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (_) { return null; }
  }
  function saveEnabledStops(list) {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(list)); } catch (_) {}
  }
  // 未設定なら全停を表示。設定済みなら交差(消えた停は無視)。
  function enabledStopsFor(data) {
    const all = stopNames(data);
    const saved = loadEnabledStops();
    if (!saved) return all;
    const kept = all.filter((n) => saved.indexOf(n) !== -1);
    return kept.length ? kept : all;
  }

  function fmtCountdown(diffMin) {
    if (diffMin <= 0) return 'まもなく';
    if (diffMin < 60) return `あと${diffMin}分`;
    const h = Math.floor(diffMin / 60), m = diffMin % 60;
    return `あと${h}時間${m}分`;
  }

  // カウントダウンを「数字を大きく・単位を小さく」組むための要素を返す。
  function countdownNode(diffMin, className) {
    const wrap = el('span', className);
    if (diffMin <= 0) {
      wrap.appendChild(el('span', 'cd-now', 'まもなく'));
      return wrap;
    }
    wrap.appendChild(el('span', 'cd-pre', 'あと'));
    if (diffMin < 60) {
      wrap.appendChild(el('span', 'cd-num', String(diffMin)));
      wrap.appendChild(el('span', 'cd-unit', '分'));
    } else {
      wrap.appendChild(el('span', 'cd-num', String(Math.floor(diffMin / 60))));
      wrap.appendChild(el('span', 'cd-unit', '時間'));
      wrap.appendChild(el('span', 'cd-num', String(diffMin % 60)));
      wrap.appendChild(el('span', 'cd-unit', '分'));
    }
    return wrap;
  }

  function urgencyClass(diffMin) {
    if (diffMin <= 3) return 'urgent';
    if (diffMin <= 15) return 'soon';
    return '';
  }

  function fmtDateTime(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return `${d.getMonth() + 1}/${d.getDate()} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  }

  function fmtUpdated(iso) {
    return `${fmtDateTime(iso)} 更新`;
  }

  function badge(operator, routeName) {
    return el('span', `route-badge op-${operator || 'other'}`, routeName);
  }

  function render() {
    const data = cachedData;
    if (!data) return;
    const now = new Date();
    const nowMin = now.getHours() * 60 + now.getMinutes();
    const scheduleType = scheduleTypeFor(now, data.holidays);
    const stops = enabledStopsFor(data);
    const groups = stopGroups(data);

    renderToday(now, scheduleType);
    renderStatus(data);
    renderFreshness(data, now);

    // 区分け情報があれば「おでかけ/帰宅」のセクションに分ける(無い旧データは平置き)
    const sections = Object.keys(groups).length ? GROUP_SECTIONS : [{ key: null }];
    const namesFor = (section) => (section.key
      ? stops.filter((n) => (groups[n] || 'depart') === section.key)
      : stops);

    renderOverview(data, sections, namesFor, scheduleType, nowMin);

    const container = $('#stops');
    container.innerHTML = '';
    for (const section of sections) {
      const names = namesFor(section);
      if (!names.length) continue;

      if (section.key) {
        const head = el('h2', `group-head group-${section.key}`);
        head.appendChild(el('span', 'group-label', section.label));
        head.appendChild(el('span', 'group-dest', section.dest));
        container.appendChild(head);
      }

      for (const stopName of names) {
        container.appendChild(buildStopCard(data, stopName, scheduleType, nowMin, section.key));
      }
    }

    renderToggles(data);
  }

  function renderToday(now, scheduleType) {
    const dow = '日月火水木金土'[now.getDay()];
    $('#today-date').textContent = `${now.getMonth() + 1}/${now.getDate()}(${dow})`;
    const label = $('#schedule-type');
    label.textContent = SCHEDULE_LABEL[scheduleType];
    label.className = `schedule-type sched-${scheduleType}`;
  }

  // 3社横断「次に来るバス」: 区分ごとに全バス停・全事業者を時刻順に並べる。
  function renderOverview(data, sections, namesFor, scheduleType, nowMin) {
    const wrap = $('#overview');
    wrap.innerHTML = '';
    let any = false;
    for (const section of sections) {
      const names = namesFor(section);
      if (!names.length) continue;
      const items = nextAcross(data, names, scheduleType, nowMin, OVERVIEW_COUNT);

      const panel = el('section', `overview-panel${section.key ? ' group-' + section.key : ''}`);
      const head = el('h2', 'overview-head');
      head.appendChild(el('span', 'overview-title', '次に来るバス'));
      if (section.key) head.appendChild(el('span', 'overview-group', `${section.label} → ${section.dest}`));
      panel.appendChild(head);

      if (!items.length) {
        panel.appendChild(el('p', 'overview-empty', '本日の運行は終了しました'));
        wrap.appendChild(panel);
        any = true;
        continue;
      }

      const list = el('ol', 'overview-list');
      items.forEach((x, i) => {
        const diff = x.min - nowMin;
        const li = el('li', `overview-item op-${x.operator}${i === 0 ? ' first' : ''} ${urgencyClass(diff)}`);
        li.appendChild(countdownNode(diff, 'overview-count'));
        const meta = el('div', 'overview-meta');
        const line1 = el('div', 'overview-line');
        line1.appendChild(badge(x.operator, x.routeName));
        line1.appendChild(el('span', 'overview-dir', x.direction));
        meta.appendChild(line1);
        const line2 = el('div', 'overview-sub');
        line2.appendChild(el('span', 'overview-time', `${x.time}発`));
        line2.appendChild(el('span', 'overview-stop', x.stopName));
        meta.appendChild(line2);
        li.appendChild(meta);
        list.appendChild(li);
      });
      panel.appendChild(list);
      wrap.appendChild(panel);
      any = true;
    }
    wrap.hidden = !any;
  }

  function buildStopCard(data, stopName, scheduleType, nowMin, groupKey) {
    const routes = routesAtStop(data, stopName, scheduleType);

    // このバス停で「次に来る」1便(系統横断で最速)を強調表示。
    let soonest = null;
    for (const r of routes) {
      const next = upcoming(r.times, nowMin, 1)[0];
      if (next && (!soonest || next.min < soonest.min)) {
        soonest = {
          min: next.min, time: next.time,
          routeName: r.routeName, direction: r.direction, operator: r.operator,
        };
      }
    }

    const card = el('section', 'stop-card' + (groupKey ? ` group-${groupKey}` : ''));

    const head = el('header', 'stop-head');
    const h = el('h3', 'stop-name', stopName);
    head.appendChild(h);
    // 事業者の色点(このバス停に来る事業者を一目で)
    const ops = [];
    for (const r of routes) if (ops.indexOf(r.operator) === -1) ops.push(r.operator);
    const dots = el('span', 'op-dots');
    for (const op of ops) {
      const dot = el('span', `op-dot op-${op}`);
      dot.title = OPERATOR_LABEL[op] || op;
      dots.appendChild(dot);
    }
    head.appendChild(dots);
    card.appendChild(head);

    if (soonest) {
      const diff = soonest.min - nowMin;
      const hero = el('div', `hero op-${soonest.operator || 'other'} ${urgencyClass(diff)}`);
      hero.appendChild(countdownNode(diff, 'hero-count'));
      const meta = el('div', 'hero-meta');
      meta.appendChild(badge(soonest.operator, soonest.routeName));
      meta.appendChild(el('span', 'hero-dir', soonest.direction));
      meta.appendChild(el('span', 'hero-time', `${soonest.time}発`));
      hero.appendChild(meta);
      card.appendChild(hero);
    } else {
      const none = el('div', 'hero none');
      none.appendChild(el('span', 'hero-end', '本日の運行は終了'));
      card.appendChild(none);
    }

    for (const r of routes) {
      const next = upcoming(r.times, nowMin, SHOW_PER_ROUTE);
      const row = el('div', `route-row op-${r.operator || 'other'}`);
      const rh = el('div', 'route-head');
      rh.appendChild(badge(r.operator, r.routeName));
      rh.appendChild(el('span', 'route-dir', r.direction));
      row.appendChild(rh);

      const times = el('div', 'route-times');
      if (next.length) {
        next.forEach((x, i) => {
          const diff = x.min - nowMin;
          const chip = el('span', 'time-chip' + (i === 0 ? ' lead' : '') + (diff <= 60 ? ' within-hour' : ''));
          chip.appendChild(el('span', 'chip-time', x.time));
          if (i === 0) chip.appendChild(el('span', 'chip-cd', fmtCountdown(diff)));
          times.appendChild(chip);
        });
      } else {
        times.appendChild(el('span', 'time-chip end', '本日終了'));
      }
      row.appendChild(times);
      card.appendChild(row);
    }

    return card;
  }

  function renderStatus(data) {
    const bar = $('#status-bar');
    const problems = [];
    for (const s of sourceSummary(data)) {
      if (s.status !== 'ok') {
        const when = s.fetchedAt ? `(${fmtDateTime(s.fetchedAt)}時点)` : '';
        problems.push(`${s.note || s.name + 'のデータが最新ではありません'}${when}`);
      }
    }
    bar.innerHTML = '';
    if (problems.length) {
      bar.hidden = false;
      for (const p of problems) bar.appendChild(el('p', 'status-line', `⚠ ${p}`));
    } else {
      bar.hidden = true;
    }
  }

  // 鮮度表示: ヘッダーのピル(相対時刻)+フッターの絶対時刻+事業者ごとの取得状態。
  function renderFreshness(data, now) {
    const f = freshnessOf(data.generated_at, now);
    const pill = $('#freshness-pill');
    pill.textContent = f.label;
    pill.className = `pill fresh-${f.level}`;
    pill.title = data.generated_at ? fmtUpdated(data.generated_at) : '';

    $('#freshness').textContent = data.generated_at
      ? `時刻表データ: ${fmtUpdated(data.generated_at)}(${f.label})`
      : '';

    const list = $('#sources');
    list.innerHTML = '';
    for (const s of sourceSummary(data)) {
      const li = el('li', `source src-${s.status}`);
      li.appendChild(el('span', `op-dot op-${s.key}`));
      li.appendChild(el('span', 'source-name', s.name));
      let text;
      if (s.status === 'ok') text = s.fetchedAt ? fmtDateTime(s.fetchedAt) : '取得済み';
      else if (s.status === 'stale') text = `前回データ(${s.fetchedAt ? fmtDateTime(s.fetchedAt) : '日時不明'})`;
      else if (s.status === 'missing') text = '未取得';
      else text = s.status;
      li.appendChild(el('span', 'source-state', text));
      list.appendChild(li);
    }
  }

  function renderToggles(data) {
    const wrap = $('#stop-toggles');
    if (wrap.dataset.built === '1') return; // 一度だけ構築
    const all = stopNames(data);
    const enabled = enabledStopsFor(data);
    const groups = stopGroups(data);
    wrap.innerHTML = '';
    all.forEach((name) => {
      const label = el('label', 'toggle');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = enabled.indexOf(name) !== -1;
      cb.addEventListener('change', () => {
        const checked = Array.from(wrap.querySelectorAll('input:checked')).map((e) => e.value);
        saveEnabledStops(checked);
        render();
      });
      cb.value = name;
      label.appendChild(cb);
      label.appendChild(el('span', 'toggle-name', name));
      const g = groups[name];
      if (g) {
        const sec = GROUP_SECTIONS.find((s) => s.key === g);
        if (sec) label.appendChild(el('span', `toggle-group group-${g}`, sec.label));
      }
      wrap.appendChild(label);
    });
    wrap.dataset.built = '1';
  }

  async function fetchData() {
    for (const url of DATA_URLS) {
      try {
        const res = await fetch(url, { cache: 'no-cache' });
        if (res.ok) return await res.json();
      } catch (_) { /* 次の候補へ */ }
    }
    throw new Error('timetable.json を取得できませんでした');
  }

  async function refresh() {
    const btn = $('#refresh');
    btn.classList.add('spinning');
    try {
      cachedData = await fetchData();
      render();
    } catch (e) {
      const bar = $('#status-bar');
      bar.hidden = false;
      bar.innerHTML = '';
      bar.appendChild(el('p', 'status-line', '⚠ データを取得できません。オフラインの可能性があります。'));
    } finally {
      btn.classList.remove('spinning');
    }
  }

  function startTicking() {
    if (tickTimer) clearInterval(tickTimer);
    // カウントダウンを追随(データ再取得はしない)。
    tickTimer = setInterval(() => { if (cachedData) render(); }, 20000);
  }

  function init() {
    $('#refresh').addEventListener('click', refresh);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) refresh(); // 復帰時に最新化
    });
    refresh();
    startTicking();

    if ('serviceWorker' in navigator) {
      window.addEventListener('load', () => {
        navigator.serviceWorker.register('./sw.js').catch(() => {});
      });
    }
  }

  document.addEventListener('DOMContentLoaded', init);
}

// node からのテスト用に純粋関数を公開。
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    toISODate, scheduleTypeFor, hhmmToMin, upcoming, routesAtStop, stopNames, stopGroups,
    nextAcross, freshnessOf, sourceSummary,
  };
}
