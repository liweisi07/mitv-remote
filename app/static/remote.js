(function () {
  'use strict';
  var dot = document.getElementById('status-dot');
  var statusText = document.getElementById('status-text');
  var statusSub = document.getElementById('status-sub');
  var msgEl = document.getElementById('msg');
  var okEl = document.getElementById('okmsg');
  var msgTimer = null;

  function showError(text) {
    msgEl.textContent = text;
    msgEl.classList.remove('hidden');
    okEl.classList.add('hidden');
    if (msgTimer) clearTimeout(msgTimer);
    msgTimer = setTimeout(function () { msgEl.classList.add('hidden'); }, 8000);
  }
  function showOk(text) {
    okEl.textContent = text;
    okEl.classList.remove('hidden');
    msgEl.classList.add('hidden');
    if (msgTimer) clearTimeout(msgTimer);
    msgTimer = setTimeout(function () { okEl.classList.add('hidden'); }, 5000);
  }
  function buzz() {
    if (navigator.vibrate) { try { navigator.vibrate(15); } catch (e) {} }
  }

  function renderStatus(data) {
    var state = data.state || 'unknown';
    dot.className = 'dot' + (state === 'connected' ? ' on'
      : (state === 'awaiting_auth' ? ' warn'
      : (state === 'refused' || state === 'error' ? ' off' : '')));
    statusText.textContent = data.state_text || data.state || '未知状态';
    var sub = [];
    if (data.tv_name) sub.push(data.tv_name);
    if (data.tv_ip) sub.push(data.tv_ip + ':' + (data.tv_port || 5555));
    if (data.model) sub.push(data.model);
    if (data.latency_ms != null) sub.push(data.latency_ms + 'ms');
    statusSub.textContent = sub.join(' · ');
    if (state === 'not_configured') {
      statusSub.textContent += ' — 点右上角“设置”填写';
    }
  }

  function refreshStatus() {
    fetch('/api/status')
      .then(function (r) { return r.json(); })
      .then(function (data) { renderStatus(data); })
      .catch(function () { statusText.textContent = '拿不到状态，请检查网络'; });
  }

  function postJson(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); });
  }

  function sendKey(key, btn) {
    btn.disabled = true;
    postJson('/api/key', { key: key })
      .then(function (data) {
        buzz();
        if (data.ok) {
          refreshStatus();
        } else {
          showError(data.error || '按键发送失败');
          refreshStatus();
        }
      })
      .catch(function () { showError('发送失败，请检查网络'); })
      .then(function () { btn.disabled = false; });
  }

  var keyBtns = document.querySelectorAll('[data-key]');
  for (var i = 0; i < keyBtns.length; i++) {
    (function (btn) {
      btn.addEventListener('click', function () { sendKey(btn.getAttribute('data-key'), btn); });
    })(keyBtns[i]);
  }

  document.getElementById('btn-power').addEventListener('click', function () {
    var btn = this;
    btn.disabled = true;
    postJson('/api/power-off', {})
      .then(function (data) {
        buzz();
        if (data.ok) { refreshStatus(); }
        else { showError(data.error || '发送失败'); refreshStatus(); }
      })
      .catch(function () { showError('发送失败，请检查网络'); })
      .then(function () { btn.disabled = false; });
  });

  document.getElementById('btn-wake').addEventListener('click', function () {
    var btn = this;
    btn.disabled = true;
    btn.textContent = '正在唤醒…';
    postJson('/api/power-on', {})
      .then(function (data) {
        buzz();
        if (data.ok) {
          showOk(data.error || '唤醒包已发出');
        } else {
          showError(data.error || '唤醒失败');
        }
        refreshStatus();
      })
      .catch(function () { showError('发送失败，请检查网络'); })
      .then(function () {
        btn.disabled = false;
        btn.textContent = '⏻ 开机（局域网唤醒）';
      });
  });

  document.getElementById('btn-test').addEventListener('click', function () {
    var btn = this;
    btn.disabled = true;
    btn.textContent = '测试中…';
    postJson('/api/test', {})
      .then(function (data) {
        if (data.ok) {
          showOk('已连接' + (data.model ? '：' + data.model : '') + '（' + data.ms + 'ms）');
        } else {
          showError(data.error || '连接失败');
        }
        refreshStatus();
      })
      .catch(function () { showError('测试失败，请检查网络'); })
      .then(function () { btn.disabled = false; btn.textContent = '测试连接'; });
  });

  document.getElementById('btn-logout').addEventListener('click', function () {
    postJson('/api/logout', {}).then(function () { location.href = '/login'; });
  });

  fetch('/api/version')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      document.getElementById('version').textContent =
        '小米电视遥控 v' + (data.version || '');
    })
    .catch(function () {});

  refreshStatus();
  setInterval(refreshStatus, 15000);
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(function () {});
  }
})();
