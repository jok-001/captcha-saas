/* ==========================================================================
   ui.js — 通用交互行为：分页跳页 / 签到 / 轻提示
   无依赖、无构建、defer 加载。所有初始化都做了存在性判断，
   页面上没有对应元素时安静退出（后端接口未就绪也不影响其它功能）。
   ========================================================================== */
(function () {
  'use strict';

  /* ----------------------------------------------------------------------
     一、轻提示 toast
        window.toast('文案')            // info
        window.toast('文案', 'success') // success | error | info
     ---------------------------------------------------------------------- */
  var host = null;
  var ICONS = { success: '✓', error: '✕', info: 'ℹ' };

  function ensureHost() {
    if (!host || !host.parentNode) {
      host = document.createElement('div');
      host.className = 'toast-host';
      host.setAttribute('role', 'status');
      host.setAttribute('aria-live', 'polite');
      document.body.appendChild(host);
    }
    return host;
  }

  function toast(message, type, duration) {
    if (!message) return null;
    if (!ICONS[type]) type = 'info';

    var el = document.createElement('div');
    el.className = 'toast toast-' + type;

    var ico = document.createElement('span');
    ico.className = 'toast-ico';
    ico.setAttribute('aria-hidden', 'true');
    ico.textContent = ICONS[type];

    var txt = document.createElement('span');
    txt.className = 'toast-txt';
    txt.textContent = String(message);   // textContent：后端文案不参与 HTML 解析

    el.appendChild(ico);
    el.appendChild(txt);
    ensureHost().appendChild(el);

    // 强制一次重排后再加类，保证 200ms 过渡真的跑起来
    void el.offsetWidth;
    el.classList.add('is-in');

    var timer = setTimeout(close, duration || 3000);

    el.addEventListener('mouseenter', function () { clearTimeout(timer); });
    el.addEventListener('mouseleave', function () { timer = setTimeout(close, 1200); });

    function close() {
      clearTimeout(timer);
      el.classList.remove('is-in');
      el.classList.add('is-out');
      setTimeout(function () {
        if (el.parentNode) el.parentNode.removeChild(el);
      }, 240);
    }

    return el;
  }

  window.toast = toast;

  /* ----------------------------------------------------------------------
     二、积分数字更新（签到成功后同步顶栏与侧栏）
     ---------------------------------------------------------------------- */
  function updateCredits(value) {
    var nodes = document.querySelectorAll('[data-credits]');
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      n.textContent = value;
      n.classList.add('bump');
      (function (node) {
        setTimeout(function () { node.classList.remove('bump'); }, 1400);
      })(n);
    }
  }

  /* ----------------------------------------------------------------------
     三、分页跳页输入框
        - 回车 / 点 ↵ / 失焦 都触发跳转
        - 越界值自动夹到 [1, pages]；空值或等于当前页则不跳
        - 用原生 form.submit() 提交，不拼 URL
     ---------------------------------------------------------------------- */
  function initJumpForms() {
    var forms = document.querySelectorAll('form[data-pg-jump]');
    for (var i = 0; i < forms.length; i++) {
      bindJumpForm(forms[i]);
    }
  }

  function bindJumpForm(form) {
    var input = form.querySelector('.pg-jump-input');
    if (!input) return;

    var min = parseInt(input.getAttribute('min'), 10) || 1;
    var max = parseInt(input.getAttribute('max'), 10) || 1;
    var cur = parseInt(input.getAttribute('data-current'), 10) || min;
    var busy = false;

    function clamped() {
      var raw = String(input.value == null ? '' : input.value).replace(/\s+/g, '');
      if (!raw) return null;
      var n = parseInt(raw, 10);
      if (isNaN(n)) return null;
      return Math.min(max, Math.max(min, n));
    }

    function go(value) {
      if (busy) return;
      busy = true;
      input.value = value;
      form.submit();   // 原生 GET 提交，参数由 hidden input 带过去
      // 正常情况页面已跳走；万一导航被打断（离线、用户点了停止），
      // 1.2s 后解除锁，避免输入框永久失效
      setTimeout(function () { busy = false; }, 1200);
    }

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var v = clamped();
      if (v === null) { input.value = ''; input.focus(); return; }
      if (v === cur) { input.value = ''; input.blur(); return; }
      go(v);
    });

    input.addEventListener('focus', function () {
      input.select();
    });

    input.addEventListener('blur', function (e) {
      // 点到「↵」按钮时不抢跑，交给 submit 流程处理
      if (e.relatedTarget && form.contains(e.relatedTarget)) return;
      var v = clamped();
      if (v === null || v === cur) { input.value = ''; return; }
      go(v);
    });
  }

  /* ----------------------------------------------------------------------
     四、右上角签到
        后端契约：POST /api/checkin
          成功 { "ok": true,  "reward": 100, "credits": 1280, "message": "…" }
          失败 { "ok": false, "message": "今日已签到" }
        未就绪（404）时只在 console 里 warn，页面不做任何破坏性动作。
     ---------------------------------------------------------------------- */
  function initCheckin() {
    var root = document.querySelector('[data-checkin]');
    if (!root) return;

    var btn = root.querySelector('.checkin-btn');
    if (!btn) return;

    var txt = btn.querySelector('.checkin-txt');
    var ico = btn.querySelector('.checkin-ico');
    var url = root.getAttribute('data-url') || '/api/checkin';

    function stopLoading() {
      btn.classList.remove('is-loading');
      btn.removeAttribute('aria-busy');
    }

    function markDone() {
      stopLoading();
      root.setAttribute('data-state', 'done');
      btn.classList.add('is-done');
      btn.disabled = true;
      btn.setAttribute('aria-disabled', 'true');
      btn.removeAttribute('title');
      if (ico) ico.textContent = '✓';
      if (txt) txt.textContent = '已签到';
    }

    btn.addEventListener('click', function () {
      var state = root.getAttribute('data-state');
      if (btn.disabled || state === 'done') return;
      if (state === 'off') {
        toast(root.getAttribute('data-msg-off') || '签到功能暂未开放', 'info');
        return;
      }

      btn.classList.add('is-loading');
      btn.setAttribute('aria-busy', 'true');

      fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Accept': 'application/json',
          'X-Requested-With': 'XMLHttpRequest'
        }
      }).then(function (res) {
        if (res.status === 404) {
          var e404 = new Error('checkin endpoint not found (404)');
          e404.soft = true;
          throw e404;
        }
        // 4xx/5xx 也尽量读后端 message（例如「今日已签到」「签到功能未开放」）
        return res.json().catch(function () { return null; }).then(function (body) {
          if (!res.ok) {
            var e = new Error('checkin failed with status ' + res.status);
            e.status = res.status;
            e.serverMessage = body && body.message;
            throw e;
          }
          // 200 但拿不到 JSON（比如网关返回了 HTML）：不能当成签到成功
          if (!body) {
            var e2 = new Error('checkin returned non-JSON response (status ' + res.status + ')');
            e2.status = res.status;
            throw e2;
          }
          return body;
        });
      }).then(function (data) {
        stopLoading();

        if (data.ok === false) {
          toast(data.message || '签到失败', 'error');
          return;
        }

        markDone();
        if (data.credits != null) updateCredits(data.credits);
        var reward = data.reward != null ? data.reward : data.amount;
        toast(
          reward != null
            ? '签到成功，+' + reward + ' 积分'
            : (data.message || '签到成功'),
          'success'
        );
      }).catch(function (err) {
        stopLoading();
        if (err && err.soft) {
          // 后端还没上线：静默降级，只留控制台线索
          console.warn('[checkin] 签到接口尚未就绪（404），入口保持可点状态：' + url);
          return;
        }
        console.warn('[checkin]', err);
        toast((err && err.serverMessage) || '签到失败，请稍后重试', 'error');
      });
    });
  }

  /* ---------------------------------------------------------------------- */
  function init() {
    initJumpForms();
    initCheckin();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
