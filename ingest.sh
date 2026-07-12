#!/bin/bash
cd /home/adrian/scorerecord
for league in EPL LA_LIGA SERIE_A BUNDESLIGA LIGUE_1 CHAMPIONSHIP; do
  .venv/bin/scorerecord ingest-football-data --league $league --from-season=2021-2022 --to-season=2024-2025
done
