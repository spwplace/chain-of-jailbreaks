function pct(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "n/a";
  return `${Math.round(value * 1000) / 10}%`;
}

function setText(id, value) {
  document.getElementById(id).textContent = value;
}

function renderEffects(effects) {
  const chart = document.getElementById("effect-chart");
  chart.innerHTML = "";
  if (!effects.length) {
    chart.innerHTML = '<p class="empty">No matched cumulative/reset effects are available yet.</p>';
    return;
  }

  const candidates = effects.some((item) => item.lure_mode === "conflict")
    ? effects.filter((item) => item.lure_mode === "conflict")
    : effects;
  const ordered = [...candidates]
    .sort((a, b) => Math.abs(b.drift_amplification) - Math.abs(a.drift_amplification))
    .slice(0, 12);

  for (const item of ordered) {
    const row = document.createElement("div");
    row.className = "bar-row";

    const label = document.createElement("div");
    label.className = "bar-label";
    label.title = `${item.model} ${item.defense} ${item.lure_mode} depth ${item.depth}`;
    label.textContent = `${item.model} · ${item.lure_mode}`;

    const track = document.createElement("div");
    track.className = "bar-track";
    const fill = document.createElement("div");
    fill.className = `bar-fill ${item.drift_amplification < 0 ? "negative" : ""}`;
    fill.style.width = `${Math.min(100, Math.abs(item.drift_amplification) * 100)}%`;
    track.appendChild(fill);

    const value = document.createElement("div");
    value.className = "bar-value";
    value.textContent = pct(item.drift_amplification);

    row.append(label, track, value);
    chart.appendChild(row);
  }
}

function renderGroups(groups) {
  const body = document.getElementById("groups-body");
  body.innerHTML = "";
  if (!groups.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">No aggregate rows are available yet.</td></tr>';
    return;
  }

  for (const group of groups) {
    const row = document.createElement("tr");
    const cells = [
      group.model,
      group.mode,
      group.lure_mode,
      String(group.depth),
      String(group.runs),
      pct(group.metrics.password_rate),
      pct(group.metrics.contract_rate),
      pct(group.metrics.drift_rate),
    ];
    for (const value of cells) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.appendChild(cell);
    }
    body.appendChild(row);
  }
}

function renderNotes(results) {
  const notes = document.getElementById("notes");
  notes.innerHTML = "";
  setText("generated", results.generated_at ? `Generated ${results.generated_at}` : "No generated timestamp.");
  const items = results.notes || [];
  if (!items.length) {
    notes.innerHTML = '<p>No notes are attached to this result file.</p>';
    return;
  }
  for (const item of items) {
    const p = document.createElement("p");
    p.textContent = item;
    notes.appendChild(p);
  }
}

async function main() {
  const response = await fetch("results.json", { cache: "no-store" });
  const results = await response.json();

  setText("protocol", results.protocol_version || "unknown");
  setText("runs", String(results.runs || 0));
  setText("groups", String((results.groups || []).length));
  setText("missing", String((results.missing_models || []).length));
  setText("errors", String(results.error_count || 0));
  renderEffects(results.effects || []);
  renderNotes(results);
  renderGroups(results.groups || []);
}

main().catch((error) => {
  document.getElementById("effect-chart").innerHTML = `<p class="empty">${error.message}</p>`;
});
