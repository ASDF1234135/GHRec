# dev: small, 2 weeks of raw data, schedule off until you turn it on. Use a separate AWS account/profile from prod.
# State is local; before sharing, add: backend "s3" { bucket = "...", key = "ghrec/dev.tfstate", region = "..." }
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

variable "region" {
  default = "ap-northeast-1"
}

variable "image" {
  type = string
}

variable "alert_email" {
  type = string
}

variable "enable_mlflow" {
  description = "Create the shared MLflow server (App Runner, bills monthly). Build and push mlflow/Dockerfile first."
  type        = bool
  default     = false
}

variable "mlflow_image" {
  type    = string
  default = ""
}

provider "aws" {
  region = var.region
}

module "data_platform" {
  source                 = "../modules/data_platform"
  prefix                 = "ghrec-dev"
  image                  = var.image
  alert_email            = var.alert_email
  monthly_budget_usd     = 20
  retention_days         = 14
  daily_schedule_enabled = false
}

module "mlflow" {
  count  = var.enable_mlflow ? 1 : 0
  source = "../modules/mlflow"
  prefix = "ghrec-dev"
  image  = var.mlflow_image
}

output "data_bucket" {
  value = module.data_platform.data_bucket
}

output "ecr_repository_url" {
  value = module.data_platform.ecr_repository_url
}
