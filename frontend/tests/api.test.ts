import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../src/lib/api";

afterEach(() => vi.unstubAllGlobals());

describe("ApiClient", () => {
  it("cria pesquisa usando somente o contrato público", async () => {
    const response = { id: "abc", query: "Contadores em Recife" };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(response), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient("http://127.0.0.1:8000/");

    await expect(client.createSearch({ query: "Contadores em Recife" })).resolves.toEqual(response);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8000/api/v1/searches");
    expect(init.credentials).toBe("omit");
    expect(init.body).toBe(JSON.stringify({ query: "Contadores em Recife" }));
  });

  it("gera erro tipado e sanitizado quando a API falha", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "search not found" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    const client = new ApiClient("http://api.test");

    await expect(client.getSearch("missing")).rejects.toEqual(
      expect.objectContaining({ status: 404, detail: "search not found" }),
    );
  });

  it("preserva arquivo e contagem retornados pela exportação", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(new Uint8Array([80, 75, 3, 4]), {
          status: 200,
          headers: {
            "Content-Disposition": "attachment; filename=empresas.xlsx",
            "X-Exported-Rows": "475",
          },
        }),
      ),
    );
    const client = new ApiClient("http://api.test");
    const artifact = await client.exportResults("search-id");

    expect(artifact.filename).toBe("empresas.xlsx");
    expect(artifact.rowCount).toBe(475);
    expect(artifact.blob.size).toBe(4);
  });
});
