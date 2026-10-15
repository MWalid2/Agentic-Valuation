# External-source fixtures

Recorded on 2026-10-13 for offline parser and integration tests.

- `betas.html`, `margins.html`, `sales_to_capital.html`, `country_risk.html`, and
  `spreads.html` preserve selected original NYU table rows and date metadata.
  Source root: `https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/`;
  filenames: `Betas.html`, `margin.html`, `capex.html`, `ctryprem.html`, `ratings.html`.
  `index.html` preserves the full-update date from the NYU `datacurrent.html` index.
- `fred-dgs10.json` is the public FRED observations response for the 2026-10-11
  vintage, with a two-week observation window. Request credentials are omitted.
- `yahoo-aapl.json` retains six daily bars and currency metadata from the live
  yfinance AAPL response. It is a parser fixture, not a complete price history.
- `apple-snapshot.json` is the prior validated SEC interpretation for Apple
  FY2025, cutoff 2026-10-11, reused to test multi-source assembly without an LLM.
  Underlying filing: accession `0000320193-25-000079`, `aapl-20250927.htm`.

Adversarial values and hypothetical splits in test code are synthetic. Integration
tests may reuse the DGS10 response shape for other allowed series; they test
orchestration, not those series' actual economic values. Live verification uses
the individual real series. No API keys, cookies, or contact headers are stored.
