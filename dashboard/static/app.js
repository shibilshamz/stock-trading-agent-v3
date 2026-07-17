(() => {
  "use strict";

  const REFRESH_INTERVAL_MS = 30000;

  const state = {
    currentRunId: null,
    currentStatus: "idle",
  };

  const el = (id) => document.getElementById(id);

  // -- fetch helper ------------------------------------------------------

  async function apiFetch(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = await response.json();
        detail = body.detail || detail;
      } catch (_) {
        /* response had no JSON body */
      }
      throw new Error(detail);
    }
    return response.json();
  }

  function showMessage(text, isError) {
    const messageEl = el("run-message");
    messageEl.textContent = text;
    messageEl.className = "message " + (isError ? "error" : "success");
  }

  // -- dropdown population ------------------------------------------------

  async function populateMarkets() {
    const markets = await apiFetch("/api/markets");
    const select = el("market-select");
    select.innerHTML = markets.map((m) => `<option value="${m.code}">${m.name}</option>`).join("");
  }

  async function populateStrategies() {
    const strategies = await apiFetch("/api/strategies");
    const select = el("strategy-select");
    select.innerHTML = strategies.map((s) => `<option value="${s.code}">${s.name}</option>`).join("");
  }

  async function populateModes() {
    const modes = await apiFetch("/api/modes");
    const select = el("mode-select");
    select.innerHTML = modes.map((m) => `<option value="${m}">${m}</option>`).join("");
  }

  async function loadDefaultParameters() {
    const code = el("strategy-select").value;
    if (!code) return;
    try {
      const params = await apiFetch(`/api/strategies/${encodeURIComponent(code)}/parameters`);
      el("parameters-input").value = JSON.stringify(params, null, 2);
    } catch (err) {
      showMessage(`Could not load default parameters: ${err.message}`, true);
    }
  }

  function updateModeVisibility() {
    const mode = el("mode-select").value;
    const needsDateRange = mode === "backtest" || mode === "historical_replay";
    const needsReplaySpeed = mode === "historical_replay";

    el("start-date-field").classList.toggle("hidden", !needsDateRange);
    el("end-date-field").classList.toggle("hidden", !needsDateRange);
    el("replay-speed-field").classList.toggle("hidden", !needsReplaySpeed);
  }

  // -- run / stop ----------------------------------------------------

  function parseSymbols() {
    const raw = el("symbols-input").value.trim();
    if (!raw) return null;
    return raw.split(",").map((s) => s.trim()).filter(Boolean);
  }

  function parseParameters() {
    const raw = el("parameters-input").value.trim();
    if (!raw) return null;
    return JSON.parse(raw); // caller handles SyntaxError
  }

  function buildRunPayload() {
    const mode = el("mode-select").value;
    const payload = {
      market: el("market-select").value,
      strategy: el("strategy-select").value,
      mode,
    };

    const symbols = parseSymbols();
    if (symbols) payload.symbols = symbols;

    try {
      const parameters = parseParameters();
      if (parameters) payload.parameters = parameters;
    } catch (err) {
      throw new Error(`Parameters must be valid JSON: ${err.message}`);
    }

    if (mode === "backtest" || mode === "historical_replay") {
      const startDate = el("start-date-input").value;
      const endDate = el("end-date-input").value;
      if (!startDate || !endDate) {
        throw new Error("Start and End date are required for this mode.");
      }
      payload.date_range = { start_date: startDate, end_date: endDate };
    }

    if (mode === "historical_replay") {
      payload.replay_speed = parseFloat(el("replay-speed-input").value) || 1.0;
    }

    return payload;
  }

  async function handleRun() {
    let payload;
    try {
      payload = buildRunPayload();
    } catch (err) {
      showMessage(err.message, true);
      return;
    }

    el("run-btn").disabled = true;
    try {
      const result = await apiFetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      showMessage(`Run started: ${result.run_id}`, false);
      await refreshAll();
    } catch (err) {
      showMessage(`Failed to start run: ${err.message}`, true);
      el("run-btn").disabled = false;
    }
  }

  async function handleStop() {
    if (!state.currentRunId) return;
    el("kill-switch-btn").disabled = true;
    try {
      await apiFetch(`/api/stop/${encodeURIComponent(state.currentRunId)}`, { method: "POST" });
      showMessage("Run stopped.", false);
      await refreshAll();
    } catch (err) {
      showMessage(`Failed to stop run: ${err.message}`, true);
      el("kill-switch-btn").disabled = false;
    }
  }

  // -- rendering ------------------------------------------------------

  function renderStatus(status) {
    state.currentStatus = status.status;
    state.currentRunId = status.run_id || null;

    const pill = el("status-pill");
    pill.className = "status-pill status-" + (status.status || "idle");
    pill.textContent = status.status || "idle";

    el("status-run-id").textContent = status.run_id || "-";
    el("status-mode").textContent = status.mode || "-";
    el("status-state").textContent = status.status || "-";
    el("status-market-strategy").textContent =
      status.market && status.strategy ? `${status.market} / ${status.strategy}` : "-";
    el("status-symbols").textContent = (status.symbols || []).join(", ") || "-";
    el("status-equity").textContent = status.equity != null ? status.equity.toFixed(2) : "-";
    el("status-open-positions").textContent = status.open_positions != null ? status.open_positions : "-";
    el("status-total-trades").textContent = status.total_trades != null ? status.total_trades : "-";
    el("status-win-rate").textContent = status.win_rate != null ? `${status.win_rate.toFixed(2)}%` : "-";

    const isRunning = status.status === "running";
    el("run-btn").disabled = isRunning;
    el("kill-switch-btn").disabled = !isRunning;

    const replayWrap = el("replay-progress-wrap");
    if (status.replay_progress) {
      replayWrap.classList.remove("hidden");
      const progress = status.replay_progress;
      el("replay-progress-fill").style.width = `${progress.progress_pct || 0}%`;
      el("replay-progress-label").textContent =
        `${(progress.progress_pct || 0).toFixed(1)}% - ${progress.processed_bars || 0} / ${progress.total_bars || 0} bars` +
        (progress.trades_so_far ? ` - ${progress.trades_so_far} trades, PnL ${progress.pnl_so_far.toFixed(2)}` : "");
    } else {
      replayWrap.classList.add("hidden");
    }
  }

  function renderPositions(positions) {
    const body = el("positions-body");
    if (!positions.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="6">No open positions</td></tr>';
      return;
    }
    body.innerHTML = positions
      .map(
        (p) => `
      <tr>
        <td>${p.symbol}</td>
        <td>${Number(p.entry_price).toFixed(2)}</td>
        <td>${p.quantity}</td>
        <td>${p.stop_loss != null ? Number(p.stop_loss).toFixed(2) : "-"}</td>
        <td>${p.take_profit != null ? Number(p.take_profit).toFixed(2) : "-"}</td>
        <td>${p.entry_time ? new Date(p.entry_time).toLocaleString() : "-"}</td>
      </tr>`
      )
      .join("");
  }

  function renderTrades(trades) {
    const body = el("trades-body");
    if (!trades.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="10">No trades yet</td></tr>';
      return;
    }
    body.innerHTML = trades
      .map((t) => {
        const pnlClass = t.pnl > 0 ? "pnl-positive" : t.pnl < 0 ? "pnl-negative" : "";
        return `
      <tr>
        <td>${t.symbol}</td>
        <td>${t.strategy_name || "-"}</td>
        <td>${t.side || "-"}</td>
        <td>${Number(t.entry_price).toFixed(2)}</td>
        <td>${t.exit_price != null ? Number(t.exit_price).toFixed(2) : "-"}</td>
        <td>${t.quantity}</td>
        <td class="${pnlClass}">${t.pnl != null ? Number(t.pnl).toFixed(2) : "-"}</td>
        <td class="${pnlClass}">${t.pnl_pct != null ? Number(t.pnl_pct).toFixed(2) + "%" : "-"}</td>
        <td>${t.duration_minutes != null ? t.duration_minutes : "-"}</td>
        <td>${t.exit_reason || "-"}</td>
      </tr>`;
      })
      .join("");
  }

  function renderBacktestResults(results) {
    const body = el("backtest-results-body");
    if (!results.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="9">No backtests run yet</td></tr>';
      return;
    }
    body.innerHTML = results
      .map((r) => {
        const pnlClass = r.net_pnl > 0 ? "pnl-positive" : r.net_pnl < 0 ? "pnl-negative" : "";
        return `
      <tr>
        <td>${r.run_id}</td>
        <td>${r.strategy_name || "-"}</td>
        <td>${r.market || "-"}</td>
        <td>${r.start_date} &rarr; ${r.end_date}</td>
        <td>${r.total_trades}</td>
        <td>${Number(r.win_rate).toFixed(2)}%</td>
        <td class="${pnlClass}">${Number(r.net_pnl).toFixed(2)}</td>
        <td>${r.sharpe_ratio != null ? Number(r.sharpe_ratio).toFixed(2) : "-"}</td>
        <td>${r.max_drawdown != null ? Number(r.max_drawdown).toFixed(2) + "%" : "-"}</td>
      </tr>`;
      })
      .join("");

    if (results.length) {
      renderEquityChart(results[0].equity_curve || []);
    }
  }

  let equityChart = null;
  function renderEquityChart(equityCurve) {
    const canvas = el("equity-chart");
    if (!canvas || typeof Chart === "undefined") return;

    const labels = equityCurve.map((p) => p.date);
    const data = equityCurve.map((p) => p.equity);

    if (equityChart) {
      equityChart.data.labels = labels;
      equityChart.data.datasets[0].data = data;
      equityChart.update();
      return;
    }

    equityChart = new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Equity (most recent backtest)",
            data,
            borderColor: "#2563eb",
            backgroundColor: "rgba(37, 99, 235, 0.1)",
            fill: true,
            tension: 0.2,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: true } },
        scales: { y: { beginAtZero: false } },
      },
    });
  }

  // -- data refresh ---------------------------------------------------

  async function refreshStatus() {
    const status = await apiFetch("/api/status");
    renderStatus(status);
  }

  async function refreshPositions() {
    const positions = await apiFetch("/api/positions");
    renderPositions(positions);
  }

  async function refreshTrades() {
    const params = new URLSearchParams();
    if (el("trades-start-date").value) params.set("start_date", el("trades-start-date").value);
    if (el("trades-end-date").value) params.set("end_date", el("trades-end-date").value);
    const trades = await apiFetch(`/api/trades?${params.toString()}`);
    renderTrades(trades);
  }

  async function refreshBacktestResults() {
    const results = await apiFetch("/api/backtest/results");
    renderBacktestResults(results);
  }

  async function refreshAll() {
    await Promise.all([
      refreshStatus().catch((err) => console.error("status refresh failed:", err)),
      refreshPositions().catch((err) => console.error("positions refresh failed:", err)),
      refreshTrades().catch((err) => console.error("trades refresh failed:", err)),
      refreshBacktestResults().catch((err) => console.error("backtest results refresh failed:", err)),
    ]);
  }

  function downloadReport() {
    const params = new URLSearchParams();
    if (el("report-start-date").value) params.set("start_date", el("report-start-date").value);
    if (el("report-end-date").value) params.set("end_date", el("report-end-date").value);
    window.location.href = `/api/report?${params.toString()}`;
  }

  // -- bootstrap ------------------------------------------------------

  async function init() {
    el("mode-select").addEventListener("change", updateModeVisibility);
    el("strategy-select").addEventListener("change", loadDefaultParameters);
    el("run-btn").addEventListener("click", handleRun);
    el("kill-switch-btn").addEventListener("click", handleStop);
    el("trades-filter-btn").addEventListener("click", () => {
      refreshTrades().catch((err) => showMessage(`Failed to load trades: ${err.message}`, true));
    });
    el("download-report-btn").addEventListener("click", downloadReport);

    try {
      await Promise.all([populateMarkets(), populateStrategies(), populateModes()]);
    } catch (err) {
      showMessage(`Failed to load run configuration: ${err.message}`, true);
    }

    updateModeVisibility();
    await loadDefaultParameters();
    await refreshAll();

    setInterval(() => {
      refreshAll();
    }, REFRESH_INTERVAL_MS);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
