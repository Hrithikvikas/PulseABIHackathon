const state = {
  rows: [],
  filtered: [],
  selectedId: null,
};

const decisionLabels = {
  auto_accept: "Auto accept",
  flag_for_review: "Review",
  reject: "Reject",
};

const decisionOrder = {
  auto_accept: 0,
  flag_for_review: 1,
  reject: 2,
};

const els = {
  search: document.querySelector("#searchInput"),
  facility: document.querySelector("#facilityFilter"),
  decisions: [...document.querySelectorAll('input[name="decision"]')],
  rows: document.querySelector("#patientRows"),
  visibleCount: document.querySelector("#visibleCount"),
  funnelTotal: document.querySelector("#funnelTotal"),
  funnelAccept: document.querySelector("#funnelAccept"),
  funnelReview: document.querySelector("#funnelReview"),
  funnelReject: document.querySelector("#funnelReject"),
  intro: document.querySelector("#introScreen"),
  introCanvas: document.querySelector("#introCanvas"),
  introAccept: document.querySelector("#introAccept"),
  introReview: document.querySelector("#introReview"),
  introReject: document.querySelector("#introReject"),
  skipIntro: document.querySelector("#skipIntro"),
  modal: document.querySelector("#patientModal"),
  closeModal: document.querySelector("#closeModal"),
  reset: document.querySelector("#resetFilters"),
  exportCsv: document.querySelector("#exportCsv"),
  detailId: document.querySelector("#detailId"),
  detailName: document.querySelector("#detailName"),
  detailBadge: document.querySelector("#detailBadge"),
  detailReason: document.querySelector("#detailReason"),
  detailHistory: document.querySelector("#detailHistory"),
  detailIcd: document.querySelector("#detailIcd"),
  detailType: document.querySelector("#detailType"),
  detailStage: document.querySelector("#detailStage"),
  detailLocation: document.querySelector("#detailLocation"),
  detailMeasures: document.querySelector("#detailMeasures"),
  detailDrainage: document.querySelector("#detailDrainage"),
  detailSource: document.querySelector("#detailSource"),
  detailNotes: document.querySelector("#detailNotes"),
  bodyView: document.querySelector("#bodyView"),
  bodyFallback: document.querySelector("#bodyFallback"),
  bodyStatus: document.querySelector("#bodyStatus"),
  markerTooltip: document.querySelector("#markerTooltip"),
  measureLength: document.querySelector("#measureLength"),
  measureWidth: document.querySelector("#measureWidth"),
  measureDepth: document.querySelector("#measureDepth"),
  measureDrainage: document.querySelector("#measureDrainage"),
};

const bodyState = {
  scene: null,
  camera: null,
  renderer: null,
  bodyGroup: null,
  markerGroup: null,
  marker: null,
  markerData: null,
  raycaster: null,
  pointer: null,
  dragging: false,
  lastX: 0,
  lastY: 0,
  manualUntil: 0,
};

const decisionColors = {
  auto_accept: 0x31d17d,
  flag_for_review: 0xffbd45,
  reject: 0x9aa4aa,
};

const regionCoordinates = {
  sacrum: { x: 0, y: 0.75, z: -0.38 },
  coccyx: { x: 0, y: 0.48, z: -0.4 },
  heel: { x: 0.2, y: -2.42, z: -0.05 },
  ischium: { x: 0.28, y: 0.48, z: -0.34 },
  trochanter: { x: 0.44, y: 0.72, z: -0.02 },
  elbow: { x: 0.98, y: 0.88, z: 0.03 },
  "lower leg": { x: 0.22, y: -1.55, z: 0.14 },
  ankle: { x: 0.23, y: -2.24, z: 0.12 },
  foot: { x: 0.22, y: -2.46, z: 0.34 },
  hip: { x: 0.38, y: 0.93, z: 0.12 },
  buttock: { x: 0.32, y: 0.62, z: -0.35 },
  shoulder: { x: 0.58, y: 1.72, z: 0.12 },
};

const locationMap = [
  { keys: ["sacrum", "sacral"], region: "sacrum", side: "center" },
  { keys: ["coccyx"], region: "coccyx", side: "center" },
  { keys: ["heel"], region: "heel" },
  { keys: ["ischium", "ischial"], region: "ischium" },
  { keys: ["trochanter"], region: "trochanter" },
  { keys: ["elbow"], region: "elbow" },
  { keys: ["lower leg", "shin", "lower extremity"], region: "lower leg" },
  { keys: ["ankle", "malleolus"], region: "ankle" },
  { keys: ["foot", "plantar", "mtp"], region: "foot" },
  { keys: ["hip"], region: "hip" },
  { keys: ["buttock"], region: "buttock" },
  { keys: ["shoulder"], region: "shoulder" },
];

async function init() {
  const response = await fetch("./results.json", { cache: "no-store" });
  if (!response.ok) {
    throw new Error("Unable to load results.json");
  }

  state.rows = await response.json();
  state.rows.sort((a, b) => {
    const byDecision = decisionOrder[a.decision] - decisionOrder[b.decision];
    if (byDecision !== 0) return byDecision;
    return a.patient_id.localeCompare(b.patient_id);
  });
  state.selectedId = state.rows[0]?.patient_id ?? null;

  populateFacilities();
  bindEvents();
  applyFilters();
  startIntroAnimation();
}

function populateFacilities() {
  const facilities = [...new Set(state.rows.map((row) => row.facility_id))].sort((a, b) => a - b);
  for (const facility of facilities) {
    const option = document.createElement("option");
    option.value = String(facility);
    option.textContent = `Facility ${facility}`;
    els.facility.append(option);
  }
}

function bindEvents() {
  els.search.addEventListener("input", applyFilters);
  els.facility.addEventListener("change", applyFilters);
  els.decisions.forEach((input) => input.addEventListener("change", applyFilters));
  els.reset.addEventListener("click", resetFilters);
  els.exportCsv.addEventListener("click", exportCsv);
  els.skipIntro.addEventListener("click", finishIntro);
  els.closeModal.addEventListener("click", closePatientModal);
  els.modal.addEventListener("click", (event) => {
    if (event.target === els.modal) closePatientModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !els.modal.hidden) closePatientModal();
  });
}

function applyFilters() {
  const query = els.search.value.trim().toLowerCase();
  const facility = els.facility.value;
  const allowedDecisions = new Set(els.decisions.filter((input) => input.checked).map((input) => input.value));

  state.filtered = state.rows.filter((row) => {
    if (!allowedDecisions.has(row.decision)) return false;
    if (facility !== "all" && String(row.facility_id) !== facility) return false;
    if (!query) return true;

    const haystack = [
      row.patient_id,
      row.name,
      row.reason,
      row.history_summary,
      row.wound_type,
      row.location,
      row.active_wound_icd10_codes?.join(" "),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();

    return haystack.includes(query);
  });

  if (!state.filtered.some((row) => row.patient_id === state.selectedId)) {
    state.selectedId = state.filtered[0]?.patient_id ?? null;
  }

  renderFunnel();
  renderRows();
}

function renderFunnel() {
  const counts = countByDecision(state.rows);
  els.funnelTotal.textContent = state.rows.length;
  els.funnelAccept.textContent = counts.auto_accept ?? 0;
  els.funnelReview.textContent = counts.flag_for_review ?? 0;
  els.funnelReject.textContent = counts.reject ?? 0;
  els.visibleCount.textContent = `${state.filtered.length} shown`;
  setVialFill(".bucket-accept .vial-fill", counts.auto_accept ?? 0, state.rows.length);
  setVialFill(".bucket-review .vial-fill", counts.flag_for_review ?? 0, state.rows.length);
  setVialFill(".bucket-reject .vial-fill", counts.reject ?? 0, state.rows.length);
}

function setVialFill(selector, value, total) {
  const fill = document.querySelector(selector);
  if (!fill) return;
  fill.style.height = `${Math.max(18, Math.round((value / Math.max(total, 1)) * 100))}%`;
}

function renderRows() {
  els.rows.replaceChildren();
  const fragment = document.createDocumentFragment();

  for (const row of state.filtered) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = `patient-card ${row.decision}`;
    card.dataset.patientId = row.patient_id;
    card.innerHTML = `
      <div class="patient-main">
        <strong>${escapeHtml(row.name || "Unknown")}</strong>
        <span>${escapeHtml(row.patient_id)} · ${escapeHtml(row.payer || "-")}</span>
      </div>
      <div class="patient-facility">
        <span>Facility</span>
        <strong>${escapeHtml(String(row.facility_id))}</strong>
      </div>
      <div class="patient-status">
        <span>Current wound status</span>
        <strong>${escapeHtml(currentWoundStatus(row))}</strong>
      </div>
      <div class="patient-reason">
        <span>Routing</span>
        <strong>${escapeHtml(row.reason || "-")}</strong>
      </div>
      ${badge(row.decision)}
      <span class="patient-action" aria-hidden="true">›</span>
    `;
    card.addEventListener("click", () => openPatientModal(row.patient_id));
    fragment.append(card);
  }

  els.rows.append(fragment);
}

function renderDetail() {
  const row = state.rows.find((item) => item.patient_id === state.selectedId);
  if (!row) {
    els.detailId.textContent = "Select a patient";
    els.detailName.textContent = "No patient selected";
    els.detailBadge.className = "badge neutral";
    els.detailBadge.textContent = "-";
    return;
  }

  els.detailId.textContent = row.patient_id;
  els.detailName.textContent = row.name || "Unknown";
  els.detailBadge.className = `badge ${row.decision}`;
  els.detailBadge.textContent = decisionLabels[row.decision];
  els.detailReason.textContent = row.reason || "-";
  els.detailHistory.textContent = row.history_summary || "-";
  els.detailIcd.textContent = row.active_wound_icd10_codes?.join(", ") || "-";
  els.detailType.textContent = labelize(row.wound_type);
  els.detailStage.textContent = row.stage || "-";
  els.detailLocation.textContent = row.location || "-";
  els.detailMeasures.textContent = formatMeasures(row);
  els.detailDrainage.textContent = row.drainage || "-";
  els.detailSource.textContent = labelize(row.source_format || row.source);
  renderMeasurementVisual(row);
  updateBodyMarker(row);

  els.detailNotes.replaceChildren();
  const notes = row.note_mappings || [];
  if (!notes.length) {
    const li = document.createElement("li");
    li.textContent = "No mapped notes";
    els.detailNotes.append(li);
  } else {
    for (const note of notes) {
      const li = document.createElement("li");
      li.textContent = `Note ${note.note_id}: ${note.mapped_icd10_code || "unmapped"} · ${note.reasons?.join(", ") || "no reason"}`;
      els.detailNotes.append(li);
    }
  }

  updateBodyMarker(row);
}

function openPatientModal(patientId) {
  state.selectedId = patientId;
  els.modal.hidden = false;
  renderDetail();
  if (!bodyState.renderer) {
    initBodyViewer();
  } else {
    resizeBodyViewer();
  }
  updateBodyMarker(state.rows.find((item) => item.patient_id === state.selectedId));
}

function closePatientModal() {
  els.modal.hidden = true;
  els.markerTooltip.hidden = true;
}

function resetFilters() {
  els.search.value = "";
  els.facility.value = "all";
  els.decisions.forEach((input) => {
    input.checked = true;
  });
  applyFilters();
}

function exportCsv() {
  const columns = [
    "patient_id",
    "name",
    "facility_id",
    "payer",
    "decision",
    "reason",
    "history_summary",
    "wound_type",
    "stage",
    "location",
    "length_cm",
    "width_cm",
    "depth_cm",
    "drainage",
    "source",
  ];
  const lines = [
    columns.join(","),
    ...state.filtered.map((row) =>
      columns.map((column) => csvCell(row[column])).join(",")
    ),
  ];
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "abi-wound-triage.csv";
  link.click();
  URL.revokeObjectURL(url);
}

function countByDecision(rows) {
  return rows.reduce((acc, row) => {
    acc[row.decision] = (acc[row.decision] || 0) + 1;
    return acc;
  }, {});
}

function badge(decision) {
  return `<span class="badge ${decision}">${escapeHtml(decisionLabels[decision] || decision)}</span>`;
}

function formatWound(row) {
  const parts = [labelize(row.wound_type), row.location].filter(Boolean);
  return parts.length ? parts.join(" · ") : "-";
}

function currentWoundStatus(row) {
  const type = labelize(row.wound_type);
  const location = row.location || "location not documented";
  const drainage = row.drainage ? `${row.drainage} drainage` : "drainage not documented";
  const stage = row.stage ? `stage ${row.stage}` : null;
  return [type, stage, location, drainage].filter(Boolean).join(" · ");
}

function formatMeasures(row) {
  const length = row.length_cm ?? "-";
  const width = row.width_cm ?? "-";
  const depth = row.depth_cm ?? "-";
  return `${length} × ${width} × ${depth} cm`;
}

function labelize(value) {
  if (!value) return "-";
  return String(value)
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function csvCell(value) {
  const text = value == null ? "" : String(value);
  return `"${text.replaceAll('"', '""')}"`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function initBodyViewer() {
  if (!window.THREE) {
    els.bodyFallback.textContent = "3D body unavailable";
    return;
  }

  const rect = els.bodyView.getBoundingClientRect();
  bodyState.scene = new THREE.Scene();
  bodyState.camera = new THREE.PerspectiveCamera(38, rect.width / rect.height, 0.1, 100);
  bodyState.camera.position.set(0, 0.08, 8.6);
  bodyState.camera.lookAt(0, 0.02, 0);

  bodyState.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  bodyState.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  bodyState.renderer.setSize(rect.width, rect.height);
  bodyState.renderer.setClearColor(0x000000, 0);
  els.bodyView.append(bodyState.renderer.domElement);
  els.bodyFallback.hidden = true;

  bodyState.raycaster = new THREE.Raycaster();
  bodyState.pointer = new THREE.Vector2();
  bodyState.bodyGroup = new THREE.Group();
  bodyState.markerGroup = new THREE.Group();
  bodyState.scene.add(bodyState.bodyGroup);
  bodyState.scene.add(bodyState.markerGroup);

  buildBody(bodyState.bodyGroup);

  const ambient = new THREE.AmbientLight(0x8fb8c9, 1.8);
  const directional = new THREE.DirectionalLight(0xffffff, 1.4);
  directional.position.set(2.5, 4, 5);
  bodyState.scene.add(ambient, directional);

  els.bodyView.addEventListener("pointerdown", onBodyPointerDown);
  els.bodyView.addEventListener("pointermove", onBodyPointerMove);
  els.bodyView.addEventListener("pointerup", onBodyPointerUp);
  els.bodyView.addEventListener("pointerleave", onBodyPointerLeave);
  els.bodyView.addEventListener("click", updateMarkerHover);
  window.addEventListener("resize", resizeBodyViewer);

  animateBody();
}

function buildBody(group) {
  const surface = new THREE.MeshStandardMaterial({
    color: 0x14313d,
    transparent: true,
    opacity: 0.42,
    roughness: 0.5,
    metalness: 0.1,
  });
  const wire = new THREE.MeshBasicMaterial({
    color: 0x74d8ff,
    transparent: true,
    opacity: 0.42,
    wireframe: true,
  });

  addBodyPart(group, new THREE.SphereGeometry(0.34, 32, 20), [0, 2.34, 0], [0.92, 1.1, 0.88], surface, wire);
  addBodyPart(group, new THREE.CylinderGeometry(0.16, 0.2, 0.26, 18), [0, 1.98, 0], [1, 1, 0.9], surface, wire);
  addBodyPart(group, new THREE.BoxGeometry(1.35, 0.34, 0.5, 8, 3, 3), [0, 1.75, 0], [1, 1, 1], surface, wire);
  addBodyPart(group, new THREE.SphereGeometry(0.46, 28, 18), [0, 1.34, 0], [1.02, 1.12, 0.66], surface, wire);
  addBodyPart(group, new THREE.SphereGeometry(0.38, 26, 16), [0, 0.78, 0], [0.86, 0.9, 0.58], surface, wire);
  addBodyPart(group, new THREE.SphereGeometry(0.42, 26, 16), [0, 0.3, -0.02], [1.18, 0.68, 0.72], surface, wire);

  addJoint(group, [-0.7, 1.72, 0], 0.15, surface, wire);
  addJoint(group, [0.7, 1.72, 0], 0.15, surface, wire);
  addJoint(group, [-1.02, 0.76, 0.02], 0.13, surface, wire);
  addJoint(group, [1.02, 0.76, 0.02], 0.13, surface, wire);
  addLimb(group, [-0.72, 1.6, 0], [-1.02, 0.82, 0.03], 0.13, surface, wire);
  addLimb(group, [0.72, 1.6, 0], [1.02, 0.82, 0.03], 0.13, surface, wire);
  addLimb(group, [-1.02, 0.7, 0.03], [-1.12, -0.08, 0.08], 0.115, surface, wire);
  addLimb(group, [1.02, 0.7, 0.03], [1.12, -0.08, 0.08], 0.115, surface, wire);
  addBodyPart(group, new THREE.SphereGeometry(0.13, 18, 12), [-1.13, -0.22, 0.09], [0.9, 1.08, 0.72], surface, wire);
  addBodyPart(group, new THREE.SphereGeometry(0.13, 18, 12), [1.13, -0.22, 0.09], [0.9, 1.08, 0.72], surface, wire);

  addJoint(group, [-0.27, -1.05, 0.04], 0.15, surface, wire);
  addJoint(group, [0.27, -1.05, 0.04], 0.15, surface, wire);
  addLimb(group, [-0.34, 0.02, 0], [-0.28, -1.05, 0.04], 0.16, surface, wire);
  addLimb(group, [0.34, 0.02, 0], [0.28, -1.05, 0.04], 0.16, surface, wire);
  addLimb(group, [-0.28, -1.08, 0.04], [-0.28, -2.23, 0.08], 0.125, surface, wire);
  addLimb(group, [0.28, -1.08, 0.04], [0.28, -2.23, 0.08], 0.125, surface, wire);

  addBodyPart(group, new THREE.BoxGeometry(0.34, 0.16, 0.7, 5, 2, 5), [-0.28, -2.49, 0.22], [1, 1, 1], surface, wire);
  addBodyPart(group, new THREE.BoxGeometry(0.34, 0.16, 0.7, 5, 2, 5), [0.28, -2.49, 0.22], [1, 1, 1], surface, wire);
}

function addBodyPart(group, geometry, position, scale, surface, wire) {
  const mesh = new THREE.Mesh(geometry, surface);
  mesh.position.set(...position);
  mesh.scale.set(...scale);
  const wireMesh = new THREE.Mesh(geometry.clone(), wire);
  wireMesh.position.copy(mesh.position);
  wireMesh.rotation.copy(mesh.rotation);
  wireMesh.scale.copy(mesh.scale);
  group.add(mesh, wireMesh);
}

function addLimb(group, start, end, radius, surface, wire) {
  const startVec = new THREE.Vector3(...start);
  const endVec = new THREE.Vector3(...end);
  const mid = startVec.clone().add(endVec).multiplyScalar(0.5);
  const direction = endVec.clone().sub(startVec);
  const geometry = new THREE.CylinderGeometry(radius, radius * 0.92, direction.length(), 20);
  const mesh = new THREE.Mesh(geometry, surface);
  mesh.position.copy(mid);
  mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.clone().normalize());
  const wireMesh = new THREE.Mesh(geometry.clone(), wire);
  wireMesh.position.copy(mesh.position);
  wireMesh.quaternion.copy(mesh.quaternion);
  group.add(mesh, wireMesh);
}

function addJoint(group, position, radius, surface, wire) {
  addBodyPart(group, new THREE.SphereGeometry(radius, 18, 12), position, [1, 1, 1], surface, wire);
}

function updateBodyMarker(row) {
  if (!bodyState.markerGroup || !window.THREE) return;
  bodyState.markerGroup.clear();
  bodyState.marker = null;
  bodyState.markerData = null;
  els.markerTooltip.hidden = true;

  const placement = resolvePlacement(row.location);
  if (!placement) {
    els.bodyStatus.textContent = row.location ? "Location not mapped" : "Location not documented";
    return;
  }

  const color = decisionColors[row.decision] ?? decisionColors.reject;
  const size = markerSize(row);
  const markerMaterial = new THREE.MeshStandardMaterial({
    color,
    emissive: color,
    emissiveIntensity: 1.8,
    roughness: 0.25,
  });
  const marker = new THREE.Mesh(new THREE.SphereGeometry(size, 24, 16), markerMaterial);
  marker.position.set(placement.x, placement.y, placement.z);
  marker.userData.isWoundMarker = true;

  const halo = new THREE.Mesh(
    new THREE.SphereGeometry(size * 1.95, 24, 16),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.18, depthWrite: false })
  );
  halo.position.copy(marker.position);
  marker.userData.halo = halo;

  bodyState.markerGroup.add(halo, marker);
  bodyState.marker = marker;
  bodyState.markerData = row;

  if (hasMissingDimension(row)) {
    const badge = new THREE.Sprite(new THREE.SpriteMaterial({ map: makeQuestionTexture(), transparent: true }));
    badge.position.set(placement.x + 0.18, placement.y + 0.16, placement.z + 0.08);
    badge.scale.set(0.22, 0.22, 0.22);
    bodyState.markerGroup.add(badge);
  }

  els.bodyStatus.textContent = `${labelize(row.wound_type)} · ${row.location}`;
}

function resolvePlacement(location) {
  if (!location) return null;
  const normalized = location.toLowerCase();
  const match = locationMap.find((item) => item.keys.some((key) => normalized.includes(key)));
  if (!match) return null;
  const base = regionCoordinates[match.region];
  const side = normalized.includes("left") ? "left" : normalized.includes("right") ? "right" : match.side || "center";
  const xSign = side === "left" ? -1 : side === "right" ? 1 : 0;
  return {
    x: xSign ? Math.abs(base.x) * xSign : 0,
    y: base.y,
    z: base.z,
    region: match.region,
    side,
  };
}

function markerSize(row) {
  const area = Number(row.length_cm) * Number(row.width_cm);
  if (!Number.isFinite(area) || area <= 0) return 0.095;
  return Math.max(0.08, Math.min(0.18, 0.075 + Math.sqrt(area) * 0.018));
}

function hasMissingDimension(row) {
  return row.length_cm == null || row.width_cm == null || row.depth_cm == null;
}

function makeQuestionTexture() {
  const canvas = document.createElement("canvas");
  canvas.width = 96;
  canvas.height = 96;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#ffbd45";
  ctx.beginPath();
  ctx.arc(48, 48, 39, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = "#172129";
  ctx.font = "700 58px system-ui";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText("?", 48, 51);
  return new THREE.CanvasTexture(canvas);
}

function onBodyPointerDown(event) {
  if (!bodyState.bodyGroup) return;
  bodyState.dragging = true;
  bodyState.lastX = event.clientX;
  bodyState.lastY = event.clientY;
  bodyState.manualUntil = performance.now() + 1800;
  els.bodyView.setPointerCapture(event.pointerId);
}

function onBodyPointerMove(event) {
  updateMarkerHover(event);
  if (!bodyState.dragging || !bodyState.bodyGroup) return;
  const deltaX = event.clientX - bodyState.lastX;
  const deltaY = event.clientY - bodyState.lastY;
  bodyState.lastX = event.clientX;
  bodyState.lastY = event.clientY;
  bodyState.bodyGroup.rotation.y += deltaX * 0.012;
  bodyState.bodyGroup.rotation.x = clamp(bodyState.bodyGroup.rotation.x + deltaY * 0.01, -1.05, 1.05);
  syncBodyRotation();
  bodyState.manualUntil = performance.now() + 1800;
}

function onBodyPointerUp(event) {
  bodyState.dragging = false;
  try {
    els.bodyView.releasePointerCapture(event.pointerId);
  } catch (_) {
    // Pointer may already be released by the browser.
  }
}

function onBodyPointerLeave() {
  bodyState.dragging = false;
  els.markerTooltip.hidden = true;
}

function updateMarkerHover(event) {
  if (!bodyState.marker || !bodyState.raycaster) return;
  const rect = els.bodyView.getBoundingClientRect();
  bodyState.pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  bodyState.pointer.y = -(((event.clientY - rect.top) / rect.height) * 2 - 1);
  bodyState.raycaster.setFromCamera(bodyState.pointer, bodyState.camera);
  const hits = bodyState.raycaster.intersectObject(bodyState.marker);
  if (!hits.length) {
    els.markerTooltip.hidden = true;
    return;
  }
  const row = bodyState.markerData;
  els.markerTooltip.hidden = false;
  els.markerTooltip.innerHTML = `
    <strong>${escapeHtml(labelize(row.wound_type))}</strong><br />
    ${escapeHtml(row.stage ? `Stage ${row.stage}` : "Stage -")}<br />
    ${escapeHtml(formatMeasures(row))}<br />
    ${escapeHtml(row.drainage || "-")} drainage<br />
    ${escapeHtml(row.reason || "")}
  `;
}

function animateBody() {
  requestAnimationFrame(animateBody);
  if (!bodyState.renderer || !bodyState.scene || !bodyState.camera) return;
  const now = performance.now();
  if (bodyState.bodyGroup && now > bodyState.manualUntil) {
    bodyState.bodyGroup.rotation.y += 0.004;
    syncBodyRotation();
  }
  if (bodyState.marker?.userData.halo) {
    const pulse = 1 + Math.sin(now * 0.006) * 0.16;
    bodyState.marker.userData.halo.scale.setScalar(pulse);
  }
  bodyState.renderer.render(bodyState.scene, bodyState.camera);
}

function syncBodyRotation() {
  if (!bodyState.bodyGroup || !bodyState.markerGroup) return;
  bodyState.markerGroup.rotation.x = bodyState.bodyGroup.rotation.x;
  bodyState.markerGroup.rotation.y = bodyState.bodyGroup.rotation.y;
  bodyState.markerGroup.rotation.z = bodyState.bodyGroup.rotation.z;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function resizeBodyViewer() {
  if (!bodyState.renderer || !bodyState.camera) return;
  const rect = els.bodyView.getBoundingClientRect();
  bodyState.camera.aspect = rect.width / rect.height;
  bodyState.camera.updateProjectionMatrix();
  bodyState.renderer.setSize(rect.width, rect.height);
}

function renderMeasurementVisual(row) {
  setMeasure(els.measureLength, row.length_cm, "cm");
  setMeasure(els.measureWidth, row.width_cm, "cm");
  setMeasure(els.measureDepth, row.depth_cm, "cm");
  setMeasure(els.measureDrainage, row.drainage, "");
}

function setMeasure(element, value, suffix) {
  const missing = value == null || value === "";
  element.textContent = missing ? "?" : `${value}${suffix ? ` ${suffix}` : ""}`;
  element.parentElement.classList.toggle("missing", missing);
}

function startIntroAnimation() {
  if (!els.introCanvas || !els.intro || !state.rows.length) {
    finishIntro();
    return;
  }

  document.body.classList.add("intro-active");
  els.intro.hidden = false;
  els.intro.classList.remove("complete");
  els.introAccept.textContent = "0";
  els.introReview.textContent = "0";
  els.introReject.textContent = "0";

  const canvas = els.introCanvas;
  const ctx = canvas.getContext("2d");
  const counts = countByDecision(state.rows);
  const particles = makeIntroParticles(canvas, state.rows);
  let start = 0;
  let finished = false;

  function frame(now) {
    if (finished) return;
    if (!start) start = now;
    const elapsed = now - start;
    const progress = Math.min(elapsed / 6500, 1);
    drawIntro(ctx, canvas, particles, progress);
    updateIntroCounts(counts, progress);
    if (progress < 1) {
      requestAnimationFrame(frame);
    } else {
      finished = true;
      setTimeout(finishIntro, 1000);
    }
  }

  resizeIntroCanvas();
  drawIntro(ctx, canvas, particles, 0);
  window.addEventListener("resize", resizeIntroCanvas);
  requestAnimationFrame(() => requestAnimationFrame(frame));
}

function resizeIntroCanvas() {
  if (!els.introCanvas) return;
  const rect = els.introCanvas.getBoundingClientRect();
  const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
  els.introCanvas.width = Math.max(1, Math.floor(rect.width * pixelRatio));
  els.introCanvas.height = Math.max(1, Math.floor(rect.height * pixelRatio));
}

function makeIntroParticles(canvas, rows) {
  const lanes = {
    auto_accept: 0.23,
    flag_for_review: 0.5,
    reject: 0.77,
  };
  return rows.map((row, index) => {
    const seed = seededRandom(index + 19);
    const seed2 = seededRandom(index * 7 + 41);
    const seed3 = seededRandom(index * 13 + 5);
    return {
      decision: row.decision,
      startX: 0.16 + seed * 0.68,
      startY: -0.18 - seed2 * 0.32,
      throatX: 0.5 + (seed3 - 0.5) * 0.08,
      throatY: 0.46 + (seed - 0.5) * 0.04,
      endX: lanes[row.decision] + (seed - 0.5) * 0.16,
      endY: 0.82 + (seed2 - 0.5) * 0.11,
      delay: (index / Math.max(rows.length - 1, 1)) * 0.32,
      radius: 2.2 + seed3 * 2.1,
    };
  });
}

function drawIntro(ctx, canvas, particles, progress) {
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  drawFunnelScene(ctx, width, height);

  for (const particle of particles) {
    const localProgress = clamp((progress - particle.delay) / Math.max(1 - particle.delay, 0.01), 0, 1);
    const routed = easeInOut(localProgress);
    const split = routed < 0.58 ? routed / 0.58 : (routed - 0.58) / 0.42;
    let x;
    let y;
    if (routed < 0.58) {
      const t = easeIn(routed / 0.58);
      x = lerp(particle.startX, particle.throatX, t);
      y = lerp(particle.startY, particle.throatY, t);
    } else {
      const t = easeOut(split);
      x = lerp(particle.throatX, particle.endX, t);
      y = lerp(particle.throatY, particle.endY, t);
    }
    drawParticle(ctx, x * width, y * height, particle.radius * (width / 1100), particle.decision);
  }
}

function drawFunnelScene(ctx, width, height) {
  const center = width * 0.5;
  const topY = height * 0.22;
  const throatY = height * 0.48;
  const throatW = width * 0.09;

  ctx.save();
  ctx.strokeStyle = "rgba(161, 223, 245, 0.54)";
  ctx.lineWidth = Math.max(2, width * 0.0022);
  ctx.beginPath();
  ctx.moveTo(width * 0.2, topY);
  ctx.lineTo(center - throatW / 2, throatY);
  ctx.lineTo(center + throatW / 2, throatY);
  ctx.lineTo(width * 0.8, topY);
  ctx.stroke();

  ctx.fillStyle = "rgba(80, 184, 220, 0.07)";
  ctx.beginPath();
  ctx.moveTo(width * 0.2, topY);
  ctx.lineTo(center - throatW / 2, throatY);
  ctx.lineTo(center + throatW / 2, throatY);
  ctx.lineTo(width * 0.8, topY);
  ctx.closePath();
  ctx.fill();

  drawVial(ctx, width * 0.23, height * 0.83, width * 0.18, height * 0.25, "#31d17d", "Auto accept");
  drawVial(ctx, width * 0.5, height * 0.83, width * 0.18, height * 0.25, "#ffbd45", "Review");
  drawVial(ctx, width * 0.77, height * 0.83, width * 0.18, height * 0.25, "#9aa4aa", "Reject");
  ctx.restore();
}

function drawVial(ctx, x, y, w, h, color, label) {
  ctx.save();
  ctx.strokeStyle = "rgba(230, 248, 255, 0.55)";
  ctx.lineWidth = 2;
  ctx.fillStyle = "rgba(5, 12, 18, 0.28)";
  roundRect(ctx, x - w / 2, y - h / 2, w, h, 14);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = color;
  ctx.globalAlpha = 0.22;
  roundRect(ctx, x - w / 2 + 8, y - h * 0.08, w - 16, h * 0.42, 10);
  ctx.fill();
  ctx.globalAlpha = 1;
  ctx.fillStyle = "rgba(233, 248, 255, 0.86)";
  ctx.font = `${Math.max(11, w * 0.08)}px system-ui`;
  ctx.textAlign = "center";
  ctx.fillText(label, x, y + h / 2 + 24);
  ctx.restore();
}

function drawParticle(ctx, x, y, radius, decision) {
  const colors = {
    auto_accept: "#31d17d",
    flag_for_review: "#ffbd45",
    reject: "#c5cbd0",
  };
  ctx.save();
  ctx.fillStyle = colors[decision] || "#ffffff";
  ctx.shadowColor = colors[decision] || "#ffffff";
  ctx.shadowBlur = radius * 2.4;
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

function updateIntroCounts(counts, progress) {
  const shown = easeOut(clamp((progress - 0.54) / 0.46, 0, 1));
  els.introAccept.textContent = Math.round((counts.auto_accept ?? 0) * shown);
  els.introReview.textContent = Math.round((counts.flag_for_review ?? 0) * shown);
  els.introReject.textContent = Math.round((counts.reject ?? 0) * shown);
}

function finishIntro() {
  document.body.classList.remove("intro-active");
  els.intro?.classList.add("complete");
  setTimeout(() => {
    if (els.intro) els.intro.hidden = true;
  }, 560);
  resizeBodyViewer();
}

function seededRandom(seed) {
  const value = Math.sin(seed * 999) * 10000;
  return value - Math.floor(value);
}

function lerp(start, end, t) {
  return start + (end - start) * t;
}

function easeIn(t) {
  return t * t;
}

function easeOut(t) {
  return 1 - Math.pow(1 - t, 3);
}

function easeInOut(t) {
  return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
}

function roundRect(ctx, x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + width, y, x + width, y + height, r);
  ctx.arcTo(x + width, y + height, x, y + height, r);
  ctx.arcTo(x, y + height, x, y, r);
  ctx.arcTo(x, y, x + width, y, r);
  ctx.closePath();
}

init().catch((error) => {
  els.rows.innerHTML = `<div class="patient-card"><div class="patient-main"><strong>${escapeHtml(error.message)}</strong><span>Unable to render patient list</span></div></div>`;
});
