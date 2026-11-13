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
    // 电视画面：刚连上立刻取一张；断开时回占位提示（有图保留最后一张）。
    var wasConnected = lastState === 'connected';
    lastState = state;
    if (state === 'connected' && !wasConnected && !shotPaused) {
      refreshShot(false);
    }
    if (state !== 'connected' && shotImg.classList.contains('hidden')) {
      showShotPlaceholder(state === 'not_configured'
        ? '未配置电视：请先在设置页填电视 IP' : '未连接电视');
    }
  }

  function refreshStatus() {
    fetch('/api/status')
      .then(function (r) { return r.json(); })
      .then(function (data) { renderStatus(data); })
      .catch(function () { statusText.textContent = '拿不到状态，请检查网络'; });
  }

  // ---- 电视画面截图：每 2 秒自动刷新一张 ----
  var shotImg = document.getElementById('shot-img');
  var shotPlaceholder = document.getElementById('shot-placeholder');
  var shotSub = document.getElementById('shot-sub');
  var shotToggle = document.getElementById('btn-shot-toggle');
  var shotWrap = document.getElementById('shot-wrap');
  var lastState = 'unknown';
  var shotPaused = false;
  var shotBusy = false;
  var shotTimer = null;
  var shotObjectUrl = null;

  function shotTime() {
    var d = new Date();
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }
  function showShotPlaceholder(text) {
    shotPlaceholder.textContent = text;
    shotPlaceholder.classList.remove('hidden');
    shotImg.classList.add('hidden');
  }
  function showShotImage(url) {
    if (shotObjectUrl && shotObjectUrl !== url) {
      try { URL.revokeObjectURL(shotObjectUrl); } catch (e) {}
    }
    shotObjectUrl = url;
    shotImg.src = url;
    shotImg.classList.remove('hidden');
    shotPlaceholder.classList.add('hidden');
  }
  function blobToUrl(blob, done) {
    if (window.URL && URL.createObjectURL) {
      done(URL.createObjectURL(blob));
    } else if (window.FileReader) {
      var reader = new FileReader();
      reader.onload = function () { done(reader.result); };
      reader.onerror = function () { done(null); };
      reader.readAsDataURL(blob);
    } else {
      done(null);
    }
  }
  function refreshShot(manual) {
    // 节流：上一张还没回来就跳过本次，不并发堆积。
    if (shotBusy) return;
    // 自动刷新只在已连接时跑；手动点按（点画面）只要不是明确未配置就试一次。
    if (!manual && (shotPaused || lastState !== 'connected')) return;
    if (manual && lastState === 'not_configured') {
      showShotPlaceholder('未配置电视：请先在设置页填电视 IP');
      shotSub.textContent = '未配置电视：请先在设置页填电视 IP';
      return;
    }
    shotBusy = true;
    fetch('/api/screenshot', { cache: 'no-store' })
      .then(function (resp) {
        var ctype = (resp.headers.get('Content-Type') || '');
        if (ctype.indexOf('image/') === 0) {
          return resp.blob().then(function (blob) {
            return { image: true, blob: blob };
          });
        }
        return resp.json().then(function (data) {
          return { image: false, error: (data && data.error) || '截图失败' };
        });
      })
      .then(function (result) {
        if (result.image) {
          blobToUrl(result.blob, function (url) {
            if (url) {
              showShotImage(url);
              shotSub.textContent = '已更新 ' + shotTime()
                + (shotPaused ? ' · 已暂停自动刷新' : ' · 每 2 秒自动刷新');
            } else {
              shotSub.textContent = '截图已取到，但浏览器无法显示';
            }
          });
        } else {
          // 失败不刷屏：只在画面下方留一句话，有图时保留上一张。
          shotSub.textContent = result.error;
          if (shotImg.classList.contains('hidden')) {
            showShotPlaceholder(result.error);
          }
        }
      })
      .catch(function () {
        shotSub.textContent = '截图失败，请检查网络';
        if (shotImg.classList.contains('hidden')) {
          showShotPlaceholder('截图失败，请检查网络');
        }
      })
      .then(function () { shotBusy = false; });
  }
  function startShotTimer() {
    if (shotTimer) return;
    shotTimer = setInterval(function () { refreshShot(false); }, 2000);
  }
  function stopShotTimer() {
    if (shotTimer) { clearInterval(shotTimer); shotTimer = null; }
  }
  function restartShotTimer() {
    // 重置 2 秒计时：停掉旧的再起一个新的，下一张在按键后满 2 秒才取，
    // 避免刚取完又立刻重复取一张。暂停时不重起（定时器本就停着）。
    stopShotTimer();
    if (!shotPaused) startShotTimer();
  }
  function refreshShotAfterKey() {
    // 按键成功后立刻刷新一次电视画面：只在已连接时触发；
    // 暂停只停定时器，不挡这一次（这是用户主动操作的结果，要看得到）。
    if (lastState !== 'connected') return;
    if (shotBusy) return; // 上一张还没回来，跳过，不堆积
    restartShotTimer();
    refreshShot(true);
  }
  shotToggle.addEventListener('click', function () {
    shotPaused = !shotPaused;
    if (shotPaused) {
      // 暂停立即生效：停掉定时器，正在取的那张取完即止。
      stopShotTimer();
      shotToggle.textContent = '▶ 继续';
      shotSub.textContent = '已暂停自动刷新（点画面仍可手动刷新）';
    } else {
      shotToggle.textContent = '⏸ 暂停';
      refreshShot(true);
      startShotTimer();
    }
  });
  shotWrap.addEventListener('click', function () { refreshShot(true); });

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
          // 按键成功后立刻刷新一次画面，不等 2 秒定时器。
          refreshShotAfterKey();
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
  startShotTimer();
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(function () {});
  }
})();
