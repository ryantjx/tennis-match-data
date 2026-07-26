import * as duckdb from "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.32.0/+esm";

const PAGE_SIZE = 25;
const DATA_FILES = ["matches.parquet", "tournaments.parquet", "players.parquet"];
const state = {
  db: null,
  connection: null,
  manifest: null,
  page: 1,
  total: 0,
  revision: 0,
  filters: {
    tour: "",
    year: "",
    lifecycle: "",
    player: "",
    playerLabel: "",
    tournament: "",
    tournamentLabel: "",
    level: "",
    surface: "",
    sort: "newest",
  },
};

const elements = Object.fromEntries(
  [
    "release-banner", "release-status", "release-meta", "scope-copy", "preview-reasons",
    "release-link", "summary-matches", "summary-players", "summary-tournaments",
    "summary-years", "summary-lifecycle", "filter-panel", "filter-note", "clear-filters",
    "tour-filter", "year-filter", "lifecycle-filter", "level-filter", "surface-filter",
    "sort-filter", "match-results", "results-count", "results-context", "loading-state",
    "error-state", "error-message", "retry-button", "empty-state", "empty-clear",
    "table-shell", "results-table", "match-cards", "pagination", "previous-page",
    "next-page", "page-info",
  ].map(id => [id, document.querySelector(`#${id}`)])
);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function sqlLiteral(value) {
  return `'${String(value).replaceAll("'", "''")}'`;
}

function rows(result) {
  return result.toArray().map(row => row.toJSON());
}

function firstRow(result) {
  return rows(result)[0] ?? {};
}

function formatNumber(value) {
  return Number(value ?? 0).toLocaleString("en-GB");
}

function formatToken(value) {
  if (!value) return "—";
  return String(value)
    .replaceAll("_", " ")
    .replace(/\b(atp|wta)\b/gi, word => word.toUpperCase())
    .replace(/\b\w/g, character => character.toUpperCase());
}

function formatDate(value) {
  if (!value) return "TBD";
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(date);
}

function manifestAsset(name) {
  return state.manifest.assets.find(asset => asset.name === name);
}

function filterWhere(exclude = "") {
  const filters = state.filters;
  const predicates = ["TRUE"];
  if (exclude !== "tour" && filters.tour) {
    predicates.push(`e.tour = ${sqlLiteral(filters.tour)}`);
  }
  if (exclude !== "year" && filters.year) {
    predicates.push(`e.year = ${Number(filters.year)}`);
  }
  if (exclude !== "lifecycle" && filters.lifecycle === "fixture") {
    predicates.push("e.status = 'fixture'");
  } else if (exclude !== "lifecycle" && filters.lifecycle === "completed") {
    predicates.push("e.status <> 'fixture'");
  }
  if (exclude !== "player" && filters.player) {
    const player = sqlLiteral(filters.player);
    predicates.push(`(list_contains(e.player1_id, ${player}) OR list_contains(e.player2_id, ${player}))`);
  }
  if (exclude !== "tournament" && filters.tournament) {
    predicates.push(`e.tournament_name = ${sqlLiteral(filters.tournament)}`);
  }
  if (exclude !== "level" && filters.level) {
    predicates.push(`e.level = ${sqlLiteral(filters.level)}`);
  }
  if (exclude !== "surface" && filters.surface) {
    predicates.push(`e.surface = ${sqlLiteral(filters.surface)}`);
  }
  return predicates.join(" AND ");
}

function sortSql() {
  if (state.filters.sort === "oldest") {
    return "e.date ASC NULLS LAST, e.tournament_name, e.match_id";
  }
  if (state.filters.sort === "tournament") {
    return "e.tournament_name, e.date DESC NULLS LAST, e.match_id";
  }
  return "e.date DESC NULLS LAST, e.tournament_name, e.match_id";
}

function setControlsDisabled(disabled) {
  for (const id of ["year-filter", "level-filter", "surface-filter"]) {
    elements[id].disabled = disabled;
  }
  playerCombobox.setDisabled(disabled);
  tournamentCombobox.setDisabled(disabled);
}

function setSelectOptions(select, options, allLabel, selected) {
  const available = new Set(options.map(option => String(option.value)));
  const nextSelected = selected && available.has(String(selected)) ? String(selected) : "";
  select.innerHTML = [
    `<option value="">${escapeHtml(allLabel)}</option>`,
    ...options.map(option => {
      const suffix = option.match_count === undefined
        ? ""
        : ` · ${formatNumber(option.match_count)}`;
      return `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}${escapeHtml(suffix)}</option>`;
    }),
  ].join("");
  select.value = nextSelected;
  return Boolean(selected && !nextSelected);
}

function createCombobox(rootId, onSelection) {
  const root = document.querySelector(`#${rootId}`);
  const input = root.querySelector("input");
  const list = root.querySelector('[role="listbox"]');
  const clear = root.querySelector(".combo-clear");
  let options = [];
  let selected = null;
  let activeIndex = -1;
  let closeTimer = null;

  function visibleOptions() {
    const query = input.value.trim().toLocaleLowerCase();
    if (!query || (selected && input.value === selected.label)) return options;
    return options.filter(option => (
      option.label.toLocaleLowerCase().includes(query)
      || String(option.detail ?? "").toLocaleLowerCase().includes(query)
    ));
  }

  function render() {
    const visible = visibleOptions();
    activeIndex = Math.min(activeIndex, visible.length - 1);
    if (!visible.length) {
      list.innerHTML = '<p class="combo-empty">No available options match that text.</p>';
      input.removeAttribute("aria-activedescendant");
      return;
    }
    list.innerHTML = visible.map((option, index) => `
      <button
        class="combo-option${index === activeIndex ? " is-active" : ""}"
        id="${escapeHtml(rootId)}-option-${index}"
        type="button"
        role="option"
        aria-selected="${selected?.value === option.value ? "true" : "false"}"
        data-index="${index}"
      >
        <span class="combo-option-main">
          <strong>${escapeHtml(option.label)}</strong>
          ${option.detail ? `<span>${escapeHtml(option.detail)}</span>` : ""}
        </span>
        <span class="combo-option-count">${formatNumber(option.match_count)} matches</span>
      </button>
    `).join("");
    if (activeIndex >= 0) {
      input.setAttribute("aria-activedescendant", `${rootId}-option-${activeIndex}`);
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }

  function open() {
    if (input.disabled) return;
    window.clearTimeout(closeTimer);
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    render();
  }

  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    activeIndex = -1;
  }

  function choose(option, {notify = true} = {}) {
    selected = option;
    input.value = option?.label ?? "";
    clear.hidden = !option;
    close();
    if (notify) onSelection(option);
  }

  input.addEventListener("focus", open);
  input.addEventListener("click", open);
  input.addEventListener("input", () => {
    if (selected && input.value !== selected.label) {
      selected = null;
      clear.hidden = true;
      onSelection(null);
    }
    activeIndex = -1;
    open();
  });
  input.addEventListener("keydown", event => {
    const visible = visibleOptions();
    if (event.key === "ArrowDown") {
      event.preventDefault();
      open();
      activeIndex = Math.min(activeIndex + 1, visible.length - 1);
      render();
      list.querySelector(".is-active")?.scrollIntoView({block: "nearest"});
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      open();
      activeIndex = activeIndex <= 0 ? visible.length - 1 : activeIndex - 1;
      render();
      list.querySelector(".is-active")?.scrollIntoView({block: "nearest"});
    } else if (event.key === "Enter" && activeIndex >= 0) {
      event.preventDefault();
      choose(visible[activeIndex]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      if (selected) input.value = selected.label;
      close();
    } else if (event.key === "Tab") {
      close();
    }
  });
  input.addEventListener("blur", () => {
    closeTimer = window.setTimeout(() => {
      if (selected) input.value = selected.label;
      else input.value = "";
      close();
    }, 120);
  });
  list.addEventListener("mousedown", event => event.preventDefault());
  list.addEventListener("click", event => {
    const button = event.target.closest(".combo-option");
    if (!button) return;
    choose(visibleOptions()[Number(button.dataset.index)]);
    input.focus();
  });
  clear.addEventListener("click", () => {
    choose(null);
    input.focus();
    open();
  });

  return {
    setDisabled(disabled) {
      input.disabled = disabled;
      if (disabled) close();
    },
    setOptions(nextOptions, selectedValue, selectedLabel) {
      options = nextOptions.map(option => ({
        ...option,
        value: String(option.value),
        label: String(option.label),
      }));
      const matching = options.find(option => option.value === String(selectedValue));
      if (matching) {
        selected = matching;
        input.value = matching.label;
        clear.hidden = false;
        return false;
      }
      const wasSelected = Boolean(selectedValue);
      selected = null;
      input.value = selectedLabel && !wasSelected ? selectedLabel : "";
      clear.hidden = true;
      return wasSelected;
    },
    clear({notify = false} = {}) {
      choose(null, {notify});
    },
  };
}

const playerCombobox = createCombobox("player-combobox", option => {
  state.filters.player = option?.value ?? "";
  state.filters.playerLabel = option?.label ?? "";
  scheduleRefresh();
});

const tournamentCombobox = createCombobox("tournament-combobox", option => {
  state.filters.tournament = option?.value ?? "";
  state.filters.tournamentLabel = option?.label ?? "";
  scheduleRefresh();
});

async function loadManifest() {
  const response = await fetch(new URL("./data/manifest.json", import.meta.url), {cache: "no-store"});
  if (!response.ok) throw new Error(`Release manifest returned HTTP ${response.status}.`);
  const manifest = await response.json();
  if (
    manifest.product !== "Open Tennis Data"
    || String(manifest.product_version) !== "3"
    || String(manifest.schema_version) !== "3.3"
  ) {
    throw new Error("This site requires an Open Tennis Data v3.3 release.");
  }
  const available = new Set(manifest.assets.map(asset => asset.name));
  for (const file of DATA_FILES) {
    if (!available.has(file)) throw new Error(`Release manifest is missing ${file}.`);
  }
  state.manifest = manifest;
}

async function initializeDatabase() {
  const bundles = duckdb.getJsDelivrBundles();
  const bundle = await duckdb.selectBundle(bundles);
  const workerUrl = URL.createObjectURL(new Blob([
    `importScripts("${bundle.mainWorker}");`,
  ], {type: "text/javascript"}));
  const worker = new Worker(workerUrl);
  const database = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), worker);
  await database.instantiate(bundle.mainModule, bundle.pthreadWorker);
  URL.revokeObjectURL(workerUrl);
  state.db = database;
  state.connection = await database.connect();

  for (const file of DATA_FILES) {
    const url = new URL(`./data/${file}`, import.meta.url).href;
    await database.registerFileURL(file, url, duckdb.DuckDBDataProtocol.HTTP, false);
  }
  await state.connection.query(`
    CREATE VIEW matches AS SELECT * FROM read_parquet('matches.parquet');
    CREATE VIEW tournaments AS SELECT * FROM read_parquet('tournaments.parquet');
    CREATE VIEW players AS SELECT * FROM read_parquet('players.parquet');
    CREATE VIEW explorer_matches AS
      SELECT m.*, t.level, t.surface, t.indoor, t.city, t.country,
        t.start_date, t.end_date
      FROM matches m
      LEFT JOIN tournaments t USING (tournament_id, tour, year);
  `);
}

function renderReleaseDetails(summary) {
  const manifest = state.manifest;
  const status = String(manifest.release_status);
  const published = new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
    timeZoneName: "short",
  }).format(new Date(manifest.as_of));
  elements["release-status"].textContent = `${formatToken(status)} release`;
  elements["release-meta"].textContent = `Updated ${published} · schema ${manifest.schema_version}`;
  elements["release-banner"].classList.toggle("is-stable", status === "stable");
  elements["release-link"].href = `https://github.com/ryantjx/tennis-match-data/releases/tag/${encodeURIComponent(manifest.release_tag)}`;

  const scope = manifest.scope ?? {};
  elements["scope-copy"].textContent = [
    `${String(scope.draw ?? "main").replaceAll("_", " ")}-draw`,
    String(scope.format ?? "singles"),
    `${summary.min_year}–${summary.max_year}`,
    "with terminal dates backed by match-level evidence.",
  ].join(" ");
  const reasons = Array.isArray(manifest.preview_reasons) ? manifest.preview_reasons : [];
  elements["preview-reasons"].innerHTML = reasons
    .map(reason => `<li>${escapeHtml(reason)}</li>`)
    .join("");
  elements["preview-reasons"].hidden = !reasons.length;

  elements["summary-matches"].textContent = formatNumber(summary.match_count);
  elements["summary-players"].textContent = formatNumber(summary.player_count);
  elements["summary-tournaments"].textContent = formatNumber(summary.tournament_count);
  elements["summary-years"].textContent = `${summary.min_year}–${summary.max_year}`;
  elements["summary-lifecycle"].textContent = `${formatNumber(summary.terminal_count)} results · ${formatNumber(summary.fixture_count)} fixtures`;
}

async function loadSummary() {
  const result = await state.connection.query(`
    SELECT
      count(*) AS match_count,
      count(*) FILTER (WHERE status = 'fixture') AS fixture_count,
      count(*) FILTER (WHERE status <> 'fixture') AS terminal_count,
      min(year) AS min_year,
      max(year) AS max_year,
      count(DISTINCT tournament_id) AS tournament_count,
      (SELECT count(*) FROM players) AS player_count
    FROM explorer_matches
  `);
  const summary = firstRow(result);
  renderReleaseDetails(summary);
}

async function queryFacet(sql) {
  return rows(await state.connection.query(sql));
}

async function updateFacets(revision) {
  const years = await queryFacet(`
    SELECT year AS value, CAST(year AS VARCHAR) AS label, count(*) AS match_count
    FROM explorer_matches e
    WHERE ${filterWhere("year")}
    GROUP BY year
    ORDER BY year DESC
  `);
  if (revision !== state.revision) return false;

  const levels = await queryFacet(`
    SELECT level AS value, level AS label, count(*) AS match_count
    FROM explorer_matches e
    WHERE ${filterWhere("level")} AND level IS NOT NULL
    GROUP BY level
    ORDER BY lower(level)
  `);
  if (revision !== state.revision) return false;

  const surfaces = await queryFacet(`
    SELECT surface AS value, surface AS label, count(*) AS match_count
    FROM explorer_matches e
    WHERE ${filterWhere("surface")} AND surface IS NOT NULL
    GROUP BY surface
    ORDER BY lower(surface)
  `);
  if (revision !== state.revision) return false;

  const players = await queryFacet(`
    WITH filtered AS (
      SELECT match_id, player1_id, player1_name, player2_id, player2_name
      FROM explorer_matches e
      WHERE ${filterWhere("player")}
    ), slots AS (
      SELECT match_id, unnest(player1_id) AS player_id, unnest(player1_name) AS player_name
      FROM filtered
      UNION ALL
      SELECT match_id, unnest(player2_id) AS player_id, unnest(player2_name) AS player_name
      FROM filtered
    )
    SELECT s.player_id AS value,
      any_value(coalesce(p.name, s.player_name)) AS label,
      any_value(p.country) AS detail,
      count(DISTINCT s.match_id) AS match_count
    FROM slots s
    LEFT JOIN players p USING (player_id)
    WHERE s.player_id IS NOT NULL AND s.player_name IS NOT NULL
    GROUP BY s.player_id
    ORDER BY lower(label), value
  `);
  if (revision !== state.revision) return false;

  const tournaments = await queryFacet(`
    SELECT tournament_name AS value, tournament_name AS label,
      CASE
        WHEN min(year) = max(year) THEN CAST(min(year) AS VARCHAR)
        ELSE concat(min(year), '–', max(year))
      END AS detail,
      count(*) AS match_count
    FROM explorer_matches e
    WHERE ${filterWhere("tournament")} AND tournament_name IS NOT NULL
    GROUP BY tournament_name
    ORDER BY lower(tournament_name)
  `);
  if (revision !== state.revision) return false;

  let cleared = false;
  if (setSelectOptions(elements["year-filter"], years, "All seasons", state.filters.year)) {
    state.filters.year = "";
    cleared = true;
  }
  const formattedLevels = levels.map(option => ({...option, label: formatToken(option.label)}));
  if (setSelectOptions(elements["level-filter"], formattedLevels, "All levels", state.filters.level)) {
    state.filters.level = "";
    cleared = true;
  }
  const formattedSurfaces = surfaces.map(option => ({...option, label: formatToken(option.label)}));
  if (setSelectOptions(elements["surface-filter"], formattedSurfaces, "All surfaces", state.filters.surface)) {
    state.filters.surface = "";
    cleared = true;
  }
  if (playerCombobox.setOptions(players, state.filters.player, state.filters.playerLabel)) {
    state.filters.player = "";
    state.filters.playerLabel = "";
    cleared = true;
  }
  if (tournamentCombobox.setOptions(
    tournaments,
    state.filters.tournament,
    state.filters.tournamentLabel,
  )) {
    state.filters.tournament = "";
    state.filters.tournamentLabel = "";
    cleared = true;
  }
  if (cleared) {
    elements["filter-note"].textContent = "A filter was cleared because it is no longer available";
  } else {
    const active = Object.entries(state.filters)
      .filter(([key, value]) => value && !["sort", "playerLabel", "tournamentLabel"].includes(key))
      .length;
    elements["filter-note"].textContent = active
      ? `${active} active filter${active === 1 ? "" : "s"}`
      : "Explore all available matches";
  }
  return cleared;
}

function resultPlayer(name, seed, winner) {
  const classes = `player-line${winner ? " is-winner" : ""}`;
  return `
    <span class="${classes}">
      ${winner ? '<span class="winner-dot" aria-label="Winner"></span>' : ""}
      <span>${escapeHtml(name || "TBD")}</span>
      ${seed ? `<span class="seed">${escapeHtml(seed)}</span>` : ""}
    </span>
  `;
}

function sourcePills(source, status) {
  const labels = String(source || "")
    .split(",")
    .map(label => label.trim())
    .filter(Boolean);
  const pills = labels.map(label => `<span class="pill">${escapeHtml(label)}</span>`);
  if (status === "fixture") pills.unshift('<span class="pill is-fixture">Fixture</span>');
  return pills.join("");
}

function renderResults(resultRows) {
  elements["results-table"].innerHTML = resultRows.map(row => `
    <tr>
      <td class="date-cell">
        <strong>${escapeHtml(formatDate(row.date))}</strong>
        <span class="cell-meta">${escapeHtml(row.tour.toUpperCase())} · ${escapeHtml(row.year)}</span>
      </td>
      <td class="event-cell">
        <strong>${escapeHtml(row.tournament_name)}</strong>
        <span class="cell-meta">${escapeHtml(formatToken(row.level))} · ${escapeHtml(formatToken(row.surface))}</span>
      </td>
      <td class="players-cell">
        ${resultPlayer(row.player1_name, row.player1_seed, Number(row.winner_side) === 1)}
        ${resultPlayer(row.player2_name, row.player2_seed, Number(row.winner_side) === 2)}
      </td>
      <td class="round-cell">${escapeHtml(row.round || "TBD")}</td>
      <td class="score-cell">${escapeHtml(row.score || (row.status === "fixture" ? "To play" : "—"))}</td>
      <td class="source-cell"><span class="pills">${sourcePills(row.sources, row.status)}</span></td>
    </tr>
  `).join("");

  elements["match-cards"].innerHTML = resultRows.map(row => `
    <article class="match-card">
      <div class="match-card-top">
        <div class="match-card-event">
          <strong>${escapeHtml(row.tournament_name)}</strong>
          <span class="cell-meta">${escapeHtml(row.tour.toUpperCase())} · ${escapeHtml(formatToken(row.level))} · ${escapeHtml(formatToken(row.surface))}</span>
        </div>
        <span class="match-card-date">${escapeHtml(formatDate(row.date))}</span>
      </div>
      <div class="match-card-players">
        ${resultPlayer(row.player1_name, row.player1_seed, Number(row.winner_side) === 1)}
        ${resultPlayer(row.player2_name, row.player2_seed, Number(row.winner_side) === 2)}
      </div>
      <div class="match-card-bottom">
        <div>
          <span class="match-card-score">${escapeHtml(row.score || (row.status === "fixture" ? "To play" : "—"))}</span>
          <span class="cell-meta">${escapeHtml(row.round || "Round TBD")}</span>
        </div>
        <span class="pills">${sourcePills(row.sources, row.status)}</span>
      </div>
    </article>
  `).join("");
}

async function updateResults(revision) {
  const started = performance.now();
  const where = filterWhere();
  const count = firstRow(await state.connection.query(`
    SELECT count(*) AS total FROM explorer_matches e WHERE ${where}
  `));
  if (revision !== state.revision) return;
  state.total = Number(count.total);
  const totalPages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
  state.page = Math.min(state.page, totalPages);
  const offset = (state.page - 1) * PAGE_SIZE;
  const result = await state.connection.query(`
    SELECT
      strftime(e.date, '%Y-%m-%d') AS date,
      e.match_id, e.tournament_name, e.tour, e.year, e.round,
      array_to_string(e.player1_name, ' / ') AS player1_name,
      e.player1_seed,
      array_to_string(e.player2_name, ' / ') AS player2_name,
      e.player2_seed,
      CASE WHEN e.winner_id = e.player1_id THEN 1
           WHEN e.winner_id = e.player2_id THEN 2
           ELSE 0 END AS winner_side,
      e.status, e.score, e.level, e.surface,
      array_to_string(e.source, ',') AS sources
    FROM explorer_matches e
    WHERE ${where}
    ORDER BY ${sortSql()}
    LIMIT ${PAGE_SIZE} OFFSET ${offset}
  `);
  if (revision !== state.revision) return;
  const resultRows = rows(result);
  renderResults(resultRows);

  const elapsed = Math.max(1, Math.round(performance.now() - started));
  elements["results-count"].textContent = `${formatNumber(state.total)} ${state.total === 1 ? "match" : "matches"}`;
  elements["results-context"].textContent = `${resultRows.length} shown · page ${state.page} of ${totalPages} · ${elapsed} ms`;
  elements["loading-state"].hidden = true;
  elements["error-state"].hidden = true;
  elements["empty-state"].hidden = state.total !== 0;
  elements["table-shell"].hidden = state.total === 0;
  elements["match-cards"].hidden = state.total === 0;
  elements["pagination"].hidden = state.total === 0;
  elements["page-info"].textContent = `Page ${state.page} of ${totalPages}`;
  elements["previous-page"].disabled = state.page <= 1;
  elements["next-page"].disabled = state.page >= totalPages;
  elements["match-results"].setAttribute("aria-busy", "false");
}

async function refresh({resetPage = true} = {}) {
  if (!state.connection) return;
  if (resetPage) state.page = 1;
  const revision = ++state.revision;
  elements["filter-panel"].setAttribute("aria-busy", "true");
  elements["match-results"].setAttribute("aria-busy", "true");
  elements["results-context"].textContent = "Updating the available options…";
  try {
    const cleared = await updateFacets(revision);
    if (revision !== state.revision) return;
    if (cleared) {
      const nextRevision = ++state.revision;
      await updateFacets(nextRevision);
      if (nextRevision !== state.revision) return;
      await updateResults(nextRevision);
    } else {
      await updateResults(revision);
    }
  } catch (error) {
    showFatal(error);
  } finally {
    if (revision === state.revision || revision + 1 === state.revision) {
      elements["filter-panel"].setAttribute("aria-busy", "false");
    }
  }
}

let refreshTimer = null;
function scheduleRefresh() {
  window.clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(() => refresh(), 80);
}

function clearAllFilters() {
  Object.assign(state.filters, {
    tour: "",
    year: "",
    lifecycle: "",
    player: "",
    playerLabel: "",
    tournament: "",
    tournamentLabel: "",
    level: "",
    surface: "",
    sort: "newest",
  });
  document.querySelectorAll("#tour-filter .segment").forEach(button => {
    const active = button.dataset.value === "";
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  elements["year-filter"].value = "";
  elements["lifecycle-filter"].value = "";
  elements["level-filter"].value = "";
  elements["surface-filter"].value = "";
  elements["sort-filter"].value = "newest";
  playerCombobox.clear();
  tournamentCombobox.clear();
  refresh();
}

function showFatal(error) {
  console.error(error);
  setControlsDisabled(true);
  elements["loading-state"].hidden = true;
  elements["empty-state"].hidden = true;
  elements["table-shell"].hidden = true;
  elements["match-cards"].hidden = true;
  elements["pagination"].hidden = true;
  elements["error-state"].hidden = false;
  elements["error-message"].textContent = error?.message || "Please try again.";
  elements["results-count"].textContent = "Dataset unavailable";
  elements["results-context"].textContent = "The release snapshot was not changed.";
  elements["match-results"].setAttribute("aria-busy", "false");
}

function bindEvents() {
  elements["tour-filter"].addEventListener("click", event => {
    const button = event.target.closest(".segment");
    if (!button) return;
    state.filters.tour = button.dataset.value;
    elements["tour-filter"].querySelectorAll(".segment").forEach(segment => {
      const active = segment === button;
      segment.classList.toggle("is-active", active);
      segment.setAttribute("aria-pressed", String(active));
    });
    scheduleRefresh();
  });
  for (const [id, key] of [
    ["year-filter", "year"],
    ["lifecycle-filter", "lifecycle"],
    ["level-filter", "level"],
    ["surface-filter", "surface"],
    ["sort-filter", "sort"],
  ]) {
    elements[id].addEventListener("change", () => {
      state.filters[key] = elements[id].value;
      scheduleRefresh();
    });
  }
  elements["clear-filters"].addEventListener("click", clearAllFilters);
  elements["empty-clear"].addEventListener("click", clearAllFilters);
  elements["retry-button"].addEventListener("click", () => window.location.reload());
  elements["previous-page"].addEventListener("click", () => {
    state.page -= 1;
    refresh({resetPage: false});
    elements["match-results"].scrollIntoView({behavior: "smooth", block: "start"});
  });
  elements["next-page"].addEventListener("click", () => {
    state.page += 1;
    refresh({resetPage: false});
    elements["match-results"].scrollIntoView({behavior: "smooth", block: "start"});
  });
}

async function initialize() {
  bindEvents();
  setControlsDisabled(true);
  try {
    await loadManifest();
    await initializeDatabase();
    await loadSummary();
    setControlsDisabled(false);
    await refresh();
  } catch (error) {
    showFatal(error);
  }
}

initialize();
