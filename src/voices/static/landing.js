// Nigtica landing interactions: word-split hero reveals, scroll reveals,
// scroll-rail + nav chapter tracking. No dependencies.

(function () {
  var reduceMQ = window.matchMedia('(prefers-reduced-motion: reduce)');

  /* ---- Word-split reveal for [data-words] ---- */
  function splitWords() {
    document.querySelectorAll('[data-words]').forEach(function (el) {
      var label = (el.getAttribute('aria-label') || el.textContent).trim();
      el.setAttribute('aria-label', label);
      var walkInto = function (src, dst) {
        src.childNodes.forEach(function (child) {
          if (child.nodeType === 3) {
            child.textContent.split(/(\s+)/).forEach(function (part) {
              if (!part) return;
              if (/^\s+$/.test(part)) { dst.appendChild(document.createTextNode(' ')); return; }
              var mask = document.createElement('span');
              mask.className = 'word-mask';
              mask.setAttribute('aria-hidden', 'true');
              var w = document.createElement('span');
              w.className = 'word';
              w.textContent = part;
              mask.appendChild(w);
              dst.appendChild(mask);
            });
          } else if (child.nodeName === 'BR') {
            dst.appendChild(document.createElement('br'));
          } else {
            var c = child.cloneNode(false);
            walkInto(child, c);
            dst.appendChild(c);
          }
        });
      };
      var frag = document.createDocumentFragment();
      Array.prototype.forEach.call(el.childNodes, function (n) { frag.appendChild(n.cloneNode(true)); });
      el.textContent = '';
      var tmp = document.createElement('div');
      tmp.appendChild(frag);
      walkInto(tmp, el);
      var i = 0;
      el.querySelectorAll('.word').forEach(function (w) {
        w.style.setProperty('--word-delay', ((i++) * 72) + 'ms');
      });
    });
  }
  splitWords();

  /* ---- Reveal on scroll ---- */
  var revealObserver = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) e.target.classList.add('rv-in');
    });
  }, { threshold: 0.25 });
  document.querySelectorAll('[data-rv]').forEach(function (el) { revealObserver.observe(el); });

  /* ---- Hero headline reveals on load ---- */
  requestAnimationFrame(function () {
    var heroH1 = document.querySelector('.hero-inner [data-words]');
    if (heroH1) heroH1.classList.add('rv-in');
  });

  /* ---- Chapter tracking: rail + nav ---- */
  var chapters = [
    { id: 'intro' },
    { id: 'voices' },
    { id: 'how' },
    { id: 'access' }
  ];
  var railLinks = Array.prototype.slice.call(document.querySelectorAll('.rail a'));
  var navLinks = Array.prototype.slice.call(document.querySelectorAll('.nav-links a'));
  var railLabel = document.getElementById('rail-label');

  function setChapter(index) {
    railLinks.forEach(function (a, i) {
      a.setAttribute('aria-current', String(i === index));
    });
    navLinks.forEach(function (a) {
      var href = a.getAttribute('href') || '';
      var id = href.charAt(0) === '#' ? href.slice(1) : '';
      a.setAttribute('aria-current', String(chapters[index] && chapters[index].id === id));
    });
    if (railLabel) railLabel.textContent = '0' + (index + 1) + ' / 04';
  }

  var chapterObserver = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (!e.isIntersecting) return;
      var idx = chapters.findIndex(function (c) { return c.id === e.target.id; });
      if (idx >= 0) setChapter(idx);
    });
  }, { rootMargin: '-45% 0px -45% 0px' });

  ['intro', 'voices', 'how', 'access'].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) chapterObserver.observe(el);
  });

  if (reduceMQ.matches) {
    document.querySelectorAll('[data-words]').forEach(function (el) { el.classList.add('rv-in'); });
  }
})();
