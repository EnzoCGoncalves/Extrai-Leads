export type SearchStatus =
  | "created"
  | "running"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled";

export type SearchStage =
  | "created"
  | "discovering"
  | "enriching"
  | "validating"
  | "deduplicating"
  | "finalizing"
  | "completed";

export type WhatsAppStatus = "confirmed" | "unconfirmed" | "not_found";
export type ProviderStatus = "pending" | "running" | "completed" | "failed" | "skipped";

export interface ProviderRun {
  provider: string;
  display_name: string;
  status: ProviderStatus;
  results_count: number;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface Search {
  id: string;
  query: string;
  category: string | null;
  location: string | null;
  status: SearchStatus;
  stage: SearchStage;
  progress_percent: number;
  discovered_count: number;
  results_count: number;
  companies_count: number;
  whatsapp_count: number;
  confirmed_whatsapp_count: number;
  enriched_count: number;
  ai_qualified_count: number;
  error_message: string | null;
  providers: ProviderRun[];
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface Company {
  id: string;
  name: string;
  phone: string | null;
  whatsapp: string | null;
  whatsapp_status: WhatsAppStatus;
  validation_status: "unverified" | "valid" | "invalid";
  address: string | null;
  city: string | null;
  state: string | null;
  category: string | null;
  website: string | null;
  instagram: string | null;
  cnpj: string | null;
  confidence: number | null;
  collected_at: string;
}

export interface ResultSource {
  provider: string;
  display_name: string;
  source_url: string | null;
  external_id: string | null;
  evidence: Record<string, unknown> | null;
}

export interface WhatsAppEvidence {
  number: string;
  evidence_type: string;
  source: string;
  source_url: string;
  official_source: boolean;
  excerpt: string | null;
  observed_at: string;
}

export interface SearchResult {
  id: string;
  rank: number | null;
  confidence: number | null;
  category_match: boolean | null;
  qualification_confidence: number | null;
  qualification_method: string | null;
  qualification_reason: string | null;
  collected_at: string;
  company: Company;
  sources: string[];
  source_details: ResultSource[];
  whatsapp_evidence: WhatsAppEvidence[];
}

export interface SearchResultsPage {
  items: SearchResult[];
  total: number;
  limit: number;
  offset: number;
}

export interface CreateSearchPayload {
  query?: string;
  category?: string;
  location?: string;
}

export const TERMINAL_SEARCH_STATUSES = new Set<SearchStatus>([
  "completed",
  "partial",
  "failed",
  "cancelled",
]);
