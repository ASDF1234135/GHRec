variable "prefix" {
  description = "Name prefix, e.g. ghrec-dev"
  type        = string
}

variable "image" {
  description = "ECR image (repository_url:tag) the jobs run. Build and push it first; the first apply can use a placeholder."
  type        = string
}

variable "alert_email" {
  description = "Receives job-failure alerts and budget alerts (confirm the SNS subscription email once)"
  type        = string
}

variable "monthly_budget_usd" {
  type = number
}

variable "retention_days" {
  description = "Expire raw_events after N days (dev keeps 2 weeks). null = keep forever (prod)."
  type        = number
  default     = null
}

variable "daily_schedule_enabled" {
  type    = bool
  default = true
}

variable "daily_cron" {
  description = "EventBridge Scheduler cron, UTC. After GH Archive has published the previous day."
  type        = string
  default     = "cron(0 3 * * ? *)"
}
