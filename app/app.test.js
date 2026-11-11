'use strict';

// app.js の純粋関数(時刻計算・鮮度・3社横断)のテスト。
//   node --test app/app.test.js
// ブラウザ・ビルドツール不要。DOM 側は含めない(ブラウザで目視確認する)。

const test = require('node:test');
const assert = require('node:assert/strict');
const app = require('./app.js');

const DATA = {
  generated_at: '2026-11-10T03:00:00+09:00',
  sources: {
    toei: { status: 'ok', fetched_at: '2026-11-10T03:00:00+09:00' },
    brt: { status: 'ok', fetched_at: '2026-11-10T03:00:00+09:00' },
    harumi: { status: 'stale', fetched_at: '2026-11-08T03:00:00+09:00', note: '晴海ライナーの取得に失敗したため前回データ' },
  },
  holidays: ['2026-11-23'],
  routes: [
    { id: 'toei-05-1-1', operator: 'toei', operator_name: '都営バス', route_name: '都05-1', direction: '東京駅丸の内南口',
      stops: [{ stop_name: 'はるみらい前', group: 'depart', weekday: ['08:00', '08:20', '08:40'], saturday: ['09:00'], holiday: ['09:00'] }] },
    { id: 'brt-b01', operator: 'brt', operator_name: '東京BRT', route_name: '選手村ルート', direction: '新橋ゆき',
      stops: [{ stop_name: '晴海五丁目ターミナル', group: 'depart', weekday: ['08:05', '08:15'], saturday: [], holiday: [] }] },
    { id: 'harumi-tokyo', operator: 'harumi', operator_name: '晴海ライナー', route_name: '晴海ライナー', direction: '東京駅八重洲北口ゆき',
      stops: [{ stop_name: '晴海二丁目', group: 'depart', weekday: ['08:02'], saturday: [], holiday: [] }] },
    { id: 'toei-05-1-0', operator: 'toei', operator_name: '都営バス', route_name: '都05-1', direction: '晴海埠頭',
      stops: [{ stop_name: '東京駅丸の内南口', group: 'return', weekday: ['18:00'], saturday: [], holiday: [] }] },
  ],
};

test('scheduleTypeFor: 平日/土曜/日曜/祝日の境界', () => {
  assert.equal(app.scheduleTypeFor(new Date(2026, 10, 10), DATA.holidays), 'weekday'); // 火
  assert.equal(app.scheduleTypeFor(new Date(2026, 10, 14), DATA.holidays), 'saturday');
  assert.equal(app.scheduleTypeFor(new Date(2026, 10, 15), DATA.holidays), 'holiday');
  assert.equal(app.scheduleTypeFor(new Date(2026, 10, 23), DATA.holidays), 'holiday'); // 勤労感謝の日(月)
});

test('upcoming: 現在時刻以降を近い順に count 件', () => {
  const r = app.upcoming(['08:40', '08:00', '08:20'], 8 * 60 + 5, 2);
  assert.deepEqual(r.map((x) => x.time), ['08:20', '08:40']);
  assert.deepEqual(app.upcoming(['07:00'], 8 * 60, 3), []);
});

test('stopNames / stopGroups', () => {
  assert.deepEqual(app.stopNames(DATA), ['はるみらい前', '晴海五丁目ターミナル', '晴海二丁目', '東京駅丸の内南口']);
  assert.deepEqual(app.stopGroups(DATA), {
    'はるみらい前': 'depart', '晴海五丁目ターミナル': 'depart', '晴海二丁目': 'depart', '東京駅丸の内南口': 'return',
  });
});

test('nextAcross: 3社横断で時刻順に並び、事業者情報を持つ', () => {
  const names = ['はるみらい前', '晴海五丁目ターミナル', '晴海二丁目'];
  const items = app.nextAcross(DATA, names, 'weekday', 8 * 60 + 1, 4);
  assert.deepEqual(items.map((x) => [x.time, x.operator, x.stopName]), [
    ['08:02', 'harumi', '晴海二丁目'],
    ['08:05', 'brt', '晴海五丁目ターミナル'],
    ['08:15', 'brt', '晴海五丁目ターミナル'],
    ['08:20', 'toei', 'はるみらい前'],
  ]);
  assert.equal(items[0].operatorName, '晴海ライナー');
  assert.equal(items[0].direction, '東京駅八重洲北口ゆき');
  // 帰宅側は含めない(区分ごとに呼び分ける)
  assert.ok(items.every((x) => x.stopName !== '東京駅丸の内南口'));
  // 運行終了後は空
  assert.deepEqual(app.nextAcross(DATA, names, 'weekday', 23 * 60, 4), []);
});

test('freshnessOf: 相対表示と段階(fresh/stale/old/unknown)', () => {
  const gen = '2026-11-10T03:00:00+09:00';
  const at = (h) => new Date(new Date(gen).getTime() + h * 3600 * 1000);
  assert.deepEqual(app.freshnessOf(gen, at(0)), { ageMin: 0, label: 'たった今更新', level: 'fresh' });
  assert.equal(app.freshnessOf(gen, at(0.5)).label, '30分前に更新');
  assert.equal(app.freshnessOf(gen, at(7)).label, '7時間前に更新');
  assert.equal(app.freshnessOf(gen, at(29)).level, 'fresh');
  assert.equal(app.freshnessOf(gen, at(31)).level, 'stale');   // 1回更新が飛んだ
  assert.equal(app.freshnessOf(gen, at(50)).label, '2日前に更新');
  assert.equal(app.freshnessOf(gen, at(73)).level, 'old');
  assert.equal(app.freshnessOf(undefined, at(0)).level, 'unknown');
  assert.equal(app.freshnessOf('garbage', at(0)).level, 'unknown');
});

test('sourceSummary: 事業者順・状態・表示名', () => {
  const s = app.sourceSummary(DATA);
  assert.deepEqual(s.map((x) => [x.key, x.name, x.status]), [
    ['toei', '都バス', 'ok'], ['brt', '東京BRT', 'ok'], ['harumi', '晴海ライナー', 'stale'],
  ]);
  assert.match(s[2].note, /前回データ/);
  assert.deepEqual(app.sourceSummary({}), []);
  // sources に無い事業者は出さない(Phase 1 の旧データ)
  assert.deepEqual(app.sourceSummary({ sources: { toei: { status: 'ok' } } }).map((x) => x.key), ['toei']);
});
