(function wireGallery() {
  const cards = [...document.querySelectorAll('[data-variant]')];
  const choiceText = document.querySelector('#choiceText');
  const clearChoice = document.querySelector('#clearChoice');

  function renderChoice(id) {
    cards.forEach((card) => card.classList.toggle('is-selected', card.dataset.variant === id));
    if (!id) {
      choiceText.textContent = '尚未选择';
      return;
    }
    const variant = window.PHRGallery.variants.find((item) => item.id === id);
    choiceText.textContent = variant ? `${variant.id.toUpperCase()} · ${variant.name}` : '尚未选择';
  }

  document.querySelectorAll('[data-select]').forEach((button) => {
    button.addEventListener('click', () => {
      const selected = window.PHRGallery.chooseVariant(button.dataset.select, window.localStorage);
      renderChoice(selected.id);
    });
  });

  clearChoice.addEventListener('click', () => {
    window.localStorage.removeItem('phr-style-choice');
    renderChoice('');
  });

  renderChoice(window.localStorage.getItem('phr-style-choice') || '');
})();
