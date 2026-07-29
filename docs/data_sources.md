# ScoreRecord data sources

This document tracks which external providers ScoreRecord can use, what each
provider is good for, and the guardrails before a source is allowed to affect
forecast output.

No API key should ever be stored in this document or committed to the repo.

## Provider roles

| Provider | Best use in ScoreRecord | Current integration | Notes |
| --- | --- | --- | --- |
| odds-api.io | Upcoming fixtures, live/prematch odds snapshots, bookmaker coverage | `ingest-fixtures-odds-api-io`, `fetch-odds`, `odds_snapshots` | Server-side only. Use `/leagues` to verify slugs before adding leagues. |
| SportMonks Football API | Fixture enrichment, participants, venue, state, scores, events, lineups, xG, odds by fixture/round | Read-only probe: `provider-probe-sportmonks-fixture` | Good enrichment candidate; do not use provider predictions/value-bet fields as ScoreRecord model truth. |
| BigBallsData Soccer API | Live/scheduled match list, standings, odds, lineups, stats/xG, events under one schema | Not yet ingested; documented as provider candidate | Useful for cross-provider fixture/score verification and future GUI enrichment. |
| TheStatsAPI | Fixtures/results, standings, match/player stats, odds and xG across many competitions | Not yet ingested; documented as provider candidate | Useful for broad coverage and xG/history checks if plan coverage includes target leagues. |
| Football-Data CSV | Historical club match results and OU2.5/1X2 odds coverage for benchmark/backtest | `ingest-football-data`, `historical-odds-report`, `bridge-club-history` | Current `/new/*.csv` feeds are results/history, not future fixtures. |

## odds-api.io

Official docs:

- API introduction: https://docs.odds-api.io/api-reference/introduction
- Events endpoint: https://docs.odds-api.io/api-reference/events/get-events
- Workflow with `/leagues` -> `/events` -> `/odds`: https://odds-api.io/blog/free-odds-api

ScoreRecord usage:

- Discover provider slugs from `/v3/leagues?apiKey=...&sport=football&all=true`.
- Ingest upcoming events from `/v3/events?apiKey=...&sport=football&league=<slug>`.
- Fetch odds snapshots from `/v3/odds?apiKey=...&eventId=<id>`.
- Store provider event IDs in `fixture.source_event_id` with `fixture.source='odds_api_io'`.

Current catalog-backed league slugs:

| Internal code | Label | odds-api.io slug | Forecast status |
| --- | --- | --- | --- |
| `LIGA_MX` | Liga MX | `mexico-liga-mx-apertura` | Active pilot |
| `MLS` | MLS | `usa-mls` | Active pilot |
| `EPL` | Premier League | `england-premier-league` | Active pilot |
| `ARGENTINA` | Argentina | `argentina-primera-lpf-clausura` | Active pilot |
| `BRASILEIRAO` | Brasileirao | `brazil-brasileiro-serie-a` | Active pilot |
| `CHAMPIONS_LEAGUE` | Champions League | `international-clubs-uefa-champions-league-qualification` | Provider watchlist only |
| `EUROPA_LEAGUE` | Europa League | `international-clubs-uefa-europa-league-qualification` | Provider watchlist only |
| `LIBERTADORES` | Libertadores | `international-clubs-conmebol-libertadores-knockout-stage` | Provider watchlist only |
| `SUDAMERICANA` | Sudamericana | `international-clubs-conmebol-sudamericana-knockout-stage` | Provider watchlist only |

Guardrail:

Do not surface a league as a forecast pilot until it has historical rows, bridged
`match_history`, resolved future fixtures, Elo coverage, and league-scoped
`model_artifact` provenance.

As of the 2026-07-29 homeserver validation, `ARGENTINA` and `BRASILEIRAO`
passed this checklist for forecast-only GUI use. Champions League, Europa
League, Libertadores, and Sudamericana remain provider watchlist only because
they do not yet have explicit league-scoped history/artifact policy in
ScoreRecord.

## SportMonks

Official docs:

- Endpoint overview: https://docs.sportmonks.com/v3/endpoints-and-entities/endpoints
- Fixtures endpoint: https://docs.sportmonks.com/v3/endpoints-and-entities/endpoints/fixtures
- Fixtures by date range: https://docs.sportmonks.com/v3/endpoints-and-entities/endpoints/fixtures/get-fixtures-by-date-range
- Leagues endpoint: https://docs.sportmonks.com/v3/endpoints-and-entities/endpoints/leagues

Useful endpoint shapes:

- Fixture detail:
  `GET /v3/football/fixtures/{fixture_id}?include=participants;league;venue;state;scores;events.type;lineups.player;xGFixture.type`
- Date range:
  `GET /v3/football/fixtures/between/{from}/{to}?include=participants;league;state;scores`
- League structure:
  `GET /v3/football/leagues/{league_id}?include=currentSeason.stages`

Best ScoreRecord fit:

- Enrich `/match` details with venue/state/lineups/xG only after fixture IDs are
  mapped to ScoreRecord fixtures.
- Use as cross-check for final scores before settlement.
- Keep SportMonks predictions/value-bet data out of ScoreRecord model scoring.

## BigBallsData

Official docs:

- Soccer API: https://bigballsdata.com/soccer-api
- Endpoint reference: https://bigballsdata.com/docs/endpoints

Useful endpoint shapes:

- `GET /v1/leagues?sport=football`
- `GET /v1/matches?sport=football&league=<league_key>`
- `GET /v1/matches/:id/odds`
- `GET /v1/stored/matches/:id/lineups`
- `GET /v1/stored/matches/:id/stats`
- `GET /v1/matches/:id/events`

Best ScoreRecord fit:

- Secondary fixture/score source.
- Future health panel provider check.
- Enrichment for stats/xG once mapped to fixture IDs.

## TheStatsAPI

Official docs:

- Football API: https://www.thestatsapi.com/football-api
- Coverage: https://www.thestatsapi.com/coverage
- MLS recipe/endpoints: https://www.thestatsapi.com/blog/soccer-stats-api

Useful endpoint shapes:

- `GET /football/competitions?search=<league>`
- `GET /football/competitions/{competition_id}/seasons`
- `GET /football/matches?competition_id=<id>&season_id=<id>`
- `GET /football/matches/{match_id}/stats`
- `GET /football/matches/{match_id}/odds`
- `GET /football/matches/{match_id}/odds/live`

Best ScoreRecord fit:

- Broad competition coverage if plan includes the target league.
- xG and odds enrichment for markets that ScoreRecord does not yet model.
- Future comparison source for provider reliability.

## League admission checklist

1. Verify provider slug/competition ID from an official catalog endpoint.
2. Ingest or load historical match rows.
3. Bridge historical rows into canonical `match_history`.
4. Ingest future fixtures.
5. Resolve all teams and aliases.
6. Rebuild Elo and confirm rating coverage.
7. Publish league-scoped predictions.
8. Confirm `prediction.artifact_id -> model_artifact.training_policy.league_code`.
9. Add the league to the GUI only after readiness is `READY_FOR_PILOT`.
