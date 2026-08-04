(function () {
  function inject() {
    if (document.getElementById('web-tools-link')) return;
    var radios = document.querySelector('a[href="#/radio"]');
    if (!radios) return;
    var link = radios.cloneNode(true);
    link.id = 'web-tools-link';
    link.href = '/tools/';
    link.removeAttribute('aria-current');
    var icon = link.querySelector('svg path');
    if (icon) {
      // wrench/tools glyph
      icon.setAttribute(
        'd',
        'M22.7 19l-9.1-9.1c.9-2.3.4-5-1.5-6.9-2-2-5-2.4-7.4-1.3L9 6l-3 3-4.3-4.3C.6 7.1 1 10.1 3 12.1c1.9 1.9 4.6 2.4 6.9 1.5l9.1 9.1c.4.4 1 .4 1.4 0l2.3-2.3c.5-.4.5-1.1 0-1.4z'
      );
    }
    var textNode = Array.prototype.find.call(
      link.childNodes,
      function (n) { return n.nodeType === 3; }
    );
    if (textNode) textNode.textContent = 'Web Tools';
    radios.insertAdjacentElement('afterend', link);
  }
  new MutationObserver(inject).observe(document.body, { childList: true, subtree: true });
  inject();
})();
