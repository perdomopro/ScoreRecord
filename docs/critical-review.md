# Critical Review

## What Is Strong

- The project is correctly scoped around one tournament, which keeps the data volume and UI small.
- SQLite is the right first database: simple, portable, and enough for 104 tournament matches plus historical priors.
- The offline-first architecture is a good fit for a low-memory homeserver.
- The plan is honest about walk-forward validation and CLV, which matter more than raw hit rate.

## What Needs Tightening Before "Value Bets"

1. Source reliability must be tiered.
   `openfootball/worldcup.json` is useful for schedule and public-domain convenience, but it should be treated as a schedule seed, not a sole official result feed.

2. API coverage cannot be assumed.
   API-Football advertises a 100 requests/day free plan, enough for cautious post-match stats. Odds APIs and player-prop APIs need real coverage checks before the GUI promises shots/cards/props for every match.

3. Alias resolution is a first-class subsystem.
   Team and player names will be the biggest source of silent bugs. Alias keys now fold case and accents, but the 48 qualified teams should still be audited when the final field is known.

4. Player props are a later phase.
   Goalscorers can be estimated from historical scorers. Player shots, assists, cards, and saves need lineups/minutes/events coverage. The first GUI should label those markets as unavailable or market-only until data proves otherwise.

5. Model claims need calibration gates.
   A recommendation should only be emitted when the model-market edge beats a threshold and the market has enough data, the model is calibrated in backtest, and the odds snapshot is fresh.

6. Bankroll logic must be conservative.
   Use fractional Kelly, a hard cap per bet, a hard cap per match/day, and store "no bet" decisions. The "no bet" log is what keeps the model honest.

## Recommended First Milestone

Fase 0 is complete when:

- `fixture` is populated from the World Cup schedule.
- `match_history` is populated from international results since 2018.
- aliases cover all teams present in both sources.
- `team_rating` has an Elo row per known team.
- `team_rating_history` has pre/post Elo rows for every team-match appearance.
- a summary command reports counts and unresolved records.

Only after that should Dixon-Coles and the first GUI be added.

## Current Fase 1.5 Direction

- `ingest_run` records job runs with status and git SHA when available.
- `data_quality` stores quality metrics such as fixture resolution ratio.
- `quality-report` reports unresolved fixtures. For the current 2026 schedule, the unresolved records are knockout placeholders, not alias failures.
- `scorer_goal_history` excludes own goals for the future scorer model.
