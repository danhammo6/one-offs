var RW = window.RW = window.RW || {};

RW.RETURN_RADIUS = 0.5;   // isotropic "home" neighborhood (one cell's half-width)

RW.sampleLattice = function sampleLattice() {
  const dir = Math.floor(Math.random() * 4);
  const dx = dir === 2 ? -1 : dir === 3 ? 1 : 0;
  const dy = dir === 0 ? -1 : dir === 1 ? 1 : 0;
  return { dx, dy, dir };
};

RW.sampleIsotropic = function sampleIsotropic() {
  const th = Math.random() * Math.PI * 2;
  return { dx: Math.cos(th), dy: Math.sin(th) };
};

// Exact expected walk length (steps to absorption) starting from the center
// of an n×n grid, walls just outside at -1 and n. Solves the discrete Poisson
// equation (I - ¼A)T = 1 in closed form via the sine eigenbasis of the
// discrete Laplacian, evaluated at the center cell. n is assumed odd.
RW.expectedExitTimeFromCenter = function expectedExitTimeFromCenter(n) {
  const np1 = n + 1;
  let T = 0;
  for (let k = 1; k <= n; k += 2) {           // even modes vanish by symmetry
    const ak = (1 / Math.tan(Math.PI * k / (2 * np1))) * Math.sin(Math.PI * k / 2);
    const ck = Math.cos(Math.PI * k / np1);
    for (let l = 1; l <= n; l += 2) {
      const al = (1 / Math.tan(Math.PI * l / (2 * np1))) * Math.sin(Math.PI * l / 2);
      const cl = Math.cos(Math.PI * l / np1);
      T += ak * al / (1 - 0.5 * (ck + cl));
    }
  }
  return (4 / (np1 * np1)) * T;
};

// Continuum analog: ∇²T = -4 on the square [0, L]², T = 0 on the walls.
// T_center = S_center L², where S solves the same PDE on the unit square.
// S_center ≈ 0.2947. Used as the theoretical mean length for the isotropic walk.
RW.continuumSCenter = function continuumSCenter() {
  if (RW._sCenter != null) return RW._sCenter;
  // S = 4u, ∇²u = -1, u(x,y) = (16/π⁴) ∑_{m,n odd} sin(mπx)sin(nπy)/(mn(m²+n²))
  let sum = 0;
  for (let m = 1; m <= 81; m += 2) {
    const sm = Math.sin(m * Math.PI / 2);
    for (let n = 1; n <= 81; n += 2) {
      const sn = Math.sin(n * Math.PI / 2);
      sum += sm * sn / (m * n * (m * m + n * n));
    }
  }
  RW._sCenter = 64 * sum / Math.pow(Math.PI, 4);
  return RW._sCenter;
};

RW.continuumExitTimeFromCenter = function continuumExitTimeFromCenter(L) {
  return RW.continuumSCenter() * L * L;
};

// First wall hit by the segment (x0,y0) → (x1,y1) leaving the square [0, L)².
// Edges: 0 top (y=0), 1 bottom (y=L), 2 left (x=0), 3 right (x=L).
RW.firstExitEdge = function firstExitEdge(x0, y0, x1, y1, L) {
  const dx = x1 - x0;
  const dy = y1 - y0;
  let bestT = Infinity;
  let edge = -1;

  const hit = (t, e, onWall) => {
    if (t <= 1e-12 || t > 1 + 1e-9) return;
    if (t >= bestT) return;
    if (!onWall(x0 + t * dx, y0 + t * dy)) return;
    bestT = t;
    edge = e;
  };

  if (dx !== 0) {
    hit((0 - x0) / dx, 2, (_x, y) => y >= -1e-9 && y <= L + 1e-9);
    hit((L - x0) / dx, 3, (_x, y) => y >= -1e-9 && y <= L + 1e-9);
  }
  if (dy !== 0) {
    hit((0 - y0) / dy, 0, (x) => x >= -1e-9 && x <= L + 1e-9);
    hit((L - y0) / dy, 1, (x) => x >= -1e-9 && x <= L + 1e-9);
  }
  return edge;
};

RW.ChiAcc = class ChiAcc {
  constructor() { this.c = [0, 0, 0, 0]; }
  add(step) { this.c[step.dir] += 1; }
  snapshot() {
    const c = this.c;
    const n = c[0] + c[1] + c[2] + c[3];
    const pct = ['–', '–', '–', '–'];
    let chi = 0;
    if (n > 0) {
      const expected = n / 4;
      for (let d = 0; d < 4; d++) {
        const diff = c[d] - expected;
        chi += (diff * diff) / expected;
        pct[d] = ((c[d] / n) * 100).toFixed(1);
      }
    }
    return { n, chi, pct };
  }
};

// Running mean direction on the circle. Rayleigh's 2N R̄² is asymptotically
// χ² with 2 degrees of freedom (mean 2; 95% threshold ≈ 5.99).
RW.RayleighAcc = class RayleighAcc {
  constructor() { this.n = 0; this.C = 0; this.S = 0; }
  add(step) {
    this.n += 1;
    this.C += step.dx;
    this.S += step.dy;
  }
  snapshot() {
    const n = this.n;
    if (n === 0) return { n: 0, Rbar: 0, rayleigh: 0 };
    const Rbar = Math.hypot(this.C / n, this.S / n);
    return { n, Rbar, rayleigh: 2 * n * Rbar * Rbar };
  }
};

RW.Ensemble = class Ensemble {
  constructor(size, continuous) {
    this.size = size;
    this.continuous = continuous;
    this.reset();
  }
  reset() {
    const n = this.size;
    this.x = this.continuous ? new Float64Array(n) : new Int32Array(n);
    this.y = this.continuous ? new Float64Array(n) : new Int32Array(n);
    this.steps = 0;
  }
  stepMany(count, sampler) {
    const n = this.size;
    const x = this.x, y = this.y;
    for (let s = 0; s < count; s++) {
      for (let k = 0; k < n; k++) {
        const step = sampler();
        x[k] += step.dx;
        y[k] += step.dy;
      }
      this.steps += 1;
    }
  }
  msd() {
    if (this.steps === 0) return 0;
    let sum = 0;
    const n = this.size, x = this.x, y = this.y;
    for (let k = 0; k < n; k++) sum += x[k] * x[k] + y[k] * y[k];
    return (sum / n) / this.steps;
  }
};

RW.RecenterWalk = class RecenterWalk {
  constructor({ continuous, canvas, statsEl, diagEl, boundaryEl }) {
    this.continuous = continuous;
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.statsEl = statsEl;
    this.diagEl = diagEl;
    this.boundaryEl = boundaryEl;
    this.returnRadius = RW.RETURN_RADIUS;
  }

  reset(grid, canvasSize) {
    this.grid = grid;
    this.canvasSize = canvasSize;
    this.expectedLen = this.continuous
      ? RW.continuumExitTimeFromCenter(grid)
      : RW.expectedExitTimeFromCenter(grid);
    this.heat = Array.from({ length: grid }, () => new Float32Array(grid));
    this.visited = Array.from({ length: grid }, () => new Uint8Array(grid));
    this.visitedCount = 0;
    this.hottest = 0;
    this.walks = 0;
    this.totalSteps = 0;
    this.longest = 0;
    this.shortest = Infinity;
    this.lastLen = 0;
    this.sumLen2 = 0;
    this.totalReturns = 0;
    this.exitCounts = [0, 0, 0, 0];
    this.startWalk();
  }

  bumpHeat(i, j) {
    if (i < 0 || j < 0 || i >= this.grid || j >= this.grid) return;
    this.heat[j][i] += 1;
    if (this.heat[j][i] > this.hottest) this.hottest = this.heat[j][i];
    if (!this.visited[j][i]) {
      this.visited[j][i] = 1;
      this.visitedCount += 1;
    }
  }

  startWalk() {
    if (this.walks > 0) {
      this.lastLen = this.steps;
      this.totalSteps += this.steps;
      this.sumLen2 += this.steps * this.steps;
      if (this.steps > this.longest) this.longest = this.steps;
      if (this.steps < this.shortest) this.shortest = this.steps;
      this.totalReturns += this.centerReturns;
    }
    const g = this.grid;
    if (this.continuous) {
      this.x = g / 2;
      this.y = g / 2;
    } else {
      this.x = Math.floor(g / 2);
      this.y = Math.floor(g / 2);
    }
    this.cx = this.x;
    this.cy = this.y;
    this.centerReturns = 0;
    this.wasInReturn = true;
    this.steps = 0;
    this.walks += 1;
    this.bumpHeat(Math.floor(this.x), Math.floor(this.y));
  }

  noteReturn() {
    if (this.continuous) {
      const dx = this.x - this.cx;
      const dy = this.y - this.cy;
      const inside = dx * dx + dy * dy <= this.returnRadius * this.returnRadius;
      if (inside && !this.wasInReturn) this.centerReturns += 1;
      this.wasInReturn = inside;
    } else if (this.x === this.cx && this.y === this.cy) {
      this.centerReturns += 1;
    }
  }

  inferExit(x1, y1) {
    if (y1 < 0) this.exitCounts[0]++;
    else if (y1 >= this.grid) this.exitCounts[1]++;
    else if (x1 < 0) this.exitCounts[2]++;
    else this.exitCounts[3]++;
  }

  step(dx, dy) {
    const g = this.grid;
    if (this.continuous) {
      const x1 = this.x + dx;
      const y1 = this.y + dy;
      this.steps += 1;
      if (x1 < 0 || x1 >= g || y1 < 0 || y1 >= g) {
        const edge = RW.firstExitEdge(this.x, this.y, x1, y1, g);
        if (edge >= 0) this.exitCounts[edge]++;
        else this.inferExit(x1, y1);
        this.startWalk();
        return;
      }
      this.x = x1;
      this.y = y1;
      this.bumpHeat(Math.floor(this.x), Math.floor(this.y));
      this.noteReturn();
    } else {
      this.x += dx;
      this.y += dy;
      this.steps += 1;
      if (this.x < 0 || this.x >= g || this.y < 0 || this.y >= g) {
        this.inferExit(this.x, this.y);
        this.startWalk();
      } else {
        this.bumpHeat(this.x, this.y);
        this.noteReturn();
      }
    }
  }

  fade(dt, rate) {
    if (rate <= 0) return;
    this.hottest = RW.fadeArray(this.heat, this.grid, Math.exp(-rate * dt));
  }

  draw(pixel) {
    const ctx = this.ctx, g = this.grid, S = this.canvasSize;
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, S, S);
    for (let j = 0; j < g; j++) {
      for (let i = 0; i < g; i++) {
        if (this.heat[j][i] > 0) {
          ctx.fillStyle = RW.heatColor(this.heat[j][i], this.hottest);
          ctx.fillRect(i * pixel, j * pixel, pixel + 1, pixel + 1);
        }
      }
    }
    RW.drawGridLines(ctx, g, pixel, S);
    const sx = this.continuous ? this.x : this.x + 0.5;
    const sy = this.continuous ? this.y : this.y + 0.5;
    RW.drawDotAt(ctx, sx * pixel, sy * pixel, pixel);
  }

  renderStats(fair) {
    const completed = this.walks - 1;
    const avg = completed > 0 ? (this.totalSteps / completed) : 0;
    const std = completed > 0
      ? Math.sqrt(Math.max(0, this.sumLen2 / completed - avg * avg)) : 0;
    const dash = completed > 0;
    this.statsEl.innerHTML =
      `<span data-explain="walks">Walks: <b>${this.walks}</b></span>` +
      `<span data-explain="current">Current: <b>${this.steps}</b></span>` +
      `<span data-explain="last">Last: <b>${dash ? this.lastLen : '–'}</b></span>` +
      `<span data-explain="avg">Avg: <b>${avg.toFixed(1)}</b></span>` +
      `<span data-explain="std">Std: <b>${std.toFixed(1)}</b></span>` +
      `<span data-explain="longest">Longest: <b>${dash ? this.longest : '–'}</b></span>`;

    const coverage = (this.visitedCount / (this.grid * this.grid)) * 100;
    const avgReturns = (this.totalReturns + this.centerReturns) / this.walks;
    if (this.continuous) {
      const rbar = fair.n > 0 ? fair.Rbar.toFixed(3) : '–';
      const ray = fair.n > 0 ? fair.rayleigh.toFixed(2) : '–';
      this.diagEl.innerHTML =
        `<span data-explain="rbar">R̄: <b>${rbar}</b></span>` +
        `<span data-explain="rayleigh">Rayleigh: <b>${ray}</b></span>` +
        `<span data-explain="coverage">Coverage: <b>${coverage.toFixed(1)}%</b></span>` +
        `<span data-explain="isoReturns">Returns to center: <b>${avgReturns.toFixed(1)}</b></span>`;
    } else {
      this.diagEl.innerHTML =
        `<span data-explain="dirbalance">U/D/L/R: <b>${fair.pct[0]} ${fair.pct[1]} ${fair.pct[2]} ${fair.pct[3]}%</b></span>` +
        `<span data-explain="chi">χ²: <b>${fair.chi.toFixed(2)}</b></span>` +
        `<span data-explain="coverage">Coverage: <b>${coverage.toFixed(1)}%</b></span>` +
        `<span data-explain="returns">Returns to center: <b>${avgReturns.toFixed(1)}</b></span>`;
    }

    const exitTotal = this.exitCounts[0] + this.exitCounts[1] + this.exitCounts[2] + this.exitCounts[3];
    const ePct = ['–', '–', '–', '–'];
    if (exitTotal > 0) {
      for (let d = 0; d < 4; d++) ePct[d] = ((this.exitCounts[d] / exitTotal) * 100).toFixed(1);
    }
    const l2const = completed > 0 ? (avg / (this.grid * this.grid)) : 0;
    const l2theory = this.expectedLen / (this.grid * this.grid);
    const measured = completed > 0 ? avg.toFixed(1) : '–';
    const l2measured = completed > 0 ? l2const.toFixed(3) : '–';
    const avgKey = this.continuous ? 'isoAvgtheory' : 'avgtheory';
    const l2Key = this.continuous ? 'isoL2const' : 'l2const';
    this.boundaryEl.innerHTML =
      `<span data-explain="exitdist">Exit T/B/L/R: <b>${ePct[0]} ${ePct[1]} ${ePct[2]} ${ePct[3]}%</b></span>` +
      `<span data-explain="${avgKey}">Avg len: <b>${measured}</b> vs theory <b>${this.expectedLen.toFixed(1)}</b></span>` +
      `<span data-explain="${l2Key}">Avg/grid²: <b>${l2measured}</b> vs theory <b>${l2theory.toFixed(3)}</b></span>`;
  }
};

RW.PanWalk = class PanWalk {
  constructor({ continuous, canvas, statsEl, diagEl }) {
    this.continuous = continuous;
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.statsEl = statsEl;
    this.diagEl = diagEl;
    this.returnRadius = RW.RETURN_RADIUS;
  }

  reset(grid, canvasSize) {
    this.grid = grid;
    this.canvasSize = canvasSize;
    this.heat = new Map();
    this.hottest = 0;
    this.x = 0;
    this.y = 0;
    this.steps = 0;
    this.returns = 0;
    this.wasInReturn = true;
    this.deadHalf = Math.max(1, Math.floor(grid / 5));
    const half = this.continuous ? grid / 2 : Math.floor(grid / 2);
    this.camX = this.x - half;
    this.camY = this.y - half;
    this.bump(0, 0);
  }

  bump(x, y) {
    const i = this.continuous ? Math.floor(x) : x;
    const j = this.continuous ? Math.floor(y) : y;
    const k = i + ',' + j;
    const v = (this.heat.get(k) || 0) + 1;
    this.heat.set(k, v);
    if (v > this.hottest) this.hottest = v;
  }

  step(dx, dy) {
    this.x += dx;
    this.y += dy;
    this.steps += 1;
    if (this.continuous) {
      const r2 = this.x * this.x + this.y * this.y;
      const inside = r2 <= this.returnRadius * this.returnRadius;
      if (inside && !this.wasInReturn) this.returns += 1;
      this.wasInReturn = inside;
    } else if (this.x === 0 && this.y === 0) {
      this.returns += 1;
    }
    this.bump(this.x, this.y);
  }

  fade(dt, rate) {
    if (rate <= 0) return;
    this.hottest = RW.fadeMap(this.heat, Math.exp(-rate * dt));
  }

  // Pan the dead-zone camera the minimum needed to keep the walker within the
  // central box. Called only at draw time (the simulation itself is unaffected).
  updateCamera() {
    const half = this.continuous ? this.grid / 2 : Math.floor(this.grid / 2);
    const vx = this.x - this.camX;
    const vy = this.y - this.camY;
    const lo = half - this.deadHalf;
    const hi = half + this.deadHalf;
    if (vx < lo) this.camX = this.x - lo;
    else if (vx > hi) this.camX = this.x - hi;
    if (vy < lo) this.camY = this.y - lo;
    else if (vy > hi) this.camY = this.y - hi;
  }

  draw(pixel) {
    this.updateCamera();
    const ctx = this.ctx, g = this.grid, S = this.canvasSize;
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, S, S);

    const i0 = Math.floor(this.camX) - 1;
    const j0 = Math.floor(this.camY) - 1;
    for (let j = j0; j <= j0 + g + 1; j++) {
      for (let i = i0; i <= i0 + g + 1; i++) {
        const v = this.heat.get(i + ',' + j);
        if (!v) continue;
        ctx.fillStyle = RW.heatColor(v, this.hottest);
        ctx.fillRect((i - this.camX) * pixel, (j - this.camY) * pixel, pixel + 1, pixel + 1);
      }
    }

    if (this.continuous) RW.drawWorldGrid(ctx, this.camX, this.camY, g, pixel, S);
    else RW.drawGridLines(ctx, g, pixel, S);

    const half = this.continuous ? g / 2 : Math.floor(g / 2);
    ctx.strokeStyle = 'rgba(255,255,255,0.18)';
    ctx.lineWidth = 1;
    const boxLo = (half - this.deadHalf) * pixel;
    const boxSize = (this.continuous ? 2 * this.deadHalf : 2 * this.deadHalf + 1) * pixel;
    ctx.strokeRect(boxLo + 0.5, boxLo + 0.5, boxSize, boxSize);

    ctx.strokeStyle = '#5b8def';
    ctx.lineWidth = 2;
    if (this.continuous) {
      const ox = (0 - this.camX) * pixel;
      const oy = (0 - this.camY) * pixel;
      ctx.beginPath();
      ctx.arc(ox, oy, this.returnRadius * pixel, 0, Math.PI * 2);
      ctx.stroke();
    } else {
      const oVi = 0 - this.camX, oVj = 0 - this.camY;
      if (oVi >= 0 && oVi < g && oVj >= 0 && oVj < g) {
        ctx.strokeRect(oVi * pixel + 1, oVj * pixel + 1, pixel - 2, pixel - 2);
      }
    }

    const sx = this.continuous ? this.x - this.camX : this.x - this.camX + 0.5;
    const sy = this.continuous ? this.y - this.camY : this.y - this.camY + 0.5;
    RW.drawDotAt(ctx, sx * pixel, sy * pixel, pixel);
  }

  renderStats(msd, ensembleSize) {
    const r2 = this.x * this.x + this.y * this.y;
    const dist = Math.sqrt(r2);
    const ratio = this.steps > 0 ? r2 / this.steps : 0;
    const returnsKey = this.continuous ? 'isoPanreturns' : 'panreturns';
    let expLabel = '–';
    if (this.steps > 1) {
      expLabel = this.continuous
        ? (this.returnRadius * this.returnRadius * Math.log(this.steps)).toFixed(2)
        : (Math.log(this.steps) / Math.PI).toFixed(2);
    }
    this.statsEl.innerHTML =
      `<span data-explain="panN">Steps N: <b>${this.steps}</b></span>` +
      `<span data-explain="dist">Distance r: <b>${dist.toFixed(1)}</b></span>` +
      `<span data-explain="r2n">r²/N: <b>${ratio.toFixed(3)}</b></span>` +
      `<span data-explain="${returnsKey}">Returns to origin: <b>${this.returns}</b> vs ~<b>${expLabel}</b></span>`;
    this.diagEl.innerHTML =
      `<span data-explain="msd">⟨r²⟩/N over ${ensembleSize} walkers: <b>${msd.toFixed(3)}</b> (theory → 1)</span>`;
  }
};
