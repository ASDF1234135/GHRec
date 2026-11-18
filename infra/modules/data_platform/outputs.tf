output "data_bucket" {
  value = aws_s3_bucket.data.bucket
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "github_token_secret_arn" {
  value = aws_secretsmanager_secret.github_token.arn
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
