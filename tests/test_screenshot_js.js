#!/usr/bin/env node
/* 电视画面前端逻辑测试（jsdom）：自动刷新节流、暂停开关、手动刷新、
 * 未连接占位、失败提示。跑：node tests/test_screenshot_js.js
 *
 * jsdom 来自工作区现成的 ~/workspace/manyue-reader/node_modules/jsdom
 *（只借来做测试，不进仓库、不打包进 fpk）。
 */
'use strict';
const fs = require('fs');
const path = require('path');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (e) {
  try {
    ({ JSDOM } = require('/home/hatch/workspace/manyue-reader/node_modules/jsdom'));
  } catch (e2) {
    console.log('SKIP: jsdom not found (' + e.message + ')');
    process.exit(0);
  }
}

const ROOT = path.resolve(__dirname, '..');
const PNG_B64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
const PNG = Buffer.from(PNG_B64, 'base64');

let passed = 0;
let failed = 0;
function check(label, ok, detail) {
  if (ok) { passed++; console.log('PASS ' + label + (detail ? ' ' + detail : '')); }
  else { failed++; console.log('FAIL ' + label + (detail ? ' ' + detail : '')); }
}

const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle(n) {
  for (let i = 0; i < (n || 6); i++) await tick();
}

// --- 桩 ---
let timers = {};
let nextTimerId = 1;
let fetchCalls = [];
let shotDeferred = null; // 当前挂起的 screenshot fetch（可控）
let connected = false;   // /api/status 返回的状态
let shotMode = 'ok';     // ok | error
let keyOk = true;        // /api/key 返回的 ok

function shotCalls() {
  return fetchCalls.filter((u) => u === '/api/screenshot').length;
}
function timerByMs(ms) {
  for (const id of Object.keys(timers)) {
    if (timers[id].ms === ms) return timers[id];
  }
  return null;
}
function timerIdByMs(ms) {
  for (const id of Object.keys(timers)) {
    if (timers[id].ms === ms) return id;
  }
  return null;
}

async function main() {
  const html = fs.readFileSync(path.join(ROOT, 'app/static/index.html'), 'utf8');
  const remoteJs = fs.readFileSync(path.join(ROOT, 'app/static/remote.js'), 'utf8');
  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:18091/',
    runScripts: 'outside-only',
    beforeParse(window) {
      window.setInterval = (fn, ms) => {
        const id = nextTimerId++;
        timers[id] = { fn, ms };
        return id;
      };
      window.clearInterval = (id) => { delete timers[id]; };
      window.URL.createObjectURL = () => 'blob:fake-shot';
      window.URL.revokeObjectURL = () => {};
      window.fetch = (url) => {
        fetchCalls.push(url);
        if (url === '/api/status') {
          return Promise.resolve({
            json: () => Promise.resolve({
              state: connected ? 'connected' : 'unknown',
              state_text: connected ? '已连接' : '未知状态',
              tv_name: '小米电视', tv_ip: '127.0.0.1', tv_port: 5555,
              model: 'MiTV-MockModel', latency_ms: 12,
            }),
          });
        }
        if (url === '/api/version') {
          return Promise.resolve({
            json: () => Promise.resolve({ version: '1.0.2', app: 'mitvremote' }),
          });
        }
        if (url === '/api/key') {
          return Promise.resolve({
            json: () => Promise.resolve(keyOk
              ? { ok: true }
              : { ok: false, error: '按键发送失败' }),
          });
        }
        if (url === '/api/screenshot') {
          if (shotMode === 'error') {
            return Promise.resolve({
              headers: { get: (n) => (n.toLowerCase() === 'content-type' ? 'application/json' : null) },
              json: () => Promise.resolve({ ok: false, state: 'refused', error: '连接被拒：电视 ADB 没开' }),
            });
          }
          // ok：返回一个可挂起的 promise，供节流测试控制
          const d = {};
          d.promise = new Promise((resolve) => { d.resolve = resolve; });
          shotDeferred = d;
          return d.promise.then(() => ({
            headers: { get: (n) => (n.toLowerCase() === 'content-type' ? 'image/png' : null) },
            blob: () => Promise.resolve(new window.Blob([PNG], { type: 'image/png' })),
          }));
        }
        return Promise.reject(new Error('unexpected fetch ' + url));
      };
    },
  });
  const { window } = dom;
  const { document } = window;
  dom.window.eval(remoteJs);

  const shotImg = document.getElementById('shot-img');
  const shotPlaceholder = document.getElementById('shot-placeholder');
  const shotSub = document.getElementById('shot-sub');
  const shotToggle = document.getElementById('btn-shot-toggle');
  const shotWrap = document.getElementById('shot-wrap');
  const statusTimer = () => timerByMs(15000);
  const shotTimer = () => timerByMs(2000);

  // 1. 初始未连接：不取截图，占位提示“未连接电视”
  await settle();
  check('初始未连接时不取截图', shotCalls() === 0, 'calls=' + shotCalls());
  check('未连接时占位显示“未连接电视”',
    shotPlaceholder.textContent.indexOf('未连接电视') !== -1
    && !shotPlaceholder.classList.contains('hidden'),
    JSON.stringify(shotPlaceholder.textContent));
  check('2 秒自动刷新定时器已启动', !!shotTimer(), 'timers=' + JSON.stringify(Object.values(timers).map((t) => t.ms)));

  // 2. 连上电视：状态刷新后立即取一张（不用等 2 秒）
  connected = true;
  statusTimer().fn();
  await settle();
  check('连上后立即取一张截图', shotCalls() === 1, 'calls=' + shotCalls());
  check('取图中占位仍在（图未回来）', !shotPlaceholder.classList.contains('hidden'));

  // 3. 节流：上一张没回来时，定时器再触发也不发新请求
  shotTimer().fn();
  shotTimer().fn();
  await settle(3);
  check('上一张未回来时定时触发不堆积', shotCalls() === 1, 'calls=' + shotCalls());

  // 4. 图回来后显示，更新时间出现
  shotDeferred.resolve();
  await settle();
  check('截图回来后图片显示', shotImg.src === 'blob:fake-shot'
    && !shotImg.classList.contains('hidden'), 'src=' + shotImg.src);
  check('截图回来后占位隐藏', shotPlaceholder.classList.contains('hidden'));
  check('截图回来后显示更新时间', shotSub.textContent.indexOf('已更新') === 0, JSON.stringify(shotSub.textContent));

  // 5. 下一轮定时器正常再取一张，图回来
  shotTimer().fn();
  await settle(3);
  check('上一张回来后定时器可再取', shotCalls() === 2, 'calls=' + shotCalls());
  shotDeferred.resolve();
  await settle();

  // 6. 暂停：立即停定时器，不再自动取；按钮文案变“继续”
  shotToggle.click();
  check('暂停后按钮变“▶ 继续”', shotToggle.textContent === '▶ 继续', JSON.stringify(shotToggle.textContent));
  check('暂停后 2 秒定时器已停', !shotTimer(), 'timers=' + JSON.stringify(Object.values(timers).map((t) => t.ms)));
  check('暂停后提示已暂停', shotSub.textContent.indexOf('已暂停') !== -1, JSON.stringify(shotSub.textContent));

  // 7. 暂停期间点画面仍可手动刷新（上一张挂起时不重复发）
  shotWrap.click();
  await settle(3);
  check('暂停时点画面手动刷新一次', shotCalls() === 3, 'calls=' + shotCalls());
  shotWrap.click();
  await settle(3);
  check('手动刷新进行中再点不重复发', shotCalls() === 3, 'calls=' + shotCalls());
  shotDeferred.resolve();
  await settle();
  check('手动刷新的图回来后正常显示', shotImg.src === 'blob:fake-shot'
    && shotSub.textContent.indexOf('已更新') === 0, JSON.stringify(shotSub.textContent));

  // 8. 继续：立即取一张，定时器恢复
  shotToggle.click();
  await settle(3);
  check('继续后按钮变回“⏸ 暂停”', shotToggle.textContent === '⏸ 暂停', JSON.stringify(shotToggle.textContent));
  check('继续后立即取一张', shotCalls() === 4, 'calls=' + shotCalls());
  check('继续后 2 秒定时器恢复', !!shotTimer());
  shotDeferred.resolve();
  await settle();

  // 9. 截图失败：下方留一句话，不刷屏、不崩、保留上一张图
  shotMode = 'error';
  shotTimer().fn();
  await settle();
  check('截图失败时显示可读原因', shotSub.textContent.indexOf('连接被拒') !== -1, JSON.stringify(shotSub.textContent));
  check('截图失败时保留上一张图', !shotImg.classList.contains('hidden')
    && shotImg.src === 'blob:fake-shot');
  check('截图失败后下一轮还能继续取', (() => { shotMode = 'ok'; shotTimer().fn(); return shotCalls() === 6; })(), 'calls=' + shotCalls());
  shotDeferred.resolve();
  await settle();

  // 10. 断开连接：有图时保留最后一张，不报错刷屏
  connected = false;
  statusTimer().fn();
  await settle();
  check('断开后保留最后一张图', !shotImg.classList.contains('hidden'));

  // 11. 按键成功后立刻刷新一次截图（1.0.2）
  const keyBtn = document.querySelector('[data-key="up"]');
  check('测试用的按键按钮存在', !!keyBtn);

  // 11a. 重新连上：状态刷新后立即取一张
  connected = true;
  statusTimer().fn();
  await settle();
  const callsAfterReconnect = shotCalls();
  check('重连后立即取一张截图', callsAfterReconnect === 7, 'calls=' + callsAfterReconnect);
  shotDeferred.resolve();
  await settle();

  // 11b. 按键成功：不等 2 秒定时器，立刻多取一张；计时器被重置（旧 id 消失、新 id 出现）
  const timerIdBefore = timerIdByMs(2000);
  const callsBeforeKey = shotCalls();
  keyBtn.click();
  await settle();
  check('按键成功后立刻多取一张截图', shotCalls() === callsBeforeKey + 1,
    'calls=' + shotCalls() + ' before=' + callsBeforeKey);
  const timerIdAfter = timerIdByMs(2000);
  check('按键后 2 秒计时器被重置', !!timerIdBefore && !!timerIdAfter && timerIdBefore !== timerIdAfter,
    'before=' + timerIdBefore + ' after=' + timerIdAfter);
  check('旧计时器 id 已清除', !(timerIdBefore in timers), 'id=' + timerIdBefore);
  shotDeferred.resolve();
  await settle();

  // 11c. 上一张截图还在取：按键后不重复发，计时器也不重置
  shotTimer().fn(); // 再取一张（挂起）
  await settle(3);
  const callsInFlight = shotCalls();
  const timerIdInFlight = timerIdByMs(2000);
  keyBtn.click();
  await settle();
  check('上一张未回来时按键不重复取截图', shotCalls() === callsInFlight, 'calls=' + shotCalls());
  check('上一张未回来时按键不重置计时器', timerIdByMs(2000) === timerIdInFlight,
    'id=' + timerIdByMs(2000));
  shotDeferred.resolve();
  await settle();

  // 11d. 暂停时：按键成功后仍立刻刷新一次，定时器保持停着
  shotToggle.click(); // 暂停
  check('暂停后 2 秒定时器已停', !shotTimer());
  const callsBeforePausedKey = shotCalls();
  keyBtn.click();
  await settle();
  check('暂停时按键仍立刻刷新一次', shotCalls() === callsBeforePausedKey + 1,
    'calls=' + shotCalls());
  check('暂停时按键后定时器仍停着', !shotTimer());
  shotDeferred.resolve();
  await settle();
  shotToggle.click(); // 恢复，免得影响后续断言
  await settle(3);
  shotDeferred.resolve();
  await settle();

  // 11e. 按键失败：不刷新截图
  keyOk = false;
  const callsBeforeFailKey = shotCalls();
  keyBtn.click();
  await settle();
  check('按键失败时不刷新截图', shotCalls() === callsBeforeFailKey, 'calls=' + shotCalls());
  keyOk = true;

  // 11f. 未连接：即使按键接口返回成功也不刷新截图
  connected = false;
  statusTimer().fn();
  await settle();
  const callsBeforeUnconnectedKey = shotCalls();
  keyBtn.click();
  await settle();
  check('未连接时按键不刷新截图', shotCalls() === callsBeforeUnconnectedKey,
    'calls=' + shotCalls());

  console.log('== 汇总：' + passed + '/' + (passed + failed) + ' 通过 ==');
  process.exit(failed ? 1 : 0);
}

main().catch((err) => {
  console.error('FAIL jsdom 测试异常', err);
  process.exit(1);
});
