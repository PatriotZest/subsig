/* Shared header and footer. Each page has <header class="top" data-page="..."></header> and <footer></footer>. */
(function () {
  const LOGO = '<svg viewBox="0 20 596 116" aria-hidden="true"><path d="M14 100 C60 100 60 36 106 36" fill="none" stroke="currentColor" stroke-width="9" stroke-linecap="round"/><circle cx="91" cy="65" r="8" fill="#F9A962"/><path d="M166 65 C162 60.5 156 60 153 60 C144 60 140 64 140 70 C140 77 148 78.5 153 80 C159 81.5 166 83 166 90 C166 96 161 100 153 100 C148 100 143 99 140 95 M181 60 V82 a18 18 0 0 0 36 0 V60 M217 60 V100 M233 36 V100 M233 80 a20 20 0 1 0 40 0 a20 20 0 1 0 -40 0 M311 65 C307 60.5 301 60 298 60 C289 60 285 64 285 70 C285 77 293 78.5 298 80 C304 81.5 311 83 311 90 C311 96 306 100 298 100 C293 100 288 99 285 95 M326 60 V100 M339 80 a20 20 0 1 0 40 0 a20 20 0 1 0 -40 0 M379 60 V104 C379 115 371 120 360 120 C351 120 345 117 342 113 M395 60 V100 M395 77 a17 17 0 0 1 34 0 V100 M429 77 a17 17 0 0 1 34 0 V100 M476 80 a20 20 0 1 0 40 0 a20 20 0 1 0 -40 0 M529 60 V100 M542 80 a20 20 0 1 0 40 0 a20 20 0 1 0 -40 0 M582 36 V100" fill="none" stroke="currentColor" stroke-width="6.5" stroke-linecap="round" stroke-linejoin="round"/><circle cx="326" cy="46" r="4.03" fill="currentColor"/><circle cx="529" cy="46" r="4.03" fill="currentColor"/></svg>';
  const LINKS = [
    ['dashboard', 'app.html', 'Dashboard', ''],
    ['method', 'method.html', 'How it works', 'hide-sm'],
    ['quiz', 'quiz.html', 'Quiz', '']
  ];
  const header = document.querySelector('header.top[data-page]');
  if (header) {
    const page = header.dataset.page;
    const nav = LINKS.map(([id, href, text, cls]) => id === 'sep' ? '<span class="sep"></span>'
      : `<a href="${href}" class="${cls}"${id === page ? ' aria-current="page"' : ''}>${text}</a>`).join('');
    header.innerHTML = `<div class="wrap"><a class="logo" href="index.html" aria-label="Subsigmoid home">${LOGO}</a><nav class="nav" aria-label="Main">${nav}</nav></div>`;
  }
  const footer = document.querySelector('footer');
  if (footer) {
    const live = window.Subsigmoid && window.Subsigmoid.live;
    footer.innerHTML = `<div class="wrap"><a class="logo" href="index.html" aria-label="Subsigmoid home">${LOGO}</a>
      <nav aria-label="Footer"><a href="app.html">Dashboard</a><a href="method.html">How it works</a><a href="quiz.html">Quiz</a></nav>
      <span class="mono">MadHacks 2026 &middot; Madison, WI${live === false ? ' &middot; showing sample data' : ''}</span></div>`;
  }
})();
