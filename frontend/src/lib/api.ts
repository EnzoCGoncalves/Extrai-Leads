import type { CreateSearchPayload, Search, SearchResultsPage } from "./types";

const DEFAULT_TIMEOUT_MS = 20_000;

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export interface ExportArtifact {
  blob: Blob;
  filename: string;
  rowCount: number | null;
}

export class ApiClient {
  readonly baseUrl: string;

  constructor(baseUrl: string, private readonly timeoutMs = DEFAULT_TIMEOUT_MS) {
    this.baseUrl = baseUrl.trim().replace(/\/+$/, "");
  }

  async createSearch(payload: CreateSearchPayload, signal?: AbortSignal): Promise<Search> {
    return this.request<Search>("/api/v1/searches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
    });
  }

  async getSearch(searchId: string, signal?: AbortSignal): Promise<Search> {
    return this.request<Search>(`/api/v1/searches/${encodeURIComponent(searchId)}`, { signal });
  }

  async getResults(
    searchId: string,
    offset: number,
    limit: number,
    signal?: AbortSignal,
  ): Promise<SearchResultsPage> {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    return this.request<SearchResultsPage>(
      `/api/v1/searches/${encodeURIComponent(searchId)}/results?${params}`,
      { signal },
    );
  }

  async exportResults(searchId: string, signal?: AbortSignal): Promise<ExportArtifact> {
    const response = await this.fetch(
      `/api/v1/searches/${encodeURIComponent(searchId)}/export.xlsx`,
      { signal },
      120_000,
    );
    if (!response.ok) {
      throw await responseError(response);
    }

    const headerFilename = parseFilename(response.headers.get("Content-Disposition"));
    const rows = Number.parseInt(response.headers.get("X-Exported-Rows") ?? "", 10);
    return {
      blob: await response.blob(),
      filename: headerFilename ?? `extrais-leads-${searchId}.xlsx`,
      rowCount: Number.isFinite(rows) ? rows : null,
    };
  }

  private async request<T>(path: string, init: RequestInit): Promise<T> {
    const response = await this.fetch(path, init, this.timeoutMs);
    if (!response.ok) {
      throw await responseError(response);
    }
    return (await response.json()) as T;
  }

  private async fetch(path: string, init: RequestInit, timeoutMs: number): Promise<Response> {
    const callerSignal = init.signal;
    const timeout = AbortSignal.timeout(timeoutMs);
    const signal = callerSignal ? AbortSignal.any([callerSignal, timeout]) : timeout;
    try {
      return await fetch(`${this.baseUrl}${path}`, {
        ...init,
        credentials: "omit",
        headers: { Accept: "application/json", ...init.headers },
        signal,
      });
    } catch (error) {
      if (callerSignal?.aborted) throw error;
      if (error instanceof DOMException && error.name === "AbortError") {
        throw new ApiError(0, "A operação excedeu o tempo limite.");
      }
      throw new ApiError(0, "Não foi possível conectar ao servidor.");
    }
  }
}

async function responseError(response: Response): Promise<ApiError> {
  let detail = `A API respondeu com status ${response.status}.`;
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail.trim()) {
      detail = body.detail;
    }
  } catch {
    // Mantém a mensagem HTTP sanitizada se a resposta não for JSON.
  }
  return new ApiError(response.status, detail);
}

export function parseFilename(contentDisposition: string | null): string | null {
  if (!contentDisposition) return null;
  const encoded = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const plain = contentDisposition.match(/filename="?([^";]+)"?/i)?.[1];
  let candidate = plain;
  if (encoded) {
    try {
      candidate = decodeURIComponent(encoded);
    } catch {
      candidate = plain;
    }
  }
  if (!candidate) return null;
  candidate = candidate.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "-").trim();
  if (!candidate.toLowerCase().endsWith(".xlsx")) candidate += ".xlsx";
  return candidate.slice(0, 160);
}
