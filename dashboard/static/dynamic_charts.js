/* Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
 * SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause
 *
 * Wlasne wykresy uzytkownika (dowolna liczba). Dolaczany jednym tagiem:
 *   <script src="/static/dynamic_charts.js" data-scope="dashboard"></script>    (strona glowna)
 *   <script src="/static/dynamic_charts.js" data-scope="energy_cost"></script>  (zakladka Koszty)
 */
(function () {
  'use strict';
  const SCOPE = (document.currentScript && document.currentScript.dataset.scope) || 'dashboard';
  const API = SCOPE === 'energy_cost' ? '/api/plugins/energy_cost/charts' : '/api/dashboard/charts';
  const COLORS = ['#4d8dfd', '#3dd68c', '#e5484d', '#f2b93d', '#c792ea', '#5ecbe0', '#ff9f68', '#8b93a1'];
  const instances = {};
  let charts = [];
  let sensors = [];
  let labels = {};
  let editing = null;

  function el(tag, props, children) {
    const e = document.createElement(tag);
    Object.assign(e, props || {});
    (children || []).forEach(c => e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
    return e;
  }

  async function api(url, options) {
    const resp = await fetch(url, options);
    if (resp.status === 401) {
      window.location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
      throw new Error('Wymagane zalogowanie');
    }
    return resp;
  }

  function ensureChartJs() {
    if (window.Chart) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = 'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.5.1/chart.umd.min.js';
      s.onload = () => resolve();
      s.onerror = () => reject(new Error('Nie zaladowano Chart.js'));
      document.head.appendChild(s);
    });
  }

  function host() {
    let h = document.getElementById('userCharts');
    if (h) return h;
    h = el('div', { id: 'userCharts' });
    const anchor = document.getElementById('historyChart');
    const card = anchor ? anchor.closest('.card') : null;
    if (card && card.parentNode) card.parentNode.insertBefore(h, card.nextSibling);
    else document.body.appendChild(h);
    return h;
  }

  function cardStyle(node) {
    node.style.cssText = 'background:var(--card,#1d2026);border:1px solid var(--border,#2c3038);' +
      'border-radius:8px;padding:16px;margin-bottom:16px;';
    return node;
  }

  function btn(text, onClick, title) {
    const b = el('button', { type: 'button', textContent: text, title: title || '' });
    b.style.cssText = 'background:#262a32;color:var(--text,#e6e8eb);border:1px solid var(--border,#2c3038);' +
      'border-radius:6px;padding:5px 10px;font-size:13px;cursor:pointer;margin-left:6px;';
    b.addEventListener('click', onClick);
    return b;
  }

  function fmtTs(ms) { return new Date(ms).toLocaleString('pl-PL'); }

  function bucketize(points, hours, n, agg) {
    const end = Date.now(), start = end - hours * 3600e3, size = (end - start) / n;
    const sums = new Array(n).fill(0), cnt = new Array(n).fill(0);
    const last = new Array(n).fill(null), lastX = new Array(n).fill(-Infinity);
    points.forEach(p => {
      if (p.x < start || p.x > end) return;
      const i = Math.min(n - 1, Math.floor((p.x - start) / size));
      sums[i] += p.y; cnt[i] += 1;
      if (p.x >= lastX[i]) { lastX[i] = p.x; last[i] = p.y; }
    });
    return {
      values: sums.map((s, i) => (!cnt[i] ? null : agg === 'sum' ? s : agg === 'last' ? last[i] : s / cnt[i])),
      labels: sums.map((_, i) => fmtTs(start + i * size)),
    };
  }

  async function drawChart(cfg, canvas) {
    await ensureChartJs();
    if (instances[cfg.id]) { instances[cfg.id].destroy(); delete instances[cfg.id]; }
    const hours = Number(cfg.hours) || 24;
    const series = await Promise.all((cfg.sensors || []).map(async (name) => {
      let data = [];
      try {
        const r = await api('/api/history?sensor=' + encodeURIComponent(name) + '&hours=' + hours);
        data = (await r.json()).map(d => ({ x: d.t * 1000, y: d.v }));
      } catch (e) { /* brak danych dla czujnika */ }
      return { name, label: labels[name] || name, data };
    }));
    const grid = '#2c3038', tick = '#8b93a1';
    if (cfg.type === 'bar') {
      const buckets = series.map(s => bucketize(s.data, hours, 48, cfg.agg || 'avg'));
      instances[cfg.id] = new Chart(canvas, {
        type: 'bar',
        data: {
          labels: buckets.length ? buckets[0].labels : [],
          datasets: series.map((s, i) => ({
            label: s.label, data: buckets[i].values,
            backgroundColor: COLORS[i % COLORS.length], borderRadius: 3,
          })),
        },
        options: {
          responsive: true, animation: false,
          scales: {
            x: { ticks: { color: tick, maxTicksLimit: 8 }, grid: { color: grid } },
            y: { ticks: { color: tick }, grid: { color: grid } },
          },
          plugins: { legend: { labels: { color: '#e6e8eb' } } },
        },
      });
    } else {
      instances[cfg.id] = new Chart(canvas, {
        type: 'line',
        data: {
          datasets: series.map((s, i) => ({
            label: s.label, data: s.data, borderColor: COLORS[i % COLORS.length],
            backgroundColor: COLORS[i % COLORS.length], pointRadius: 0, tension: 0.2, fill: false,
          })),
        },
        options: {
          responsive: true, animation: false, parsing: false,
          scales: {
            x: { type: 'linear', ticks: { color: tick, maxTicksLimit: 8, callback: v => fmtTs(v) }, grid: { color: grid } },
            y: { ticks: { color: tick }, grid: { color: grid } },
          },
          plugins: { legend: { labels: { color: '#e6e8eb' } } },
        },
      });
    }
  }

  async function save() {
    const resp = await api(API, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ charts }),
    });
    const data = await resp.json();
    if (!resp.ok) { alert('Blad zapisu: ' + (data.error || resp.status)); return false; }
    charts = data.charts || charts;
    return true;
  }

  function editorCard(cfg) {
    const card = cardStyle(el('div'));
    const title = el('input', { type: 'text', value: cfg.title || '', placeholder: 'Tytul wykresu' });
    const hours = el('select');
    [['1', '1 godzina'], ['6', '6 godzin'], ['24', '24 godziny'], ['168', '7 dni'], ['720', '30 dni']].forEach(([v, t]) => {
      hours.appendChild(el('option', { value: v, textContent: t }));
    });
    hours.value = String(cfg.hours || 24);
    if (!hours.value) hours.value = '24';
    const type = el('select');
    [['line', 'Liniowy'], ['bar', 'Slupkowy']].forEach(([v, t]) => type.appendChild(el('option', { value: v, textContent: t })));
    type.value = cfg.type || 'line';
    const agg = el('select', { title: 'Agregacja w przedziale (wykres s\u0142upkowy)' });
    [['avg', '\u015arednia'], ['sum', 'Suma'], ['last', 'Ostatnia warto\u015b\u0107']].forEach(([v, t]) => agg.appendChild(el('option', { value: v, textContent: t })));
    agg.value = cfg.agg || 'avg';
    const syncAgg = () => { agg.disabled = type.value !== 'bar'; };
    type.addEventListener('change', syncAgg);
    syncAgg();
    const filter = el('input', { type: 'text', placeholder: 'Szukaj czujnika...' });
    const list = el('div');
    list.style.cssText = 'max-height:220px;overflow-y:auto;margin-top:8px;';
    const chosen = new Set(cfg.sensors || []);
    function renderList() {
      list.replaceChildren();
      const q = filter.value.trim().toLowerCase();
      sensors.filter(n => !q || n.toLowerCase().includes(q) || (labels[n] || '').toLowerCase().includes(q)).forEach(n => {
        const cb = el('input', { type: 'checkbox', checked: chosen.has(n) });
        cb.addEventListener('change', () => { if (cb.checked) chosen.add(n); else chosen.delete(n); });
        const row = el('label', {}, [cb, ' ' + (labels[n] || n)]);
        row.style.cssText = 'display:block;padding:3px 0;font-size:13px;cursor:pointer;';
        list.appendChild(row);
      });
    }
    filter.addEventListener('input', renderList);
    renderList();
    [title, hours, type, agg, filter].forEach(i => {
      i.style.cssText = 'background:#262a32;color:var(--text,#e6e8eb);border:1px solid var(--border,#2c3038);' +
        'border-radius:6px;padding:6px 10px;font-size:13px;margin:0 8px 8px 0;';
    });
    card.appendChild(el('h2', { textContent: cfg._new ? 'Nowy wykres' : 'Edycja wykresu' }));
    card.appendChild(title); card.appendChild(hours); card.appendChild(type); card.appendChild(agg); card.appendChild(filter);
    card.appendChild(list);
    const actions = el('div');
    actions.style.marginTop = '10px';
    actions.appendChild(btn('Zapisz', async () => {
      const next = {
        id: cfg.id, title: title.value.trim() || 'Wykres', sensors: Array.from(chosen),
        type: type.value, hours: Number(hours.value), agg: agg.value,
      };
      const idx = charts.findIndex(c => c.id === cfg.id);
      if (idx >= 0) charts[idx] = next; else charts.push(next);
      if (await save()) { editing = null; render(); }
    }));
    actions.appendChild(btn('Anuluj', () => { editing = null; render(); }));
    card.appendChild(actions);
    return card;
  }

  function render() {
    const h = host();
    Object.keys(instances).forEach(k => { instances[k].destroy(); delete instances[k]; });
    h.replaceChildren();
    const bar = cardStyle(el('div'));
    bar.appendChild(el('strong', { textContent: 'Wlasne wykresy' }));
    bar.appendChild(btn('+ Dodaj wykres', () => {
      editing = { id: 'chart_' + Date.now().toString(36), title: '', sensors: [], type: 'line', hours: 24, _new: true };
      render();
    }));
    h.appendChild(bar);
    if (editing) h.appendChild(editorCard(editing));
    charts.forEach(cfg => {
      const card = cardStyle(el('div'));
      const head = el('div');
      head.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;';
      head.appendChild(el('strong', { textContent: cfg.title }));
      const tools = el('div');
      tools.appendChild(btn('Edytuj', () => { editing = Object.assign({}, cfg); render(); }));
      tools.appendChild(btn('Usun', async () => {
        if (!confirm('Usunac wykres "' + cfg.title + '"?')) return;
        charts = charts.filter(c => c.id !== cfg.id);
        if (await save()) render();
      }));
      head.appendChild(tools);
      card.appendChild(head);
      if (!(cfg.sensors || []).length) {
        card.appendChild(el('div', { textContent: 'Brak wybranych czujnikow - kliknij Edytuj.' }));
      } else {
        const canvas = el('canvas');
        canvas.style.maxHeight = '320px';
        card.appendChild(canvas);
        drawChart(cfg, canvas).catch(e => card.appendChild(el('div', { textContent: 'Blad wykresu: ' + e.message })));
      }
      h.appendChild(card);
    });
  }

  async function init() {
    try {
      const [cfgResp, sensResp, metaResp] = await Promise.all([
        api(API), api('/api/sensors'), fetch('/api/config/sensors').catch(() => null),
      ]);
      charts = (await cfgResp.json()).charts || [];
      sensors = await sensResp.json();
      if (metaResp && metaResp.ok) {
        (await metaResp.json()).forEach(s => { if (s && s.name) labels[s.name] = s.label || s.name; });
      }
    } catch (e) {
      console.error('dynamic_charts:', e);
      return;
    }
    render();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
