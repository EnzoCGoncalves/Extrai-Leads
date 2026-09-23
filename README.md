# Extrai Leads

Sistema assíncrono para descobrir, normalizar, enriquecer e deduplicar empresas a partir de
fontes públicas verificáveis.

## Estado atual

A **Etapa 5 — interface web profissional** está implementada. O fluxo real é:

```text
consulta
  → OpenStreetMap/Overpass + Tavily opcional
  → deduplicação inicial
  → enriquecimento limitado do website oficial
  → normalização de telefone e resolução de evidências de WhatsApp
  → deduplicação final e qualificação de categoria
  → resultados persistidos
  → exportação .xlsx auditável sob demanda
  → acompanhamento e download pela interface Astro responsiva
```

Está entregue:

- frontend Astro 7 + TypeScript separado do FastAPI, sem framework de UI no navegador;
- interface responsiva baseada no `design2`, com tema claro/escuro e componentes reutilizáveis;
- pesquisa real por categoria/localidade ou consulta livre, com retomada pelo ID na URL;
- polling controlado com pausa em aba oculta, backoff de reconexão e status por provider;
- estados de carregamento, progresso, concluído, parcial, vazio, erro e exportação;
- resultados paginados, detalhes de fontes, evidências e qualificação sem reinterpretar dados;
- status `confirmed`, `unconfirmed` e `not_found` preservados integralmente no frontend;
- download Excel real com nome retornado pela API e feedback de conclusão/falha;

- interpretação determinística de consultas como `Restaurantes em Campinas`;
- provider OpenStreetMap que usa Nominatim apenas para geocodificar a cidade e Overpass para
  consultar estabelecimentos;
- provider Tavily opcional, com consultas complementares limitadas e parada por baixo ganho;
- cache TTL/LRU das chamadas por provider;
- timeout, retry com backoff, `Retry-After`, limite de concorrência e falha isolada por fonte;
- busca assíncrona com progresso e diagnóstico de cada provider;
- formato interno único para nome, telefone, WhatsApp candidato, endereço, cidade, estado,
  categoria, site, Instagram, CNPJ, fontes e evidências;
- deduplicação por CNPJ, contato, website, nome + endereço e nome + localidade;
- proteção contra fusão de CNPJs conflitantes e filiais homônimas;
- enriquecimento por fusão de campos encontrados em fontes diferentes;
- confiança de 0 a 100 baseada em evidência, corroboração e completude;
- persistência das URLs e evidências de todas as fontes que sustentam cada resultado;
- normalização brasileira de telefone em `55 + DDD + número`, sem inferir disponibilidade;
- estados de WhatsApp `confirmed`, `unconfirmed` e `not_found` com política conservadora;
- evidência de WhatsApp persistida com número, tipo, fonte, URL, trecho e data de observação;
- crawler de website oficial com `robots.txt`, mesma origem, proteção SSRF, rate limit, timeout,
  limites de páginas/bytes/redirects e cache;
- enriquecimento público de telefone, WhatsApp, Instagram, endereço e CNPJ;
- qualificação determinística de categoria e Gemini opcional somente para casos ambíguos;
- chamadas Gemini em lotes pequenos, com dados minimizados, cache, orçamento e fallback local;
- exportação `.xlsx` dos resultados persistidos, em lotes e com escrita de memória limitada;
- download com nome seguro, headers HTTP de segurança e remoção automática do arquivo temporário;
- API paginada e migrações Alembic;
- testes sem chamadas externas, usando transports simulados.

## Política de WhatsApp

O sistema não consulta, enumera nem testa números no WhatsApp e não envia mensagens para
"validá-los". Também não usa WhatsApp Web, sessão, QR code, bypass ou scraping da plataforma.

- `confirmed`: o website oficial atribuído à própria empresa publicou um link de contato
  WhatsApp ou rotulou explicitamente aquele mesmo número como WhatsApp;
- `unconfirmed`: existe um candidato em fonte pública de terceiro, como diretório ou tag
  comunitária, mas falta publicação oficial suficiente;
- `not_found`: não foi encontrada evidência específica de WhatsApp.

Um telefone comum nunca é copiado automaticamente para o campo WhatsApp. `confirmed` significa
"confirmado por evidência pública oficial", não uma verificação técnica em tempo real da conta.
A URL e o motivo permanecem na resposta da API para auditoria.

## Fontes e decisões técnicas

### OpenStreetMap

O provider gratuito principal suporta atualmente:

- restaurantes: `amenity=restaurant`;
- clínicas odontológicas/dentistas;
- contadores/escritórios de contabilidade;
- oficinas mecânicas;
- imobiliárias/corretores de imóveis.

O Nominatim resolve uma localização uma vez e seu resultado fica em cache. O provider respeita o
mínimo de um segundo entre chamadas ao servidor público, envia `User-Agent` identificável e não o
usa para enumerar empresas. O Overpass faz uma consulta unificada por categoria e cidade, sem um
limite artificial de 10, 20 ou 50 itens na query.

O endpoint público é um recurso comunitário e não oferece SLA. Para volume comercial, configure
uma instância própria ou processe extratos OSM. Consulte a
[política do Nominatim](https://operations.osmfoundation.org/policies/nominatim/), a
[documentação do Overpass](https://wiki.openstreetmap.org/wiki/Overpass_API) e as
[regras de atribuição OSM](https://osmfoundation.org/wiki/Licence/Attribution_Guidelines).

### Overture Maps Places

O provider Overture consulta o recorte geográfico da localidade diretamente nos GeoParquet
oficiais, usando o cliente `overturemaps` e o bounding box resolvido pelo mesmo cache Nominatim do
OSM. O conjunto global não é baixado. A release mais recente é descoberta no catálogo oficial e
os resultados idênticos continuam protegidos pelo cache geral da pesquisa.

A seleção usa a taxonomia Overture, confiança mínima e estado operacional. Categorias genéricas
de negócios imobiliários exigem também evidência no nome para não transformar condomínios e
empreendimentos residenciais em imobiliárias. Cada lead preserva GERS ID, release, datasets,
licenças, atribuição, coordenadas e confiança. Consulte o
[guia de Places](https://docs.overturemaps.org/guides/places/) e as
[regras de atribuição](https://docs.overturemaps.org/attribution/).

### Tavily

Quando `TAVILY_API_KEY` está preenchida, a Tavily amplia a descoberta de sites e páginas
públicas. A API limita cada chamada a 20 resultados e não fornece paginação real; por isso o
sistema usa consultas complementares determinísticas e deduplica as URLs. Todas as variações
planejadas são executadas, salvo quando a trava global de itens é alcançada. O padrão é quatro
consultas `basic`, configurável até oito. Consulte a
[Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search),
os [créditos](https://docs.tavily.com/documentation/api-credits) e os
[limites](https://docs.tavily.com/documentation/rate-limits).

### Website oficial

O enriquecimento só eleva evidência a oficial quando o domínio pode ser atribuído à empresa por
uma fonte estruturada ou pelo próprio domínio. Sites sociais, links compartilhados e diretórios
não recebem essa confiança. O crawler acessa somente HTML público na mesma origem, prioriza a
home e páginas de contato/sobre, respeita `robots.txt` e aplica limites configuráveis. Falha em um
site não elimina a empresa nem interrompe as demais.

### Gemini

O Gemini não é fonte de empresas ou contatos. Primeiro roda a classificação determinística; o
modelo `gemini-3.5-flash-lite` só recebe casos ambíguos, agrupados em lotes, e só pode classificar
`category_match`, confiança e motivo a partir das referências fornecidas. A aplicação remove
telefone, WhatsApp, e-mail, CNPJ, URL, endereço, localidade e o próprio nome empresarial do
payload gratuito, valida o JSON estruturado e rejeita referências inexistentes. Timeout, `429`,
erro de autenticação ou resposta inválida mantêm o resultado determinístico.

Agno não foi adicionado: esta etapa é uma classificação estruturada sem ferramentas, memória ou
delegação, e uma camada de agentes aumentaria dependências sem benefício técnico.

No plano gratuito, entradas e saídas podem ser usadas pelo Google para melhoria dos produtos e
submetidas a revisão humana. Por isso a minimização é obrigatória e nenhum dado pessoal,
confidencial ou de contato é enviado. Consulte os
[termos da Gemini API](https://ai.google.dev/gemini-api/terms), os
[limites ativos](https://ai.google.dev/gemini-api/docs/rate-limits) e a
[documentação de saída estruturada](https://ai.google.dev/gemini-api/docs/structured-output).

### Fontes deliberadamente excluídas

- Google Places/Maps não é provider: exige billing e suas políticas restringem armazenamento,
  indexação e redistribuição dos cadastros necessários a este produto.
- Gemini Grounding não é provider: os termos não permitem usar os resultados fundamentados para
  formar um banco persistente de leads.
- Gemini Grounding, busca e ferramentas não são usados. O classificador opcional não pode criar
  empresas, contatos, endereços ou qualquer outro fato.
- Não há scraping de Google Maps, bypass de CAPTCHA/login ou coleta de áreas privadas.

## Arquitetura

```text
src/extrais_leads/
├── api/                 # rotas e dependências HTTP
├── cache/               # contrato e cache TTL/LRU
├── core/                # configuração e logs com redação de segredos
├── db/                  # SQLAlchemy assíncrono
├── models/              # entidades persistentes
├── providers/           # contrato, OpenStreetMap, Overture e Tavily
├── repositories/        # consultas ao banco
├── schemas/             # contratos da API
└── services/            # busca, deduplicação, enrichment, evidências e qualificação
```

`SearchRunner` não conhece payloads proprietários. Todo provider implementa `SearchProvider` e
retorna `ProviderLead`. Isso permite substituir a fonte, adicionar um extrato OSM local ou usar
uma API compatível sem reescrever a API e a deduplicação.

Entidades principais:

- `Search`: consulta, critérios, status, etapa, progresso e contagens;
- `SearchProviderRun`: sucesso, falha e contagem individual de cada provider;
- `Company`: visão consolidada da empresa;
- `SearchResult`: empresa única dentro da pesquisa, rank e confiança;
- `Source` e `ResultSource`: proveniência, URL e evidência pública;
- `ContactEvidence`: prova auditável associada ao WhatsApp daquele resultado.

## Instalação

Requisitos: Python 3.12 e [uv](https://docs.astral.sh/uv/).

```powershell
uv sync
Copy-Item .env.example .env
uv run alembic upgrade head
```

Preencha `TAVILY_API_KEY` para ativar a Tavily e `GEMINI_API_KEY` para permitir a qualificação
opcional de casos ambíguos. OpenStreetMap e Overture funcionam sem chave; configure
`OSM_CONTACT_EMAIL` e um `OSM_USER_AGENT` que identifique sua instalação. Nunca coloque
credenciais no `.env.example` ou no repositório.

Para PostgreSQL:

```powershell
uv sync --extra postgres
```

```dotenv
DATABASE_URL=postgresql+asyncpg://usuario:senha@host/banco
```

## Configuração relevante

| Variável | Padrão | Finalidade |
|---|---:|---|
| `SEARCH_BACKGROUND_ENABLED` | `true` | inicia a pesquisa após o POST |
| `SEARCH_QUERY_VARIATIONS` | `4` | consultas complementares Tavily, máximo 8 |
| `PROVIDER_TIMEOUT_SECONDS` | `45` | timeout externo por tentativa |
| `PROVIDER_MAX_RETRIES` | `2` | retries de falhas transitórias |
| `PROVIDER_MAX_CONCURRENCY` | `3` | providers simultâneos por processo |
| `PROVIDER_MAX_PAGES` | `10` | trava de segurança para providers paginados |
| `PROVIDER_MAX_ITEMS` | `5000` | proteção de memória, não limite da query OSM |
| `CACHE_DEFAULT_TTL_SECONDS` | `86400` | validade de resultados externos idênticos |
| `OSM_ENABLED` | `true` | ativa OpenStreetMap |
| `OSM_REQUEST_INTERVAL_SECONDS` | `1` | intervalo obrigatório do Nominatim público |
| `OVERTURE_ENABLED` | `true` | ativa Overture Maps Places regional |
| `OVERTURE_MIN_CONFIDENCE` | `0.2` | piso de confiança contra registros suspeitos |
| `OVERTURE_USE_STAC` | `false` | usa índice STAC quando a release cobrir corretamente a região |
| `TAVILY_API_KEY` | vazio | ativa Tavily quando configurada |
| `WEBSITE_ENRICHMENT_ENABLED` | `true` | ativa enriquecimento limitado do site oficial |
| `WEBSITE_ENRICHMENT_MAX_COMPANIES` | `250` | orçamento de sites por pesquisa |
| `WEBSITE_ENRICHMENT_MAX_PAGES_PER_SITE` | `3` | páginas públicas por domínio |
| `WEBSITE_ENRICHMENT_MAX_BYTES_PER_PAGE` | `1000000` | limite de bytes de cada HTML |
| `WEBSITE_ENRICHMENT_CONCURRENCY` | `4` | sites enriquecidos simultaneamente |
| `AI_QUALIFICATION_ENABLED` | `true` | permite Gemini somente com chave configurada |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | modelo estável de classificação |
| `GEMINI_MAX_QUALIFICATIONS_PER_SEARCH` | `25` | empresas ambíguas enviadas à IA |
| `GEMINI_QUALIFICATION_BATCH_SIZE` | `10` | empresas por chamada estruturada |
| `GEMINI_API_KEY` | vazio | habilita a etapa opcional de IA |
| `EXCEL_EXPORT_BATCH_SIZE` | `500` | resultados lidos do banco por lote durante a exportação |

Veja todas as opções em `.env.example`.

## Execução e API

### Backend

```powershell
uv run uvicorn extrais_leads.main:app --reload
```

- API: `http://127.0.0.1:8000`;
- health check: `http://127.0.0.1:8000/health`;
- OpenAPI: `http://127.0.0.1:8000/docs`.

### Frontend

Em outro terminal:

```powershell
cd frontend
Copy-Item .env.example .env.local
npm install
npm run dev
```

Acesse `http://127.0.0.1:3000`. A variável pública `PUBLIC_API_BASE_URL` aponta para o backend e
não deve conter chaves, tokens ou credenciais. O servidor FastAPI já permite as origens locais da
porta 3000 e expõe somente os headers necessários para nome e contagem da exportação.

Para retomar uma pesquisa já criada, abra
`http://127.0.0.1:3000/?search=<ID_DA_PESQUISA>`.

Endpoints:

| Método | Endpoint | Resultado |
|---|---|---|
| `POST` | `/api/v1/searches` | cria a pesquisa e agenda a execução |
| `GET` | `/api/v1/searches/{id}` | progresso, contagens e diagnóstico por provider |
| `GET` | `/api/v1/searches/{id}/results` | empresas deduplicadas, fontes e evidências |
| `GET` | `/api/v1/searches/{id}/export.xlsx` | baixa todos os resultados persistidos em Excel |

Exemplo:

```powershell
$body = @{ query = "Restaurantes em Campinas" } | ConvertTo-Json
$search = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/searches `
  -ContentType "application/json" -Body $body
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/searches/$($search.id)"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/searches/$($search.id)/results"
Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/searches/$($search.id)/export.xlsx" `
  -OutFile "empresas.xlsx"
```

Estados terminais:

- `completed`: todos os providers configurados concluíram;
- `partial`: pelo menos um concluiu e outro falhou, ou uma trava operacional foi atingida;
- `failed`: nenhum provider conseguiu concluir;
- `cancelled`: a aplicação encerrou antes do fim.

O GET da pesquisa retorna `discovered_count` (observações únicas dos providers),
`companies_count`/`results_count` (empresas após deduplicação), `whatsapp_count`,
`confirmed_whatsapp_count`, `enriched_count`, `ai_qualified_count`, `progress_percent`,
`providers` e eventuais mensagens de erro. Cada resultado separa a confiança dos dados da
`qualification_confidence` e expõe `qualification_method`, `qualification_reason` e
`whatsapp_evidence`.

## Exportação Excel

O endpoint de exportação aceita pesquisas em estado terminal e gera uma planilha a partir dos
resultados persistidos. Pesquisas ainda em execução retornam HTTP `409`; uma pesquisa concluída
sem empresas produz um arquivo válido contendo apenas os cabeçalhos.

A planilha inclui empresa, telefone, WhatsApp, status e evidências, fontes e URLs públicas,
endereço, localidade, categoria, CNPJ, website, Instagram, métricas de confiança, qualificação,
data UTC e identificadores de auditoria. Dados ausentes permanecem vazios e os valores
`confirmed`, `unconfirmed` e `not_found` não são reinterpretados.

O banco é lido em lotes configuráveis e o workbook usa o modo `write_only` do `openpyxl`.
Consultas acima do limite físico de linhas de uma aba são divididas em abas adicionais. O exportador
não inclui o JSON bruto dos providers, remove credenciais e parâmetros sensíveis de URLs e
neutraliza conteúdo que poderia ser interpretado pelo Excel como fórmula.

## Deduplicação e confiança

A resolução usa apenas dados observados. Identificadores fortes têm precedência, CNPJs
conflitantes nunca são unidos e uma empresa homônima com outro endereço permanece separada.
Valores alternativos e motivos de união continuam no rastro de auditoria.

A confiança considera:

- quantidade de providers independentes;
- URL pública de origem;
- CNPJ, contato, website, endereço e localidade observados;
- concordância do mesmo campo entre fontes;
- evidência estruturada do provider.

Ausência de dado permanece `null`; Gemini ou heurísticas não preenchem fatos inexistentes. A
confiança geral da empresa e a confiança da correspondência de categoria são métricas distintas.

## Testes e auditoria

```powershell
uv run pytest -W error
uv run ruff check .
uv run ruff format --check .
uv run alembic check
```

Frontend:

```powershell
cd frontend
npm test
npm run check
npm run build
npm run test:e2e
```

O E2E usa o Chrome local e valida o fluxo em desktop e mobile com APIs externas simuladas. Para
usar outro executável Chromium compatível, defina `PLAYWRIGHT_CHROME_PATH` apenas no ambiente
local.

Os testes cobrem providers, normalização de telefone, evidência/status de WhatsApp, crawler e
proteção SSRF, `robots.txt`, qualificação determinística/Gemini, sanitização, batching, cache,
deduplicação, persistência, migrações, exportação Excel em lotes e API. APIs externas são simuladas com
`httpx.MockTransport`.

## Limites operacionais

- `MemoryCache` e tarefas assíncronas são locais ao processo. Produção com vários workers
  deve usar Redis e uma fila durável.
- SQLite aceita poucos escritores; para carga real use PostgreSQL.
- O banco dentro do OneDrive pode sofrer contenção. Prefira um caminho local não sincronizado.
- Exportações grandes usam pouca memória, mas ainda exigem espaço temporário em disco até o fim
  do download.
- Resultados OSM dependem da cobertura colaborativa existente na região.
- Overture é atualizado em releases mensais e pode conter registros incompletos ou duplicados;
  o sistema preserva a proveniência e aplica filtros/deduplicação, mas não presume perfeição.
- Sites sem atribuição segura, bloqueados por `robots.txt`, indisponíveis ou acima do orçamento
  permanecem sem enriquecimento; o sistema não contorna a restrição.
- A evidência pública de WhatsApp pode ficar desatualizada e não equivale a consultar a conta em
  tempo real.
- No plano gratuito do Gemini, mantenha a sanitização habilitada e não envie dados pessoais ou
  confidenciais.
- A plataforma localiza empresas; ela não autoriza spam nem automação de mensagens não
  solicitadas. Respeite LGPD, termos das fontes e políticas de contato.
