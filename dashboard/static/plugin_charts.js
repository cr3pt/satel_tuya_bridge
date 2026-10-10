/* Wspólne wykresy wtyczek (Chart.js 4 musi być załadowany wcześniej).
 * Kolejność barw kategorii jest stała (L1/L2/L3, strefy) - kolor należy do bytu,
 * nie do pozycji po filtrowaniu. Paleta sprawdzona walidatorem na tle #1d2026. */
(function () {
  const COLORS = ['#3987e5', '#d95926', '#199e70'];
  const SURFACE = '#1d2026', TEXT = '#e6e8eb', MUTED = '#8b93a1', GRID = '#2c3038';

  const crosshair = {
    id: 'crosshair',
    afterDatasetsDraw(chart) {
      const act = chart.tooltip && chart.tooltip.getActiveElements ? chart.tooltip.getActiveElements() : [];
      if (!act.length || chart.config.type !== 'line') return;
      const x = act[0].element.x, a = chart.chartArea, c = chart.ctx;
      c.save(); c.strokeStyle = MUTED; c.lineWidth = 1; c.setLineDash([3, 3]);
      c.beginPath(); c.moveTo(x, a.top); c.lineTo(x, a.bottom); c.stroke(); c.restore();
    },
  };

  function fmtTime(ts, withDate) {
    const d = new Date(ts * 1000);
    const t = d.toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit' });
    return withDate ? d.toLocaleDateString('pl-PL', { day: '2-digit', month: '2-digit' }) + ' ' + t : t;
  }

  function baseOptions(unit, extra) {
    return Object.assign({
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false } },
      scales: {
        y: { title: { display: !!unit, text: unit, color: MUTED }, grid: { color: GRID }, ticks: { color: MUTED }, border: { display: false } },
      },
    }, extra || {});
  }

  /* datasets: [{label, color, points: [[ts, value], ...]}] - jedna oś Y (jedna jednostka). */
  function lineChart(canvas, datasets, unit, spanSeconds) {
    const withDate = spanSeconds > 36 * 3600;
    const opts = baseOptions(unit);
    opts.interaction = { mode: 'nearest', axis: 'x', intersect: false };
    opts.scales.x = { type: 'linear', grid: { color: GRID }, ticks: { color: MUTED, maxTicksLimit: 8, callback: v => fmtTime(v, withDate) }, border: { color: GRID } };
    opts.plugins.tooltip = { callbacks: {
      title: items => items.length ? fmtTime(items[0].parsed.x, true) : '',
      label: it => `${it.dataset.label}: ${it.parsed.y.toLocaleString('pl-PL', { maximumFractionDigits: 3 })} ${unit}`,
    } };
    return new Chart(canvas, {
      type: 'line', plugins: [crosshair], options: opts,
      data: { datasets: datasets.map((d, i) => ({
        label: d.label, borderColor: d.color || COLORS[i % 3], backgroundColor: d.color || COLORS[i % 3],
        data: d.points.map(p => ({ x: p[0], y: p[1] })), parsing: false,
        borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, pointHoverBorderWidth: 2, pointHoverBorderColor: SURFACE, tension: 0.2,
      })) },
    });
  }

  /* datasets: [{label, color, values: []}], stos z 2px przerwą powierzchni między segmentami. */
  function stackedBar(canvas, labels, datasets, unit) {
    const opts = baseOptions(unit);
    opts.interaction = { mode: 'nearest', intersect: true };
    opts.scales.x = { stacked: true, grid: { display: false }, ticks: { color: MUTED, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }, border: { color: GRID } };
    opts.scales.y.stacked = true;
    opts.plugins.tooltip = { callbacks: {
      label: it => `${it.dataset.label}: ${it.parsed.y.toLocaleString('pl-PL', { maximumFractionDigits: 2 })} ${unit}`,
    } };
    return new Chart(canvas, {
      type: 'bar', options: opts,
      data: { labels, datasets: datasets.map((d, i) => ({
        label: d.label, data: d.values, backgroundColor: d.color || COLORS[i % 3],
        borderColor: SURFACE, borderWidth: 2, borderRadius: 4, borderSkipped: false, maxBarThickness: 28,
      })) },
    });
  }

  function simpleBar(canvas, labels, values, unit, color) {
    return stackedBar(canvas, labels, [{ label: 'Koszt brutto', values, color: color || COLORS[0] }], unit);
  }

  /* Legenda HTML (tekst zawsze w kolorze tekstu, kolor tylko w znaczniku); klik = ukryj/pokaż. */
  function legend(container, chart) {
    container.replaceChildren();
    chart.data.datasets.forEach((ds, i) => {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'legend-item';
      const sw = document.createElement('span'); sw.className = 'legend-sw'; sw.style.background = ds.borderColor && chart.config.type === 'line' ? ds.borderColor : ds.backgroundColor;
      b.appendChild(sw); b.appendChild(document.createTextNode(ds.label));
      b.addEventListener('click', () => {
        const vis = chart.isDatasetVisible(i);
        chart.setDatasetVisibility(i, !vis); b.classList.toggle('off', vis); chart.update();
      });
      container.appendChild(b);
    });
  }

  /* Widok tabelowy tych samych danych: headers: [..], rows: [[..], ..] (textContent). */
  function fillTable(table, headers, rows) {
    table.replaceChildren();
    const thead = document.createElement('thead'), tr = document.createElement('tr');
    headers.forEach(h => { const th = document.createElement('th'); th.textContent = h; tr.appendChild(th); });
    thead.appendChild(tr); table.appendChild(thead);
    const tbody = document.createElement('tbody');
    rows.forEach(r => { const row = document.createElement('tr'); r.forEach(v => { const td = document.createElement('td'); td.textContent = v; row.appendChild(td); }); tbody.appendChild(row); });
    table.appendChild(tbody);
  }

  window.PluginCharts = { COLORS, lineChart, stackedBar, simpleBar, legend, fillTable, fmtTime };
})();
