import { ApiClient } from "../lib/api";
import { buildSearchPayload, publicErrorMessage } from "../lib/presentation";
import type { CreateSearchPayload, Search, SearchResultsPage } from "../lib/types";
import { TERMINAL_SEARCH_STATUSES } from "../lib/types";
import { renderResults, renderSearchMetrics, renderSearchProgress, showToast } from "./render";

const PAGE_SIZE = 50;
const POLL_INTERVAL_MS = 2_500;
const BACKOFF_INTERVAL_MS = 5_000;
const API_BASE_URL = import.meta.env.PUBLIC_API_BASE_URL || "http://127.0.0.1:8000";

const elements = {
  form: required<HTMLFormElement>("[data-search-form]"),
  category: required<HTMLInputElement>("[data-category-input]"),
  location: required<HTMLInputElement>("[data-location-input]"),
  categoryError: required<HTMLElement>("[data-category-error]"),
  locationError: required<HTMLElement>("[data-location-error]"),
  submit: required<HTMLButtonElement>("[data-search-submit]"),
  submitLabel: required<HTMLElement>("[data-submit-label]"),
  progress: required<HTMLElement>("[data-progress-panel]"),
  state: required<HTMLElement>("[data-state-panel]"),
  initial: required<HTMLElement>("[data-initial-state]"),
  empty: required<HTMLElement>("[data-empty-state]"),
  error: required<HTMLElement>("[data-error-state]"),
  errorMessage: required<HTMLElement>("[data-error-message]"),
  retry: required<HTMLButtonElement>("[data-retry-button]"),
  results: required<HTMLElement>("[data-results-section]"),
  exportButton: required<HTMLButtonElement>("[data-export-button]"),
  exportLabel: required<HTMLElement>("[data-export-label]"),
  previous: required<HTMLButtonElement>("[data-previous-page]"),
  next: required<HTMLButtonElement>("[data-next-page]"),
  apiIndicator: required<HTMLElement>("[data-api-indicator]"),
  apiLabel: required<HTMLElement>("[data-api-label]"),
};

const api = new ApiClient(API_BASE_URL);
let activeSearch: Search | null = null;
let currentPage: SearchResultsPage | null = null;
let currentOffset = 0;
let lastPayload: CreateSearchPayload | null = null;
let requestController: AbortController | null = null;
let pollTimer: number | null = null;
let pollingFailures = 0;

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const payload = validatedPayload();
  if (payload) void createSearch(payload);
});

document.querySelectorAll<HTMLButtonElement>("[data-suggestion-category]").forEach((button) => {
  button.addEventListener("click", () => {
    elements.category.value = button.dataset.suggestionCategory ?? "";
    elements.location.value = button.dataset.suggestionLocation ?? "";
    elements.form.requestSubmit();
  });
});

elements.retry.addEventListener("click", () => {
  if (lastPayload) void createSearch(lastPayload);
});

elements.previous.addEventListener("click", () => {
  if (!activeSearch || !currentPage) return;
  void loadResults(activeSearch, Math.max(0, currentOffset - PAGE_SIZE), true);
});

elements.next.addEventListener("click", () => {
  if (!activeSearch || !currentPage) return;
  const nextOffset = currentOffset + PAGE_SIZE;
  if (nextOffset < currentPage.total) void loadResults(activeSearch, nextOffset, true);
});

elements.exportButton.addEventListener("click", () => void exportResults());

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && activeSearch && !TERMINAL_SEARCH_STATUSES.has(activeSearch.status)) {
    schedulePoll(100);
  }
});

window.addEventListener("beforeunload", stopRequests);
void resumeFromUrl();

async function createSearch(payload: CreateSearchPayload): Promise<void> {
  stopRequests();
  lastPayload = payload;
  setSubmitting(true);
  setApiState("loading", "Conectando");
  showOnly("progress");
  resetProgress(payload);
  requestController = new AbortController();
  try {
    const search = await api.createSearch(payload, requestController.signal);
    activeSearch = search;
    currentOffset = 0;
    pollingFailures = 0;
    updateSearchUrl(search.id);
    setApiState("online", "API conectada");
    renderSearchProgress(search);
    if (TERMINAL_SEARCH_STATUSES.has(search.status)) await finishSearch(search);
    else schedulePoll(900);
  } catch (error) {
    if (isAbort(error)) return;
    setApiState("error", "API indisponível");
    showError(publicErrorMessage(error));
    setSubmitting(false);
  }
}

async function pollSearch(): Promise<void> {
  if (!activeSearch || document.hidden) {
    schedulePoll(POLL_INTERVAL_MS);
    return;
  }
  try {
    const search = await api.getSearch(activeSearch.id, requestController?.signal);
    activeSearch = search;
    pollingFailures = 0;
    setApiState("online", "API conectada");
    renderSearchProgress(search);
    if (TERMINAL_SEARCH_STATUSES.has(search.status)) await finishSearch(search);
    else schedulePoll(POLL_INTERVAL_MS);
  } catch (error) {
    if (isAbort(error)) return;
    pollingFailures += 1;
    setApiState("error", "Reconectando");
    if (pollingFailures === 2) {
      showToast("A conexão oscilou. O acompanhamento continuará automaticamente.", "error");
    }
    schedulePoll(BACKOFF_INTERVAL_MS);
  }
}

async function finishSearch(search: Search): Promise<void> {
  clearPoll();
  setSubmitting(false);
  renderSearchProgress(search);
  if ((search.status === "failed" || search.status === "cancelled") && search.results_count === 0) {
    showError(search.error_message || "Nenhuma fonte conseguiu concluir esta pesquisa.");
    return;
  }
  await loadResults(search, 0, false);
  if (search.status === "partial") {
    showToast("Pesquisa concluída com resultados parciais. Consulte o status das fontes.", "info");
  }
}

async function loadResults(search: Search, offset: number, scroll: boolean): Promise<void> {
  setApiState("loading", "Carregando");
  try {
    const page = await api.getResults(search.id, offset, PAGE_SIZE, requestController?.signal);
    activeSearch = search;
    currentPage = page;
    currentOffset = offset;
    setApiState("online", "API conectada");
    renderSearchMetrics(search);
    renderResults(page);
    if (page.total === 0) showOnly("empty");
    else {
      showOnly("results");
      elements.progress.hidden = false;
      if (scroll) elements.results.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  } catch (error) {
    if (isAbort(error)) return;
    setApiState("error", "API indisponível");
    showError(publicErrorMessage(error));
  }
}

async function exportResults(): Promise<void> {
  if (!activeSearch || !TERMINAL_SEARCH_STATUSES.has(activeSearch.status)) return;
  elements.exportButton.disabled = true;
  elements.exportButton.dataset.loading = "true";
  elements.exportLabel.textContent = "Gerando Excel…";
  try {
    const artifact = await api.exportResults(activeSearch.id);
    const objectUrl = URL.createObjectURL(artifact.blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = artifact.filename;
    anchor.hidden = true;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 10_000);
    const rows = artifact.rowCount === null ? "" : ` com ${artifact.rowCount.toLocaleString("pt-BR")} registros`;
    showToast(`Planilha gerada${rows}.`, "success");
  } catch (error) {
    showToast(publicErrorMessage(error), "error");
  } finally {
    elements.exportButton.disabled = false;
    elements.exportButton.dataset.loading = "false";
    elements.exportLabel.textContent = "Exportar Excel";
  }
}

async function resumeFromUrl(): Promise<void> {
  const searchId = new URLSearchParams(window.location.search).get("search");
  if (!searchId || !isUuid(searchId)) return;
  stopRequests();
  requestController = new AbortController();
  setSubmitting(true, "Retomando…");
  showOnly("progress");
  setApiState("loading", "Reconectando");
  try {
    const search = await api.getSearch(searchId, requestController.signal);
    activeSearch = search;
    lastPayload = { query: search.query };
    elements.category.value = search.category ?? search.query;
    elements.location.value = search.location ?? "";
    setApiState("online", "API conectada");
    renderSearchProgress(search);
    if (TERMINAL_SEARCH_STATUSES.has(search.status)) await finishSearch(search);
    else schedulePoll(500);
  } catch (error) {
    if (isAbort(error)) return;
    setSubmitting(false);
    setApiState("error", "API indisponível");
    showError(publicErrorMessage(error));
  }
}

function validatedPayload(): CreateSearchPayload | null {
  const category = elements.category.value.trim();
  const location = elements.location.value.trim();
  elements.categoryError.textContent = "";
  elements.locationError.textContent = "";
  elements.category.removeAttribute("aria-invalid");
  elements.location.removeAttribute("aria-invalid");
  let valid = true;
  if (category.length < 2) {
    elements.categoryError.textContent = "Informe uma categoria ou pesquisa com ao menos 2 caracteres.";
    elements.category.setAttribute("aria-invalid", "true");
    valid = false;
  }
  if (location.length === 1) {
    elements.locationError.textContent = "Informe uma localidade com ao menos 2 caracteres.";
    elements.location.setAttribute("aria-invalid", "true");
    valid = false;
  }
  if (!valid) return null;
  return buildSearchPayload(category, location);
}

function resetProgress(payload: CreateSearchPayload): void {
  const query = payload.query ?? `${payload.category} em ${payload.location}`;
  const emptySearch: Search = {
    id: "",
    query,
    category: payload.category ?? null,
    location: payload.location ?? null,
    status: "created",
    stage: "created",
    progress_percent: 0,
    discovered_count: 0,
    results_count: 0,
    companies_count: 0,
    whatsapp_count: 0,
    confirmed_whatsapp_count: 0,
    enriched_count: 0,
    ai_qualified_count: 0,
    error_message: null,
    providers: [],
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    started_at: null,
    completed_at: null,
  };
  renderSearchProgress(emptySearch);
}

function showOnly(view: "progress" | "results" | "empty" | "error"): void {
  elements.progress.hidden = view !== "progress";
  elements.results.hidden = view !== "results";
  elements.state.hidden = view === "progress" || view === "results";
  elements.initial.hidden = true;
  elements.empty.hidden = view !== "empty";
  elements.error.hidden = view !== "error";
}

function showError(message: string): void {
  elements.errorMessage.textContent = message;
  showOnly("error");
}

function setSubmitting(loading: boolean, label?: string): void {
  elements.submit.disabled = loading;
  elements.submit.dataset.loading = String(loading);
  elements.submitLabel.textContent = label ?? (loading ? "Pesquisando…" : "Buscar empresas");
}

function setApiState(state: "loading" | "online" | "error", label: string): void {
  elements.apiIndicator.dataset.state = state;
  elements.apiLabel.textContent = label;
}

function schedulePoll(delay: number): void {
  clearPoll();
  pollTimer = window.setTimeout(() => void pollSearch(), delay);
}

function clearPoll(): void {
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  pollTimer = null;
}

function stopRequests(): void {
  clearPoll();
  requestController?.abort();
  requestController = null;
}

function updateSearchUrl(searchId: string): void {
  const url = new URL(window.location.href);
  url.searchParams.set("search", searchId);
  window.history.replaceState({}, "", url);
}

function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function required<T extends Element>(selector: string): T {
  const node = document.querySelector<T>(selector);
  if (!node) throw new Error(`Elemento obrigatório ausente: ${selector}`);
  return node;
}
