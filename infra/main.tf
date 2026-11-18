# Azure infra: API (Container Apps) + scheduled ingest/train jobs sharing an Azure Files volume.
# NOT validated with `terraform plan` (terraform not installed when written) - run plan first.
terraform {
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.0" }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

variable "subscription_id" { type = string }
variable "location" { default = "eastasia" }
variable "prefix" { default = "ghrec" }
variable "image" {
  description = "ACR image tag built by CD, e.g. ghrec.azurecr.io/ghrec:abc123"
  type        = string
}
variable "github_token" {
  description = "Read-only GitHub token for the enrich job (no scopes needed)"
  type        = string
  sensitive   = true
}
variable "redis_url" {
  description = "Redis connection URL (Azure Managed Redis); empty = in-memory fallback"
  type        = string
  default     = ""
  sensitive   = true
}

resource "azurerm_resource_group" "rg" {
  name     = "${var.prefix}-rg"
  location = var.location
}

resource "azurerm_log_analytics_workspace" "law" {
  name                = "${var.prefix}-law"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

# shortcut: ACR admin credentials; move to managed identity + AcrPull when hardening
resource "azurerm_container_registry" "acr" {
  name                = "${var.prefix}acr${substr(md5(azurerm_resource_group.rg.id), 0, 6)}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  sku                 = "Basic"
  admin_enabled       = true
}

resource "azurerm_storage_account" "sa" {
  name                     = "${var.prefix}sa${substr(md5(azurerm_resource_group.rg.id), 0, 6)}"
  resource_group_name      = azurerm_resource_group.rg.name
  location                 = azurerm_resource_group.rg.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
}

resource "azurerm_storage_share" "data" {
  name               = "data"
  storage_account_id = azurerm_storage_account.sa.id
  quota              = 100
}

resource "azurerm_container_app_environment" "env" {
  name                       = "${var.prefix}-env"
  location                   = azurerm_resource_group.rg.location
  resource_group_name        = azurerm_resource_group.rg.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.law.id
}

resource "azurerm_container_app_environment_storage" "data" {
  name                         = "data"
  container_app_environment_id = azurerm_container_app_environment.env.id
  account_name                 = azurerm_storage_account.sa.name
  share_name                   = azurerm_storage_share.data.name
  access_key                   = azurerm_storage_account.sa.primary_access_key
  access_mode                  = "ReadWrite"
}

locals {
  registry = {
    server   = azurerm_container_registry.acr.login_server
    username = azurerm_container_registry.acr.admin_username
    password = azurerm_container_registry.acr.admin_password
  }
}

resource "azurerm_container_app" "api" {
  name                         = "${var.prefix}-api"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Multiple" # keeps old revisions for traffic-split canary / rollback

  registry {
    server               = local.registry.server
    username             = local.registry.username
    password_secret_name = "acr-pw"
  }
  secret {
    name  = "acr-pw"
    value = local.registry.password
  }
  secret {
    name  = "redis-url"
    value = var.redis_url == "" ? "none" : var.redis_url
  }

  template {
    min_replicas = 0
    max_replicas = 3
    container {
      name   = "api"
      image  = var.image
      cpu    = 1
      memory = "2Gi"
      env {
        name        = "REDIS_URL"
        secret_name = "redis-url"
      }
      liveness_probe {
        transport = "HTTP"
        path      = "/health"
        port      = 8000
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
}

# Daily ingest (yesterday) and weekly retrain share one image and one Azure Files volume.
# Ingest command needs a date: wrap in `sh -c` so the date is computed at run time.
resource "azurerm_container_app_job" "ingest" {
  name                         = "${var.prefix}-ingest"
  location                     = azurerm_resource_group.rg.location
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  replica_timeout_in_seconds   = 3600
  replica_retry_limit          = 1

  registry {
    server               = local.registry.server
    username             = local.registry.username
    password_secret_name = "acr-pw"
  }
  secret {
    name  = "acr-pw"
    value = local.registry.password
  }
  schedule_trigger_config {
    cron_expression = "0 3 * * *"
  }
  template {
    volume {
      name         = "data"
      storage_name = azurerm_container_app_environment_storage.data.name
      storage_type = "AzureFile"
    }
    container {
      name    = "ingest"
      image   = var.image
      cpu     = 1
      memory  = "2Gi"
      command = ["sh", "-c", "python -m ghrec.ingest $(date -u -d yesterday +%F) --root /data"]
      volume_mounts {
        name = "data"
        path = "/data"
      }
    }
  }
}

# Daily, after ingest. Each run fetches up to 5000 repos not refreshed in 7 days (busiest first), resuming across runs.
# shortcut: token is a Container Apps secret and lives in Terraform state; move to a Key Vault reference before sharing state.
resource "azurerm_container_app_job" "enrich" {
  name                         = "${var.prefix}-enrich"
  location                     = azurerm_resource_group.rg.location
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  replica_timeout_in_seconds   = 3600
  replica_retry_limit          = 1

  registry {
    server               = local.registry.server
    username             = local.registry.username
    password_secret_name = "acr-pw"
  }
  secret {
    name  = "acr-pw"
    value = local.registry.password
  }
  secret {
    name  = "github-token"
    value = var.github_token
  }
  schedule_trigger_config {
    cron_expression = "0 5 * * *"
  }
  template {
    volume {
      name         = "data"
      storage_name = azurerm_container_app_environment_storage.data.name
      storage_type = "AzureFile"
    }
    container {
      name    = "enrich"
      image   = var.image
      cpu     = 0.5
      memory  = "1Gi"
      command = ["python", "-m", "ghrec.enrich", "--root", "/data", "--min-events", "3", "--limit", "5000"]
      env {
        name        = "GITHUB_TOKEN"
        secret_name = "github-token"
      }
      volume_mounts {
        name = "data"
        path = "/data"
      }
    }
  }
}

resource "azurerm_container_app_job" "train" {
  name                         = "${var.prefix}-train"
  location                     = azurerm_resource_group.rg.location
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  replica_timeout_in_seconds   = 14400
  replica_retry_limit          = 0

  registry {
    server               = local.registry.server
    username             = local.registry.username
    password_secret_name = "acr-pw"
  }
  secret {
    name  = "acr-pw"
    value = local.registry.password
  }
  schedule_trigger_config {
    cron_expression = "0 6 * * 0"
  }
  template {
    volume {
      name         = "data"
      storage_name = azurerm_container_app_environment_storage.data.name
      storage_type = "AzureFile"
    }
    container {
      name    = "train"
      image   = var.image
      cpu     = 2
      memory  = "8Gi"
      command = ["python", "-m", "ghrec.train", "--root", "/data", "--artifacts", "/data/artifacts"]
      volume_mounts {
        name = "data"
        path = "/data"
      }
    }
  }
}

output "api_url" { value = "https://${azurerm_container_app.api.ingress[0].fqdn}" }
output "acr" { value = azurerm_container_registry.acr.login_server }
