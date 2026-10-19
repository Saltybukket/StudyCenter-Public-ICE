// Post-it status buttons use the shared quick-form submission and its result.
document.querySelectorAll('[data-postit-status]').forEach(form => {
  const status = form.querySelector('input[name="status"]');
  const submit = form.querySelector('.postit-submit');
  const choices = [...form.querySelectorAll('[data-status-choice]')];
  let saved = status.value, busy = false;
  status.disabled = false;
  choices.forEach(button => {
    button.type = 'button';
    button.removeAttribute('name');
    button.addEventListener('click', () => {
      if (busy || button.dataset.statusChoice === saved) return;
      busy = true;
      status.value = button.dataset.statusChoice;
      form.setAttribute('aria-busy', 'true');
      choices.forEach(choice => { choice.disabled = true; });
      form.requestSubmit(submit);
    });
  });
  form.addEventListener('quick-form-result', event => {
    if (event.detail.ok) saved = status.value;
    else status.value = saved;
    busy = false;
    form.removeAttribute('aria-busy');
    choices.forEach(choice => {
      choice.disabled = false;
      choice.setAttribute('aria-pressed', String(choice.dataset.statusChoice === saved));
    });
  });
});
