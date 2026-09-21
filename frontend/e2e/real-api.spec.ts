import { expect, test } from "@playwright/test";

const searchId = process.env.REAL_SEARCH_ID;

test.skip(!searchId, "Defina REAL_SEARCH_ID para executar o smoke test contra a API local real.");

test("carrega uma pesquisa persistida e baixa o Excel pela API real", async ({ page }) => {
  await page.goto(`/?search=${searchId}`);
  await expect(page.getByRole("heading", { name: "Empresas encontradas" })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.locator("[data-results-section]")).toContainText("empresas únicas");

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Exportar Excel" }).click();
  const artifact = await download;
  expect(artifact.suggestedFilename()).toMatch(/\.xlsx$/i);
  await expect(page.getByText(/Planilha gerada com [\d.]+ registros\./)).toBeVisible();
});
