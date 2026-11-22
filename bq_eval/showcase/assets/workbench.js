function switchTab(panelId, btnEl) {
  document.querySelectorAll('.tab-panel').forEach((el) => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach((el) => {
    el.classList.remove('active');
    el.setAttribute('aria-selected', 'false');
  });
  const target = document.getElementById(panelId);
  if (target) target.classList.add('active');
  if (btnEl) {
    btnEl.classList.add('active');
    btnEl.setAttribute('aria-selected', 'true');
  }
}

function switchCorpus(selectEl) {
  const mainEl = document.querySelector('main');
  if (!mainEl || mainEl.dataset.liveApi !== 'true' || !selectEl) return;
  const nextCorpus = encodeURIComponent(selectEl.value);
  window.location.assign('/?corpus=' + nextCorpus);
}
