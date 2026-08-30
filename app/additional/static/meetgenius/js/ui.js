/**
 * MeetGenius UI — 取代 Bootstrap JS 與舊的三支主題腳本。
 * 提供：主題切換、分頁、下拉選單、對話框、手風琴、關閉提示。
 * 另提供最小的 `window.bootstrap` 相容層，供既有頁面腳本呼叫。
 */
(() => {
  'use strict';

  const THEME_KEY = 'meetgenius-theme';

  /* ---------------------------------------------------------------- 主題 */

  const applyTheme = (theme) => {
    if (theme === 'light' || theme === 'dark') {
      document.documentElement.setAttribute('data-theme', theme);
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
  };

  const currentTheme = () => {
    const stored = document.documentElement.getAttribute('data-theme');
    if (stored) return stored;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  };

  const initTheme = () => {
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-theme-toggle]');
      if (!btn) return;
      const next = currentTheme() === 'dark' ? 'light' : 'dark';
      applyTheme(next);
      try { localStorage.setItem(THEME_KEY, next); } catch (_) {}
    });
  };

  /* ---------------------------------------------------------------- 分頁 */

  const showTab = (trigger) => {
    const targetSel = trigger.dataset.tabTarget || trigger.dataset.bsTarget;
    if (!targetSel) return;
    const pane = document.querySelector(targetSel);
    if (!pane) return;

    const tablist = trigger.closest('[role="tablist"], .tabs');
    if (tablist) {
      tablist.querySelectorAll('.tab, [role="tab"]').forEach((t) => {
        t.classList.remove('active');
        t.setAttribute('aria-selected', 'false');
      });
    }
    const container = pane.parentElement;
    if (container) {
      container.querySelectorAll(':scope > .tab-pane').forEach((p) => p.classList.remove('active', 'show'));
    }

    trigger.classList.add('active');
    trigger.setAttribute('aria-selected', 'true');
    pane.classList.add('active', 'show');

    // 沿用 Bootstrap 的事件名稱：既有頁面腳本以 show.bs.tab 觸發資料載入
    trigger.dispatchEvent(new CustomEvent('show.bs.tab', { bubbles: true }));
    pane.dispatchEvent(new CustomEvent('tab:shown', { bubbles: true }));
  };

  const initTabs = () => {
    document.addEventListener('click', (e) => {
      const trigger = e.target.closest('[data-tab-target], [data-bs-toggle="tab"]');
      if (!trigger) return;
      e.preventDefault();
      showTab(trigger);
    });
  };

  /* ------------------------------------------------------------ 下拉選單 */

  const closeAllDropdowns = (except) => {
    document.querySelectorAll('.dropdown-menu.show').forEach((menu) => {
      if (menu !== except) menu.classList.remove('show');
    });
  };

  const initDropdowns = () => {
    document.addEventListener('click', (e) => {
      const toggle = e.target.closest('[data-dropdown-toggle], [data-bs-toggle="dropdown"]');

      if (toggle) {
        e.preventDefault();
        const menu = toggle.parentElement.querySelector('.dropdown-menu');
        if (!menu) return;
        const willOpen = !menu.classList.contains('show');
        closeAllDropdowns(menu);
        menu.classList.toggle('show', willOpen);
        toggle.setAttribute('aria-expanded', String(willOpen));
        return;
      }

      // 點選項目或點外面都收合
      closeAllDropdowns();
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeAllDropdowns();
    });
  };

  /* ------------------------------------------------------------ 對話框 */

  const openModal = (el) => {
    if (!el) return;
    el.classList.add('show');
    document.body.style.overflow = 'hidden';
  };

  const closeModal = (el) => {
    if (!el) return;
    el.classList.remove('show');
    document.body.style.overflow = '';
    el.dispatchEvent(new CustomEvent('modal:hidden', { bubbles: true }));
  };

  const initModals = () => {
    document.addEventListener('click', (e) => {
      const opener = e.target.closest('[data-modal-open]');
      if (opener) {
        e.preventDefault();
        openModal(document.querySelector(opener.dataset.modalOpen));
        return;
      }

      const closer = e.target.closest('[data-modal-close], [data-bs-dismiss="modal"]');
      if (closer) {
        e.preventDefault();
        closeModal(closer.closest('.modal'));
        return;
      }

      // 點背景關閉
      if (e.target.classList && e.target.classList.contains('modal')) {
        closeModal(e.target);
      }
    });

    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      document.querySelectorAll('.modal.show').forEach(closeModal);
    });
  };

  /* ------------------------------------------------------------ 手風琴 */

  const initAccordions = () => {
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('.accordion-button');
      if (!btn) return;
      e.preventDefault();

      const targetSel = btn.dataset.accordionTarget || btn.dataset.bsTarget;
      const panel = targetSel ? document.querySelector(targetSel)
                              : btn.closest('.accordion-item')?.querySelector('.accordion-collapse');
      if (!panel) return;

      const opening = !panel.classList.contains('show');

      // 同一組內其他項目收合
      const group = btn.closest('.accordion');
      if (group && opening) {
        group.querySelectorAll('.accordion-collapse.show').forEach((p) => {
          if (p === panel) return;
          p.classList.remove('show');
          const b = p.closest('.accordion-item')?.querySelector('.accordion-button');
          if (b) { b.classList.add('collapsed'); b.setAttribute('aria-expanded', 'false'); }
        });
      }

      panel.classList.toggle('show', opening);
      btn.classList.toggle('collapsed', !opening);
      btn.setAttribute('aria-expanded', String(opening));
    });
  };

  /* -------------------------------------------------------- 關閉提示訊息 */

  const initDismiss = () => {
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('.btn-close, [data-bs-dismiss="alert"]');
      if (!btn || btn.closest('.modal')) return;
      const box = btn.closest('.alert');
      if (box) box.remove();
    });
  };

  /* --------------------------------------------- Bootstrap 相容層（最小） */

  const bootstrapShim = () => {
    if (window.bootstrap) return;
    window.bootstrap = {
      Tab: {
        getOrCreateInstance: (el) => ({ show: () => showTab(el) }),
      },
      Alert: {
        getOrCreateInstance: (el) => ({ close: () => el && el.remove() }),
      },
      Modal: {
        getInstance: (el) => (el ? { show: () => openModal(el), hide: () => closeModal(el) } : null),
        getOrCreateInstance: (el) => ({ show: () => openModal(el), hide: () => closeModal(el) }),
      },
    };
    window.bootstrap.Modal = Object.assign(
      function (el) { return { show: () => openModal(el), hide: () => closeModal(el) }; },
      window.bootstrap.Modal
    );
  };

  /* ---------------------------------------------------------------- 啟動 */

  bootstrapShim();
  initTheme();
  initTabs();
  initDropdowns();
  initModals();
  initAccordions();
  initDismiss();

  window.MG = { applyTheme, currentTheme, openModal, closeModal, showTab };
})();
