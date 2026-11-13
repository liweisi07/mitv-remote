(function () {
  'use strict';
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
  function postJson(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); });
  }

  var nameEl = document.getElementById('tv-name');
  var ipEl = document.getElementById('tv-ip');
  var portEl = document.getElementById('tv-port');
  var macEl = document.getElementById('tv-mac');

  fetch('/api/settings')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      nameEl.value = data.tv_name || '';
      ipEl.value = data.tv_ip || '';
      portEl.value = data.tv_port || 5555;
      macEl.value = data.tv_mac || '';
    })
    .catch(function () { showError('读取设置失败，请检查网络'); });

  document.getElementById('btn-save').addEventListener('click', function () {
    var btn = this;
    btn.disabled = true;
    postJson('/api/settings', {
      tv_name: nameEl.value.trim(),
      tv_ip: ipEl.value.trim(),
      tv_port: parseInt(portEl.value.trim(), 10) || 5555,
      tv_mac: macEl.value.trim()
    })
      .then(function (data) {
        if (data.ok) { showOk('已保存'); }
        else { showError(data.error || '保存失败'); }
      })
      .catch(function () { showError('保存失败，请检查网络'); })
      .then(function () { btn.disabled = false; });
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
      })
      .catch(function () { showError('测试失败，请检查网络'); })
      .then(function () { btn.disabled = false; btn.textContent = '测试连接'; });
  });

  document.getElementById('btn-discover').addEventListener('click', function () {
    var btn = this;
    var listEl = document.getElementById('dev-list');
    btn.disabled = true;
    btn.textContent = '扫描中…（约几秒）';
    listEl.innerHTML = '';
    postJson('/api/discover', {})
      .then(function (data) {
        if (!data.ok) { showError(data.error || '扫描失败'); return; }
        if (!data.devices || data.devices.length === 0) {
          listEl.innerHTML = '<p class="muted">网段 ' + (data.subnet || '') +
            ' 里没发现开着 ADB 的设备。请确认电视 ADB 已开、和 NAS 在同一网段。</p>';
          return;
        }
        data.devices.forEach(function (dev) {
          var item = document.createElement('div');
          item.className = 'dev-item';
          var left = document.createElement('div');
          var ip = document.createElement('div');
          ip.className = 'ip';
          ip.textContent = dev.ip + ':' + dev.port;
          var model = document.createElement('div');
          model.className = 'model';
          model.textContent = (dev.model ? dev.model + ' · ' : '') + (dev.detail || dev.state);
          left.appendChild(ip);
          left.appendChild(model);
          var fill = document.createElement('button');
          fill.type = 'button';
          fill.textContent = '填入';
          fill.addEventListener('click', function () {
            ipEl.value = dev.ip;
            portEl.value = dev.port;
            showOk('已填入 ' + dev.ip + '，点“保存”生效');
            window.scrollTo(0, 0);
          });
          item.appendChild(left);
          item.appendChild(fill);
          listEl.appendChild(item);
        });
      })
      .catch(function () { showError('扫描失败，请检查网络'); })
      .then(function () { btn.disabled = false; btn.textContent = '开始扫描'; });
  });

  document.getElementById('btn-pass').addEventListener('click', function () {
    var btn = this;
    var cur = document.getElementById('cur-pass').value;
    var nu = document.getElementById('new-user').value.trim();
    var np = document.getElementById('new-pass').value;
    if (!cur || !np) { showError('请填写当前密码和新密码'); return; }
    btn.disabled = true;
    postJson('/api/change-password', {
      current_password: cur,
      new_username: nu,
      new_password: np
    })
      .then(function (data) {
        if (data.ok) {
          showOk('账号密码已修改');
          document.getElementById('cur-pass').value = '';
          document.getElementById('new-user').value = '';
          document.getElementById('new-pass').value = '';
        } else {
          showError(data.error || '修改失败');
        }
      })
      .catch(function () { showError('修改失败，请检查网络'); })
      .then(function () { btn.disabled = false; });
  });
})();
