import { describe, expect, it } from "vitest";

import { parseFilename } from "../src/lib/api";
import {
  buildSearchPayload,
  formatConfidence,
  formatLocation,
  formatPhone,
  publicErrorMessage,
  safeExternalUrl,
  WHATSAPP_LABELS,
} from "../src/lib/presentation";

describe("apresentação de dados reais", () => {
  it("monta critérios estruturados sem alterar os termos do usuário", () => {
    expect(buildSearchPayload("  Clínicas odontológicas ", " São Paulo ")).toEqual({
      category: "Clínicas odontológicas",
      location: "São Paulo",
    });
    expect(buildSearchPayload("Restaurantes em Campinas", "")).toEqual({
      query: "Restaurantes em Campinas",
    });
  });

  it("formata telefones brasileiros sem afirmar que são WhatsApp", () => {
    expect(formatPhone("5511999990000")).toBe("+55 (11) 99999-0000");
    expect(formatPhone("551133330000")).toBe("+55 (11) 3333-0000");
    expect(WHATSAPP_LABELS.unconfirmed).toBe("Não confirmado");
    expect(WHATSAPP_LABELS.not_found).toBe("Não encontrado");
  });

  it("mantém ausências explícitas e limita confiança ao intervalo visual", () => {
    expect(formatPhone(null)).toBe("—");
    expect(formatLocation(null, null)).toBe("Local não informado");
    expect(formatConfidence(null)).toBe("—");
    expect(formatConfidence(110)).toBe("100%");
  });

  it("aceita apenas links externos HTTP seguros", () => {
    expect(safeExternalUrl("https://empresa.example/contato")).toBe(
      "https://empresa.example/contato",
    );
    expect(safeExternalUrl("javascript:alert(1)")).toBeNull();
    expect(safeExternalUrl("not-a-url")).toBeNull();
  });

  it("não repassa mensagens internas arbitrárias para o usuário", () => {
    expect(publicErrorMessage(new Error("database password=secret crashed"))).toBe(
      "Não foi possível concluir esta operação. Tente novamente.",
    );
  });
});

describe("download Excel", () => {
  it("lê e sanitiza o nome do Content-Disposition", () => {
    expect(parseFilename("attachment; filename=\"leads-2026.xlsx\"")).toBe("leads-2026.xlsx");
    expect(parseFilename("attachment; filename=\"../../segredo\"")).toBe("..-..-segredo.xlsx");
    expect(parseFilename("attachment; filename=seguro.xlsx; filename*=UTF-8''%E0%A4%A")).toBe(
      "seguro.xlsx",
    );
    expect(parseFilename(null)).toBeNull();
  });
});
