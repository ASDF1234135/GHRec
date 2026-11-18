#!/bin/sh
# Shared MLflow tracking server with basic auth. Needs env: BACKEND_STORE_URI (postgresql://...),
# MLFLOW_ADMIN_PASSWORD, MLFLOW_FLASK_SERVER_SECRET_KEY, ARTIFACT_ROOT (s3://bucket/prefix). Optional: PORT (default 8080), MLFLOW_ALLOWED_HOSTS.
# MLflow 3 rejects unknown Host headers by default; the App Runner hostname is not known until after creation, so
# default to "*" (every route is behind basic auth anyway). Set MLFLOW_ALLOWED_HOSTS to your hostname to tighten it.
set -eu
cat > /tmp/basic_auth.ini <<INI
[mlflow]
default_permission = NO_PERMISSIONS
database_uri = ${BACKEND_STORE_URI}
admin_username = admin
admin_password = ${MLFLOW_ADMIN_PASSWORD}
authorization_function = mlflow.server.auth:authenticate_request_basic_auth
INI
export MLFLOW_AUTH_CONFIG_PATH=/tmp/basic_auth.ini
exec mlflow server --app-name basic-auth --host 0.0.0.0 --port "${PORT:-8080}" \
  --backend-store-uri "${BACKEND_STORE_URI}" --artifacts-destination "${ARTIFACT_ROOT}" --serve-artifacts \
  --allowed-hosts "${MLFLOW_ALLOWED_HOSTS:-*}"
