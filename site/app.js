import * as duckdb from "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.32.0/+esm";

const bundles = duckdb.getJsDelivrBundles();
const bundle = await duckdb.selectBundle(bundles);
const workerUrl = URL.createObjectURL(new Blob([
  `importScripts("${bundle.mainWorker}");`
], {type: "text/javascript"}));
const worker = new Worker(workerUrl);
const db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), worker);
await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
const connection = await db.connect();

const status = document.querySelector("#status");
const runButton = document.querySelector("#run");
const table = document.querySelector("#results");
status.textContent = "Ready";

async function registerPartition() {
  const tour = document.querySelector("#tour").value;
  const year = document.querySelector("#year").value;
  const url = `https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/matches/tour=${tour}/year=${year}/matches.parquet`;
  await db.registerFileURL("matches.parquet", url, duckdb.DuckDBDataProtocol.HTTP, false);
  await connection.query("CREATE OR REPLACE VIEW matches AS SELECT * FROM read_parquet('matches.parquet')");
}

function render(result) {
  const escape = value => String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[character]);
  const columns = result.schema.fields.map(field => field.name);
  const rows = result.toArray().map(row => row.toJSON());
  table.innerHTML = `<thead><tr>${columns.map(c => `<th>${escape(c)}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows.map(row => `<tr>${columns.map(c => `<td>${escape(row[c])}</td>`).join("")}</tr>`).join("")}</tbody>`;
}

runButton.addEventListener("click", async () => {
  runButton.disabled = true;
  status.textContent = "Reading selected Parquet partition…";
  try {
    await registerPartition();
    const started = performance.now();
    const result = await connection.query(document.querySelector("#sql").value);
    render(result);
    status.textContent = `${result.numRows} rows in ${Math.round(performance.now() - started)} ms`;
  } catch (error) {
    status.textContent = error.message;
  } finally {
    runButton.disabled = false;
  }
});

document.querySelector("#example").addEventListener("click", () => {
  document.querySelector("#sql").value = `SELECT event_name, round, player1_name, player2_name, score\nFROM matches\nWHERE level = 'grand_slam'\nORDER BY event_start_date DESC, round_order DESC\nLIMIT 100;`;
});
