# trader-tax fixtures

A fixture financial year (FY 2025-26, with trades on both sides of 1 April 2026) for `trader-tax`.

- `zerodha-tradebook-fy2025-26.csv`: Zerodha Console's tradebook layout. Every price is a real NSE price on its
  date: equity at the day's open or close (trader-lab's daily bars in `tests/fixtures/trader-lab/`), NIFTY
  futures and options at the open or close in NSE's F&O bhavcopy (`BhavCopy_NSE_FO_0_0_0_<date>_F_0000.csv.zip`
  for 18 Nov 2025, 20 Nov 2025, 27 Mar 2026 and 6 Apr 2026). Trade and order ids are made up; no personal data.
- `trader/tax/holdings.csv`: 100 SBIN bought on 15 Jun 2017 at that day's close (284.00, NSE bhavcopy
  cm15JUN2017) with its highest price on 31 Jan 2018 (316.60, cm31JAN2018), for the grandfathering rule.
- `trader/tax/carry-forward.csv`, `expenses.csv`, `profile.json`: example inputs (illustrative amounts).
