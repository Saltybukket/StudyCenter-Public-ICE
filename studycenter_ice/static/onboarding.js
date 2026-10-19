// Open only the two real source forms linked from the setup tour.
(() => {
  const shell = document.querySelector('.welcome-shell');
  if (shell) {
    // Keep the active station visible in the narrow, horizontally scrolling list.
    const stations = shell.querySelector('.welcome-steps ol');
    const current = stations.querySelector('[aria-current="step"]');
    if (current && stations.scrollWidth > stations.clientWidth) {
      stations.scrollLeft += current.getBoundingClientRect().left - stations.getBoundingClientRect().left
        - (stations.clientWidth - current.offsetWidth) / 2;
    }
    let busy = false;
    shell.querySelectorAll('form').forEach(form => form.addEventListener('submit', async event => {
      event.preventDefault();
      if (busy) return;
      busy = true;
      const error = document.querySelector('#welcome-error');
      error.hidden = true;
      const buttons = [...shell.querySelectorAll('button')];
      buttons.forEach(button => { button.disabled = true; });
      shell.setAttribute('aria-busy', 'true');
      try {
        const result = await api('/welcome', {method: 'POST', body: new FormData(form), headers: {'X-Requested-With': 'fetch'}});
        if (typeof result.url !== 'string' || !result.url.startsWith('/') || result.url.startsWith('//')) throw new Error('Einrichtungsziel nicht verfügbar.');
        location.assign(result.url);
      } catch (failure) {
        error.textContent = 'Fortschritt nicht bestätigt: ' + failure.message + ' Bitte erneut versuchen.';
        error.hidden = false;
        error.focus();
        busy = false;
        buttons.forEach(button => { button.disabled = false; });
        shell.removeAttribute('aria-busy');
      }
    }));
  }
  function reveal() {
    const key = location.hash.slice(1);
    if (!['course-create', 'source-create'].includes(key)) return;
    const target = document.getElementById(key);
    if (!target) return;
    for (let element = target; element; element = element.parentElement) {
      if (element.tagName === 'DETAILS') element.open = true;
    }
    requestAnimationFrame(() => target.scrollIntoView({block: 'start'}));
  }
  reveal();
  addEventListener('hashchange', reveal);
})();
