(function () {
  'use strict';

  function initializeExpenseDisclosure(root) {
    const details = root.querySelector('#new-expense-claim');
    const opener = root.querySelector('[data-open-expense-form]');
    const purpose = root.querySelector('[data-expense-purpose]');
    if (!details || !opener || opener.disabled) return;

    const syncExpanded = () => {
      opener.setAttribute('aria-expanded', details.open ? 'true' : 'false');
    };
    details.addEventListener('toggle', syncExpanded);
    opener.addEventListener('click', () => {
      details.open = true;
      syncExpanded();
      details.scrollIntoView({ behavior: 'smooth', block: 'start' });
      requestAnimationFrame(() => purpose?.focus());
    });
    syncExpanded();
  }

  if (document.readyState === 'loading') {
    document.addEventListener(
      'DOMContentLoaded',
      () => initializeExpenseDisclosure(document),
      { once: true },
    );
  } else {
    initializeExpenseDisclosure(document);
  }
})();
