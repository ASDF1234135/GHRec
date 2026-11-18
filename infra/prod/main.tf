# prod: full history, daily schedule on. Use a separate AWS account/profile from dev.
# State is local; before sharing, add: backend "s3" { bucket = "...", key = "ghrec/prod.tfstate", region = "..." }
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

provider "aws" {
  region = var.region
}

module "data_platform" {
  source                 = "../modules/data_platform"
  prefix                 = "ghrec-prod"
  image                  = var.image
  alert_email            = var.alert_email
  monthly_budget_usd     = 50
  daily_schedule_enabled = true
}

output "data_bucket" {
  value = module.data_platform.data_bucket
}

output "ecr_repository_url" {
  value = module.data_platform.ecr_repository_url
}
