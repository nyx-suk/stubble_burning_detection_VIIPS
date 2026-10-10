const productDescriptions = {
  fusion: { text: 'Cross-checks near real-time and daily VIIRS fire products.', resolution: 'Fusion product · 375 m and 1 km inputs' },
  lance: { text: 'Near real-time active fire detections from the VIIRS LANCE C2 feed.', resolution: 'Nominal source pixel · 375 m' },
  daily: { text: 'Daily fire mask detections from the VIIRS VNP14A1 product.', resolution: 'Nominal source pixel · 1 km' }
};

const map = L.map('map', { zoomControl: false }).setView([30.9, 75.8], 6);
L.control.zoom({ position: 'bottomright' }).addTo(map);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 18,
  attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);

const stateSelect = document.getElementById('state');
const districtSelect = document.getElementById('district');
const productSelect = document.getElementById('product');
const startInput = document.getElementById('startDate');
const endInput = document.getElementById('endDate');
const runButton = document.getElementById('analyzeButton');
const exportButton = document.getElementById('exportButton');
const statusNode = document.getElementById('status');
const mapStatus = document.getElementById('mapStatus');
const dataMode = document.getElementById('dataMode');
let hotspotLayer = L.layerGroup().addTo(map);
let boundaryLayer = null;
let currentPage = 1;
let exportPoll = null;

function dateInputValue(date) {
  const offset = date.getTimezoneOffset();
  return new Date(date.getTime() - offset * 60000).toISOString().slice(0, 10);
}

const today = new Date();
endInput.value = dateInputValue(today);
const initialStart = new Date(today);
initialStart.setDate(today.getDate() - 7);
startInput.value = dateInputValue(initialStart);

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || `Request failed (${response.status})`);
  return data;
}

function setStatus(message, kind = '') {
  statusNode.textContent = message;
  statusNode.className = `status-message ${kind}`;
}

function setBusy(busy) {
  runButton.disabled = busy;
  runButton.textContent = busy ? 'Analyzing…' : 'Analyze hotspots';
}

async function loadStates() {
  try {
    const { states } = await requestJson('/api/states');
    stateSelect.replaceChildren(new Option('Select a state', ''));
    states.forEach(state => stateSelect.add(new Option(state, state)));
  } catch (error) {
    stateSelect.replaceChildren(new Option('Could not load states', ''));
    setStatus(error.message, 'error');
  }
}

async function loadDistricts(state) {
  districtSelect.disabled = true;
  districtSelect.replaceChildren(new Option('Loading districts…', ''));
  if (!state) {
    districtSelect.replaceChildren(new Option('Choose a state first', ''));
    return;
  }
  try {
    const { districts } = await requestJson(`/api/districts/${encodeURIComponent(state)}`);
    districtSelect.replaceChildren(new Option('Select a district', ''));
    districts.forEach(district => districtSelect.add(new Option(district, district)));
    districtSelect.disabled = false;
  } catch (error) {
    districtSelect.replaceChildren(new Option('Could not load districts', ''));
    setStatus(error.message, 'error');
  }
}

async function showBoundary(state, district) {
  if (boundaryLayer) map.removeLayer(boundaryLayer);
  if (!state || !district) return;
  if (['demo', 'uploaded'].includes(dataMode.value)) return;
  try {
    const result = await requestJson(`/api/district-boundary/${encodeURIComponent(state)}/${encodeURIComponent(district)}`);
    if (!result.boundary_geojson) return;
    boundaryLayer = L.geoJSON(result.boundary_geojson, {
      style: { color: '#67ddbd', weight: 2, fillOpacity: 0.03 }
    }).addTo(map);
    map.fitBounds(boundaryLayer.getBounds(), { padding: [22, 22], maxZoom: 10 });
  } catch (error) {
    setStatus(`Boundary unavailable: ${error.message}`, 'error');
  }
}

function renderHotspots(points) {
  hotspotLayer.clearLayers();
  let count = 0;
  (points || []).forEach(point => {
    const latitude = Number(point.latitude);
    const longitude = Number(point.longitude);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return;
    const marker = L.circleMarker([latitude, longitude], {
      radius: 6,
      color: '#ffd5bd',
      weight: 1,
      fillColor: '#ff6347',
      fillOpacity: 0.9
    });
    const frp = point.frp === undefined ? '—' : `${point.frp} MW`;
    const popup = document.createElement('div');
    popup.textContent = `${point.mode === 'demo' ? 'DEMO · simulated thermal point' : 'Thermal observation'} | FRP: ${frp} | Confidence: ${point.confidence ?? '—'} | Source: ${point.source ?? 'VIIRS'}`;
    marker.bindPopup(popup);
    marker.addTo(hotspotLayer);
    count += 1;
  });
  return count;
}

function updateProductDescription() {
  const details = productDescriptions[productSelect.value];
  document.getElementById('productDescription').textContent = details.text;
  document.getElementById('resolutionNote').textContent = details.resolution;
}

function getNominalFootprint(data) {
  return data.nominal_footprint_hectares ?? data.fire_area_hectares;
}

async function analyze(page = 1) {
  const state = stateSelect.value;
  const district = districtSelect.value;
  if (!state || !district) return setStatus('Select a state and district first.', 'error');
  if (!startInput.value || !endInput.value) return setStatus('Choose both dates.', 'error');
  if (endInput.value < startInput.value) return setStatus('End date must be on or after start date.', 'error');

  setBusy(true);
  setStatus('Querying Earth Engine satellite data…');
  mapStatus.textContent = 'Loading detections';
  try {
    const data = await requestJson(`/api/detect/${productSelect.value}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state, district, start_date: startInput.value, end_date: endInput.value,
        limit: 5000, page, page_size: Number(document.getElementById('pageSize').value) })
    });
    const points = data.hotspots_coordinates || [];
    dataMode.value = 'live'; updateSystemMode();
    document.getElementById('dataMessage').textContent = 'Live Earth Engine query result. The original composites lack per-hotspot acquisition dates, so this map result is not inserted into the dated prediction ledger. Import timestamped historical CSV for risk/outlook.';
    const count = renderHotspots(points);
    currentPage = page;
    const pagination = data.pagination || {};
    document.getElementById('hotspotCount').textContent = (pagination.total_sampled ?? data.fire_hotspots ?? data.fire_pixels ?? count).toLocaleString();
    const footprint = getNominalFootprint(data);
    document.getElementById('footprint').textContent = Number.isFinite(Number(footprint)) ? `${Number(footprint).toLocaleString()} ha` : 'Not provided';
    document.getElementById('maxFrp').textContent = `${data.max_frp ?? 0} MW`;
    document.getElementById('imageCount').textContent = data.viirs_images_used ?? '—';
    document.getElementById('previousPage').disabled = !pagination.has_previous;
    document.getElementById('nextPage').disabled = !pagination.has_next;
    document.getElementById('pageInfo').textContent = `Page ${pagination.page ?? page} of ${pagination.total_pages ?? 1}${pagination.truncated ? ' · sample capped' : ''}`;
    document.getElementById('results').classList.remove('hidden');
    exportButton.disabled = count === 0;
    const message = data.status || `Analysis complete · ${count} mapped hotspot${count === 1 ? '' : 's'}`;
    setStatus(`${message} · ${count} points on this page`, data.status && data.status.toLowerCase().includes('error') ? 'error' : 'success');
    mapStatus.textContent = `${count.toLocaleString()} hotspots · ${district}, ${state}`;
    if (data.district_boundary) {
      if (boundaryLayer) map.removeLayer(boundaryLayer);
      boundaryLayer = L.geoJSON(data.district_boundary, { style: { color: '#67ddbd', weight: 2, fillOpacity: 0.03 } }).addTo(map);
      map.fitBounds(boundaryLayer.getBounds(), { padding: [22, 22], maxZoom: 10 });
    }
  } catch (error) {
    setStatus(`${error.message} For long historical windows, create a batch export below.`, 'error');
    mapStatus.textContent = 'Analysis failed';
    exportButton.disabled = true;
  } finally {
    setBusy(false);
  }
}

stateSelect.addEventListener('change', async () => {
  await loadDistricts(stateSelect.value);
  if (districtSelect.value) showBoundary(stateSelect.value, districtSelect.value);
});
districtSelect.addEventListener('change', () => showBoundary(stateSelect.value, districtSelect.value));
productSelect.addEventListener('change', updateProductDescription);
runButton.addEventListener('click', analyze);
exportButton.addEventListener('click', () => { window.location.href = '/api/export-csv'; });
document.getElementById('previousPage').addEventListener('click', () => analyze(Math.max(1, currentPage - 1)));
document.getElementById('nextPage').addEventListener('click', () => analyze(currentPage + 1));

async function refreshExports() {
  try {
    const { jobs } = await requestJson('/api/exports');
    const list = document.getElementById('exportJobs'); list.replaceChildren();
    jobs.slice(0, 5).forEach(job => {
      const row = document.createElement('div'); row.className = 'job-row';
      const label = document.createElement('span'); label.textContent = `${job.id} · ${job.status}${job.error ? ` · ${job.error}` : ''}`; row.append(label);
      if (job.status === 'completed') { const link = document.createElement('a'); link.href = job.download_url; link.textContent = 'Download'; row.append(link); }
      if (job.status === 'queued') { const cancel = document.createElement('button'); cancel.className = 'button button-small'; cancel.textContent = 'Cancel'; cancel.onclick = async () => { try { await requestJson(`/api/exports/${job.id}/cancel`, { method: 'POST' }); await refreshExports(); } catch (e) { setStatus(e.message, 'error'); } }; row.append(cancel); }
      if (job.status === 'failed') { const retry = document.createElement('button'); retry.className = 'button button-small'; retry.textContent = 'Retry'; retry.onclick = async () => { try { const next = await requestJson(`/api/exports/${job.id}/retry`, { method: 'POST' }); setStatus(`Retry ${next.id} queued.`, 'success'); await refreshExports(); } catch (e) { setStatus(e.message, 'error'); } }; row.append(retry); }
      list.append(row);
    });
    const pending = jobs.some(j => ['queued', 'running'].includes(j.status));
    if (pending && !exportPoll) exportPoll = setInterval(refreshExports, 2500);
    if (!pending && exportPoll) { clearInterval(exportPoll); exportPoll = null; }
  } catch (e) { document.getElementById('exportJobs').textContent = `Export status unavailable: ${e.message}`; }
}

document.getElementById('batchExportButton').addEventListener('click', async () => {
  const button = document.getElementById('batchExportButton');
  const payload = { state: stateSelect.value, district: districtSelect.value, start_date: startInput.value,
    end_date: endInput.value, product: productSelect.value, limit: 5000,
    file_format: document.getElementById('exportFormat').value };
  if (!payload.state || !payload.district) return setStatus('Select a state and district before exporting.', 'error');
  button.disabled = true; button.textContent = 'Submitting…';
  try { const job = await requestJson('/api/exports/batch', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    setStatus(`Export ${job.id} ${job.deduplicated ? 'already exists' : 'queued'}.`, 'success'); await refreshExports();
  } catch (e) { setStatus(e.message, 'error'); }
  finally { button.disabled = false; button.textContent = 'Create historical export'; }
});

updateProductDescription();
loadStates();
refreshExports();

function selectedMode() { return dataMode.value; }
function updateSystemMode() {
  const labels = { demo: 'SIMULATED demo data', uploaded: 'Imported CSV data', live: 'Earth Engine live mode', all: 'Combined stored datasets' };
  document.getElementById('systemMode').textContent = labels[selectedMode()] || 'Select data mode';
}
async function loadLocalObservations() {
  const state = stateSelect.value, district = districtSelect.value;
  if (!state || !district) return setStatus('Choose a state and district to show local observations.', 'error');
  const q = new URLSearchParams({ state, district, mode: selectedMode(), limit: '5000' });
  if (startInput.value) q.set('start_date', startInput.value);
  if (endInput.value) q.set('end_date', endInput.value);
  try {
    const data = await requestJson(`/api/monitor/observations?${q}`);
    const count = renderHotspots(data.observations || []);
    document.getElementById('results').classList.remove('hidden');
    document.getElementById('hotspotCount').textContent = data.count.toLocaleString();
    document.getElementById('footprint').textContent = 'Not estimated from demo/CSV rows';
    document.getElementById('maxFrp').textContent = `${Math.max(0, ...data.observations.map(x => Number(x.frp) || 0)).toFixed(2)} MW`;
    document.getElementById('imageCount').textContent = 'Local records';
    exportButton.disabled = true;
    mapStatus.textContent = `${count} ${selectedMode()} observations · ${district}, ${state}`;
    if (count) map.fitBounds(hotspotLayer.getBounds(), { padding: [30, 30], maxZoom: 11 });
    setStatus(`Loaded ${count} ${selectedMode()} observation(s)${data.warning ? ' · simulated examples, not live detections' : ''}.`, 'success');
  } catch (error) { setStatus(`Could not load local observations: ${error.message}`, 'error'); }
}

function addRiskParagraph(parent, text, className = '') {
  const item = document.createElement('p'); item.textContent = text; if (className) item.className = className; parent.append(item);
}

async function assessDistrict() {
  const state = stateSelect.value, district = districtSelect.value;
  if (!state || !district) return setStatus('Choose a district before risk assessment.', 'error');
  const panel = document.getElementById('riskPanel'); panel.textContent = 'Calculating from stored observations…';
  const q = new URLSearchParams({ state, district, mode: selectedMode() });
  try {
    const risk = await requestJson(`/api/risk?${q}`);
    const prediction = await requestJson(`/api/prediction/next-day?${q}`);
    panel.replaceChildren();
    addRiskParagraph(panel, risk.score === null ? `${risk.category} · ${risk.evidence_level}` : `${risk.category} risk · ${risk.score}/100 · ${risk.evidence_level}`, 'risk-title');
    addRiskParagraph(panel, `As of ${risk.as_of || 'no dated data'} · ${risk.observations_7d ?? 0} observations in 7 days; ${risk.observations_30d ?? 0} in 30 days.`);
    (risk.factors || []).forEach(f => addRiskParagraph(panel, `${f.name}: +${f.points} — ${f.evidence}`));
    addRiskParagraph(panel, `Action: ${risk.recommendation} Urgency: ${risk.urgency}; follow up ${risk.follow_up}.`, 'risk-action');
    if (prediction.prediction) {
      addRiskParagraph(panel, `Next-day outlook for ${prediction.target_date}: ${prediction.prediction} (rule score ${prediction.rule_score}/100; ${prediction.evidence_level}).`, 'risk-title');
      addRiskParagraph(panel, prediction.limitations, 'hint');
    } else addRiskParagraph(panel, prediction.message || 'No next-day outlook available.');
    await refreshAlerts();
    setStatus('District risk and outlook updated from the selected dataset.', 'success');
  } catch (error) { panel.textContent = `Assessment unavailable: ${error.message}`; }
}

async function refreshAlerts() {
  const panel = document.getElementById('alertsPanel');
  const q = new URLSearchParams({ mode: selectedMode() });
  if (stateSelect.value) q.set('state', stateSelect.value);
  if (districtSelect.value) q.set('district', districtSelect.value);
  try {
    const { alerts } = await requestJson(`/api/alerts?${q}`);
    panel.replaceChildren();
    if (!alerts.length) { panel.textContent = 'No alerts for this area and dataset. Assess the district after loading observations.'; return; }
    alerts.forEach(alert => {
      const card = document.createElement('article'); card.className = 'alert-card';
      const heading = document.createElement('strong'); heading.textContent = `${alert.is_test ? 'TEST · ' : ''}${alert.severity} · ${alert.status} · ${alert.risk_score}/100`; card.append(heading);
      addRiskParagraph(card, `${alert.district}, ${alert.state} · ${new Date(alert.created_at).toLocaleString()}`);
      addRiskParagraph(card, alert.reason);
      addRiskParagraph(card, `Recommended: ${alert.recommendation}`);
      if (alert.status === 'Open') for (const action of ['acknowledge', 'dismiss']) {
        const button = document.createElement('button'); button.className = 'button button-small'; button.textContent = action === 'acknowledge' ? 'Acknowledge' : 'Dismiss';
        button.onclick = async () => { try { await requestJson(`/api/alerts/${alert.id}/${action}`, { method: 'POST' }); await refreshAlerts(); } catch (e) { setStatus(e.message, 'error'); } };
        card.append(button);
      }
      panel.append(card);
    });
  } catch (error) { panel.textContent = `Alerts unavailable: ${error.message}`; }
}

document.getElementById('loadDemoButton').addEventListener('click', async () => {
  const button = document.getElementById('loadDemoButton'); button.disabled = true;
  try {
    const data = await requestJson('/api/demo/load', { method: 'POST' });
    dataMode.value = 'demo'; updateSystemMode(); await loadDistricts('Punjab'); stateSelect.value = 'Punjab'; districtSelect.value = 'Amritsar';
    startInput.value = data.start_date; endInput.value = data.end_date;
    document.getElementById('dataMessage').textContent = `${data.label} · ${data.inserted} rows loaded; ${data.alerts_created.length} risk alert(s) generated.`;
    await loadLocalObservations(); await assessDistrict(); await refreshAlerts();
  } catch (error) { setStatus(`Demo data could not load: ${error.message}`, 'error'); }
  finally { button.disabled = false; }
});

document.getElementById('uploadCsvButton').addEventListener('click', async () => {
  const file = document.getElementById('csvFile').files[0];
  if (!file) return setStatus('Choose a CSV file first.', 'error');
  if (!stateSelect.value || !districtSelect.value) return setStatus('Select the state and district the CSV covers.', 'error');
  const q = new URLSearchParams({ default_state: stateSelect.value, district: districtSelect.value });
  try {
    const data = await requestJson(`/api/observations/upload?${q}`, { method: 'POST', headers: { 'Content-Type': 'text/csv' }, body: await file.text() });
    dataMode.value = 'uploaded'; updateSystemMode();
    if (data.start_date) startInput.value = data.start_date;
    if (data.end_date) endInput.value = data.end_date;
    document.getElementById('dataMessage').textContent = `Imported ${data.inserted} observation(s), ${data.duplicates} duplicate(s), ${data.rejected} rejected row(s). Stored locally.`;
    await loadLocalObservations(); await assessDistrict(); await refreshAlerts();
  } catch (error) { setStatus(`CSV import failed: ${error.message}`, 'error'); }
});

document.getElementById('showLocalButton').addEventListener('click', loadLocalObservations);
document.getElementById('assessButton').addEventListener('click', assessDistrict);
document.getElementById('testAlertButton').addEventListener('click', async () => {
  if (!stateSelect.value || !districtSelect.value) return setStatus('Choose a district before creating a test alert.', 'error');
  try { await requestJson(`/api/alerts/test?${new URLSearchParams({ state: stateSelect.value, district: districtSelect.value })}`, { method: 'POST' }); await refreshAlerts(); setStatus('Simulated test alert created. Acknowledge or dismiss it below.', 'success'); }
  catch (error) { setStatus(error.message, 'error'); }
});
dataMode.addEventListener('change', () => { updateSystemMode(); loadLocalObservations(); refreshAlerts(); });
stateSelect.addEventListener('change', () => { refreshAlerts(); });
districtSelect.addEventListener('change', () => { refreshAlerts(); });
startInput.addEventListener('change', () => { if (['demo','uploaded','all'].includes(selectedMode())) loadLocalObservations(); });
endInput.addEventListener('change', () => { if (['demo','uploaded','all'].includes(selectedMode())) loadLocalObservations(); });
updateSystemMode();
