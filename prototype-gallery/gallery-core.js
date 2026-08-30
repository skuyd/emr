(function attachGalleryCore(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.PHRGallery = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function createGalleryCore() {
  const variants = Object.freeze([
    Object.freeze({ id: 'a', name: '暖笺', font: 'editorial-serif', palette: 'cream-terracotta-sage', layout: 'top-nav-editorial-cards' }),
    Object.freeze({ id: 'b', name: '明晰', font: 'system-sans', palette: 'white-cobalt-cyan', layout: 'left-rail-data-grid' }),
    Object.freeze({ id: 'c', name: '经纬', font: 'geometric-display', palette: 'navy-mint-lime', layout: 'chronological-canvas' }),
    Object.freeze({ id: 'd', name: '随身', font: 'rounded-humanist', palette: 'mist-violet-coral', layout: 'focused-mobile-column' }),
  ]);

  function chooseVariant(id, storage) {
    const selected = variants.find((variant) => variant.id === id);
    if (!selected) throw new Error(`未知的设计方向：${id}`);
    if (storage && typeof storage.setItem === 'function') storage.setItem('phr-style-choice', id);
    return selected;
  }

  return Object.freeze({ variants, chooseVariant });
});

