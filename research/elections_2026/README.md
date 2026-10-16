# 2026 House vote-share model vs Kalshi ladders (research)

A first, crude model for `KXVOTEGENERAL` ("<district> House election: <candidate> vote percent")
ladders, to find rungs where a district-level swing model disagrees with the market maker
that quotes nearly all of them. Research only: nothing here trades.

## Model

- **Base**: 2024 two-party Democratic share in the district (538's `election_results_house.csv`).
- **Swing**: coefficients fitted on 2016 -> 2018 (the closest analogue: a midterm wave against
  the president's party), `d2p_2018 = a + b*d2p_2016 + c*incumbent_running*sign + d*winner_sign`.
  Residual SD 3.5 pts. Applied to 2024, shifted by how much bigger the 2026 national swing is
  than 2018's (generic ballot margin is the first argument of `model.py`, default D+8).
- **Uncertainty**: district residual (3.5) and national environment (2.0) in quadrature, plus
  third-party share.
- **Candidate share** = two-party share x (1 - third-party share), third party half 2024's, half the median.

Skipped: states with new maps for 2026 (CA, TX, MO, NC, OH, UT, FL, AL, LA, TN), Alaska (RCV),
uncontested 2024 races, districts whose 2024 totals in the 538 file look incomplete (NY), and
races where the two candidates' parties can't be told.

## Run

```bash
cd research/elections_2026
mkdir -p data/538
curl -sS -o data/538/election_results_house.csv \
  https://raw.githubusercontent.com/fivethirtyeight/election-results/main/election_results_house.csv
python fetch_elec.py      # open KXVOTEGENERAL and KXMIDTERMMOV markets -> data/elections.json
python model.py 8         # generic ballot D+8 -> data/model_rows.json
python compare.py         # model vs market-implied medians
python edges.py           # rungs with >= 5c edge after the taker fee
```

## Known gaps

Big disagreements are mostly the model's gaps, not the market's errors: no candidate quality,
no sophomore surge, no district presidential baseline, no polls. Treat `edges.py` output as a
research list, not as trades.
