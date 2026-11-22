/* Front tests — Stock Analysis page (node tests_front/test_calc.js, no dependency).
 *  1. parity: the page's arithmetic (templates/partials/stock/_sa_calc.html) against the Python
 *     engine's own outputs in data/stock (implied growth, ROIC>WACC count, portfolio volatility…)
 *  2. properties of the Monte Carlo bootstrap and of the optimiser
 *  3. contract between markup and script: every element the script looks up exists
 * Exit code 1 on any failure (CI). */
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const ROOT = path.resolve(__dirname, '..');
const read = p => fs.readFileSync(path.join(ROOT, p), 'utf8');
let pass = 0, fail = 0;
const ok = (name, cond, info) => { if(cond){ pass++; } else { fail++; console.log('  ✗ ' + name + (info !== undefined ? ' — ' + info : '')); } };
const near = (a, b, tol) => typeof a === 'number' && typeof b === 'number' && Math.abs(a - b) <= tol;

// load the pure module exactly as the page does
const calcSrc = read('templates/partials/stock/_sa_calc.html').match(/<script>([\s\S]*)<\/script>/)[1];
const ctx = {window: {}}; vm.createContext(ctx); vm.runInContext(calcSrc, ctx);
const C = ctx.window.SACALC;
ok('SACALC loaded', !!C && typeof C.bootstrap === 'function');

console.log('1. Parity with the Python engine');
const vmap = JSON.parse(read('data/stock/valuation_map.json'));
let nG = 0, nShare = 0;
vmap.rows.forEach(r => {
  const d0 = C.derive(r, 0);
  if(typeof r.implied_growth === 'number'){ nG++; ok('implied growth at base WACC = engine (' + r.ticker + ')', near(d0.g, r.implied_growth, 1e-12)); }
  const m = /^(\d+)\/(\d+)$/.exec(String(r.roic_years_above_wacc || ''));
  if(m){ nShare++; ok('ROIC > WACC count = engine (' + r.ticker + ')', d0.up === +m[1] && d0.n === +m[2], d0.up + '/' + d0.n + ' vs ' + r.roic_years_above_wacc); }
  const R = r.rdcf || {};
  if(typeof r.wacc === 'number' && typeof R.ev === 'number' && R.fcf_adj > 0){
    const w = r.wacc + 0.01, g = (w * R.ev - R.fcf_adj) / (R.ev + R.fcf_adj);
    ok('implied growth at WACC +1 pt (' + r.ticker + ')', near(C.derive(r, 0.01).g, g, 1e-12));
    ok('higher WACC → higher implied growth (' + r.ticker + ')', C.derive(r, 0.01).g > C.derive(r, 0).g);
  }
});
ok('implied growth compared for most companies', nG >= 20, nG);
ok('ROIC counts compared for most companies', nShare >= 20, nShare);

const lab = JSON.parse(read('data/stock/lab_data.json'));
const rep = JSON.parse(read('data/stock/portfolio_report_USD.json'));
const B = C.decode(lab.main), pos = lab.holdings.map(h => ({t: h.t, cur: h.cur || 'USD', w: 1 / lab.holdings.length}));
const port = C.portfolio(B, pos, 'USD'), s = C.stats(B, port.p, null, 0.04);
const pyVol = rep.bloc_2_risque.risk_pure.volatility_annualized, pyMdd = rep.bloc_2_risque.risk_pure.max_drawdown.max_drawdown;
ok('equal-weight volatility = engine within 0.6 pt', near(s.vol, pyVol, 0.006), s.vol.toFixed(4) + ' vs ' + pyVol.toFixed(4));
ok('equal-weight max drawdown = engine within 1.5 pt', near(s.mdd, pyMdd, 0.015), s.mdd.toFixed(4) + ' vs ' + pyMdd.toFixed(4));
ok('Sharpe and Sortino computed', typeof s.sharpe === 'number' && typeof s.sortino === 'number');
const rsum = C.riskContrib(pos, port.ser, port.p).reduce((a, b) => a + b, 0);
ok('risk contributions sum to ~100%', near(rsum, 1, 0.05), rsum.toFixed(3));
const chf = C.portfolio(B, pos, 'CHF');
ok('currency lens changes the returns', chf.p.some((v, i) => i > 0 && Math.abs(v - port.p[i]) > 1e-9));

console.log('2. Monte Carlo and optimiser');
const flat = new Array(500).fill(0.0004);
const bf = C.bootstrap(flat, {days: 252, paths: 200, seed: 7});
ok('bootstrap: constant returns → no spread', near(bf.final.p5, bf.final.p95, 1e-12) && near(bf.final.p50, Math.pow(1.0004, 252), 1e-9));
ok('bootstrap: constant gains → no loss, no fall', bf.probLoss === 0 && bf.medianMdd === 0);
const real = Array.from(port.p).slice(1);
const b1 = C.bootstrap(real, {days: 756, paths: 500, seed: 11}), b2 = C.bootstrap(real, {days: 756, paths: 500, seed: 11});
ok('bootstrap: same seed → same result', b1.final.p50 === b2.final.p50 && b1.probLoss === b2.probLoss);
ok('bootstrap: percentiles ordered', b1.final.p5 <= b1.final.p25 && b1.final.p25 <= b1.final.p50 && b1.final.p50 <= b1.final.p75 && b1.final.p75 <= b1.final.p95);
ok('bootstrap: bands start at 1', b1.bands.p5[0] === 1 && b1.bands.p95[0] === 1);
ok('bootstrap: probabilities in [0, 1]', b1.probLoss >= 0 && b1.probLoss <= 1 && b1.probDD30 >= 0 && b1.probDD30 <= 1);
ok('bootstrap: falls are ≤ 0', b1.medianMdd <= 0);

const pc = C.projectCapped([0.9, 0.05, 0.05, 0.0, 0.0], 0.3);
ok('capped projection: sums to 1, within [0, cap]', near(pc.reduce((a, b) => a + b, 0), 1, 1e-9) && pc.every(x => x >= -1e-12 && x <= 0.3 + 1e-12));
const names = lab.holdings.slice(0, 8).map(h => C.refSeries(B, h.t, h.cur || 'USD', 'USD'));
const rows = C.aligned(names), m = C.moments(names, rows, C.refSeries(B, 'ACWI', 'USD', 'USD'));
const eq = names.map(() => 1 / names.length);
const wS = C.optimize(m, {mode: 'sharpe', rf: 0.04, cap: 0.3}), wV = C.optimize(m, {mode: 'minvol', cap: 0.3});
const sharpe = w => C.evaluate(names, rows, w, 0.04).sharpe, vol = w => C.evaluate(names, rows, w, 0).vol;
ok('optimiser: weights sum to 1 within the cap', near(wS.reduce((a, b) => a + b, 0), 1, 1e-6) && wS.every(x => x >= -1e-9 && x <= 0.3 + 1e-9));
ok('optimiser: best Sharpe ≥ equal weight (in sample)', sharpe(wS) >= sharpe(eq) - 1e-9, sharpe(wS).toFixed(3) + ' vs ' + sharpe(eq).toFixed(3));
ok('optimiser: lowest volatility ≤ equal weight', vol(wV) <= vol(eq) + 1e-9, vol(wV).toFixed(4) + ' vs ' + vol(eq).toFixed(4));
const wB = C.optimize(m, {mode: 'sharpe', rf: 0.04, cap: 0.3, betaCap: 0.6}), beta = w => w.reduce((a, x, i) => a + x * m.beta[i], 0);
ok('optimiser: beta cap lowers the market beta', beta(wB) <= beta(wS) + 1e-9, beta(wB).toFixed(2) + ' vs ' + beta(wS).toFixed(2));
ok('optimiser: beta cap respected within 0.02', beta(wB) <= 0.62 || beta(wS) <= 0.6, beta(wB).toFixed(3));

console.log('2b. Fade model and currencies');
const S0 = {price: 100, shares: 10, nd: 50};
const w0 = 0.09, fcf0 = 60;
ok('fade with g1 = gT equals the single-stage value', near(C.fadeValue(S0, 0.03, 0.03, 10, w0, fcf0), C.simValue(S0, 0.03, w0, fcf0), 1e-9));
const gF = C.fadeImplied(S0, 0.03, 10, w0, fcf0);
ok('fade implied growth reproduces the price', typeof gF === 'number' && near(C.fadeValue(S0, gF, 0.03, 10, w0, fcf0), S0.price, 1e-6));
ok('fade value rises with year-1 growth', C.fadeValue(S0, 0.10, 0.03, 10, w0, fcf0) > C.fadeValue(S0, 0.05, 0.03, 10, w0, fcf0));
ok('fade: terminal rate at or above WACC → N/D', C.fadeValue(S0, 0.05, 0.09, 10, w0, fcf0) === null);
const fx = {USD: {usd_per_unit: 1}, EUR: {usd_per_unit: 1.08, date: '2026-11-20', source: 'test'}, GBP: {usd_per_unit: 1.25, date: '2026-11-20', source: 'test'}};
ok('fx: EUR per USD', near(C.fxRate(fx, 'USD', 'EUR').r, 1 / 1.08, 1e-12));
ok('fx: pence are a hundredth of a pound', near(C.fxRate(fx, 'GBp', 'USD').r, 0.0125, 1e-12));
ok('fx: unknown currency → null (N/D), never a guess', C.fxRate(fx, 'KZT', 'EUR') === null);
const vfx = vmap.fx || {};
ok('published fx carries date and source for every non-USD currency', Object.keys(vfx).filter(k => k !== 'USD').every(k => vfx[k].usd_per_unit == null || (vfx[k].date && vfx[k].source)));

console.log('3. Markup ↔ script contract');
const html = read('templates/partials/stock/_sa_sections.html'), js = read('templates/partials/stock/_sa_js.html');
const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map(x => x[1]).concat([...js.matchAll(/id=\\?"([a-z0-9-]+)\\?"/gi)].map(x => x[1])));
ids.add('sa-tt');   // created by the script
const looked = new Set([...js.matchAll(/\$\('#([a-z0-9-]+)['\s]/gi), ...js.matchAll(/getElementById\('([a-z0-9-]+)'\)/gi)].map(x => x[1]));
const missing = [...looked].filter(i => !ids.has(i) && !/^(dd-cur-btn|pt-cur-label|sa-tr-)/.test(i));
ok('every element the script looks up exists', missing.length === 0, missing.join(', '));
ok('no Jinja comment opener in the page assets', !/\{#/.test(js + read('templates/partials/stock/_sa_style.html') + calcSrc));

console.log(`\n${'='.repeat(60)}\nRÉSULTAT : ${pass} ✓ / ${fail} ✗`);
process.exit(fail ? 1 : 0);
