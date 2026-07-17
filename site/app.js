import * as duckdb from "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.32.0/+esm";

const PAGE_SIZE = 10;
const RAW_ROOT = ["127.0.0.1", "localhost"].includes(window.location.hostname)
  ? `${window.location.origin}/data`
  : "https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data";
const currentYear = new Date().getUTCFullYear();

const TABLES = {
  matches: {
    path: ({tour, year}) => `matches/tour=${tour}/year=${year}/matches.parquet`,
    year: true,
  },
  tournaments: {
    path: ({tour, year}) => `tournaments/tour=${tour}/year=${year}/tournaments.parquet`,
    year: true,
  },
  match_stats: {
    path: ({tour, year}) => `match_stats/tour=${tour}/year=${year}/match-stats.parquet`,
    year: true,
  },
  observations: {
    path: ({tour, year}) => `observations/tour=${tour}/year=${year}/observations.parquet`,
    year: true,
  },
  rankings: {
    path: ({tour, year}) => `rankings/tour=${tour}/year=${year}/rankings.parquet`,
    year: true,
  },
  players: {
    path: ({tour}) => `players/tour=${tour}/players.parquet`,
    year: false,
  },
  fixtures: {
    path: ({tour}) => `fixtures/tour=${tour}/current.parquet`,
    year: false,
  },
  sources: {
    path: () => "coverage/source-audit.parquet",
    year: false,
  },
};

const SEARCH_COLUMNS = [
  "date",
  "tournament_name",
  "round",
  "format",
  "player1_name",
  "player1_seed",
  "player2_name",
  "player2_seed",
  "winner_name",
  "score",
  "best_of",
  "status",
  "level",
  "surface",
  "source_url",
];
const SEARCH_LABELS = {
  date: "Date",
  tournament_name: "Tournament",
  round: "Round",
  format: "Format",
  player1_name: "Player/Team 1",
  player1_seed: "Seed 1",
  player2_name: "Player/Team 2",
  player2_seed: "Seed 2",
  winner_name: "Winner",
  score: "Score",
  best_of: "Best of",
  status: "Status",
  level: "Level",
  surface: "Surface",
  source_url: "Source",
};

const state = {
  db: null,
  connection: null,
  initialized: false,
  search: {page: 1, total: 0, where: "TRUE", partition: null},
  query: {page: 1, total: 0, sql: "", table: null},
};

const elements = Object.fromEntries(
  [
    "search-tab", "explorer-tab", "search-panel", "explorer-panel",
    "search-form", "search-query", "search-kind", "search-tour", "search-year", "search-level",
    "search-surface", "search-button", "search-status", "search-results", "search-empty",
    "search-pagination", "search-previous", "search-next", "search-page-info",
    "explorer-table", "explorer-tour", "explorer-year", "load-table", "schema-list",
    "schema-count", "active-source", "load-example", "sql", "run-query", "query-status",
    "query-results", "query-empty", "query-pagination", "query-previous", "query-next",
    "query-page-info",
  ].map(id => [id, document.querySelector(`#${id}`)])
);

elements["search-year"].max = currentYear + 1;
elements["search-year"].value = currentYear;
elements["explorer-year"].max = currentYear;
elements["explorer-year"].value = currentYear;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function sqlLiteral(value) {
  return `'${String(value).replaceAll("'", "''")}'`;
}

function resultRows(result) {
  return result.toArray().map(row => row.toJSON());
}

function firstValue(result) {
  const row = resultRows(result)[0];
  return row ? Number(Object.values(row)[0]) : 0;
}

function displayValue(value, type) {
  if (value === null || value === undefined || value === "") return "—";
  const typeName = String(type);
  if (typeName.includes("Date")) {
    const date = value instanceof Date ? value : new Date(Number(value));
    if (!Number.isNaN(date.getTime())) return date.toISOString().slice(0, 10);
  }
  if (typeName.includes("Timestamp")) {
    const numeric = Number(value);
    const milliseconds = Math.abs(numeric) > 10_000_000_000_000 ? numeric / 1000 : numeric;
    const timestamp = value instanceof Date ? value : new Date(milliseconds);
    if (!Number.isNaN(timestamp.getTime())) return timestamp.toISOString().replace("T", " ").slice(0, 19);
  }
  return String(value);
}

function safeSourceLink(value) {
  if (!value) return "—";
  try {
    const url = new URL(String(value));
    if (!["http:", "https:"].includes(url.protocol)) return "—";
    return `<a href="${escapeHtml(url.href)}" target="_blank" rel="noopener noreferrer">Source ↗</a>`;
  } catch {
    return "—";
  }
}

function renderTable(table, empty, result, labels = {}) {
  const fields = result.schema.fields;
  const columns = fields.map(field => field.name);
  const types = Object.fromEntries(fields.map(field => [field.name, field.type]));
  const rows = resultRows(result);
  table.innerHTML = rows.length ? (
    `<thead><tr>${columns.map(column => `<th>${escapeHtml(labels[column] ?? column)}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows.map(row => `<tr>${columns.map(column => `<td>${column === "source_url" ? safeSourceLink(row[column]) : escapeHtml(displayValue(row[column], types[column]))}</td>`).join("")}</tr>`).join("")}</tbody>`
  ) : "";
  empty.hidden = rows.length > 0;
}

function updatePagination(kind) {
  const current = state[kind];
  const totalPages = Math.max(1, Math.ceil(current.total / PAGE_SIZE));
  elements[`${kind}-pagination`].hidden = current.total === 0;
  elements[`${kind}-page-info`].textContent = `Page ${current.page} of ${totalPages}`;
  elements[`${kind}-previous`].disabled = current.page <= 1;
  elements[`${kind}-next`].disabled = current.page >= totalPages;
}

function setBusy(button, busy, busyText, idleText) {
  button.disabled = busy;
  button.textContent = busy ? busyText : idleText;
}

async function initializeDuckDB() {
  try {
    const bundles = duckdb.getJsDelivrBundles();
    const bundle = await duckdb.selectBundle(bundles);
    const workerUrl = URL.createObjectURL(new Blob([
      `importScripts("${bundle.mainWorker}");`,
    ], {type: "text/javascript"}));
    const worker = new Worker(workerUrl);
    const db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), worker);
    await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
    state.db = db;
    state.connection = await db.connect();
    state.initialized = true;
    elements["search-status"].textContent = "Ready. Choose filters and search.";
    elements["query-status"].textContent = "Choose a table to begin.";
  } catch (error) {
    const message = `DuckDB could not initialize: ${error.message}`;
    elements["search-status"].textContent = message;
    elements["query-status"].textContent = message;
  }
}

function ensureReady() {
  if (!state.initialized || !state.connection) {
    throw new Error("DuckDB is still initializing. Try again in a moment.");
  }
}

async function registerTable(table, tour, year) {
  ensureReady();
  const definition = TABLES[table];
  const relativePath = definition.path({tour, year});
  const fileName = relativePath.replaceAll("/", "-").replaceAll("=", "-");
  const url = `${RAW_ROOT}/${relativePath}`;
  await state.db.registerFileURL(fileName, url, duckdb.DuckDBDataProtocol.HTTP, false);
  await state.connection.query(
    `CREATE OR REPLACE VIEW ${table} AS SELECT * FROM read_parquet(${sqlLiteral(fileName)})`
  );
  return {table, tour, year: definition.year ? year : null, relativePath};
}

function searchPredicate() {
  const query = elements["search-query"].value.trim().toLowerCase();
  const level = elements["search-level"].value;
  const surface = elements["search-surface"].value;
  const predicates = ["TRUE"];
  if (query) {
    const pattern = sqlLiteral(`%${query}%`);
    predicates.push(`(
      lower(coalesce(tournament_name, '')) LIKE ${pattern}
      OR lower(coalesce(player1_name, '')) LIKE ${pattern}
      OR lower(coalesce(player2_name, '')) LIKE ${pattern}
    )`);
  }
  if (level) predicates.push(`level = ${sqlLiteral(level)}`);
  if (surface) predicates.push(`surface = ${sqlLiteral(surface)}`);
  return predicates.join(" AND ");
}

async function runSearch({resetPage = true} = {}) {
  setBusy(elements["search-button"], true, "Searching…", "Search database");
  elements["search-status"].textContent = "Reading the selected Parquet partition…";
  try {
    const tour = elements["search-tour"].value;
    const kind = elements["search-kind"].value;
    const year = Number(elements["search-year"].value);
    const maximumYear = kind === "fixtures" ? currentYear + 1 : currentYear;
    if (year < 1968 || year > maximumYear) throw new Error(`Choose a year from 1968 to ${maximumYear}.`);
    const partitionKey = `${kind}/${tour}/${year}`;
    if (state.search.partition !== partitionKey) {
      await registerTable(kind, tour, year);
      await registerTable("tournaments", tour, year);
      await registerTable("observations", tour, year);
      await registerTable("sources", tour, year);
      await state.connection.query(`
        CREATE OR REPLACE VIEW search_matches AS
        WITH links AS (
          SELECT o.match_id, o.tour, o.year, min(s.source_url) AS source_url
          FROM observations o JOIN sources s USING(source_file_id)
          WHERE starts_with(s.source_url, 'http://') OR starts_with(s.source_url, 'https://')
          GROUP BY o.match_id, o.tour, o.year
        )
        SELECT m.date, m.tournament_id, m.tournament_name, m.round, m.format,
          array_to_string(m.player1_name, ' / ') AS player1_name, m.player1_seed,
          array_to_string(m.player2_name, ' / ') AS player2_name, m.player2_seed,
          CASE WHEN m.winner_id=m.player1_id THEN array_to_string(m.player1_name, ' / ')
               WHEN m.winner_id=m.player2_id THEN array_to_string(m.player2_name, ' / ')
               ELSE NULL END AS winner_name,
          m.score, m.best_of, m.status, t.level, t.surface, links.source_url,
          m.match_id, m.tour, m.year, m.draw
        FROM ${kind} m
        JOIN tournaments t USING (tournament_id, tour, year)
        LEFT JOIN links USING(match_id, tour, year)
        WHERE m.year=${year}
      `);
      state.search.partition = partitionKey;
    }
    if (resetPage) state.search.page = 1;
    state.search.where = searchPredicate();
    const started = performance.now();
    const countResult = await state.connection.query(
      `SELECT count(*) AS total FROM search_matches WHERE ${state.search.where}`
    );
    state.search.total = firstValue(countResult);
    const offset = (state.search.page - 1) * PAGE_SIZE;
    const result = await state.connection.query(`
      SELECT ${SEARCH_COLUMNS.join(", ")}
      FROM search_matches
      WHERE ${state.search.where}
      ORDER BY date DESC NULLS LAST, tournament_id, round DESC, match_id
      LIMIT ${PAGE_SIZE} OFFSET ${offset}
    `);
    renderTable(elements["search-results"], elements["search-empty"], result, SEARCH_LABELS);
    updatePagination("search");
    const elapsed = Math.round(performance.now() - started);
    const noun = state.search.total === 1 ? "match" : "matches";
    elements["search-status"].textContent = `${state.search.total.toLocaleString()} ${noun} · ${kind === "fixtures" ? "future" : "completed"} · ${tour.toUpperCase()} ${year} · ${elapsed} ms`;
  } catch (error) {
    elements["search-status"].textContent = error.message;
  } finally {
    setBusy(elements["search-button"], false, "Searching…", "Search database");
  }
}

function renderSchema(result) {
  const rows = resultRows(result);
  elements["schema-count"].textContent = rows.length;
  elements["schema-list"].innerHTML = rows.map(row => `
    <div class="schema-row">
      <span>${escapeHtml(row.column_name)}</span>
      <code>${escapeHtml(row.column_type)}</code>
    </div>
  `).join("");
}

async function loadExplorerTable({replaceSql = true} = {}) {
  setBusy(elements["load-table"], true, "Loading…", "Load table");
  elements["query-status"].textContent = "Registering the selected Parquet table…";
  try {
    const table = elements["explorer-table"].value;
    const tour = elements["explorer-tour"].value;
    const year = Number(elements["explorer-year"].value);
    if (TABLES[table].year && (year < 1968 || year > currentYear)) {
      throw new Error(`Choose a year from 1968 to ${currentYear}.`);
    }
    const source = await registerTable(table, tour, year);
    state.query.table = table;
    const schema = await state.connection.query(`DESCRIBE SELECT * FROM ${table}`);
    renderSchema(schema);
    elements["active-source"].textContent = `${table} · ${tour.toUpperCase()}${source.year ? ` ${source.year}` : ""}`;
    elements["query-status"].textContent = `Loaded data/${source.relativePath}`;
    if (replaceSql) elements.sql.value = `SELECT *\nFROM ${table}\nLIMIT 100;`;
    state.query.page = 1;
    state.query.total = 0;
    elements["query-results"].innerHTML = "";
    elements["query-empty"].hidden = false;
    elements["query-pagination"].hidden = true;
  } catch (error) {
    elements["query-status"].textContent = error.message;
  } finally {
    setBusy(elements["load-table"], false, "Loading…", "Load table");
  }
}

function normalizedReadOnlySql() {
  const sql = elements.sql.value.trim().replace(/;+\s*$/, "");
  if (!/^(select|with)\b/i.test(sql)) {
    throw new Error("The browser explorer accepts read-only SELECT or WITH queries.");
  }
  if (/;/.test(sql)) throw new Error("Run one SQL statement at a time.");
  return sql;
}

async function runExplorerQuery({resetPage = true} = {}) {
  setBusy(elements["run-query"], true, "Running…", "Run query");
  elements["query-status"].textContent = "Running query locally…";
  try {
    ensureReady();
    if (!state.query.table) await loadExplorerTable({replaceSql: false});
    const sql = normalizedReadOnlySql();
    if (resetPage || state.query.sql !== sql) state.query.page = 1;
    state.query.sql = sql;
    const started = performance.now();
    const countResult = await state.connection.query(
      `SELECT count(*) AS total FROM (${sql}) AS browser_query`
    );
    state.query.total = firstValue(countResult);
    const offset = (state.query.page - 1) * PAGE_SIZE;
    const result = await state.connection.query(
      `SELECT * FROM (${sql}) AS browser_query LIMIT ${PAGE_SIZE} OFFSET ${offset}`
    );
    renderTable(elements["query-results"], elements["query-empty"], result);
    updatePagination("query");
    elements["query-status"].textContent = `${state.query.total.toLocaleString()} rows · ${Math.round(performance.now() - started)} ms`;
  } catch (error) {
    elements["query-status"].textContent = error.message;
  } finally {
    setBusy(elements["run-query"], false, "Running…", "Run query");
  }
}

function activateTab(tabName) {
  const searchActive = tabName === "search";
  elements["search-tab"].classList.toggle("is-active", searchActive);
  elements["explorer-tab"].classList.toggle("is-active", !searchActive);
  elements["search-tab"].setAttribute("aria-selected", String(searchActive));
  elements["explorer-tab"].setAttribute("aria-selected", String(!searchActive));
  elements["search-panel"].hidden = !searchActive;
  elements["explorer-panel"].hidden = searchActive;
}

elements["search-tab"].addEventListener("click", () => activateTab("search"));
elements["explorer-tab"].addEventListener("click", () => activateTab("explorer"));
elements["search-form"].addEventListener("submit", event => {
  event.preventDefault();
  runSearch();
});
elements["search-previous"].addEventListener("click", () => {
  state.search.page -= 1;
  runSearch({resetPage: false});
});
elements["search-next"].addEventListener("click", () => {
  state.search.page += 1;
  runSearch({resetPage: false});
});
elements["explorer-table"].addEventListener("change", () => {
  const needsYear = TABLES[elements["explorer-table"].value].year;
  elements["explorer-year"].disabled = !needsYear;
});
elements["load-table"].addEventListener("click", () => loadExplorerTable());
elements["load-example"].addEventListener("click", () => {
  elements["explorer-table"].value = "matches";
  elements["explorer-year"].disabled = false;
  elements.sql.value = `SELECT tournament_id, round, player1_name, player2_name, score\nFROM matches\nORDER BY year DESC, tournament_id, round DESC\nLIMIT 100;`;
});
elements["run-query"].addEventListener("click", () => runExplorerQuery());
elements["query-previous"].addEventListener("click", () => {
  state.query.page -= 1;
  runExplorerQuery({resetPage: false});
});
elements["query-next"].addEventListener("click", () => {
  state.query.page += 1;
  runExplorerQuery({resetPage: false});
});

initializeDuckDB();
