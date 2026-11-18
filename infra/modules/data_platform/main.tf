# Data platform on AWS: S3 data lake, ECR, ECS Fargate daily job on EventBridge Scheduler,
# failure alerts (SNS) and a budget alert. Nothing here is applied automatically.
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

data "aws_caller_identity" "me" {}
data "aws_region" "current" {}

# shortcut: default VPC with public IPs (no NAT cost); move to a dedicated VPC with private subnets before storing sensitive data.
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_security_group" "jobs" {
  name        = "${var.prefix}-jobs"
  description = "Egress only"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------- storage ----------
resource "aws_s3_bucket" "data" {
  bucket = "${var.prefix}-data-${data.aws_caller_identity.me.account_id}"
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  count  = var.retention_days == null ? 0 : 1
  bucket = aws_s3_bucket.data.id

  rule {
    id     = "expire-raw-events"
    status = "Enabled"
    filter {
      prefix = "raw_events/"
    }
    expiration {
      days = var.retention_days
    }
  }
}

resource "aws_ecr_repository" "app" {
  name = var.prefix
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_cloudwatch_log_group" "jobs" {
  name              = "/ghrec/${var.prefix}"
  retention_in_days = 30
}

# The secret is created empty on purpose: set the value yourself so it never enters Terraform state:
#   aws secretsmanager put-secret-value --secret-id <prefix>/github-token --secret-string <token>
resource "aws_secretsmanager_secret" "github_token" {
  name = "${var.prefix}/github-token"
}

# ---------- IAM ----------
data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.prefix}-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secret" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.github_token.arn }]
  })
}

resource "aws_iam_role" "task" {
  name               = "${var.prefix}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "task_s3" {
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.data.arn },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${aws_s3_bucket.data.arn}/*" },
    ]
  })
}

# ---------- daily job ----------
resource "aws_ecs_cluster" "this" {
  name = var.prefix
}

resource "aws_ecs_task_definition" "daily" {
  family                   = "${var.prefix}-daily"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 4096
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  ephemeral_storage {
    size_in_gib = 30
  }

  container_definitions = jsonencode([{
    name      = "daily"
    image     = var.image
    essential = true
    # pull data -> ingest yesterday -> refresh repo metadata -> feature tables -> push data. Any failure stops the chain.
    command = ["sh", "-c", "python -m ghrec.s3sync pull && python -m ghrec.ingest $(date -u -d yesterday +%F) --root /data && python -m ghrec.enrich --root /data --limit 5000 && python -m ghrec.features $(date -u +%F) --root /data && python -m ghrec.s3sync push"]
    environment = [
      { name = "DATA_BUCKET", value = aws_s3_bucket.data.bucket },
      { name = "DATA_ROOT", value = "/data" },
    ]
    secrets = [{ name = "GITHUB_TOKEN", valueFrom = aws_secretsmanager_secret.github_token.arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.jobs.name
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "daily"
      }
    }
  }])
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecs:RunTask"], Resource = "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.me.account_id}:task-definition/${aws_ecs_task_definition.daily.family}:*" },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = [aws_iam_role.execution.arn, aws_iam_role.task.arn] },
    ]
  })
}

resource "aws_scheduler_schedule" "daily" {
  name                = "${var.prefix}-daily"
  schedule_expression = var.daily_cron
  state               = var.daily_schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_ecs_cluster.this.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.daily.arn
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = data.aws_subnets.default.ids
        security_groups  = [aws_security_group.jobs.id]
        assign_public_ip = true
      }
    }

    retry_policy {
      maximum_retry_attempts = 0 # a failed run alerts; ingest is idempotent, so rerun by hand
    }
  }
}

# ---------- alerts ----------
resource "aws_sns_topic" "alerts" {
  name = "${var.prefix}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Two failure shapes: a container exits non-zero, or the task never starts (bad image, missing secret).
locals {
  failure_patterns = {
    exit_nonzero = { lastStatus = ["STOPPED"], containers = { exitCode = [{ "anything-but" = 0 }] } }
    not_started  = { lastStatus = ["STOPPED"], stopCode = ["TaskFailedToStart"] }
  }
}

resource "aws_cloudwatch_event_rule" "task_failed" {
  for_each = local.failure_patterns
  name     = "${var.prefix}-${each.key}"
  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail        = merge({ clusterArn = [aws_ecs_cluster.this.arn] }, each.value)
  })
}

resource "aws_cloudwatch_event_target" "task_failed" {
  for_each = local.failure_patterns
  rule     = aws_cloudwatch_event_rule.task_failed[each.key].name
  arn      = aws_sns_topic.alerts.arn
}

resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.alerts.arn
    }]
  })
}

resource "aws_budgets_budget" "monthly" {
  name         = "${var.prefix}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
