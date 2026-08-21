(() => {
  const CANVAS_SIZE = 560;
  const ENSEMBLE = 400;
  const PLOT_H = 190;

  const pauseBtn = document.getElementById('pauseBtn');
  const resetBtn = document.getElementById('resetBtn');
  const speedEl = document.getElementById('speed');
  const gridEl = document.getElementById('grid');
  const gridValEl = document.getElementById('gridVal');
  const fadeEl = document.getElementById('fade');
  const fadeValEl = document.getElementById('fadeVal');

  function sizeCanvases(mainId, plotId) {
    const main = document.getElementById(mainId);
    const plot = document.getElementById(plotId);
    main.width = main.height = CANVAS_SIZE;
    plot.width = CANVAS_SIZE;
    plot.height = PLOT_H;
    return { main, plot };
  }

  const latB = sizeCanvases('latBoundedCanvas', 'latChiPlot');
  const latP = sizeCanvases('latPanCanvas', 'latMsdPlot');
  const isoB = sizeCanvases('isoBoundedCanvas', 'isoRayleighPlot');
  const isoP = sizeCanvases('isoPanCanvas', 'isoMsdPlot');

  function makePair({ continuous, sampler, b, p, fairPlot, msdPlot, ids }) {
    const recenter = new RW.RecenterWalk({
      continuous,
      canvas: b.main,
      statsEl: document.getElementById(ids.bStats),
      diagEl: document.getElementById(ids.bDiag),
      boundaryEl: document.getElementById(ids.bBoundary),
    });
    const pan = new RW.PanWalk({
      continuous,
      canvas: p.main,
      statsEl: document.getElementById(ids.pStats),
      diagEl: document.getElementById(ids.pDiag),
    });
    return {
      continuous,
      sampler,
      recenter,
      pan,
      ensemble: new RW.Ensemble(ENSEMBLE, continuous),
      fairCtx: fairPlot.getContext('2d'),
      msdCtx: msdPlot.getContext('2d'),
      fairness: null,
      fairHistory: [],
      msdHistory: [],
    };
  }

  const pairs = [
    makePair({
      continuous: false,
      sampler: RW.sampleLattice,
      b: latB, p: latP,
      fairPlot: latB.plot, msdPlot: latP.plot,
      ids: {
        bStats: 'latBStats', bDiag: 'latBDiag', bBoundary: 'latBBoundary',
        pStats: 'latPStats', pDiag: 'latPDiag',
      },
    }),
    makePair({
      continuous: true,
      sampler: RW.sampleIsotropic,
      b: isoB, p: isoP,
      fairPlot: isoB.plot, msdPlot: isoP.plot,
      ids: {
        bStats: 'isoBStats', bDiag: 'isoBDiag', bBoundary: 'isoBBoundary',
        pStats: 'isoPStats', pDiag: 'isoPDiag',
      },
    }),
  ];

  let GRID, paused, lastTime;
  const sidebar = RW.initSidebar();

  function resetPair(pair) {
    pair.fairness = pair.continuous ? new RW.RayleighAcc() : new RW.ChiAcc();
    pair.fairHistory = [];
    pair.msdHistory = [];
    pair.recenter.reset(GRID, CANVAS_SIZE);
    pair.pan.reset(GRID, CANVAS_SIZE);
    pair.ensemble.reset();
  }

  function reset() {
    GRID = Number(gridEl.value);
    for (const pair of pairs) resetPair(pair);
    lastTime = performance.now();
  }

  function stepPair(pair) {
    const s = pair.sampler();
    pair.fairness.add(s);
    pair.recenter.step(s.dx, s.dy);
    pair.pan.step(s.dx, s.dy);
  }

  function renderPair(pair, pixel) {
    pair.recenter.draw(pixel);
    pair.pan.draw(pixel);

    const fair = pair.fairness.snapshot();
    pair.recenter.renderStats(fair);
    const msd = pair.ensemble.msd();
    pair.pan.renderStats(msd, ENSEMBLE);

    if (fair.n > 0) {
      const v = pair.continuous ? fair.rayleigh : fair.chi;
      pair.fairHistory = RW.record(pair.fairHistory, fair.n, v);
    }
    if (pair.ensemble.steps > 0) {
      pair.msdHistory = RW.record(pair.msdHistory, pair.ensemble.steps, msd);
    }

    let yMax = pair.continuous ? 6 : 8;
    const hist = pair.fairHistory;
    for (let i = 0; i < hist.length; i++) {
      if (hist[i][1] > yMax) yMax = hist[i][1];
    }
    yMax = Math.ceil(yMax / 2) * 2;
    const refs = pair.continuous
      ? [[2, 'rgba(154,187,255,0.6)'], [5.99, 'rgba(255,140,140,0.4)']]
      : [[3, 'rgba(154,187,255,0.6)'], [7.81, 'rgba(255,140,140,0.4)']];
    RW.drawSeries(pair.fairCtx, pair.fairHistory, yMax, '#9abbff', refs, CANVAS_SIZE, PLOT_H);
    RW.drawSeries(pair.msdCtx, pair.msdHistory, 2, '#aadd99',
      [[1, 'rgba(170,221,153,0.5)']], CANVAS_SIZE, PLOT_H);
  }

  function tick(now) {
    const dt = Math.min(0.1, (now - lastTime) / 1000);
    lastTime = now;
    if (!paused) {
      const n = Number(speedEl.value);
      const fade = Number(fadeEl.value) / 100;
      for (const pair of pairs) {
        for (let i = 0; i < n; i++) stepPair(pair);
        pair.ensemble.stepMany(n, pair.sampler);
        pair.recenter.fade(dt, fade);
        pair.pan.fade(dt, fade / 5);
      }
    }
    const pixel = CANVAS_SIZE / GRID;
    for (const pair of pairs) renderPair(pair, pixel);
    sidebar.applyActiveHighlight();
    requestAnimationFrame(tick);
  }

  pauseBtn.addEventListener('click', () => {
    paused = !paused;
    pauseBtn.textContent = paused ? 'Resume' : 'Pause';
  });
  resetBtn.addEventListener('click', reset);
  gridEl.addEventListener('input', () => { gridValEl.textContent = gridEl.value; reset(); });
  fadeEl.addEventListener('input', () => { fadeValEl.textContent = fadeEl.value; });

  paused = false;
  reset();
  requestAnimationFrame((t) => { lastTime = t; tick(t); });
})();
