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
let hotspotLayer = L.layerGroup().addTo(map);
let boundaryLayer = null;

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
    marker.bindPopup(`<strong>Satellite hotspot</strong><br>FRP: ${frp}<br>Confidence: ${point.confidence ?? '—'}<br>Source: ${point.source ?? 'VIIRS'}`);
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

async function analyze() {
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
      body: JSON.stringify({ state, district, start_date: startInput.value, end_date: endInput.value })
    });
    const points = data.hotspots_coordinates || [];
    const count = renderHotspots(points);
    document.getElementById('hotspotCount').textContent = (data.fire_hotspots ?? data.fire_pixels ?? count).toLocaleString();
    const footprint = getNominalFootprint(data);
    document.getElementById('footprint').textContent = Number.isFinite(Number(footprint)) ? `${Number(footprint).toLocaleString()} ha` : 'Not provided';
    document.getElementById('maxFrp').textContent = `${data.max_frp ?? 0} MW`;
    document.getElementById('imageCount').textContent = data.viirs_images_used ?? '—';
    document.getElementById('results').classList.remove('hidden');
    exportButton.disabled = count === 0;
    const message = data.status || `Analysis complete · ${count} mapped hotspot${count === 1 ? '' : 's'}`;
    setStatus(message, data.status && data.status.toLowerCase().includes('error') ? 'error' : 'success');
    mapStatus.textContent = `${count.toLocaleString()} hotspots · ${district}, ${state}`;
    if (data.district_boundary) {
      if (boundaryLayer) map.removeLayer(boundaryLayer);
      boundaryLayer = L.geoJSON(data.district_boundary, { style: { color: '#67ddbd', weight: 2, fillOpacity: 0.03 } }).addTo(map);
      map.fitBounds(boundaryLayer.getBounds(), { padding: [22, 22], maxZoom: 10 });
    }
  } catch (error) {
    setStatus(error.message, 'error');
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

updateProductDescription();
loadStates();
