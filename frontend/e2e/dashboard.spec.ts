import { expect, test, type Page, type Route } from "@playwright/test";

const searchId = "11111111-1111-4111-8111-111111111111";
const now = "2026-09-21T15:00:00Z";

const completedSearch = {
  id: searchId,
  query: "Clínicas odontológicas em São Paulo",
  category: "Clínicas odontológicas",
  location: "São Paulo",
  status: "completed",
  stage: "completed",
  progress_percent: 100,
  discovered_count: 4,
  results_count: 2,
  companies_count: 2,
  whatsapp_count: 2,
  confirmed_whatsapp_count: 1,
  enriched_count: 2,
  ai_qualified_count: 0,
  error_message: null,
  providers: [
    {
      provider: "openstreetmap",
      display_name: "OpenStreetMap",
      status: "completed",
      results_count: 2,
      error_message: null,
      started_at: now,
      completed_at: now,
    },
  ],
  created_at: now,
  updated_at: now,
  started_at: now,
  completed_at: now,
};

const runningSearch = {
  ...completedSearch,
  status: "running",
  stage: "discovering",
  progress_percent: 30,
  results_count: 0,
  companies_count: 0,
  whatsapp_count: 0,
  confirmed_whatsapp_count: 0,
  enriched_count: 0,
  completed_at: null,
  providers: completedSearch.providers.map((provider) => ({
    ...provider,
    status: "running",
    results_count: 0,
    completed_at: null,
  })),
};

const results = {
  total: 2,
  limit: 50,
  offset: 0,
  items: [
    result("Clínica São José", "confirmed", "5511999990000", true),
    result("Odonto Centro", "unconfirmed", "5511988880000", false),
  ],
};

test("pesquisa, exibe evidências e exporta os dados reais", async ({ page }, testInfo) => {
  let polls = 0;
  await mockApi(page, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "POST" && url.pathname.endsWith("/searches")) {
      await route.fulfill({ status: 201, json: runningSearch });
    } else if (url.pathname.endsWith("/results")) {
      await route.fulfill({ status: 200, json: results });
    } else if (url.pathname.endsWith("/export.xlsx")) {
      await route.fulfill({
        status: 200,
        body: Buffer.from([80, 75, 3, 4]),
        headers: {
          "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          "Content-Disposition": "attachment; filename=clinicas.xlsx",
          "Access-Control-Expose-Headers": "Content-Disposition, X-Exported-Rows",
          "X-Exported-Rows": "2",
        },
      });
    } else if (url.pathname.includes(`/searches/${searchId}`)) {
      polls += 1;
      await route.fulfill({ status: 200, json: polls > 0 ? completedSearch : runningSearch });
    } else {
      await route.abort();
    }
  });

  await page.goto("/");
  await page.getByLabel("O que você procura?").fill("Clínicas odontológicas");
  await page.getByLabel("Onde?").fill("São Paulo");
  await page.getByRole("button", { name: "Buscar empresas" }).click();

  await expect(page.getByRole("heading", { name: "Empresas encontradas" })).toBeVisible();
  await expect(page.locator(".metric-card").filter({ hasText: "empresas únicas" })).toContainText("2");
  await expect(page.locator(".whatsapp-badge:visible").filter({ hasText: /^Confirmado$/ }).first()).toBeVisible();
  await expect(
    page.locator(".whatsapp-badge:visible").filter({ hasText: /^Não confirmado$/ }).first(),
  ).toBeVisible();

  if (testInfo.project.name === "desktop") {
    await page.getByRole("button", { name: "Ver detalhes de Clínica São José" }).click();
    await expect(page.getByRole("link", { name: "Site oficial" }).first()).toBeVisible();
    await expect(page.getByText("Fonte oficial · website").first()).toBeVisible();
  } else {
    await expect(page.locator("[data-mobile-results]")).toBeVisible();
  }

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Exportar Excel" }).click();
  await download;
  await expect(page.getByText("Planilha gerada com 2 registros.")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("trata pesquisa concluída sem resultados", async ({ page }) => {
  const emptySearch = {
    ...completedSearch,
    results_count: 0,
    companies_count: 0,
    whatsapp_count: 0,
    confirmed_whatsapp_count: 0,
    enriched_count: 0,
  };
  await mockApi(page, async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === "POST") await route.fulfill({ status: 201, json: emptySearch });
    else if (url.pathname.endsWith("/results")) {
      await route.fulfill({ status: 200, json: { items: [], total: 0, limit: 50, offset: 0 } });
    } else await route.fulfill({ status: 200, json: emptySearch });
  });
  await page.goto("/");
  await page.getByLabel("O que você procura?").fill("Categoria inexistente");
  await page.getByLabel("Onde?").fill("Campinas");
  await page.getByRole("button", { name: "Buscar empresas" }).click();
  await expect(page.getByRole("heading", { name: "Nenhuma empresa foi encontrada." })).toBeVisible();
});

test("mostra erro sanitizado quando o backend está indisponível", async ({ page }) => {
  await mockApi(page, async (route) => {
    await route.fulfill({ status: 500, json: { detail: "internal database password=secret" } });
  });
  await page.goto("/");
  await page.getByLabel("O que você procura?").fill("Restaurantes");
  await page.getByLabel("Onde?").fill("Campinas");
  await page.getByRole("button", { name: "Buscar empresas" }).click();
  await expect(page.getByText("Não foi possível concluir esta operação. Tente novamente.")).toBeVisible();
  await expect(page.getByText("password=secret")).toHaveCount(0);
});

async function mockApi(page: Page, handler: (route: Route) => Promise<void>): Promise<void> {
  await page.route("http://127.0.0.1:8000/**", handler);
}

function result(name: string, whatsappStatus: string, whatsapp: string, official: boolean) {
  return {
    id: crypto.randomUUID(),
    rank: 1,
    confidence: official ? 94 : 71,
    category_match: true,
    qualification_confidence: 95,
    qualification_method: "deterministic",
    qualification_reason: "A página pública informa atendimento odontológico.",
    collected_at: now,
    company: {
      id: crypto.randomUUID(),
      name,
      phone: "551133330000",
      whatsapp,
      whatsapp_status: whatsappStatus,
      validation_status: "valid",
      address: "Av. Paulista, 1000 — São Paulo/SP",
      city: "São Paulo",
      state: "SP",
      category: "Clínica odontológica",
      website: "https://clinica.example/",
      instagram: "https://instagram.com/clinica",
      cnpj: "12345678000190",
      confidence: official ? 94 : 71,
      collected_at: now,
    },
    sources: ["Site oficial"],
    source_details: [
      {
        provider: "website",
        display_name: "Site oficial",
        source_url: "https://clinica.example/contato",
        external_id: null,
        evidence: null,
      },
    ],
    whatsapp_evidence: official
      ? [
          {
            number: whatsapp,
            evidence_type: "direct_link",
            source: "website",
            source_url: "https://clinica.example/contato",
            official_source: true,
            excerpt: "Fale conosco pelo WhatsApp",
            observed_at: now,
          },
        ]
      : [],
  };
}
