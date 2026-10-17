# Forecast anchoring review

For each company: is the base case a plausible view of the business, given its history, industry and guidance? Judge the assumptions, not the distance from the price (the price is shown for context only and was never an input). Tick one verdict and say which assumption you would change.

Across the golden set: median weighted value / price 0.54; correlation of log base value with log price 0.74; 9 of 10 below the price.

## COCA COLA CO (KO)

- Weighted value 50.56 (bear 38.55, base 50.87, bull 61.95); market price 88.05.
- WACC 7.73% (risk-free 5.22%, ERP 4.23%, beta 0.63 bottom_up); terminal WACC 7.73%.
- Base: growth 5.0% in year 1 to 3.0% terminal; margin 29.8% to 29.8%; sales to capital 1.54; terminal ROIC 16.4%; terminal value 64% of EV.
  - growth: fades_normally: guidance: Organic revenues (non-GAAP): Approx. 5% growth; held 2 years then a linear fade to 3.00%. Evidence: FY2022-2025 growth rates decline from 11.3% to 1.9% but CAGR remains 5.5%
  - margin: holds: from clean operating margin, TTM 29.8% to 29.8% in 5 years. Evidence: Clean operating margin increases from 26.8% to 31.2% over 2021-2025 with 1.3% annual trend
  - reinvestment: history: the industry's 1.54: the company's 0.78 includes acquired goodwill, more than new growth needs; used 1.54. Evidence: Sales to invested capital median 0.78 vs industry 1.54, ROIC 19.3% vs industry 20.6%
  - tax: guidance 19.9%, converging to the marginal 22.2%
  - terminal_growth: min(risk-free 5.22%, 3%)
  - terminal_roic: moat wide (chosen wide): terminal WACC + 8.7% (keeps 75% of the median ROIC's 19.3% excess over WACC, within 5%-15%). Evidence: ROIC (19.3%) exceeds WACC (7.7%) with strong margin expansion and low reinvestment needs
  - moat: wide: ROIC (19.3%) exceeds WACC (7.7%) with strong margin expansion and low reinvestment needs
- Your verdict: [ ] plausible  [ ] too low  [ ] too high  Assumption you would change: 

## MICROSOFT CORP (MSFT)

- Weighted value 423.17 (bear 342.07, base 426.22, bull 498.18); market price 535.07.
- WACC 10.67% (risk-free 5.22%, ERP 4.23%, beta 1.28 bottom_up); terminal WACC 9.52%.
- Base: growth 15.8% in year 1 to 3.0% terminal; margin 47.3% to 48.5%; sales to capital 1.54; terminal ROIC 24.5%; terminal value 60% of EV.
  - growth: durable: half the latest growth (17.8%), half the CAGR (13.7%); held 4 years then a linear fade to 3.00%. Evidence: FY2023-2026 growth > 6% with CAGR 13.7%
  - margin: expands: from clean operating margin, TTM 47.0% to 48.5% in 5 years. Evidence: Clean operating margin +1.3% annually from 42.0% to 47.0%
  - reinvestment: history: the industry's 1.54: the company's 1.07 includes acquired goodwill, more than new growth needs; used 1.54. Evidence: Sales to invested capital median 1.07 < industry 1.54
  - tax: median effective 18.2%, converging to the marginal 22.5%
  - terminal_growth: min(risk-free 5.22%, 3%)
  - terminal_roic: moat wide (chosen wide): terminal WACC + 15.0% (keeps 75% of the median ROIC's 39.2% excess over WACC, within 5%-15%). Evidence: ROIC 39.2% > WACC 10.7% with strong margin expansion
  - moat: wide: ROIC 39.2% > WACC 10.7% with strong margin expansion
- Your verdict: [ ] plausible  [ ] too low  [ ] too high  Assumption you would change: 

## NVIDIA CORP (NVDA)

- Weighted value 295.01 (bear 227.21, base 279.09, bull 394.67); market price 229.28.
- WACC 11.78% (risk-free 5.22%, ERP 4.23%, beta 1.51 bottom_up); terminal WACC 9.64%.
- Base: growth 44.7% in year 1 to 3.0% terminal; margin 65.6% to 66.8%; sales to capital 2.51; terminal ROIC 24.6%; terminal value 58% of EV.
  - growth: fades_fast: half the latest growth (40.3%), half the CAGR (68.6%); held 0 years then a convex fade to 3.00%. Evidence: FY2024 growth 125.9% then FY2025 114.2% (declining) but TTM growth 40.3% (recently high)
  - margin: expands: from clean operating margin, TTM 65.3% to 66.8% in 5 years. Evidence: FY2022 37.4% → FY2024 54.2% → FY2025 62.5% (rising)
  - reinvestment: history: the company's median sales to invested capital 2.51; used 2.51. Evidence: Sales to invested capital median 2.51 (vs industry 1.21) but ROIC 119.9% (high)
  - tax: guidance 17.0%, converging to the marginal 21.4%
  - terminal_growth: min(risk-free 5.22%, 3%)
  - terminal_roic: moat wide (chosen wide): terminal WACC + 15.0% (keeps 75% of the median ROIC's 119.9% excess over WACC, within 5%-15%). Evidence: ROIC 119.9% >> WACC 11.8% (high returns above cost of capital)
  - moat: wide: ROIC 119.9% >> WACC 11.8% (high returns above cost of capital)
- Your verdict: [ ] plausible  [ ] too low  [ ] too high  Assumption you would change: 

## CATERPILLAR INC (CAT)

- Weighted value 447.04 (bear 301.91, base 423.31, bull 639.65); market price 800.47.
- WACC 9.83% (risk-free 5.22%, ERP 4.23%, beta 1.08 bottom_up); terminal WACC 9.52%.
- Base: growth 8.9% in year 1 to 3.0% terminal; margin 17.6% to 17.6%; sales to capital 3.62; terminal ROIC 15.5%; terminal value 57% of EV.
  - growth: durable: half the latest growth (10.6%), half the CAGR (7.3%); held 4 years then a linear fade to 3.00%. Evidence: rule: recent growth 10.6% above the CAGR 7.3%
  - margin: holds: from clean operating margin, TTM 17.6% to 17.6% in 5 years. Evidence: Clean operating margin: 13.5% (FY2021) to 20.2% (FY2024) with TTM 17.6%
  - reinvestment: history: 2.5 x the industry's 1.45: the company's 3.65 reflects very little invested capital, not the capital new growth needs; used 3.62. Evidence: Sales to invested capital: median 3.65, industry 1.45
  - tax: median effective 21.3%, converging to the marginal 21.8%
  - terminal_growth: min(risk-free 5.22%, 3%)
  - terminal_roic: moat narrow (chosen narrow): terminal WACC + 6.0% (keeps 50% of the median ROIC's 45.9% excess over WACC, within 2%-6%). Evidence: ROIC median 45.9% vs WACC 9.8%, industry operating margin 15.3%
  - moat: narrow: ROIC median 45.9% vs WACC 9.8%, industry operating margin 15.3%
- Your verdict: [ ] plausible  [ ] too low  [ ] too high  Assumption you would change: 

## NIKE, Inc. (NKE)

- Weighted value 16.71 (bear 6.61, base 16.55, bull 27.13); market price 34.71.
- WACC 9.37% (risk-free 5.22%, ERP 4.23%, beta 1.17 bottom_up); terminal WACC 8.79%.
- Base: growth 0.1% in year 1 to 3.0% terminal; margin 7.8% to 5.4%; sales to capital 3.13; terminal ROIC 14.8%; terminal value 53% of EV.
  - growth: fades_fast: half the latest growth (-1.1%), half the CAGR (-0.2%); held 0 years then a convex fade to 3.00%. Evidence: FY2025 growth -9.8% after FY2024 0.3%
  - margin: compresses: from clean operating margin, TTM 8.4% to 5.4% in 5 years. Evidence: FY2022-2026 operating margin trend -1.4% annually
  - reinvestment: history: the company's median sales to invested capital 3.13; used 3.13. Evidence: Sales to capital 3.13 vs industry 2.62
  - tax: guidance 25.0%, converging to the marginal 22.9%
  - terminal_growth: min(risk-free 5.22%, 3%)
  - terminal_roic: moat narrow (chosen narrow): terminal WACC + 6.0% (keeps 50% of the median ROIC's 32.5% excess over WACC, within 2%-6%). Evidence: Operating margin 11.8% vs industry 9.1%
  - moat: narrow: Operating margin 11.8% vs industry 9.1%
- Your verdict: [ ] plausible  [ ] too low  [ ] too high  Assumption you would change: 
