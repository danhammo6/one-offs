var RW = window.RW = window.RW || {};

// Each entry: { kicker, title, html }. html may contain MathJax (\( \) and $$).
RW.EXPLAIN = {
  walks: { kicker: 'Recenter · walk length', title: 'Walks',
    html: `<p>How many walks have started since the last reset. A new walk begins
      every time the bounded walker falls off an edge and is dropped back at the center.</p>
      <p>It climbs faster on a <strong>small</strong> grid (the walker escapes quickly) and
      slower on a large one. Both rows share the Grid slider, so you can compare lattice vs
      any-angle escape on the same box.</p>` },
  current: { kicker: 'Recenter · walk length', title: 'Current steps',
    html: `<p>The number of steps the <strong>in-progress</strong> walk has taken so far,
      before it falls off the edge. Resets to 0 at the start of each walk.</p>` },
  last: { kicker: 'Recenter · walk length', title: 'Last walk length',
    html: `<p>How many steps the <strong>most recently finished</strong> walk lasted.
      Watch it jump around — walk lengths are heavy-tailed, so this varies wildly from one
      walk to the next.</p>` },
  avg: { kicker: 'Recenter · walk length', title: 'Average walk length',
    html: `<p>Mean steps-to-escape over all completed walks. This is a measured estimate of a
      quantity with a theoretical value — see <strong>Avg len vs theory</strong> on that board.</p>
      <p>It grows like the <strong>square</strong> of the grid size: double the grid and this
      roughly quadruples. <a href="boundary-explained.html" target="_blank">Why? →</a></p>` },
  std: { kicker: 'Recenter · walk length', title: 'Std. deviation of length',
    html: `<p>The spread of completed walk lengths around the average. It's <em>large</em> —
      comparable to the mean itself — because walk lengths are heavy-tailed: most walks die
      young near the center, but a few wander a long time before escaping.</p>
      $$\\text{Std} = \\sqrt{\\langle L^2\\rangle - \\langle L\\rangle^2}$$` },
  longest: { kicker: 'Recenter · walk length', title: 'Longest walk',
    html: `<p>The single longest walk seen so far, in steps. A record-tracker for the tail of
      the distribution — it keeps creeping up as rare long walks occur.</p>` },

  dirbalance: { kicker: 'Lattice · fairness', title: 'Direction balance (U/D/L/R)',
    html: `<p>The share of all moves that went up, down, left, and right. The same random
      stream drives <em>both</em> boards in this row, so this is a direct check on the random number
      generator.</p>
      <p>For a fair RNG all four converge toward <strong>25%</strong>. See <strong>χ²</strong>
      for the single-number version of this test.</p>` },
  chi: { kicker: 'Lattice · fairness', title: 'Chi-square (χ²)',
    html: `<p>Turns "are the four directions balanced?" into one number, measuring how far the
      counts stray from a perfect 25% split:</p>
      $$\\chi^2 = \\sum_{d=1}^{4} \\frac{(\\text{observed}_d - \\text{expected}_d)^2}{\\text{expected}_d}$$
      <p>With <strong>3 degrees of freedom</strong> it should hover around its mean of
      <strong>3</strong>, rarely poking above 7.81. It's a random variable — it never settles.
      A value pinned near 0 (too perfect) or climbing past 12 and staying there (biased) would
      be the warning signs. <a href="returns-and-chi-explained.html" target="_blank">More →</a></p>` },
  rbar: { kicker: 'Isotropic · fairness', title: 'Mean resultant length R̄',
    html: `<p>Each any-angle step is a unit vector \\((\\cos\\theta, \\sin\\theta)\\). Add them
      up, divide by N, and take the length of that average vector:</p>
      $$\\bar{R} = \\Big\\lVert \\frac{1}{N}\\sum_{i=1}^{N} (\\cos\\theta_i,\\,\\sin\\theta_i) \\Big\\rVert$$
      <p>If directions are uniform on the circle, the vectors cancel and \\(\\bar{R}\\to 0\\).
      A systematic preference for one heading would pin it away from 0. This is the circular
      analog of the lattice row's U/D/L/R percents.</p>` },
  rayleigh: { kicker: 'Isotropic · fairness', title: 'Rayleigh statistic',
    html: `<p>Turns "are the headings uniform on the circle?" into one number:</p>
      $$2N\\bar{R}^2$$
      <p>Under a fair RNG this is asymptotically \\(\\chi^2\\) with <strong>2 degrees of
      freedom</strong> — mean <strong>2</strong>, 95% threshold about <strong>5.99</strong>.
      Like the lattice χ² it <em>never settles</em>; it orbits its mean. A value stuck near 0
      (too circularly perfect) or climbing without bound (a preferred heading) would be the
      warning signs.</p>` },
  coverage: { kicker: 'Recenter · exploration', title: 'Coverage',
    html: `<p>The fraction of grid cells the bounded walker has <em>ever</em> visited since
      reset. The any-angle walker is binned into the same cells (via floor), so both rows
      measure the same thing. Trends toward 100% — slowly, because a 2D walk keeps crossing
      its own path.</p>` },
  returns: { kicker: 'Lattice · recurrence', title: 'Returns to center',
    html: `<p>Average number of times a walk steps back onto its <strong>starting cell</strong>
      before escaping. A window into <strong>Pólya's recurrence theorem</strong>: a 2D random
      walk is recurrent, so the walker keeps drifting back home.</p>
      <p>Bigger grid → walks last longer → more returns. <a href="returns-and-chi-explained.html" target="_blank">More →</a></p>` },
  isoReturns: { kicker: 'Isotropic · recurrence', title: 'Returns to center (disk)',
    html: `<p>On a continuous walk, hitting the exact start <em>point</em> has probability 0.
      Instead we count <strong>entries</strong> into a disk of radius ½ around the start —
      the neighborhood of the starting cell.</p>
      <p>The walk still comes home (2D is recurrent), but "home" is a small patch rather than
      a single lattice site. <a href="returns-and-chi-explained.html" target="_blank">More →</a></p>` },

  exitdist: { kicker: 'Recenter · first-passage', title: 'Exit edge distribution',
    html: `<p>Which edge completed walks left through (Top/Bottom/Left/Right). Starting from the
      center of a square, 4-fold symmetry predicts <strong>~25% each</strong>.</p>
      <p>On the any-angle row a step can clip a corner; the board records the
      <strong>first</strong> wall the step segment hits. This distribution over exit points is
      <strong>harmonic measure</strong>.
      <a href="boundary-explained.html" target="_blank">Why? →</a></p>` },
  avgtheory: { kicker: 'Lattice · first-passage', title: 'Avg length vs theory',
    html: `<p>The measured average walk length next to its <strong>exact</strong> theoretical
      value. The theory number isn't an approximation — it's the closed-form solution of the
      discrete Poisson equation \\(\\nabla^2 T = -4\\) (with \\(T=0\\) on the walls), evaluated
      at the center cell via the sine eigenbasis.</p>
      <p>Let the sim run and the measured value homes in on it.
      <a href="boundary-explained.html" target="_blank">The math →</a></p>` },
  isoAvgtheory: { kicker: 'Isotropic · first-passage', title: 'Avg length vs continuum theory',
    html: `<p>Same measured average, but the theory is now the <strong>continuum</strong> Poisson
      problem — Brownian motion in a square — not the discrete 4-neighbor solver.</p>
      <p>A unit-length isotropic step has \\(\\langle r^2\\rangle = N\\), so diffusion constant
      \\(D = 1/4\\). Mean exit time then solves \\(D\\nabla^2 T = -1\\), i.e. the same
      \\(\\nabla^2 T = -4\\) as the lattice's continuum limit:</p>
      $$T_{\\text{center}} \\approx 0.2947\\, L^2$$
      <p>On a modest grid the lattice's <em>exact discrete</em> T sits a touch higher; both
      approach this number as the box grows.
      <a href="boundary-explained.html" target="_blank">The math →</a></p>` },
  l2const: { kicker: 'Lattice · first-passage', title: 'Avg / grid² (the L² law)',
    html: `<p>Average walk length divided by grid², shown next to its theoretical value. Expected
      escape time grows like the <strong>square</strong> of the box size, so this ratio stays
      roughly <em>constant</em> as you drag the Grid slider — even though Avg itself changes a lot.</p>
      <p>It is the √N diffusion law in disguise: distance \\(\\sim\\sqrt{\\text{time}}\\)
      \\(\\Leftrightarrow\\) time-to-escape \\(\\sim \\text{size}^2\\).</p>
      <p>As the grid grows the ratio converges to a <strong>universal constant</strong> — the
      center value of the continuum solution of \\(\\nabla^2 S = -4\\) on a unit square:</p>
      $$\\frac{\\text{Avg}}{\\text{grid}^2} \\;\\longrightarrow\\; S_{\\text{center}} \\approx 0.2947$$
      <p>(The theory shown uses the exact discrete value over grid², which lands a touch higher on
      small grids and approaches 0.2947 as the grid grows.)
      <a href="boundary-explained.html" target="_blank">More →</a></p>` },
  isoL2const: { kicker: 'Isotropic · first-passage', title: 'Avg / grid² (continuum L² law)',
    html: `<p>Same ratio, with theory locked at the continuum constant \\(S_{\\text{center}}\\approx 0.2947\\)
      rather than the discrete sine-sum. Drag the Grid slider: Avg itself jumps around, this
      ratio should not.</p>
      $$\\frac{\\text{Avg}}{L^2} \\;\\longrightarrow\\; S_{\\text{center}} \\approx 0.2947$$
      <p>Comparing the two rows: lattice theory is exact-for-the-grid and a bit above 0.2947
      on small boxes; isotropic theory <em>is</em> 0.2947. Both measured ratios should home in
      on that number as \\(L\\) grows.</p>` },

  panN: { kicker: 'Pan · unbounded', title: 'Steps N',
    html: `<p>Total steps the unbounded walker has taken. It never dies — the camera just
      follows it — so N grows without bound. It's the "time" axis for the
      mean-squared-displacement law.</p>` },
  dist: { kicker: 'Pan · diffusion', title: 'Distance r',
    html: `<p>The walker's straight-line distance from the origin (the blue mark). It grows,
      but <strong>slowly</strong> — like \\(\\sqrt{N}\\), not \\(N\\). To get twice as far the
      walker needs four times as many steps. <a href="msd-explained.html" target="_blank">Why? →</a></p>` },
  r2n: { kicker: 'Pan · diffusion', title: 'r² / N (single walker)',
    html: `<p>The squared distance divided by steps, for this <em>one</em> walker. Theory says
      it should average to 1 — for <em>both</em> the lattice and any-angle unit step — but a
      single walker is an extremely noisy sample. The smooth version is <strong>⟨r²⟩/N</strong>
      below, averaged over 400 walkers.</p>` },
  panreturns: { kicker: 'Lattice · recurrence', title: 'Returns to origin',
    html: `<p>How many times the unbounded walker has stepped back onto its start cell (0,0),
      next to the theoretical estimate.</p>
      <p>In 2D this grows <strong>logarithmically</strong> — not like √N:</p>
      $$\\mathbb{E}[\\text{returns}] \\approx \\frac{\\ln N}{\\pi}$$
      <p>That slow logarithm is why 2D is "barely" recurrent: infinitely many returns, but
      agonizingly rare. (In 3D the walk is transient and may never return.)</p>` },
  isoPanreturns: { kicker: 'Isotropic · recurrence', title: 'Returns to origin (disk)',
    html: `<p>Entries into a disk of radius \\(a = 1/2\\) around the origin (the blue circle),
      not hits on a single point — which have probability 0 off-lattice.</p>
      <p>A 2D Gaussian with \\(\\langle r^2\\rangle = N\\) has density \\(1/(\\pi N)\\) at the
      origin, so the chance of landing in the disk is about \\(a^2 / N\\). Summing that up:</p>
      $$\\mathbb{E}[\\text{visits}] \\approx a^2 \\ln N = 0.25\\,\\ln N$$
      <p>Same logarithmic growth as the lattice \\((\\ln N)/\\pi\\), with a prefactor set by
      the neighborhood's area. With step length 1 and diameter 1, an "entry" is usually a
      single visit, so the count tracks that estimate.</p>` },
  msd: { kicker: 'Pan · diffusion', title: '⟨r²⟩/N over 400 walkers',
    html: `<p>The mean-squared displacement per step, averaged over 400 independent ghost
      walkers of the <em>same</em> step type as this row. The averaging cancels the noise, so
      unlike the single-walker r²/N this <strong>converges</strong> to the theoretical value:</p>
      $$\\langle r^2 \\rangle = N \\quad\\Longrightarrow\\quad \\langle r^2\\rangle / N \\to 1$$
      <p>That identity needs only unit-length, mean-zero steps — it does not care whether
      headings are 4-way or any-angle. Each row has its own ensemble.
      <a href="msd-explained.html" target="_blank">The derivation →</a></p>` },
  chiPlot: { kicker: 'Lattice · fairness', title: 'χ² vs N plot',
    html: `<p>The χ² statistic plotted over time. The blue dashed line marks its mean (3); the
      red dashed line marks the 95% threshold (7.81).</p>
      <p>It <strong>never converges</strong> — a fair RNG produces a value that perpetually
      orbits 3. At very large N it appears to drift because each new step barely moves the
      cumulative count, so excursions linger. <a href="returns-and-chi-explained.html" target="_blank">More →</a></p>` },
  rayleighPlot: { kicker: 'Isotropic · fairness', title: 'Rayleigh vs N plot',
    html: `<p>The Rayleigh statistic \\(2N\\bar{R}^2\\) over time. Blue dashed line at its mean
      (2); red dashed line at the 95% threshold (5.99).</p>
      <p>Like the lattice χ² it <strong>never converges</strong> — it orbits 2 forever. The
      MSD plot beside it <em>does</em> converge; that contrast is the same lesson as the top
      row, now for headings on a circle rather than four bins.</p>` },
  msdPlot: { kicker: 'Pan · diffusion', title: '⟨r²⟩/N vs N plot',
    html: `<p>The ensemble mean-squared-displacement ratio plotted over time, with the green
      dashed line at the theoretical limit of 1.</p>
      <p>Unlike the fairness plot, this one <strong>converges</strong>: it starts noisy at small N and
      flattens onto the line as the 400-walker average tightens. Both rows should do this —
      the step length is 1 in either case.
      <a href="msd-explained.html" target="_blank">More →</a></p>` },
};

RW.initSidebar = function initSidebar() {
  const sbKicker = document.getElementById('sbKicker');
  const sbTitle = document.getElementById('sbTitle');
  const sbBody = document.getElementById('sbBody');
  let activeKey = null;

  function showExplanation(key) {
    const e = RW.EXPLAIN[key];
    if (!e) return;
    activeKey = key;
    sbKicker.textContent = e.kicker;
    sbTitle.textContent = e.title;
    sbBody.innerHTML = e.html;
    if (window.MathJax && MathJax.typesetPromise) {
      MathJax.typesetClear && MathJax.typesetClear([sbBody]);
      MathJax.typesetPromise([sbBody]);
    }
    applyActiveHighlight();
  }

  function applyActiveHighlight() {
    document.querySelectorAll('[data-explain].active')
      .forEach((el) => el.classList.remove('active'));
    if (activeKey) {
      document.querySelectorAll(`[data-explain="${activeKey}"]`)
        .forEach((el) => el.classList.add('active'));
    }
  }

  // Use mousedown, not click: stats spans are rebuilt every frame, so a press
  // and release rarely land on the same element instance.
  document.body.addEventListener('mousedown', (ev) => {
    const target = ev.target.closest('[data-explain]');
    if (target) showExplanation(target.getAttribute('data-explain'));
  });

  return { applyActiveHighlight };
};
