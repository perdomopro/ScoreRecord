CREATE TABLE IF NOT EXISTS team (
  team_id INTEGER PRIMARY KEY,
  canonical TEXT UNIQUE NOT NULL,
  canonical_key TEXT UNIQUE,
  confederation TEXT,
  created_utc TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS team_alias (
  alias TEXT PRIMARY KEY,
  alias_key TEXT UNIQUE,
  team_id INTEGER NOT NULL REFERENCES team(team_id),
  source TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS player (
  player_id INTEGER PRIMARY KEY,
  canonical TEXT NOT NULL,
  team_id INTEGER REFERENCES team(team_id),
  UNIQUE(canonical, team_id)
);

CREATE TABLE IF NOT EXISTS player_alias (
  alias TEXT PRIMARY KEY,
  player_id INTEGER NOT NULL REFERENCES player(player_id),
  source TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS match_history (
  source_match_id TEXT PRIMARY KEY,
  date TEXT NOT NULL,
  home_id INTEGER NOT NULL REFERENCES team(team_id),
  away_id INTEGER NOT NULL REFERENCES team(team_id),
  home_score INTEGER NOT NULL,
  away_score INTEGER NOT NULL,
  tournament TEXT,
  city TEXT,
  country TEXT,
  neutral INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_match_history_date ON match_history(date);
CREATE INDEX IF NOT EXISTS idx_match_history_teams ON match_history(home_id, away_id);

CREATE TABLE IF NOT EXISTS goal_history (
  source_goal_id TEXT PRIMARY KEY,
  source_match_id TEXT NOT NULL REFERENCES match_history(source_match_id),
  scorer_id INTEGER REFERENCES player(player_id),
  team_id INTEGER REFERENCES team(team_id),
  minute INTEGER,
  own_goal INTEGER NOT NULL DEFAULT 0,
  penalty INTEGER NOT NULL DEFAULT 0
);

CREATE VIEW IF NOT EXISTS scorer_goal_history AS
SELECT *
FROM goal_history
WHERE own_goal = 0;

CREATE TABLE IF NOT EXISTS fixture (
  fixture_id TEXT PRIMARY KEY,
  kickoff_local TEXT,
  kickoff_utc TEXT,
  stage TEXT,
  group_name TEXT,
  home_id INTEGER REFERENCES team(team_id),
  away_id INTEGER REFERENCES team(team_id),
  home_placeholder TEXT,
  away_placeholder TEXT,
  status TEXT NOT NULL DEFAULT 'scheduled',
  home_score INTEGER,
  away_score INTEGER,
  ground TEXT,
  source TEXT NOT NULL DEFAULT 'openfootball',
  updated_utc TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_fixture_kickoff ON fixture(kickoff_utc);
CREATE INDEX IF NOT EXISTS idx_fixture_status ON fixture(status);

CREATE TABLE IF NOT EXISTS fixture_stats (
  fixture_id TEXT NOT NULL REFERENCES fixture(fixture_id),
  team_id INTEGER NOT NULL REFERENCES team(team_id),
  shots INTEGER,
  shots_on_target INTEGER,
  corners INTEGER,
  yellow INTEGER,
  red INTEGER,
  possession REAL,
  source TEXT NOT NULL,
  captured_utc TEXT NOT NULL,
  PRIMARY KEY (fixture_id, team_id, source, captured_utc)
);

CREATE TABLE IF NOT EXISTS odds (
  fixture_id TEXT NOT NULL REFERENCES fixture(fixture_id),
  market TEXT NOT NULL,
  selection TEXT NOT NULL,
  decimal_odds REAL NOT NULL,
  bookmaker TEXT NOT NULL,
  captured_utc TEXT NOT NULL,
  is_closing INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (fixture_id, market, selection, bookmaker, captured_utc)
);

CREATE TABLE IF NOT EXISTS edge_analysis (
  fixture_id TEXT NOT NULL REFERENCES fixture(fixture_id),
  run_id INTEGER NOT NULL REFERENCES ingest_run(run_id),
  prediction_run_id INTEGER REFERENCES ingest_run(run_id),
  model_version TEXT NOT NULL,
  market TEXT NOT NULL,
  selection TEXT NOT NULL,
  prob_model REAL NOT NULL,
  odds_decimal REAL NOT NULL,
  prob_market_devig REAL NOT NULL,
  edge_ev REAL NOT NULL,
  edge_prob REAL NOT NULL,
  bookmaker TEXT NOT NULL,
  captured_utc TEXT NOT NULL,
  prediction_generated_utc TEXT,
  generated_utc TEXT NOT NULL,
  PRIMARY KEY (fixture_id, run_id, market, selection, bookmaker)
);

CREATE INDEX IF NOT EXISTS idx_edge_analysis_lookup
  ON edge_analysis(fixture_id, market, selection, bookmaker, captured_utc);

CREATE TABLE IF NOT EXISTS player_prop (
  fixture_id TEXT NOT NULL REFERENCES fixture(fixture_id),
  player_id INTEGER NOT NULL REFERENCES player(player_id),
  market TEXT NOT NULL,
  line REAL NOT NULL,
  over_odds REAL,
  under_odds REAL,
  bookmaker TEXT,
  captured_utc TEXT NOT NULL,
  PRIMARY KEY (fixture_id, player_id, market, line, bookmaker, captured_utc)
);

CREATE TABLE IF NOT EXISTS prediction (
  run_id INTEGER REFERENCES ingest_run(run_id),
  fixture_id TEXT NOT NULL REFERENCES fixture(fixture_id),
  model TEXT NOT NULL,
  market TEXT NOT NULL,
  selection TEXT NOT NULL,
  prob REAL NOT NULL CHECK (prob >= 0 AND prob <= 1),
  fair_odds REAL NOT NULL,
  generated_utc TEXT NOT NULL,
  model_version TEXT NOT NULL DEFAULT 'dev',
  PRIMARY KEY (fixture_id, model, market, selection, generated_utc)
);

CREATE TABLE IF NOT EXISTS prediction_result (
  result_id INTEGER PRIMARY KEY,
  settlement_run_id INTEGER NOT NULL REFERENCES ingest_run(run_id),
  prediction_run_id INTEGER NOT NULL REFERENCES ingest_run(run_id),
  fixture_id TEXT NOT NULL REFERENCES fixture(fixture_id),
  model_version TEXT NOT NULL,
  market TEXT NOT NULL,
  selection TEXT NOT NULL,
  predicted_prob REAL NOT NULL,
  label INTEGER NOT NULL,
  brier REAL NOT NULL,
  logloss REAL NOT NULL,
  settled_utc TEXT NOT NULL,
  prediction_generated_utc TEXT NOT NULL,
  UNIQUE(prediction_run_id, fixture_id, model_version, market, selection, prediction_generated_utc)
);

CREATE INDEX IF NOT EXISTS idx_prediction_result_fixture
  ON prediction_result(fixture_id, model_version, market);

CREATE TABLE IF NOT EXISTS recommendation (
  run_id INTEGER REFERENCES ingest_run(run_id),
  fixture_id TEXT NOT NULL REFERENCES fixture(fixture_id),
  market TEXT NOT NULL,
  selection TEXT NOT NULL,
  model_prob REAL NOT NULL,
  market_odds REAL NOT NULL,
  edge REAL NOT NULL,
  edge_prob REAL,
  kelly_fraction REAL NOT NULL,
  stake_units REAL NOT NULL,
  confidence TEXT NOT NULL,
  bookmaker TEXT,
  model_version TEXT NOT NULL DEFAULT 'dev',
  odds_captured_utc TEXT,
  paper INTEGER NOT NULL DEFAULT 1,
  generated_utc TEXT NOT NULL,
  PRIMARY KEY (fixture_id, market, selection, generated_utc)
);

CREATE TABLE IF NOT EXISTS recommendation_result (
  result_id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES ingest_run(run_id),
  recommendation_run_id INTEGER REFERENCES ingest_run(run_id),
  fixture_id TEXT NOT NULL REFERENCES fixture(fixture_id),
  market TEXT NOT NULL,
  selection TEXT NOT NULL,
  bookmaker TEXT NOT NULL,
  model_version TEXT NOT NULL,
  odds_taken REAL NOT NULL,
  closing_odds REAL NOT NULL,
  clv REAL NOT NULL,
  recommendation_generated_utc TEXT NOT NULL,
  closing_captured_utc TEXT NOT NULL,
  computed_utc TEXT NOT NULL,
  UNIQUE(recommendation_run_id, fixture_id, market, selection, bookmaker, recommendation_generated_utc)
);

CREATE INDEX IF NOT EXISTS idx_recommendation_result_lookup
  ON recommendation_result(fixture_id, market, selection, bookmaker);

CREATE TABLE IF NOT EXISTS eval_metric (
  run_id INTEGER REFERENCES ingest_run(run_id),
  as_of TEXT NOT NULL,
  model TEXT NOT NULL,
  market TEXT NOT NULL,
  n INTEGER NOT NULL,
  brier REAL,
  logloss REAL,
  roi REAL,
  clv REAL,
  PRIMARY KEY (as_of, model, market)
);

CREATE TABLE IF NOT EXISTS team_rating (
  team_id INTEGER NOT NULL REFERENCES team(team_id),
  rating_system TEXT NOT NULL,
  rating REAL NOT NULL,
  as_of TEXT NOT NULL,
  metadata_json TEXT,
  PRIMARY KEY (team_id, rating_system, as_of)
);

CREATE TABLE IF NOT EXISTS team_rating_history (
  source_match_id TEXT NOT NULL REFERENCES match_history(source_match_id),
  team_id INTEGER NOT NULL REFERENCES team(team_id),
  rating_system TEXT NOT NULL,
  as_of TEXT NOT NULL,
  rating_pre REAL NOT NULL,
  rating_post REAL NOT NULL,
  opponent_id INTEGER NOT NULL REFERENCES team(team_id),
  is_home INTEGER NOT NULL,
  expected_result REAL NOT NULL,
  actual_result REAL NOT NULL,
  metadata_json TEXT,
  PRIMARY KEY (source_match_id, team_id, rating_system)
);

CREATE INDEX IF NOT EXISTS idx_team_rating_history_lookup
  ON team_rating_history(team_id, rating_system, as_of);

CREATE TABLE IF NOT EXISTS ingest_run (
  run_id INTEGER PRIMARY KEY,
  job_name TEXT NOT NULL,
  status TEXT NOT NULL,
  started_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_utc TEXT,
  source TEXT,
  code_git_sha TEXT,
  message TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingest_run_job_started
  ON ingest_run(job_name, started_utc);

CREATE TABLE IF NOT EXISTS data_quality (
  quality_id INTEGER PRIMARY KEY,
  run_id INTEGER REFERENCES ingest_run(run_id),
  check_name TEXT NOT NULL,
  metric_name TEXT NOT NULL,
  metric_value REAL,
  details_json TEXT,
  created_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_data_quality_check
  ON data_quality(check_name, created_utc);
