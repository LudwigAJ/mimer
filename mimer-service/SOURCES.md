# SOURCES.md

## Purpose

This document catalogs candidate sources for financial data that can be used for market data, fund data, instrument reference data, holdings, dividends/distributions, documents, and related analytics. The focus is on being practically useful for developers building importers, security-master/identifier workflows, price ingestion, and fund-specific workflows for ETFs, mutual funds, equities/stocks, FX, rates, and later derivatives. All sources below were checked against public documentation or official product pages on **2026-06-20**; unclear information is explicitly marked as `needs verification`. citeturn43view0turn22search8turn20view0

Production systems should **cache responsibly**, store **source/provenance**, **as-of date**, actual **fetch time**, and observed **freshness** for every datapoint. Tests should use **fixtures/mocks** rather than live calls to external APIs. citeturn11view2turn13search11turn22search6

## Source selection principles

Start with **identifier/security-master sources** before trying to match on ticker alone. A ticker is not an identity: the same ticker can be reused, can mean different things in different markets, or can represent different share classes. For funds, therefore, preserve at least ticker, exchange, MIC or exchCode, the correct currency, the legal fund entity, and ideally ISIN and/or FIGI. OpenFIGI is the clearest free candidate for normalizing this. citeturn43view0turn43view1turn43view2

For **fund facts, holdings, distributions, and documents**, first prefer **official issuer sources**. For **tradeable prices**, first prefer **exchange or market-data sources**. For **FX** and **yield curves**, use central banks or government sources where possible, for example the ECB, Bank of England, and U.S. Treasury. Free/public data can be delayed, limited, fragile, or licensing-restricted; robust solutions for bond, options, and futures data are often paid data. citeturn30search5turn31search9turn30search1turn32search0turn28search0

Live APIs should not be used as test dependencies. Market-data APIs in particular publish explicit rate limits, quotas, or bandwidth limits, and several data sources require licenses or restrict redistribution/display. Therefore, always preserve raw responses or normalized fixtures for regression testing. citeturn43view1turn9view3turn22search6turn13search0turn11view2

## Summary table

| Source | Category | Asset classes | Data types | Free/Freemium/Paid | Auth required | API/docs URL | Recommended use | Caveats |
|---|---|---|---|---|---|---|---|---|
| OpenFIGI | identifier/security master | equities, ETFs, funds, bonds, derivatives, etc. | FIGI mapping, ticker/exchange to FIGI, ISIN to FIGI | Free/Freemium | API key optional but recommended | `https://www.openfigi.com/api/documentation` | Primary identifier normalization | FIGI is not the same as ISIN; licensed identifiers such as CUSIP/SEDOL may be missing or require other sources |
| Vanguard official product pages | issuer/fund facts, docs, NAV | ETFs/funds | fund facts, NAV, market price, documents, sometimes price download | Free/public | No | `https://www.vanguard.co.uk/professional/product/etf/...` | UCITS facts and documents directly from issuer | Pages and URL patterns can be jurisdiction-specific |
| iShares/BlackRock product pages | issuer/fund facts, holdings, docs, premium/discount | ETFs | facts, holdings, documents, premium/discount, data download | Free/public | No | `https://www.ishares.com/.../products/...` | Very strong source for ETF facts and holdings | Pages are JS-heavy; exact download endpoints vary |
| J.P. Morgan AM product pages | issuer/fund facts | ETFs/funds | fund facts, documents | Free/public | No | `https://am.jpmorgan.com/.../products/...` | Official fund attributes and documents | Holdings/distribution flows need separate verification |
| SPDR/State Street product pages/factsheets | issuer/fund facts, docs | ETFs | factsheets, benchmark, TER, distribution frequency | Free/public | No | `https://www.ssga.com/...` | Official SPDR facts, especially for US/UCITS | PDF-heavy workflows |
| FMP | holdings, fund info, quotes, disclosures | ETFs, mutual funds, equities, FX, indices, commodities | ETF/fund holdings, info, quotes, disclosures | Freemium/Paid | API key | `https://site.financialmodelingprep.com/developer/docs` | Fast API integration when official issuer feed is missing | Display/redistribution requires separate license agreement |
| Alpha Vantage | market prices, FX, options, corporate actions | equities, ETFs, mutual funds, FX, commodities, options | daily adjusted, quotes, FX, options, listing status | Freemium/Paid | API key | `https://www.alphavantage.co/documentation/` | Easy starting point for EOD/prices and FX | Free tier is very small; US realtime/delay is premium |
| Tiingo | market prices, corporate actions, FX | equities, ETFs, mutual funds, FX | EOD, dividends, splits, FX | Freemium/Paid | API token | `https://www.tiingo.com/documentation/` | Clean EOD feed with corporate actions | Documentation is JS-heavy; some details are easiest to see in snippets |
| Finnhub | prices, ETF/mutual fund metadata, FX | equities, ETFs, mutual funds, FX, some bond/alternative data | quotes, forex rates, ETF holdings, mutual fund profile, ISIN-related fields | Freemium/Paid | API key | `https://finnhub.io/docs/api` | Broad secondary source when you need one API | Exact endpoint patterns should be double-checked against current docs |
| Massive / Polygon | market prices, options, futures, indices, reference | US equities, options, futures, indices, forex | trades, quotes, aggs, snapshots, reference | Freemium/Paid | API key | `https://massive.com/docs` | Strong source for US market data and later derivatives | Free plans are heavily limited; primarily US |
| Nasdaq Data Link | market prices, options, central-bank data, tables | multiple asset classes depending on dataset | tables API, bulk CSV, real-time/delayed APIs depending on product | Free + Paid datasets | API key for full use | `https://docs.data.nasdaq.com/` | Dataset hub; good for official and specialized datasets | Dataset-specific licensing; uneven coverage |
| Stooq | market prices | equities, ETFs, indices, FX, commodities | historical CSV files | Free/public | No, or apikey flow for some CSV links | `https://stooq.com/db/` | Practical free EOD/backfill | Not a stable official developer platform; scraping-like usage should be avoided |
| Yahoo Finance web/endpoints | market prices, options, FX, index pages | equities, ETFs, options, FX, indices | history, options pages, web data | Free/public | Often no | `https://finance.yahoo.com/` | Research/prototyping, not primary production | Unofficial for API usage; rights and stability uncertain |
| ECB | FX | FX | reference rates, SDMX API | Free/public | No | `https://data.ecb.europa.eu/help/api/data` | Official EUR-based FX source | Not transaction rates; published for information purposes |
| Bank of England | FX, yields | FX, sterling rates, yield curves | CSV/XLSX/JSON, yield curves, bank rate | Free/public | No | `https://www.bankofengland.co.uk/boeapps/database/` | Official GBP-related rates | Not official transaction rates for spot FX |
| U.S. Treasury | bonds/fixed income | US Treasuries | par yield curves, bill rates, XML/CSV | Free/public | No | `https://home.treasury.gov/policy-issues/financing-the-government/interest-rate-statistics` | Official USD curve | Covers government rates, not broad corporate bond pricing |
| UK DMO | bonds/fixed income | gilts, T-bills | gilt reference/prices/yields, ISINs, issue data | Free/public + non-commercial caveats | No | `https://www.dmo.gov.uk/data/` | UK gilt reference and issuance data | FTSE-Tradeweb prices have usage restrictions |
| FRED | bonds/fixed income, macro | rate series, macro | observations, releases, metadata | Free/public | API key | `https://fred.stlouisfed.org/docs/api/fred/` | Complementary curves/macro for analytics | Not market best-execution prices |
| Databento | futures, options, symbology | futures, options, equities | market data, continuous contracts, symbol definitions | Paid/Free trial | API key | `https://databento.com/docs` | Strong candidate for later futures/options | Commercial service; requires payment for serious use |
| CME Group APIs | futures, options on futures | CME/CBOT/NYMEX/COMEX | real-time top-of-book, reference data | Paid/commercial access | Yes | `https://www.cmegroup.com/market-data/market-data-api.html` | Official source if CME data is needed directly from source | Access/process requires onboarding/license |
| Eurex Reference Data API | futures/options reference | Eurex derivatives | products, contracts, trading hours, expirations | Free/public for reference data | Not clear for public API | `https://www.eurex.com/ex-en/data/free-reference-data-api` | Contract metadata | Market prices require other feeds/licenses |
| ICE Developer Center / reports | futures/options, data services | ICE markets | API products, reports, volume/OI | Paid/commercial | Yes | `https://developer.theice.com/hc/en-us` | Official ICE entry point | Public pricing details are limited in open documentation |
| Tradier | options, broker imports | US equities/options | options chains, expirations, strikes, history, account/positions | Freemium/Brokerage-tied | Bearer token | `https://docs.tradier.com/` | US options chain + broker/account import | Realtime requires a Tradier Brokerage account |
| Saxo OpenAPI | broker/user imports, portfolio, reference data | multi-asset brokerage account | positions, net positions, balances, closed positions, reference data | Paid/account-based | OAuth | `https://www.developer.saxo/openapi/referencedocs` | Strong model for portfolio import from broker | Partner-/account-dependent access |
| IBKR Client Portal / TWS / Flex | broker/user imports, portfolio | multi-asset brokerage account | positions, intra-day portfolio, statements/Flex XML/CSV/TXT | Account-based | Session/auth/token | `https://ibkrcampus.com/campus/ibkr-api-page/webapi-ref/` | Strong source for portfolio import and transaction history | Multiple API paths; Flex endpoints need careful versioning |
| Trading 212 CSV/API Beta | broker/user imports | investment account | account/history/orders/portfolio via API beta; CSV exports | Account-based | API key or app login | `https://helpcentre.trading212.com/` | Simple retail import | Public API is beta; coverage/policy can change |

## Detailed source catalog

### Identifier / security master / crosswalk sources

**OpenFIGI**  
**Type:** official API  
**Pricing:** free; higher rate limit with API key  
**Authentication:** optional but recommended via `X-OPENFIGI-APIKEY`  
**Checked:** 2026-06-20  
**Docs:** `https://www.openfigi.com/api/documentation`  
**OpenAPI schema:** `https://api.openfigi.com/schema`  
**Base URL:** `https://api.openfigi.com`  

OpenFIGI is the most important free candidate for mapping third-party identifiers to FIGI. The documentation verifies `POST /v3/mapping`, an array-based request format, support for fields including `idType`, `idValue`, `exchCode`, `micCode`, `currency`, `marketSecDes`, `securityType`, and `securityType2`, and that responses can contain `figi`, `ticker`, `name`, `exchCode`, `shareClassFIGI`, `compositeFIGI`, `securityType`, `securityType2`, and `securityDescription`. It is therefore a good first building block for symbol normalization before price or fund retrieval. citeturn43view0turn43view1turn43view2

Examples of verified request formats:

```text
POST https://api.openfigi.com/v3/mapping
Content-Type: application/json

[
  {"idType":"TICKER","idValue":"IBM","exchCode":"US"}
]
```

```text
POST https://api.openfigi.com/v3/mapping
Content-Type: application/json

[
  {"idType":"ID_ISIN","idValue":"US4592001014"}
]
```

```text
POST https://api.openfigi.com/v3/mapping
Content-Type: application/json
X-OPENFIGI-APIKEY: YOUR_API_KEY

[
  {"idType":"TICKER","idValue":"VUSA","micCode":"XLON"}
]
```

Published rate limits are relatively clear: without an API key, the Mapping API is stated as **25 requests/minute** and Search/Filter as **5 requests/minute**; with an API key, the Mapping API is stated as **25 requests per 6 seconds** and Search/Filter as **20 requests/minute**. The documentation also shows max jobs per request and rate-limit headers. Note that the docs mention different mapping job limits in different places; handle this defensively and preferably verify against the current schema/runtime behavior before production use. citeturn43view0turn43view1turn43view2

**Recommended use:**  
use OpenFIGI to normalize instruments to FIGI and disambiguate ticker + exchange/MIC before retrieving prices, holdings, or corporate actions from other sources. **Limitation:** OpenFIGI is not a full security master for all licensed identifiers and should not be the only source of truth for complete CUSIP/SEDOL/RIC crosswalks. If you need complete commercial crosswalks, this area remains a gap in this draft and commercial sources are **`needs verification`**. citeturn43view1turn43view2

**Secondary, practical reference sources**  
- **Massive / Polygon `GET /v3/reference/tickers`** provides useful symbol metadata for supported tickers, market, currency, and active status, but is not a full ISIN/FIGI/CUSIP crosswalk. It is a good complementary reference for US-centered workflows. citeturn20view1  
- **Finnhub** shows in official docs snippets that some profiles contain **ISIN**, but the field is not globally available and sometimes requires entitlement; use it as a secondary enriched metadata source, not as a canonical identifier master. citeturn16search3turn19search0  
- **Databento** has strong symbology documentation, including continuous contracts and definition schemas for derivatives, but this draft does not verify that the service can replace a general cross-asset security master for funds/ETFs. citeturn33search4turn33search8

### ETF / mutual fund issuer facts sources

**Vanguard**  
**Type:** official issuer site  
**Pricing:** free/public  
**Authentication:** no for public product pages  
**Checked:** 2026-06-20  
**Example product:** `https://www.vanguard.co.uk/professional/product/etf/equity/9503/sp-500-ucits-etf-usd-distributing`  

Vanguard's official product pages verifiably show **NAV**, **market price**, timestamps, and downloadable price series at product level. The verified VUSA page shows daily NAV in USD, market price in GBP, and an explicit “Download prices” function. This makes Vanguard a strong official source for fund facts, documents, and product-level history for UCITS products. VUSA/VWRP/VWRA-like product URLs exist, but exact jurisdiction-specific URL patterns for every share class should always be found through the official search/fund explorer rather than hard-coded. citeturn4view1turn28search4

**Important fields that appear to be available on product pages or documents:** fund name, NAV, market price, price/NAV date; other central fund fields such as TER/OCF, benchmark, distribution policy, and number of holdings often appear, but this draft does not verify them consistently for every Vanguard product and therefore marks them partially as `needs verification`. The document generator in Vanguard Investor UK also makes it possible to generate/download statements, valuations, and other reports in a logged-in context. citeturn4view1turn42search2

**iShares / BlackRock**  
**Type:** official issuer site  
**Pricing:** free/public  
**Authentication:** no for public product pages  
**Checked:** 2026-06-20  
**Example products:**  
- UK/UCITS ISF: `https://www.ishares.com/uk/individual/en/products/251795/ishares-ftse-100-ucits-etf-inc-fund`  
- US IVV: `https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf`  

iShares is probably the most productive official issuer source in this list. Verified product pages show that iShares provides **Key Facts**, **Holdings**, **Performance**, **Fees**, **Documents**, **Prospectus PDF**, and **Data Download Excel** on at least many US ETF pages; the IVV page also shows **Closing Price**, **Net Assets of Fund**, **Premium/Discount**, **Distribution Frequency**, benchmark, and exchange. UK/UCITS pages also show fund information and download functions, but they are more JS-heavy and may be harder to read programmatically. citeturn6search3turn6search5turn6search2

**Practical recommendation:** use iShares product pages and their document links as the primary source for US ETF/UCITS ETF facts, HTML-readable holdings summaries, PDF factsheets, and Excel downloads. **Caveat:** the exact download endpoint for holdings/distributions should be versioned cautiously and feature-flagged, because iShares' web structure may change. Jurisdiction variants (`/uk/individual`, `/us`, etc.) must be handled explicitly. citeturn6search3turn6search5turn5view2

**J.P. Morgan Asset Management**  
**Type:** official issuer site  
**Checked:** 2026-06-20  
**Example:** `https://am.jpmorgan.com/gb/en/asset-management/adv/products/jpm-global-equity-premium-income-active-ucits-etf-usd-dist-ie0003uvyc20`  

JPM AM has official product URLs for ETFs; JEGP/JEPG-related products can be found both on the issuer side and on LSE listing pages. In this draft, the existence of the official product URL itself is verified, but deeper machine-readable downloads for holdings, distribution history, and document patterns are not fully verified and should be marked `needs verification` until tested per product/fund family. citeturn4view2turn4view3

**State Street / SPDR**  
**Type:** official issuer site + official factsheet PDFs  
**Checked:** 2026-06-20  
**Examples:**  
- SPY product: `https://www.ssga.com/us/en/intermediary/etfs/state-street-spdr-sp-500-etf-trust-spy`  
- SPY/UCITS factsheet PDF example: `https://www.ssga.com/library-content/products/factsheets/etfs/emea/factsheet-emea-en_gb-spy5-gy.pdf`  

SPDR/SSGA is strong for **PDF-based factsheets**. The verified UCITS factsheet includes, among other fields, **ISIN**, **Index Name**, **Index Ticker**, **TER**, **Income Treatment**, **Domicile**, **fund/base currency**, and number of constituents in the benchmark. The SPY product pages also confirm benchmark and investment objective description. For developers, this means SPDR works well for fund facts and document archiving, but extra work is needed for PDF extraction if you want to normalize the same fields as from HTML-based issuer sites. citeturn7search4turn7search13turn7search16

**Invesco**  
Official issuer source is in scope, but this draft does not verify a specific public product/download URL at detailed level. **`needs verification`** for exact holdings/distribution/document endpoints.  

**Amundi**  
**Type:** official issuer site  
**Checked:** 2026-06-20  
**Docs/library:** `https://www.amundietf.lu/`  

Amundi verifies a **document library** with downloadable **KIDs, factsheets, and prospectuses**. A product page in the Luxembourg environment has also been verified. For developers, this is promising for document retrieval, but exact holdings/distribution APIs or stable CSV/JSON downloads are not verified here. citeturn8search3turn8search11turn8search19

**Xtrackers / DWS**  
**Type:** official issuer site  
**Checked:** 2026-06-20  
**Site:** `https://www.xtrackers.com/`  

Xtrackers' official website is verified, including general ETF pages. However, this draft does not verify specific holdings/distribution/document endpoints per fund. **`needs verification`** for exact adapter points. citeturn7search3

**WisdomTree**  
**Type:** official issuer site + official PDF documents  
**Checked:** 2026-06-20  
**Site:** `https://www.wisdomtree.eu/en-gb`  

WisdomTree Europe verifies product and distribution documents, including a published **distribution schedule PDF**. Product lists and product detail pages for UCITS ETFs are available. This makes WisdomTree useful for distribution calendars and product facts, but machine-readable holdings feeds are not fully verified in this draft. citeturn8search0turn8search8turn8search16

**VanEck**  
**Type:** official issuer site  
**Checked:** 2026-06-20  
**Site:** `https://www.vaneck.com/uk/en/`  

VanEck's product and fund pages are verified, including text stating that prospectuses and KIID/KID are available free of charge. The exact download pattern for holdings or distribution history has not been verified here. **`needs verification`** for automated integration. citeturn8search9turn8search13turn8search5

**Legal & General / LGIM**  
**Type:** official issuer site/fund centre  
**Checked:** 2026-06-20  
**Site:** `https://fundcentres.landg.com/en/uk/private-investors/fund-centre/etf/`  

LGIM's ETF fund centre is verified as an official entry point, but this draft does not verify a stable public holdings or distributions API. Expect documents and fund facts via official fund-centre pages, but **`needs verification`** for machine-readable exports. citeturn8search18turn8search2

**UBS Asset Management**  
UBS may be relevant, but it was not verified sufficiently in this session. **`needs verification`**.

### ETF holdings / constituents sources

**iShares / BlackRock official holdings/data downloads**  
For iShares, product pages verify that **Holdings** and **Data Download Excel** are available on at least several US fund pages. For developers, it is often better to retrieve holdings directly from the issuer rather than from third-party APIs, because as-of date, the fund's legal identity, and related disclaimers come with it. For IVV, it is also verified that the holdings section and Data Download Excel are exposed in the UI. **The exact download URL for Excel/CSV should, however, be discovered dynamically or kept configurable.** citeturn6search3turn6search2turn6search9

**Vanguard official product pages**  
Vanguard's verified product pages explicitly show NAV/price history and price download, but this draft does not verify an equally clear public holdings CSV API as with some other issuers. Vanguard should still be treated as a **primary official source** for fund facts and documents, and a **secondary candidate** for holdings where such export exists per fund. **`needs verification`** per specific fund family. citeturn4view1

**J.P. Morgan AM**  
Official producer of fund facts and ETF product pages, but holdings download per UCITS ETF was not verified sufficiently here. **`needs verification`**. citeturn4view2

**Financial Modeling Prep ETF & Fund Holdings API**  
**Type:** official commercial API  
**Checked:** 2026-06-20  
**Docs:** `https://site.financialmodelingprep.com/developer/docs/stable/holdings`  
**Endpoint example:** `https://financialmodelingprep.com/stable/etf/holdings?symbol=SPY&apikey=YOUR_API_KEY`  

FMP's official documentation verifies that its holdings API for ETFs/funds returns details such as **asset names**, **symbols**, **ISINs**, **market values**, **weight percentages**, and for example share count for a holding such as AAPL in SPY. FMP describes holdings as a real-time updated information product, but as a developer you should treat this type of third-party holdings data as a **secondary source** behind the issuer's official files when precision is business-critical. FMP is practical when you want a quick homogeneous API surface across many funds. citeturn9view0turn9view3

**Fields to normalize for holdings adapters**  
The minimum normalized fields should be: `fund_identifier`, `fund_isin`, `holding_name`, `holding_ticker`, `holding_isin/cusip/sedol/figi` where available, `sector`, `country`, `currency`, `weight`, `market_value`, `quantity/shares`, `as_of_date`, `source_url`. OpenFIGI can be used afterwards to enrich holding identification when the raw source does not provide FIGI. citeturn9view0turn43view2

### ETF / fund distributions / dividends sources

**Official issuer pages first**  
For ETF/fund distributions, the issuer's distribution pages or documents should be prioritized over general stock-dividend APIs. WisdomTree verifies a **distribution schedule PDF**. iShares product environments expose distribution-related information and Documents/Data Download. Vanguard has document generators and product/price history; however, distribution history for each fund still needs to be verified per fund. SPDR factsheets at least show distribution frequency. citeturn8search16turn6search3turn42search2turn7search13

**Tiingo corporate actions**  
**Type:** official API  
**Checked:** 2026-06-20  
**Docs:**  
- Dividends: `https://www.tiingo.com/documentation/corporate-actions/dividends`  
- Splits: `https://www.tiingo.com/documentation/corporate-actions/splits`  

Tiingo documents that its corporate-actions endpoints cover **stocks, ETFs, and mutual funds** for both **dividends/distributions** and **splits**. This makes Tiingo a practical secondary source for distribution history and corporate-actions normalization when the issuer's public files are harder to use directly. citeturn12search28turn12search20turn15search7

**Alpha Vantage adjusted time series**  
Alpha Vantage documents that `TIME_SERIES_DAILY_ADJUSTED` includes historical **split/dividend events** together with OHLCV and adjusted close. This makes it useful for equity/ETF corporate actions and deriving adjusted history. For fund-specific distribution fields such as `record date`, `payment date`, or `distribution type`, issuer sources are still better. citeturn11view0turn11view2

**Tradier / brokerage history**  
Tradier can function as the user's actual **broker-side truth** for received dividends/cash events in imports, rather than as a global master source for fund distributions. History and account flows are therefore valuable for portfolio reconciliation, but not as a general fund-distribution database. citeturn35search1turn35search7

### Market price sources for equities, ETFs, funds

**Stooq**  
**Type:** web/CSV source, not a classic developer API  
**Pricing:** free/public  
**Authentication:** varies; official apikey flow appears for some CSV calls  
**Checked:** 2026-06-20  
**Site:** `https://stooq.com/db/`  

Stooq offers free historical data files and web CSV flows. Official pages show historical databases, update times, and CSV downloads. An important detail is that Stooq now also shows a `get_apikey` flow for some direct CSV URLs, which indicates that usage should no longer be assumed to be fully anonymous or stable over time. Stooq is therefore useful for free backfill/EOD, but should not be the only production source. citeturn23search0turn23search2turn26search0turn25search3

**Examples:**  
```text
GET https://stooq.com/q/d/l/?s=aapl.us&i=d
GET https://stooq.com/q/d/l/?s=^spx&i=d
```

**Yahoo Finance / yfinance / chart endpoints**  
**Type:** official web pages, but API usage is effectively unofficial  
**Pricing:** free/public  
**Authentication:** usually no for web pages  
**Checked:** 2026-06-20  

Yahoo Finance verifies public history pages for equities, indices, options, and FX, and premium offerings mention downloading historical data. At the same time, popular wrappers such as `yfinance` state clearly that they use **Yahoo's publicly available APIs**, are intended for research/education, and that the user must comply with Yahoo's terms. Conclusion: good for prototyping and research, but **fragile/unofficial** as a production API. citeturn27search3turn27search17turn27search13turn27search8

**Alpha Vantage**  
**Type:** official API  
**Pricing:** freemium + premium  
**Authentication:** API key  
**Checked:** 2026-06-20  
**Docs:** `https://www.alphavantage.co/documentation/`  

Alpha Vantage documents support for global **stocks, ETFs, mutual funds**, as well as daily adjusted, quote endpoints, FX, and an options section. The free tier is verified on the support page as **25 requests/day**; premium plans are published openly with per-minute tiers. For price series, `TIME_SERIES_DAILY_ADJUSTED` is especially practical because it includes adjusted close and dividend/split events. Realtime and 15-minute delayed US data are premium and licensed exchange data. citeturn11view0turn11view1turn11view2

**Examples:**  
```text
GET https://www.alphavantage.co/query?function=TIME_SERIES_DAILY_ADJUSTED&symbol=AAPL&outputsize=full&apikey=YOUR_API_KEY
GET https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=SPY&apikey=YOUR_API_KEY
```

**Financial Modeling Prep**  
**Type:** official API  
**Pricing:** freemium/paid  
**Authentication:** API key  
**Checked:** 2026-06-20  

FMP publishes a large number of price endpoints, including **ETF Price Quotes**, **Mutual Fund Price Quotes**, **Forex quotes**, **Index quotes**, and **Commodity quotes**. The official pricing page shows that the free tier is EOD-oriented and heavily limited, while paid plans provide realtime, global coverage, and higher call rates. Important licensing note: FMP explicitly writes that display/redistribution requires a separate data display/licensing agreement. That is a very important production warning. citeturn19search7turn9view3

**Examples:**  
```text
GET https://financialmodelingprep.com/stable/batch-etf-quotes?apikey=YOUR_API_KEY
GET https://financialmodelingprep.com/stable/batch-mutualfund-quotes?apikey=YOUR_API_KEY
GET https://financialmodelingprep.com/stable/batch-forex-quotes?apikey=YOUR_API_KEY
GET https://financialmodelingprep.com/stable/batch-index-quotes?apikey=YOUR_API_KEY
```

**Tiingo**  
**Type:** official API  
**Pricing:** freemium/paid  
**Authentication:** API token  
**Checked:** 2026-06-20  

Tiingo verifies EOD price data for more than **65,000 equities, mutual funds, and ETFs**, including splits and dividends, plus a separate FX API for **140+ pairs**. Tiingo also publishes usage limits in its product snippets: the free plan shows **50 hourly requests**, **1,000 daily requests**, and a monthly bandwidth cap; paid plans raise this significantly. Good choice for clean EOD retrieval if you prefer fixed pricing to per-call throttles. citeturn12search0turn12search8turn15search7turn15search9turn13search1

**Examples:**  
```text
GET https://api.tiingo.com/tiingo/daily/aapl/prices?startDate=2025-01-01&token=YOUR_API_KEY
GET https://api.tiingo.com/tiingo/daily/prices?tickers=SPY,IVV,VOO&token=YOUR_API_KEY
```
The second URL pattern is supported in Tiingo's own knowledge article but should still be treated as an operational pattern rather than a complete official reference specification. citeturn13search11

**Finnhub**  
**Type:** official API  
**Pricing:** freemium/paid  
**Authentication:** API key  
**Checked:** 2026-06-20  

Finnhub offers quotes, candles, ETF/mutual-fund data, and forex rates. The official pricing snippet openly shows plan prices and API calls per minute. Rate-limit documentation also states a global limit of **30 API calls/second** on top of plan limits. Finnhub is a practical broad secondary API, but exact endpoint patterns for some ETF/fund resources were not fully verified in this draft and should be double-checked against current docs before implementation. citeturn16search0turn16search5turn12search1turn12search21turn16search10

**Massive / Polygon**  
**Type:** official API  
**Pricing:** freemium/paid  
**Authentication:** API key  
**Checked:** 2026-06-20  
**Docs:** `https://massive.com/docs`  

Massive is strong for **US stocks**, with verified endpoint-catalog support for **reference tickers**, **last trade**, **NBBO**, aggregates, snapshots, options, futures, indices, and forex. The official knowledge base says the free tier has **5 requests/minute** and paid customers have “unlimited” REST requests. For US equity/ETF prices this is a very strong developer experience, but coverage is in practice US-centered, and format/symbology need to be mapped for LSE/UCITS ETF workflows. citeturn20view0turn20view1turn21search6turn13search0

**Examples:**  
```text
GET https://api.massive.com/v3/reference/tickers?apiKey=YOUR_API_KEY
GET https://api.massive.com/v2/last/trade/AAPL?apiKey=YOUR_API_KEY
GET https://api.massive.com/v2/aggs/ticker/SPY/range/1/day/2026-01-01/2026-06-19?apiKey=YOUR_API_KEY
```
The base domain for requests should be verified against the current quickstart before a production implementation; the docs provide API paths, but this draft did not open the full quickstart specification. `needs verification` for the exact request host if you want to hard-code it directly from this text. citeturn20view1turn21search14

**Nasdaq Data Link**  
Nasdaq Data Link is more of a **dataset hub** than a single homogeneous price feed. For the tables API, an API key is required, and rate limits are clearly published. It is best when you have already chosen specific datasets, for example end-of-day pricing or official central-bank series, rather than as the first generic price adapter. citeturn22search0turn22search6turn22search8turn22search11

**London Stock Exchange delayed data**  
LSE's official market and instrument pages explicitly show that data is **delayed by at least 15 minutes**. This makes LSE useful as an official web source/check for listed symbol, price indication, and listing context for UK/LSE ETFs such as VUSA, ISF, VWRA, or JEGP, but not as a well-documented public developer API at present. For a free EOD flow for LSE/UK ETFs, Stooq, Yahoo, or issuers' own price/NAV pages are often practically easier, with all their caveats. citeturn28search0turn28search1turn28search4turn6search18

**IEX Cloud**  
IEX Cloud appears to have been shut down and should not be planned for new implementations. Public closure notices state that the service was retired on **2024-08-31**. This should be treated as sufficiently verified to **not** choose IEX Cloud for new development, though if an organization still has an old integration, it is still wise to double-check internal contract history and any archive notices. citeturn29search1turn29search5

### NAV / iNAV / ETF premium-discount sources

Free, robust, and officially machine-readable **iNAV** is one of the biggest gaps. This research did not verify any broad free source that consistently covers both US ETFs and UCITS ETFs with a stable iNAV API. This should be stated clearly: **reliable free iNAV data appears hard to obtain**. citeturn4view1turn6search3

What does exist:
- **Vanguard** shows product-page history with **NAV** and **market price** plus price download. citeturn4view1  
- **iShares IVV** shows **Closing Price**, **Net Assets of Fund**, and **Premium/Discount** on the product page. This makes iShares useful for premium/discount and NAV-related presentation data at fund level. citeturn6search3  
- **FMP ETF/fund information** provides fund metadata, but this draft does not verify it as a reliable source for full NAV history for UCITS funds. `needs verification`. citeturn9view1  
- **LSE** shows official delayed exchange data, but was not verified here as a broad NAV/iNAV source. citeturn28search0

**Practical recommendation:** start with issuer pages for NAV/premium-discount for the fund families you actually support first. If iNAV becomes business-critical for intraday workflows, a commercial vendor will likely be needed. `needs verification` for the best commercial candidate in this draft.

### FX rates

**ECB**  
**Type:** official central-bank source  
**Checked:** 2026-06-20  
**Docs:** `https://data.ecb.europa.eu/help/api/data`  
**Data API example pattern:** `https://data-api.ecb.europa.eu/service/data/EXR/...`  

The ECB Data Portal verifies an SDMX 2.1 REST API and publishes euro reference rates that are normally updated at around **16:00 CET** each working day. The documentation also shows concrete EXR examples, e.g. `EXR/M.USD.EUR.SP00.A`. For portfolio valuation, the ECB is very good for **official EUR reference rates**, but the ECB explicitly emphasizes that rates are published **for information purposes** and advises against using them for transaction purposes. citeturn30search2turn30search17turn30search5turn30search20

**Examples:**  
```text
GET https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A
GET https://data-api.ecb.europa.eu/service/data/EXR/D.GBP.EUR.SP00.A
```

**Bank of England**  
**Type:** official central-bank source  
**Checked:** 2026-06-20  
**Site/DB:** `https://www.bankofengland.co.uk/boeapps/database/`  

The BoE publishes **daily spot rates against Sterling** and states that data can be downloaded as **CSV, XLSX, JSON**. At the same time, the BoE explicitly says that the exchange rates are **not official rates** and are no more authoritative than commercial banks' London FX rates. For GBP-based portfolio valuation they are still very useful, especially if you want an official UK source with easy export. citeturn31search9turn31search1turn31search15

**Alpha Vantage FX**  
Alpha Vantage documents a separate FX section and `CURRENCY_EXCHANGE_RATE` for fiat pairs. Good as a simple secondary FX adapter with an API key, but ECB/BoE should be prioritized when an “official” reference rate matters more than API simplicity. citeturn11view0

**Tiingo FX**  
Tiingo verifies real-time and historical FX for **140+ pairs**, with usage limits published in product snippets. Good if you are already using Tiingo for EOD prices and want to keep the number of providers down. citeturn12search36turn15search9

**Finnhub FX**  
Finnhub has official docs for **Forex Candles** and **Forex All Rates**. Good as a secondary broad API source, but official central-bank sources are still better for reference rates and auditability. citeturn12search1turn16search10

**Frankfurter / exchangerate.host**  
These were mentioned in the requirements, but were not sufficiently verified during the session. **`needs verification`**.

### Bonds / fixed income sources

Robust bond reference data and bond prices are significantly harder than equity/ETF EOD. The most important conclusion is therefore explicit: **broad and robust corporate bond reference/pricing is often paid data**; free/public sources are best for **sovereign curves**, **gilt/Treasury metadata**, and some reference prices, not as a complete bond master/pricing stack. citeturn32search0turn30search1turn33search10

**OpenFIGI**  
OpenFIGI can still be used for bond identification/crosswalk where FIGI exists, but it does not replace a full fixed-income master. citeturn43view2

**U.S. Treasury**  
Treasury publishes official **Daily Treasury Par Yield Curve Rates**, **Bill Rates**, and XML/CSV archives. This is excellent for USD government curves, duration/discounting, and reference curves. citeturn30search1turn30search4turn30search13

**Example:**  
```text
GET https://api.stlouisfed.org/fred/series/observations?series_id=DGS10&api_key=YOUR_API_KEY&file_type=json
```
This is via FRED for the Treasury series, not directly via the Treasury API, but is practical for consumption. citeturn30search15

**UK DMO**  
DMO publishes **gilts in issue**, **ISIN**, issue dates, redemption/dividend dates, and historical gilt yields. Important licensing note: end-of-day reference prices are produced by **FTSE-Tradeweb**, available free of charge for **non-commercial use** from the following day, while commercial users are referred to Tradeweb for access. This is a clear example of why bond pricing quickly becomes a licensing issue. citeturn32search0turn32search2turn32search5turn32search6turn32search10

**Bank of England**  
The BoE publishes yield curves and related database series via its database. Good for GBP rates and analysis, but not a full bond-pricing feed for individual corporate bonds. citeturn31search1turn31search15

**ECB**  
The ECB Data Portal can be used for euro-oriented government/rate series via the SDMX API, but the exact suitable dataset key for each curve series needs to be selected in the database. Good for reference curves, not for all bond pricing. citeturn30search2turn30search8

**FRED**  
FRED offers API access to large numbers of rate series and macro series, with publicly documented request formats and an API-key requirement. Good as a complementary macro/curve source, but not as a canonical bond-price vendor. citeturn30search0turn30search3turn30search15

**Nasdaq Data Link**  
Can be very valuable for specific fixed-income datasets, but the choice is dataset-dependent. Good as a hub rather than as one generic bond adapter. citeturn22search8turn22search18

**Commercial vendors**  
- **ICE Developer Center** confirms that ICE Data Services has API products and developer docs. citeturn33search10  
- **CME Reference Data API** covers contract/product specifications for CME derivatives, not bond master data. citeturn33search9  

For broad corporate bond master/pricing in production, commercial data vendors should be evaluated separately. In this draft, the best vendor choice is **`needs verification`**.

### Futures data sources

**Databento**  
**Type:** official commercial API/docs  
**Checked:** 2026-06-20  
**Docs:** `https://databento.com/docs`  

Databento is a strong developer-friendly candidate for futures. The documentation verifies **introduction to futures**, **continuous contracts**, symbol standards, and examples/tutorials for **volume, open interest, settlement prices**, futures trading hours, and options on futures. Continuous-contract symbology is also verified: Databento uses formats like `[ROOT].[ROLL_RULE].[RANK]` and explains that its continuous prices are **original, unadjusted** rather than back-adjusted. This is excellent for transparent analytics but important to know for backtests. citeturn33search4turn33search0turn33search8turn33search12

**Massive / Polygon Futures**  
Massive now has general futures documents and shows support for **reference data**, **aggs**, **quotes**, **snapshot**, **products**, and **exchanges**, with plans from free tier through business-level feeds. Futures snippets mention top U.S. futures from **CME, CBOT, COMEX, NYMEX**. Good candidate if you already use Massive for US equities/options, but Databento or official exchange sources may be stronger if futures are central. citeturn21search0turn21search7turn21search10turn21search16turn21search15

**CME Group**  
CME offers official **Market Data APIs** and a specific **Real-Time Futures & Options Data API** via WebSocket in JSON, plus a separate **Reference Data API**. This is the clearest official source if you need to consume CME data directly from the source. Access appears to require commercial onboarding/request access. citeturn33search1turn33search5turn33search9turn33search17

**ICE**  
ICE's Developer Center officially confirms that ICE Data Services provides API products, documentation, demos, and SDKs. Public ICE Report Center pages also show reports for futures/options, volume, and open interest. This research does not, however, verify a simple public retail-friendly REST solution for general futures prices. citeturn33search10turn33search6

**Eurex**  
Eurex verifies a **free public reference data API** based on **GraphQL + JSON**, with information about **Products, Contracts, Trading hours, Expirations**. This is very useful for contract metadata and schedules. For market prices/microstructure, there are also T7 market/reference manuals, but clean public price APIs were not verified here. citeturn33search3turn33search11turn33search19

**Summary for futures**  
For implementation, split futures into at least two adapters:
- **reference/contracts metadata**
- **market data prices/trades/quotes**

Databento and CME stand out as the most promising first candidates based on what was verified here. citeturn33search4turn33search5

### Options data sources

**Tradier**  
**Type:** official broker/API  
**Checked:** 2026-06-20  
**Docs:** `https://docs.tradier.com/`  

Tradier is the most concretely verified source for options chains in this research. Official docs verify:
- `GET https://api.tradier.com/v1/markets/options/chains`
- `GET https://api.tradier.com/v1/markets/options/expirations`
- `GET https://api.tradier.com/v1/markets/options/strikes`
- `GET https://api.tradier.com/v1/markets/history`

The chains endpoint requires `symbol` and `expiration`; `greeks=true` can be included and the docs state that **Greek and IV data is included courtesy of ORATS**. Tradier also documents that real-time data for US stocks/options is available to **Tradier Brokerage account holders**; otherwise the delayed data sandbox path applies. citeturn35search0turn35search5turn35search2turn35search7turn35search4turn35search8

**Massive / Polygon Options**  
Massive verifies very strong options documentation, including:
- `GET /v3/reference/options/contracts`
- `GET /v3/reference/options/contracts/{options_ticker}`
- `GET /v2/aggs/ticker/{optionsTicker}/range/{multiplier}/{timespan}/{from}/{to}`
- `GET /v1/open-close/{optionsTicker}/{date}`
- snapshots with **break-even price**, **implied volatility**, **open interest**, **greeks**, plus latest quote/trade.

This makes Massive very strong for US options market data if budget is available. The options overview page also describes coverage across all 17 U.S. options exchanges via OPRA. citeturn20view2turn21search8

**Alpha Vantage**  
Alpha Vantage documents an **Options Data APIs** section with realtime and historical US options and 15+ years of history. However, the exact function parameter for an endpoint line was not verified in this session. Therefore, use Alpha Vantage for options only after double-checking the exact request name in current docs. **`needs verification`** for the exact endpoint example in this draft. citeturn11view0

**Databento**  
Databento has official guides for **equity options** and **options on futures**, plus options-chain-related tutorials. Very promising for more institutional workflows, but this draft did not verify exact REST patterns at endpoint level. citeturn33search4turn33search12turn33search16

**Cboe DataShop / All Access API**  
Cboe publishes official API product information for options/equities, with real-time, delayed, and historical options. Verified dataset pages show that options quote intervals can include **Implied Volatility and Greeks** as add-ons, that intraday files are often **15 minutes delayed**, and that coverage applies to listed options on US stocks, ETFs, and indices across OPRA. This is very promising for serious options workflows, but commercial/licensing terms are significant. citeturn34search2turn34search4turn34search12turn34search16turn34search8

**Nasdaq options data**  
Nasdaq markets real-time options data via API and shows that its options market data covers several options exchanges. This is official and relevant, but this session did not verify a simple retail-friendly options-chain REST API for general use beyond web chain pages. citeturn34search5turn34search1

### Indices and benchmark data

Index levels are available from several sources:
- **LSE indices/pages** show that index data on the site is delayed by at least 15 minutes. citeturn28search17turn28search5  
- **Yahoo Finance** has public history pages for indices such as the S&P 500 and Nasdaq-100. citeturn27search3turn27search7  
- **Nasdaq Data Link** has both pricing datasets and index-related database products. citeturn22search11turn12search15  
- **Massive / Polygon** has a dedicated indices product scope in its docs. citeturn20view0  

The big problem is **benchmark constituents**. This draft did not verify that full constituents for **MSCI, FTSE, or S&P** are freely available via official public APIs; in practice, such data is often licensed and restricted. Therefore, in many ETF look-through scenarios, it is better to use **ETF holdings** from the issuer rather than trying to replicate benchmark constituent data without a license. This should be treated as an important product decision rather than a technical detail. **Full benchmark constituent access: `needs verification` and is often expected to be licensed.** citeturn6search3turn7search13turn28search5

### Corporate actions / events

**Alpha Vantage**  
`TIME_SERIES_DAILY_ADJUSTED` includes historical **split and dividend events**, making Alpha Vantage useful for equity/ETF corporate-actions normalization. citeturn11view0turn11view2

**Tiingo**  
Tiingo has separate docs for **dividends** and **splits** that explicitly cover **stocks, ETFs, and mutual funds**. For a corporate-actions adapter, Tiingo is therefore one of the clearest verified candidates in this research. citeturn12search20turn12search28

**Saxo OpenAPI**  
Saxo reference docs show a separate service group for **Corporate Actions** in the platform's reference documentation. This is relevant for broker- or account-linked event/import logic, but this draft did not verify the exact resource paths for corporate actions specifically. **`needs verification`** if it is to be used directly as an adapter in the first phase. citeturn37search1turn37search4

**Issuer/exchange notices**  
For fund closures, name changes, benchmark changes, and distribution-policy changes, issuer and exchange notices are often more important than generic corporate-actions APIs. LSE company/news/notices and issuers' documents/announcements should therefore be part of the monitoring stack, even if they do not always come as clean APIs. citeturn28search4turn6search18turn42search2

### Document sources

**Vanguard**  
Public product pages and a logged-in document/report generator exist. This is enough to plan document archiving for factsheets, prospectuses, valuation reports, and contract notes. PDF parsing and version management should be done locally. citeturn4view1turn42search2

**iShares / BlackRock**  
iShares product pages show **Prospectus PDF**, **Financial and Legal Documents**, and **Data Download Excel**. For document archiving, iShares is therefore a recommended first issuer to support. Hashing/versioning of documents should be performed locally because public URLs or files can change over time. citeturn6search3turn6search9

**SPDR / State Street**  
SPDR uses many PDF-based factsheets, prospectuses, and document resources. Good for document storage but requires a PDF pipeline or metadata extraction from filenames/dates. citeturn7search13turn7search16

**Amundi**  
Amundi verifies a document library with **KIDs, factsheets, and prospectuses**. Strong document source if you need EU fund material. citeturn8search3turn8search19

**WisdomTree**  
Verified PDF documents for distribution schedule and product lists. Reasonable candidate for document archive. citeturn8search16turn8search4

**VanEck**  
VanEck explicitly says that prospectuses and local KIIDs/KIDs are available free of charge. Good document source, but URL patterns for machine retrieval should be verified per region. citeturn8search13

**LGIM**  
LGIM fund centre is verified as an official ETF entry point; use this for document discovery rather than third parties. citeturn8search18

**Xtrackers / DWS**  
Official site verified, but exact document patterns were not verified in detail. **`needs verification`**. citeturn7search3

**Common document caveats**
- PDF scraping breaks easily when layouts change.
- Jurisdiction variants can provide different document sets for the same fund family.
- Terms of use can restrict redistribution even if the file is publicly available.
- Local hashing/versioning is needed if you want to detect updated factsheets/KIDs/prospectus versions. citeturn5view2turn8search11turn8search13

### Broker / user import sources

**Interactive Brokers**  
**Type:** official broker/API  
**Checked:** 2026-06-20  

IBKR has several relevant import paths:
- **Client Portal Web API** for near-real-time portfolio/account access.
- **TWS API** for trading/market data/portfolio.
- **Flex Queries / Flex Web Service** for history and reports in **XML, CSV, or Text**.

IBKR's reporting docs verify that Flex Queries can generate very detailed statements per field section. Campus pages also verify that the Client Portal Web API provides real-time access to live market data, scanners, and **intra-day portfolio updates**. For imports, IBKR is therefore a top candidate because it covers both position snapshots and detailed transaction history. citeturn38search0turn38search2turn38search6turn38search3

**Practical note:**  
Exact Flex Web Service URLs and versions appear in official-like IBKR guides and community references, but this draft does not want to lock in an exact production URL without extra manual checking. **`needs verification`** for the exact v2/v3 endpoint host before implementation, even though the references are strong. citeturn40search1turn40search10

**Saxo Bank OpenAPI**  
**Type:** official broker/API  
**Checked:** 2026-06-20  
**Docs:** `https://www.developer.saxo/openapi/referencedocs`  

Saxo has very strong broker/import coverage. Verified portfolio endpoints include **positions**, **netpositions**, **closedpositions**, balances, and account hierarchies. The documentation also shows concrete GET examples under `/port/v1/positions`, plus reference data for instruments, option roots, and future spaces. This makes Saxo an excellent design reference even if you do not start with Saxo as the first broker integration. OAuth flows are officially documented. citeturn37search0turn37search5turn37search8turn37search10turn37search12turn36search4turn36search5

**Trading 212**  
**Type:** official help center + API beta  
**Checked:** 2026-06-20  

Trading 212 verifies both **CSV export** of history/orders/transactions/dividends and an **API (Beta)** where API keys can be given access to account data, history, orders, and portfolio. The help center also states that the export function previously did not exist for CFD, but that a separate CFD export now exists in newer app versions. This makes Trading 212 a promising retail import source, but because the public API is beta, contract/field stability should be assessed cautiously. citeturn41search0turn41search1turn41search7turn41search4

**Vanguard UK**  
Vanguard Investor UK lets the user generate/download statements, valuations, and other documents through a logged-in document center. Good for document-based import if no public portfolio API exists. citeturn42search2

**Hargreaves Lansdown**  
HL verifies that investment reports contain **valuation** and **recent transactions** and are available online. This is useful for document/statement import, but this research did not verify an official CSV/API path. **`needs verification`** for machine-readable export. citeturn42search0turn42search20

**AJ Bell**  
No sufficiently verified public docs in this session. **`needs verification`**.

**Generic CSV import**  
Regardless of broker, a generic CSV import is almost always required for positions, transactions, cash, dividends, and fees. For developers, this is often the fastest path to user value before broker-specific APIs are built. Source-specific provenance should then be stored at row level.

## Candidate endpoint examples and implementation notes

### Candidate endpoint examples

The examples below use **placeholder keys only**.

```text
OpenFIGI
POST https://api.openfigi.com/v3/mapping
Content-Type: application/json
X-OPENFIGI-APIKEY: YOUR_API_KEY

[
  {"idType":"TICKER","idValue":"IBM","exchCode":"US"}
]
```

```text
OpenFIGI
POST https://api.openfigi.com/v3/mapping
Content-Type: application/json

[
  {"idType":"ID_ISIN","idValue":"US4592001014"}
]
```

```text
Alpha Vantage
GET https://www.alphavantage.co/query?function=TIME_SERIES_DAILY_ADJUSTED&symbol=AAPL&outputsize=full&apikey=YOUR_API_KEY
```

```text
Alpha Vantage
GET https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=SPY&apikey=YOUR_API_KEY
```

```text
FMP ETF holdings
GET https://financialmodelingprep.com/stable/etf/holdings?symbol=SPY&apikey=YOUR_API_KEY
```

```text
FMP ETF info
GET https://financialmodelingprep.com/stable/etf/info?symbol=SPY&apikey=YOUR_API_KEY
```

```text
FMP batch ETF quotes
GET https://financialmodelingprep.com/stable/batch-etf-quotes?apikey=YOUR_API_KEY
```

```text
Stooq
GET https://stooq.com/q/d/l/?s=aapl.us&i=d
GET https://stooq.com/q/d/l/?s=^spx&i=d
```

```text
ECB
GET https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A
GET https://data-api.ecb.europa.eu/service/data/EXR/D.GBP.EUR.SP00.A
```

```text
FRED
GET https://api.stlouisfed.org/fred/series/observations?series_id=DGS10&api_key=YOUR_API_KEY&file_type=json
```

```text
Tradier options expirations
GET https://api.tradier.com/v1/markets/options/expirations?symbol=SPY&includeAllRoots=true
Authorization: Bearer YOUR_API_KEY
Accept: application/json
```

```text
Tradier options chains
GET https://api.tradier.com/v1/markets/options/chains?symbol=SPY&expiration=2026-07-17&greeks=true
Authorization: Bearer YOUR_API_KEY
Accept: application/json
```

```text
Tradier historical prices
GET https://api.tradier.com/v1/markets/history?symbol=AAPL&interval=daily&start=2026-01-01&end=2026-06-19
Authorization: Bearer YOUR_API_KEY
Accept: application/json
```

```text
Massive options contracts
GET /v3/reference/options/contracts
GET /v3/reference/options/contracts/{options_ticker}
```

```text
Massive futures exchanges
GET /futures/v1/exchanges
```

```text
Saxo positions
GET https://gateway.saxobank.com/sim/openapi/port/v1/positions?ClientKey=YOUR_CLIENT_KEY
Authorization: Bearer YOUR_ACCESS_TOKEN
```

Exact request hosts, query parameters, and response payloads should always be mirrored from the current official documentation in the implementation; several of the sources above change version or host patterns over time. citeturn43view0turn11view0turn19search7turn35search0turn37search5turn21search16

### Recommended implementation order

**Identifier resolution/security master first.**  
Start with OpenFIGI plus an internal instrument registry that can store multiple identifiers, exchange/MIC, currency, security type, and source provenance. Without this, the rest of the pipeline becomes ticker-fragile. citeturn43view2

**Then ETF/fund distributions ingestion.**  
Distributions and corporate actions affect return calculation, cashflow, and adjusted prices. Official issuer pages plus Tiingo/Alpha Vantage as secondary sources provide quick value. citeturn8search16turn12search28turn11view0

**Then holdings for a single issuer.**  
First build a robust holdings adapter for **one** issuer, preferably iShares or Vanguard. iShares is particularly attractive because products are verified to have holdings and data-download components. Get a working as-of/provenance pattern before adding more issuers. citeturn6search3turn4view1

**FX adapter from official source.**  
Add ECB and possibly Bank of England next. FX is necessary for portfolio valuation across trading currency and base currency. citeturn30search5turn31search9

**Document snapshot ingestion.**  
Store factsheets/KIDs/prospectuses locally with hash/version. This provides an audit trail and support for UI/document viewing without needing to re-fetch everything live. citeturn6search3turn8search3turn42search2

**Improved price sources.**  
After the foundations: add a combination of Alpha Vantage/Tiingo/FMP or Massive depending on budget, coverage, and realtime needs. citeturn11view0turn15search7turn9view3turn20view1

**NAV ingestion where and if it exists.**  
This should follow core price/distribution/holdings because free and robust NAV/iNAV coverage is uneven. citeturn4view1turn6search3

**Broker CSV import.**  
Then build generic CSV, followed by broker-specific adapters such as IBKR Flex, Saxo, and Trading 212. This provides quick user value without locking you into one broker first. citeturn38search2turn37search5turn41search1

**Bond reference/pricing after the instrument model is stable.**  
Fixed income requires better identification, day-count conventions, coupon logic, and licensing decisions. citeturn32search0turn30search1

**Futures/options last.**  
Derivatives should only come once core ETF/equity/fund workflows are stable, because symbology, licensing, and intraday requirements are significantly more complex. citeturn33search4turn35search0turn20view2

### Adapter design notes

The following are **design suggestions**, not implementation requirements.

```python
class PriceSource:
    async def fetch_prices(...): ...

class IdentifierSource:
    async def resolve_identifier(...): ...

class IssuerFactsSource:
    async def fetch_fund_facts(...): ...

class HoldingsSource:
    async def fetch_holdings(...): ...

class DistributionSource:
    async def fetch_distributions(...): ...

class FxSource:
    async def fetch_fx_rates(...): ...

class DocumentSource:
    async def fetch_documents(...): ...

class NavSource:
    async def fetch_nav(...): ...

class BondReferenceSource:
    async def fetch_bond_reference(...): ...

class OptionChainSource:
    async def fetch_option_chain(...): ...

class FuturesSource:
    async def fetch_contracts(...): ...
```

**Normalized output fields to plan for**

`IdentifierSource`  
- input identifier type/value  
- resolved identifiers: ticker, exchange, MIC, ISIN, FIGI, CUSIP, SEDOL, name  
- security type, currency, country  
- source, fetched_at, as_of_date, confidence

`PriceSource`  
- instrument_id  
- open/high/low/close/adjusted_close/volume  
- price_currency  
- interval  
- realtime/delayed/EOD flag  
- corporate_action_adjusted flag  
- source, as_of_date, fetched_at

`IssuerFactsSource`  
- fund_name  
- provider  
- isin  
- domicile  
- base_currency  
- distribution_policy  
- benchmark  
- ter/ocf  
- aum/fund_size  
- holdings_count  
- factsheet_date  
- source_url

`HoldingsSource`  
- fund_id/fund_isin  
- holding_name  
- holding_identifiers  
- country/sector/currency  
- quantity  
- market_value  
- weight  
- as_of_date  
- source_url

`DistributionSource`  
- fund_id  
- ex_date  
- record_date  
- payment_date  
- amount  
- currency  
- status  
- distribution_type  
- source_url

`FxSource`  
- base_currency  
- quote_currency  
- rate  
- fixing_date  
- source_url  
- official_reference flag

`DocumentSource`  
- fund_id  
- document_type  
- document_date  
- language  
- url  
- local_hash  
- fetched_at

`NavSource`  
- fund_id  
- nav  
- nav_currency  
- nav_date  
- market_price  
- premium_discount  
- source_url

`BondReferenceSource`  
- isin/cusip/figi  
- issuer  
- coupon  
- maturity  
- day_count  
- currency  
- market  
- source_url

`OptionChainSource`  
- underlying  
- expiry  
- strike  
- call_put  
- bid/ask/last  
- iv  
- greeks  
- open_interest  
- volume  
- quote_timestamp

`FuturesSource`  
- root  
- contract  
- expiry  
- exchange  
- tick_size  
- multiplier  
- settlement  
- open_interest  
- volume  
- trading_hours

The important design pattern is that **all adapters return provenance** and that normalization never discards original identities or original timestamps. This reduces future debugging and makes it possible to switch providers without losing comparability. citeturn43view1turn22search8turn11view2

## Caveats and short summary

### Caveats and legal / usage notes

This document is **not legal advice**. Always check the provider's agreement, display terms, and redistribution rules before commercial use. This applies even when data is “free” or generally available on the web. FMP explicitly states that display/redistribution requires a separate agreement. DMO explicitly states that FTSE-Tradeweb prices are free for **non-commercial use**, but that commercial users should contact Tradeweb. Alpha Vantage notes exchange regulation for realtime/delayed US data. citeturn9view3turn32search0turn11view2

Exchange data can be **delayed** or licensed. LSE marks all data on the site as at least **15 minutes delayed**. Bank of England says its FX rates are not official transaction rates. The ECB says its reference rates are published for information purposes and should not be used as transaction rates. citeturn28search0turn31search9turn30search5

Issuer sites are often not stable APIs. HTML structure, download links, PDF files, language/jurisdiction variants, and session/cookie behavior can change. Aggressive scraping should be avoided. If you must collect from the web, do so cautiously, with caching and a clear fallback. citeturn6search3turn8search3turn4view1

Tests should use fixtures/mocks rather than live API calls. Live calls should be reserved for integration checks and should be rate-limited. Provenance, uncertainty, and `needs verification` fields should be preserved in your data model and preferably exposed in internal observability. citeturn22search6turn13search11turn11view2

### Open questions / limitations

- Exact machine-readable holdings/distribution downloads for all named issuers were not fully verified; several are **JS-heavy** or document-driven.  
- The best commercial solution for **broad corporate bond reference/pricing** was not verified in this session.  
- Broad, free, and robust **iNAV** coverage for both UCITS ETFs and US ETFs remained a clear gap.  
- Some broker flows, especially **AJ Bell** and more detailed **HL** exports, need separate verification.  
- For **Finnhub** and some larger vendor suites, not every exact endpoint path was verified line by line; use the docs URLs as starting points and double-check before implementation.

### Short summary

**Best sources to implement first**  
Start with **OpenFIGI** for identification, then **iShares/Vanguard** for official fund facts and documents, **ECB/BoE** for FX, and a simple price adapter such as **Alpha Vantage**, **Tiingo**, or **FMP**, depending on budget and needs. For broker import, **IBKR Flex/Client Portal**, **Saxo OpenAPI**, and **Trading 212 CSV/API beta** are the clearest candidates from this research. citeturn43view2turn6search3turn4view1turn30search5turn31search9turn11view0turn15search7turn9view3turn38search6turn37search5turn41search0

**Promising but likely to need API keys / payment**  
**Massive/Polygon**, **Databento**, **FMP**, **Tiingo**, **Finnhub**, **Tradier**, **Cboe DataShop**, **Nasdaq Data Link premium datasets**, **CME APIs**, and **ICE Data Services** look strong, but in practice they require API keys, a brokerage account, a paid plan, or commercial onboarding for real production use. citeturn20view0turn33search4turn9view3turn15search7turn16search0turn35search4turn34search2turn22search8turn33search1turn33search10

**Risky / fragile / scraping-based sources**  
**Yahoo Finance** and parts of **Stooq** are useful for research, backfill, and prototyping but should be treated as fragile or at least non-contractual integration surfaces. LSE's public web pages are official but are more web pages than developer APIs. citeturn27search8turn23search0turn26search0turn28search0

**Biggest gaps**  
The biggest gaps are in **bonds/options/futures** and **NAV/iNAV**. Sovereign curves and some reference data can be obtained officially from Treasury, DMO, ECB, and BoE, but broad bond pricing is still difficult without paid data. Options and futures have good candidates, but they are generally commercial or US-focused. Free, robust iNAV data was not verified as a general solution. citeturn30search1turn32search0turn33search4turn35search0turn20view2turn4view1turn6search3
