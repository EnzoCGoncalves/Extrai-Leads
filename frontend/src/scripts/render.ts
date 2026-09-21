import {
  formatConfidence,
  formatCount,
  formatDate,
  formatLocation,
  formatPhone,
  PROVIDER_STATUS_LABELS,
  safeExternalUrl,
  SEARCH_STAGE_LABELS,
  SEARCH_STATUS_LABELS,
  WHATSAPP_LABELS,
} from "../lib/presentation";
import type { Search, SearchResult, SearchResultsPage } from "../lib/types";

export function renderSearchProgress(search: Search): void {
  text("[data-progress-title]", SEARCH_STAGE_LABELS[search.stage]);
  text("[data-progress-query]", search.query);
  text("[data-search-status]", SEARCH_STATUS_LABELS[search.status]);
  text("[data-progress-message]", progressMessage(search));
  text("[data-progress-percent]", `${search.progress_percent}%`);

  const status = required<HTMLElement>("[data-search-status]");
  status.dataset.tone = statusTone(search.status);
  const track = required<HTMLElement>("[data-progress-track]");
  track.setAttribute("aria-valuenow", String(search.progress_percent));
  required<HTMLElement>("[data-progress-bar]").style.width = `${search.progress_percent}%`;

  const providerList = required<HTMLElement>("[data-provider-list]");
  providerList.replaceChildren(...search.providers.map(providerNode));
}

export function renderSearchMetrics(search: Search): void {
  text("[data-metric-companies]", formatCount(search.companies_count));
  text("[data-metric-whatsapp]", formatCount(search.whatsapp_count));
  text("[data-metric-confirmed]", formatCount(search.confirmed_whatsapp_count));
  text("[data-metric-enriched]", formatCount(search.enriched_count));
  text(
    "[data-results-summary]",
    `${formatCount(search.results_count)} resultado${search.results_count === 1 ? "" : "s"} para “${search.query}”.`,
  );
}

export function renderResults(page: SearchResultsPage): void {
  const body = required<HTMLTableSectionElement>("[data-results-body]");
  const mobile = required<HTMLElement>("[data-mobile-results]");
  body.replaceChildren();
  mobile.replaceChildren();

  page.items.forEach((item) => {
    const { row, details } = resultRows(item);
    body.append(row, details);
    mobile.append(mobileCard(item));
  });

  const start = page.total === 0 ? 0 : page.offset + 1;
  const end = Math.min(page.offset + page.limit, page.total);
  const currentPage = Math.floor(page.offset / page.limit) + 1;
  const pages = Math.max(1, Math.ceil(page.total / page.limit));
  text("[data-page-summary]", `Exibindo ${formatCount(start)}–${formatCount(end)} de ${formatCount(page.total)}`);
  text("[data-page-number]", `Página ${currentPage} de ${pages}`);
  required<HTMLButtonElement>("[data-previous-page]").disabled = page.offset === 0;
  required<HTMLButtonElement>("[data-next-page]").disabled = end >= page.total;
}

export function showToast(message: string, tone: "success" | "error" | "info" = "info"): void {
  const region = required<HTMLElement>("[data-toast-region]");
  const toast = element("div", "toast");
  toast.dataset.tone = tone;
  toast.setAttribute("role", tone === "error" ? "alert" : "status");
  toast.append(iconNode(tone === "success" ? "check" : tone === "error" ? "alert" : "info"));
  toast.append(element("span", "", message));
  const close = element("button") as HTMLButtonElement;
  close.type = "button";
  close.setAttribute("aria-label", "Fechar aviso");
  close.append(iconNode("x"));
  close.addEventListener("click", () => toast.remove());
  toast.append(close);
  region.append(toast);
  window.setTimeout(() => toast.remove(), 5_500);
}

function providerNode(provider: Search["providers"][number]): HTMLElement {
  const item = element("div", "provider-item");
  item.dataset.status = provider.status;
  item.title = provider.error_message ?? provider.display_name;
  item.append(element("span", "provider-item__dot"));
  item.append(element("span", "provider-item__name", provider.display_name));
  const suffix = provider.results_count ? ` · ${formatCount(provider.results_count)}` : "";
  item.append(
    element(
      "span",
      "provider-item__status",
      `${PROVIDER_STATUS_LABELS[provider.status]}${suffix}`,
    ),
  );
  return item;
}

function resultRows(item: SearchResult): { row: HTMLTableRowElement; details: HTMLTableRowElement } {
  const row = element("tr") as HTMLTableRowElement;
  const detailsId = `details-${item.id}`;

  const companyCell = cell();
  companyCell.append(companyIdentity(item));
  row.append(companyCell);

  const contactCell = cell();
  contactCell.append(element("span", "contact-value", formatPhone(item.company.phone)));
  const links = contactLinks(item);
  if (links.childElementCount) contactCell.append(links);
  row.append(contactCell);

  const whatsappCell = cell();
  whatsappCell.append(whatsappBadge(item.company.whatsapp_status));
  if (item.company.whatsapp) {
    whatsappCell.append(element("span", "contact-value", formatPhone(item.company.whatsapp)));
  }
  row.append(whatsappCell);

  const locationCell = cell();
  locationCell.append(
    element(
      "span",
      "location-value",
      item.company.address || formatLocation(item.company.city, item.company.state),
    ),
  );
  row.append(locationCell);

  const confidenceCell = cell();
  confidenceCell.append(confidenceNode(item.confidence ?? item.company.confidence));
  row.append(confidenceCell);

  const actionCell = cell();
  const detailButton = element("button", "detail-button") as HTMLButtonElement;
  detailButton.type = "button";
  detailButton.setAttribute("aria-label", `Ver detalhes de ${item.company.name}`);
  detailButton.setAttribute("aria-expanded", "false");
  detailButton.setAttribute("aria-controls", detailsId);
  detailButton.append(iconNode("chevron"));
  actionCell.append(detailButton);
  row.append(actionCell);

  const details = element("tr", "details-row") as HTMLTableRowElement;
  details.id = detailsId;
  details.hidden = true;
  const detailsCell = cell();
  detailsCell.colSpan = 6;
  detailsCell.append(detailsContent(item));
  details.append(detailsCell);
  detailButton.addEventListener("click", () => {
    details.hidden = !details.hidden;
    detailButton.setAttribute("aria-expanded", String(!details.hidden));
  });
  return { row, details };
}

function mobileCard(item: SearchResult): HTMLElement {
  const card = element("article", "result-mobile-card");
  const head = element("div", "result-mobile-card__head");
  head.append(companyIdentity(item), confidenceNode(item.confidence ?? item.company.confidence));
  card.append(head);
  card.append(mobileLine("Telefone", formatPhone(item.company.phone)));
  const whatsappLine = mobileLine("WhatsApp", "");
  whatsappLine.lastElementChild?.replaceWith(whatsappBadge(item.company.whatsapp_status));
  card.append(whatsappLine);
  card.append(
    mobileLine("Localização", item.company.address || formatLocation(item.company.city, item.company.state)),
  );
  const disclosure = element("details", "mobile-details");
  disclosure.append(element("summary", "", "Ver fontes e evidências"), detailsContent(item));
  card.append(disclosure);
  return card;
}

function companyIdentity(item: SearchResult): HTMLElement {
  const wrapper = element("div", "company-cell");
  wrapper.append(element("span", "company-avatar", item.company.name.slice(0, 1)));
  const copy = element("div");
  copy.append(element("span", "company-name", item.company.name));
  copy.append(element("span", "company-category", item.company.category ?? "Categoria não informada"));
  wrapper.append(copy);
  return wrapper;
}

function contactLinks(item: SearchResult): HTMLElement {
  const links = element("div", "contact-links");
  const website = safeExternalUrl(item.company.website);
  const instagram = safeExternalUrl(item.company.instagram);
  if (website) links.append(externalLink("Site", website));
  if (instagram) links.append(externalLink("Instagram", instagram));
  return links;
}

function whatsappBadge(status: SearchResult["company"]["whatsapp_status"]): HTMLElement {
  const badge = element("span", "whatsapp-badge", WHATSAPP_LABELS[status]);
  badge.dataset.status = status;
  return badge;
}

function confidenceNode(value: number | null): HTMLElement {
  const wrapper = element("div", "confidence");
  const track = element("span", "confidence__track");
  const bar = element("span");
  bar.style.width = `${Math.max(0, Math.min(100, value ?? 0))}%`;
  track.append(bar);
  wrapper.append(track, element("strong", "", formatConfidence(value)));
  return wrapper;
}

function detailsContent(item: SearchResult): HTMLElement {
  const content = element("div", "result-details");
  const data = detailGroup("Dados da empresa");
  const dataList = element("ul");
  addFact(dataList, "CNPJ", item.company.cnpj);
  addFact(dataList, "Cidade/UF", formatLocation(item.company.city, item.company.state));
  addFact(dataList, "Coletado em", formatDate(item.collected_at));
  addFact(dataList, "Correspondência", categoryMatch(item.category_match));
  data.append(dataList);

  const sources = detailGroup("Fontes públicas");
  const sourceList = element("ul");
  if (item.source_details.length === 0) {
    sourceList.append(element("li", "", "Nenhuma fonte detalhada disponível."));
  }
  item.source_details.forEach((source) => {
    const line = element("li");
    const url = safeExternalUrl(source.source_url);
    if (url) line.append(externalLink(source.display_name, url));
    else line.textContent = source.display_name;
    sourceList.append(line);
  });
  sources.append(sourceList);

  const evidence = detailGroup("WhatsApp e qualificação");
  const evidenceList = element("ul");
  if (item.whatsapp_evidence.length === 0) {
    evidenceList.append(element("li", "", "Nenhuma evidência pública de WhatsApp armazenada."));
  }
  item.whatsapp_evidence.forEach((record) => {
    const line = element("li");
    const url = safeExternalUrl(record.source_url);
    const label = `${record.official_source ? "Fonte oficial" : "Fonte pública"} · ${record.source}`;
    if (url) line.append(externalLink(label, url));
    else line.textContent = label;
    if (record.excerpt) line.append(document.createTextNode(` — ${record.excerpt}`));
    evidenceList.append(line);
  });
  if (item.qualification_reason) {
    evidenceList.append(element("li", "", `Qualificação: ${item.qualification_reason}`));
  }
  evidence.append(evidenceList);
  content.append(data, sources, evidence);
  return content;
}

function detailGroup(title: string): HTMLElement {
  const group = element("div", "detail-group");
  group.append(element("h4", "", title));
  return group;
}

function addFact(list: HTMLElement, label: string, value: string | null): void {
  if (!value || value === "—" || value === "Local não informado") return;
  list.append(element("li", "", `${label}: ${value}`));
}

function categoryMatch(value: boolean | null): string | null {
  if (value === null) return null;
  return value ? "Categoria compatível" : "Categoria não confirmada";
}

function externalLink(label: string, url: string): HTMLAnchorElement {
  const anchor = element("a", "", label) as HTMLAnchorElement;
  anchor.href = url;
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
  return anchor;
}

function mobileLine(label: string, value: string): HTMLElement {
  const line = element("div", "result-mobile-card__line");
  line.append(element("span", "", label), element("span", "", value));
  return line;
}

function progressMessage(search: Search): string {
  if (search.status === "completed") return `${formatCount(search.companies_count)} empresas consolidadas.`;
  if (search.status === "partial") return "Resultados disponíveis; uma ou mais fontes tiveram limitações.";
  if (search.status === "failed") return "As fontes configuradas não concluíram a pesquisa.";
  if (search.status === "cancelled") return "O processamento foi interrompido.";
  if (search.stage === "discovering") return `${formatCount(search.discovered_count)} registros públicos descobertos.`;
  if (search.stage === "enriching") return `${formatCount(search.enriched_count)} empresas enriquecidas até agora.`;
  return SEARCH_STAGE_LABELS[search.stage];
}

function statusTone(status: Search["status"]): "positive" | "warning" | "danger" | "" {
  if (status === "completed") return "positive";
  if (status === "partial") return "warning";
  if (status === "failed" || status === "cancelled") return "danger";
  return "";
}

function cell(): HTMLTableCellElement {
  return document.createElement("td");
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className = "",
  value?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function iconNode(name: "check" | "alert" | "info" | "x" | "chevron"): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const paths: Record<typeof name, string[]> = {
    check: ["M20 6 9 17l-5-5"],
    alert: ["M12 8v4", "M12 16h.01", "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20"],
    info: ["M12 16v-4", "M12 8h.01", "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20"],
    x: ["M18 6 6 18", "M6 6l12 12"],
    chevron: ["m9 18 6-6-6-6"],
  };
  paths[name].forEach((data) => {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", data);
    svg.append(path);
  });
  return svg;
}

function text(selector: string, value: string): void {
  required<HTMLElement>(selector).textContent = value;
}

function required<T extends Element>(selector: string): T {
  const node = document.querySelector<T>(selector);
  if (!node) throw new Error(`Elemento obrigatório ausente: ${selector}`);
  return node;
}
