var RW = window.RW = window.RW || {};

RW.heatColor = function heatColor(v, hot) {
  if (v <= 0) return '#000';
  const t = Math.log(1 + v) / Math.log(1 + Math.max(hot, 1));
  const stops = [
    [0.00,   0,   0,   0],
    [0.15,  20,  10,  80],
    [0.35, 130,  20, 140],
    [0.55, 220,  40,  60],
    [0.75, 250, 140,  20],
    [0.90, 250, 230,  60],
    [1.00, 255, 255, 255],
  ];
  for (let i = 1; i < stops.length; i++) {
    if (t <= stops[i][0]) {
      const a = stops[i - 1], b = stops[i];
      const f = (t - a[0]) / (b[0] - a[0]);
      const r = Math.round(a[1] + f * (b[1] - a[1]));
      const g = Math.round(a[2] + f * (b[2] - a[2]));
      const bl = Math.round(a[3] + f * (b[3] - a[3]));
      return `rgb(${r},${g},${bl})`;
    }
  }
  return '#fff';
};

RW.drawGridLines = function drawGridLines(ctx, grid, pixel, canvasSize) {
  if (pixel < 6) return;
  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= grid; i++) {
    const p = Math.round(i * pixel) + 0.5;
    ctx.beginPath(); ctx.moveTo(p, 0); ctx.lineTo(p, canvasSize); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(0, p); ctx.lineTo(canvasSize, p); ctx.stroke();
  }
};

RW.drawWorldGrid = function drawWorldGrid(ctx, camX, camY, grid, pixel, canvasSize) {
  if (pixel < 6) return;
  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.lineWidth = 1;
  const i0 = Math.floor(camX);
  const j0 = Math.floor(camY);
  for (let i = i0; i <= i0 + grid + 1; i++) {
    const p = Math.round((i - camX) * pixel) + 0.5;
    ctx.beginPath(); ctx.moveTo(p, 0); ctx.lineTo(p, canvasSize); ctx.stroke();
  }
  for (let j = j0; j <= j0 + grid + 1; j++) {
    const p = Math.round((j - camY) * pixel) + 0.5;
    ctx.beginPath(); ctx.moveTo(0, p); ctx.lineTo(canvasSize, p); ctx.stroke();
  }
};

RW.drawDotAt = function drawDotAt(ctx, px, py, pixel, color) {
  ctx.fillStyle = color || '#fff';
  ctx.beginPath();
  const r = Math.max(1.5, pixel / 2 - 1);
  ctx.arc(px, py, r, 0, Math.PI * 2);
  ctx.fill();
};

RW.fadeArray = function fadeArray(heat, grid, factor) {
  let newHot = 0;
  for (let j = 0; j < grid; j++) {
    const row = heat[j];
    for (let i = 0; i < grid; i++) {
      const v = row[i] * factor;
      row[i] = v < 0.001 ? 0 : v;
      if (row[i] > newHot) newHot = row[i];
    }
  }
  return newHot;
};

RW.fadeMap = function fadeMap(map, factor) {
  let newHot = 0;
  for (const [k, v] of map) {
    const nv = v * factor;
    if (nv < 0.001) map.delete(k);
    else {
      map.set(k, nv);
      if (nv > newHot) newHot = nv;
    }
  }
  return newHot;
};

// Generic time-series plotter. `refLines` are dashed horizontal markers
// [value, color]; `yMax` sets the visible top (yMin is always 0).
RW.drawSeries = function drawSeries(ctx, history, yMax, color, refLines, canvasSize, plotH) {
  const W = canvasSize, H = plotH;
  const padL = 34, padR = 8, padT = 8, padB = 18;
  ctx.fillStyle = '#0c0d10';
  ctx.fillRect(0, 0, W, H);

  const x0 = padL, x1 = W - padR, y0 = H - padB, y1 = padT;
  const yToPx = (v) => y0 + (v / yMax) * (y1 - y0);

  const ticks = yMax <= 2 ? [0, 1, 2] : [0, yMax / 2, yMax];
  ctx.strokeStyle = 'rgba(255,255,255,0.08)';
  ctx.fillStyle = 'rgba(255,255,255,0.5)';
  ctx.font = '10px system-ui, sans-serif';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  ctx.lineWidth = 1;
  for (const v of ticks) {
    const py = Math.round(yToPx(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(x0, py); ctx.lineTo(x1, py); ctx.stroke();
    ctx.fillText(v % 1 === 0 ? v.toFixed(0) : v.toFixed(1), x0 - 5, yToPx(v));
  }

  ctx.setLineDash([4, 4]);
  for (const [v, c] of refLines) {
    if (v > yMax) continue;
    ctx.strokeStyle = c;
    const py = Math.round(yToPx(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(x0, py); ctx.lineTo(x1, py); ctx.stroke();
  }
  ctx.setLineDash([]);

  ctx.textAlign = 'center';
  ctx.fillStyle = 'rgba(255,255,255,0.4)';
  ctx.fillText('N (steps) →', (x0 + x1) / 2, H - 5);

  if (history.length < 2) return;
  const nMax = history[history.length - 1][0];
  const xToPx = (n) => x0 + (nMax > 0 ? n / nMax : 0) * (x1 - x0);

  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  for (let i = 0; i < history.length; i++) {
    const [n, v] = history[i];
    const px = xToPx(n);
    const py = yToPx(Math.max(0, Math.min(yMax, v)));
    if (i === 0) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  }
  ctx.stroke();
};

// Push a sample and decimate (keep every other point) when the buffer fills,
// preserving the full N=0..now range so the line always spans the plot.
RW.record = function record(history, n, v) {
  history.push([n, v]);
  if (history.length > 2000) {
    const half = [];
    for (let i = 0; i < history.length; i += 2) half.push(history[i]);
    return half;
  }
  return history;
};
