# ghrec — GitHub repo recommender (AWS)

Recall (ItemKNN + trending) → LightGBM LambdaRank → owner-dedup rerank, trained on GH Archive engagement events
(stars, forks, opened issues/PRs, comments, reviews).

## Run locally

```bash
pip install -e ".[dev]"
pytest -q
python -m ghrec.ingest 2026-10-11 2026-11-09 --root data      # backfill; a day that fails QA is skipped and reported
GITHUB_TOKEN=... python -m ghrec.enrich --root data --limit 5000   # repo metadata (read-only token, no scopes)
python -m ghrec.features 2026-11-10 --root data               # point-in-time feature tables
python -m ghrec.train --root data --T 2026-11-10 --spans 30,9,6,3 --k 2,3 --artifacts artifacts/real
MODEL_PATH=artifacts/real/champion.pkl uvicorn service.app:app   # needs ".[service]"
```

`--synthetic` trains on generated data (no network). Set `MLFLOW_TRACKING_URI` to log runs (`pip install ".[mlflow]"`).

## Data platform

| Piece | Where |
|---|---|
| Daily ingest + QA + manifest | `ghrec/ingest.py` |
| Cleaning, k-core (training rows only), versioned snapshots | `ghrec/data.py`, `ghrec/train.py` |
| Repo metadata, append-only history (as-of lookups) | `ghrec/enrich.py` |
| Repo / user feature tables, point-in-time | `ghrec/features.py` |
| S3 <-> local mirror for the stateless job | `ghrec/s3sync.py` |
| MLflow server image | `mlflow/` |
| AWS (Terraform) | `infra/modules/{data_platform,mlflow}`, `infra/{dev,prod}` |

Daily job (ECS Fargate, EventBridge Scheduler, 03:00 UTC): pull S3 → ingest yesterday → enrich ≤5000 repos →
features → push S3. A non-zero exit or a task that never starts publishes to the SNS alerts topic.

## Deploying to AWS (nothing is applied automatically)

Use a separate AWS account/profile for dev and prod. Per environment:

1. `cd infra/dev && terraform init && terraform plan -var image=<placeholder> -var alert_email=<you>` — review.
2. After you approve: `terraform apply`. Confirm the SNS subscription email.
3. Build and push the image to the ECR repo from the outputs (`docker build -t <repo>:<tag> .`).
4. Put the token in Secrets Manager (never in Terraform state):
   `aws secretsmanager put-secret-value --secret-id ghrec-dev/github-token --secret-string <token>`
5. Run the task once by hand (`aws ecs run-task ...`), read the CloudWatch logs, then enable the schedule
   (`daily_schedule_enabled`).

MLflow (optional, `-var enable_mlflow=true -var mlflow_image=...`): build `mlflow/Dockerfile`, push it, and set the three
`<prefix>/mlflow-*` secrets (Postgres URI, admin password, Flask secret). Postgres is external (e.g. Neon free tier).

GPU training quota: request it on day one in Service Quotas (SageMaker "ml.g5.xlarge for training job usage" or EC2
"Running On-Demand G and VT instances"); approval can take days. Needed from the model stage onward.

## Known limits

- Default VPC with public IPs for the job (no NAT cost); use a dedicated VPC before handling sensitive data.
- S3 sync compares file size only and copies the whole bucket; fine for a few GB.
- The API, CD with canary, monitoring and the weekly retrain job are later stages (not in the Terraform yet).
