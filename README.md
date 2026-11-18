# ghrec — GitHub repo recommender (Azure)

Recall (ItemKNN + trending) → LightGBM LambdaRank → owner-dedup rerank, trained on GH Archive star/fork/issue/PR events.

```bash
pip install -e ".[dev]"
pytest -q
python -m ghrec.train --synthetic --T 2026-11-08      # smoke run, no network
python -m ghrec.ingest 2026-08-10 2026-11-08 --root data   # real backfill (~3 months)
python -m ghrec.train --root data --T 2026-11-08           # train + gate -> artifacts/champion.pkl
MODEL_PATH=artifacts/champion.pkl uvicorn service.app:app  # needs ".[service]"
```

API: `POST /recommend {likes:[owner/repo], session_id}`, `GET /trending`, `POST /feedback`, `GET /health`.

Azure: `infra/main.tf` (Container Apps API + cron jobs, ACR, Azure Files, Log Analytics).
Build the image after training so `artifacts/champion.pkl` is baked in; set `REDIS_URL` for Azure Managed Redis.

Not built yet (see spec): repo enrichment, LightGCN/two-tower/ALS, MMR/exploration rerank, Event Hubs feedback,
monitoring/dashboards, load tests, A/B analysis, CD canary workflow.
