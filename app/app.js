'use strict';

// はるみバス アプリ本体。
// 役割は時刻計算と表示のみ。パースの複雑さは pipeline 側(timetable.json)に寄せてある。

// 本番(Pages)では app/ がルート → ./data、ローカルはリポジトリ直下配信で /app/ を開く → ../data。
// 両方を順に試すことで配置差を吸収する。
const DATA_URLS = ['./data/timetable.json', '../data/timetable.json'];

const SCHEDULE_LABEL = { weekday: '平日ダイヤ', saturday: '土曜ダイヤ', holiday: '休日ダイヤ' };
const STORE_KEY = 'harumi-bus:enabled-stops';
const SHOW_PER_ROUTE = 3; // 各系統・方面で先読みする便数

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
        operatorName: r.operator_name,
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

// ---- 以降はブラウザ専用(DOM)。node からの require では実行しない。----------
if (typeof document !== 'undefined') {
  let cachedData = null;
  let tickTimer = null;

  const $ = (sel) => document.querySelector(sel);

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

  function fmtUpdated(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return `${d.getMonth() + 1}/${d.getDate()} ${pad2(d.getHours())}:${pad2(d.getMinutes())} 更新`;
  }

  function render() {
    const data = cachedData;
    if (!data) return;
    const now = new Date();
    const nowMin = now.getHours() * 60 + now.getMinutes();
    const scheduleType = scheduleTypeFor(now, data.holidays);

    $('#schedule-type').textContent = `本日: ${SCHEDULE_LABEL[scheduleType]}`;
    renderStatus(data);
    renderFreshness(data);

    const container = $('#stops');
    container.innerHTML = '';
    const stops = enabledStopsFor(data);

    for (const stopName of stops) {
      const routes = routesAtStop(data, stopName, scheduleType);

      // このバス停で「次に来る」1便(系統横断で最速)を強調表示。
      let soonest = null;
      for (const r of routes) {
        const next = upcoming(r.times, nowMin, 1)[0];
        if (next && (!soonest || next.min < soonest.min)) {
          soonest = { min: next.min, time: next.time, routeName: r.routeName, direction: r.direction };
        }
      }

      const card = document.createElement('section');
      card.className = 'stop-card';

      const h = document.createElement('h2');
      h.className = 'stop-name';
      h.textContent = stopName;
      card.appendChild(h);

      if (soonest) {
        const hero = document.createElement('div');
        hero.className = 'hero';
        hero.innerHTML =
          `<span class="hero-count">${fmtCountdown(soonest.min - nowMin)}</span>` +
          `<span class="hero-meta">${soonest.routeName} ${soonest.direction} ・ ${soonest.time}発</span>`;
        card.appendChild(hero);
      } else {
        const none = document.createElement('div');
        none.className = 'hero none';
        none.innerHTML = '<span class="hero-count">本日の運行は終了</span>';
        card.appendChild(none);
      }

      for (const r of routes) {
        const next = upcoming(r.times, nowMin, SHOW_PER_ROUTE);
        const row = document.createElement('div');
        row.className = 'route-row';
        const head = document.createElement('div');
        head.className = 'route-head';
        head.innerHTML = `<span class="route-badge">${r.routeName}</span><span class="route-dir">${r.direction}</span>`;
        row.appendChild(head);

        const times = document.createElement('div');
        times.className = 'route-times';
        if (next.length) {
          next.forEach((x, i) => {
            const span = document.createElement('span');
            span.className = 'time-chip' + (i === 0 ? ' soon' : '') + (x.min - nowMin <= 60 ? ' within-hour' : '');
            span.textContent = i === 0 ? `${x.time}(${fmtCountdown(x.min - nowMin)})` : x.time;
            times.appendChild(span);
          });
        } else {
          const span = document.createElement('span');
          span.className = 'time-chip end';
          span.textContent = '本日終了';
          times.appendChild(span);
        }
        row.appendChild(times);
        card.appendChild(row);
      }

      container.appendChild(card);
    }

    renderToggles(data);
  }

  function renderStatus(data) {
    const bar = $('#status-bar');
    const problems = [];
    const sources = data.sources || {};
    for (const key of Object.keys(sources)) {
      const s = sources[key];
      if (s.status && s.status !== 'ok') {
        const when = s.fetched_at ? fmtUpdated(s.fetched_at) : '';
        problems.push(`⚠ ${s.note || key + ' のデータが最新ではありません'}${when ? '(' + when + ')' : ''}`);
      }
    }
    if (problems.length) {
      bar.hidden = false;
      bar.textContent = problems.join(' / ');
    } else {
      bar.hidden = true;
    }
  }

  function renderFreshness(data) {
    $('#freshness').textContent = data.generated_at ? fmtUpdated(data.generated_at) : '';
  }

  function renderToggles(data) {
    const wrap = $('#stop-toggles');
    if (wrap.dataset.built === '1') return; // 一度だけ構築
    const all = stopNames(data);
    const enabled = enabledStopsFor(data);
    wrap.innerHTML = '';
    all.forEach((name) => {
      const label = document.createElement('label');
      label.className = 'toggle';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = enabled.indexOf(name) !== -1;
      cb.addEventListener('change', () => {
        const checked = Array.from(wrap.querySelectorAll('input:checked')).map((el) => el.value);
        saveEnabledStops(checked);
        render();
      });
      cb.value = name;
      label.appendChild(cb);
      label.appendChild(document.createTextNode(' ' + name));
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
      bar.textContent = '⚠ データを取得できません。オフラインの可能性があります。';
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
  module.exports = { toISODate, scheduleTypeFor, hhmmToMin, upcoming, routesAtStop, stopNames };
}
