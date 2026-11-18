# Shared MLflow tracking server on App Runner: Postgres backend (external, e.g. Neon free tier), artifacts in S3,
# basic-auth admin. The image comes from ../../mlflow/Dockerfile. Not applied automatically; App Runner bills ~USD 5+/month.
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

variable "prefix" {
  type = string
}

variable "image" {
  description = "ECR image of the MLflow server (repository_url:tag)"
  type        = string
}

data "aws_caller_identity" "me" {}

resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.prefix}-mlflow-${data.aws_caller_identity.me.account_id}"
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_ecr_repository" "mlflow" {
  name = "${var.prefix}-mlflow"
}

# Created empty; set the values yourself so they never enter Terraform state:
#   aws secretsmanager put-secret-value --secret-id <prefix>/mlflow-backend-uri --secret-string 'postgresql://user:pw@host/db'
#   aws secretsmanager put-secret-value --secret-id <prefix>/mlflow-admin-password --secret-string '<strong password>'
#   aws secretsmanager put-secret-value --secret-id <prefix>/mlflow-flask-secret --secret-string '<long random string>'
resource "aws_secretsmanager_secret" "backend_uri" {
  name = "${var.prefix}/mlflow-backend-uri"
}

resource "aws_secretsmanager_secret" "admin_password" {
  name = "${var.prefix}/mlflow-admin-password"
}

# MLflow's auth app refuses to start without a Flask secret key (CSRF); keep it stable across restarts.
resource "aws_secretsmanager_secret" "flask_secret" {
  name = "${var.prefix}/mlflow-flask-secret"
}

data "aws_iam_policy_document" "build_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["build.apprunner.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "build" {
  name               = "${var.prefix}-mlflow-build"
  assume_role_policy = data.aws_iam_policy_document.build_assume.json
}

resource "aws_iam_role_policy_attachment" "build" {
  role       = aws_iam_role.build.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess"
}

data "aws_iam_policy_document" "instance_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["tasks.apprunner.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${var.prefix}-mlflow-instance"
  assume_role_policy = data.aws_iam_policy_document.instance_assume.json
}

resource "aws_iam_role_policy" "instance" {
  role = aws_iam_role.instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [aws_secretsmanager_secret.backend_uri.arn, aws_secretsmanager_secret.admin_password.arn, aws_secretsmanager_secret.flask_secret.arn] },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.artifacts.arn },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "${aws_s3_bucket.artifacts.arn}/*" },
    ]
  })
}

resource "aws_apprunner_service" "mlflow" {
  service_name = "${var.prefix}-mlflow"

  source_configuration {
    authentication_configuration {
      access_role_arn = aws_iam_role.build.arn
    }
    image_repository {
      image_identifier      = var.image
      image_repository_type = "ECR"
      image_configuration {
        port = "8080"
        runtime_environment_variables = {
          ARTIFACT_ROOT = "s3://${aws_s3_bucket.artifacts.bucket}/artifacts"
        }
        runtime_environment_secrets = {
          BACKEND_STORE_URI              = aws_secretsmanager_secret.backend_uri.arn
          MLFLOW_ADMIN_PASSWORD          = aws_secretsmanager_secret.admin_password.arn
          MLFLOW_FLASK_SERVER_SECRET_KEY = aws_secretsmanager_secret.flask_secret.arn
        }
      }
    }
    auto_deployments_enabled = false
  }

  instance_configuration {
    cpu               = "512"
    memory            = "1024"
    instance_role_arn = aws_iam_role.instance.arn
  }

  # TCP, not HTTP: every MLflow route sits behind basic auth, so an HTTP probe would get 401.
  health_check_configuration {
    protocol = "TCP"
  }
}

output "mlflow_url" {
  value = "https://${aws_apprunner_service.mlflow.service_url}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.mlflow.repository_url
}
