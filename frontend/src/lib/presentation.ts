import type {
  ProviderStatus,
  SearchStage,
  SearchStatus,
  WhatsAppStatus,
} from "./types";

export const SEARCH_STATUS_LABELS: Record<SearchStatus, string> = {
  created: "Preparando",
  running: "Em andamento",
  completed: "Concluída",
  partial: "Concluída parcialmente",
  failed: "Falhou",
  cancelled: "Cancelada",
};

export const SEARCH_STAGE_LABELS: Record<SearchStage, string> = {
  created: "Preparando pesquisa",
  discovering: "Consultando fontes públicas",
  enriching: "Enriquecendo empresas",
  validating: "Validando dados e WhatsApp",
  deduplicating: "Removendo duplicatas",
  finalizing: "Finalizando resultados",
  completed: "Pesquisa concluída",
};

export const WHATSAPP_LABELS: Record<WhatsAppStatus, string> = {
  confirmed: "Confirmado",
  unconfirmed: "Não confirmado",
  not_found: "Não encontrado",
};

export const PROVIDER_STATUS_LABELS: Record<ProviderStatus, string> = {
  pending: "Aguardando",
  running: "Consultando",
  completed: "Concluído",
  failed: "Indisponível",
  skipped: "Não configurado",
};

const numberFormatter = new Intl.NumberFormat("pt-BR");
const dateFormatter = new Intl.DateTimeFormat("pt-BR", {
  dateStyle: "short",
  timeStyle: "short",
});

export function formatCount(value: number): string {
  return numberFormatter.format(value);
}

export function formatConfidence(value: number | null): string {
  return value === null ? "—" : `${Math.max(0, Math.min(100, value))}%`;
}

export function formatDate(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : dateFormatter.format(date);
}

export function formatPhone(value: string | null): string {
  if (!value) return "—";
  const digits = value.replace(/\D/g, "");
  const national = digits.startsWith("55") && digits.length >= 12 ? digits.slice(2) : digits;
  if (national.length === 11) {
    return `+55 (${national.slice(0, 2)}) ${national.slice(2, 7)}-${national.slice(7)}`;
  }
  if (national.length === 10) {
    return `+55 (${national.slice(0, 2)}) ${national.slice(2, 6)}-${national.slice(6)}`;
  }
  return value;
}

export function formatLocation(city: string | null, state: string | null): string {
  return [city, state].filter(Boolean).join(" / ") || "Local não informado";
}

export function safeExternalUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

export function buildSearchPayload(
  categoryOrQuery: string,
  location: string,
): { query?: string; category?: string; location?: string } {
  const category = categoryOrQuery.trim();
  const place = location.trim();
  if (!place) return { query: category };
  return { category, location: place };
}

export function publicErrorMessage(error: unknown): string {
  if (!(error instanceof Error)) return "Ocorreu um erro inesperado. Tente novamente.";
  const message = error.message.toLowerCase();
  if (message.includes("conectar") || message.includes("network") || message.includes("fetch")) {
    return "Não foi possível conectar ao backend. Confirme se a API está em execução.";
  }
  if (message.includes("tempo limite") || message.includes("timeout")) {
    return "A operação demorou mais que o esperado. Tente novamente em instantes.";
  }
  if (message.includes("still processing")) {
    return "A pesquisa ainda está sendo processada. Aguarde a conclusão para exportar.";
  }
  return "Não foi possível concluir esta operação. Tente novamente.";
}
