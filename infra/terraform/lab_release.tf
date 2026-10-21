# The lab API invokes a candidate alias, never the function's mutable $LATEST.
variable "lab_environment" {
  type    = string
  default = "staging"
  validation {
    condition     = var.lab_environment == "staging"
    error_message = "This teaching stack must be isolated staging."
  }
}

resource "aws_lambda_alias" "candidate" {
  name             = "candidate"
  function_name    = aws_lambda_function.app["chat"].function_name
  function_version = aws_lambda_function.app["chat"].version
  # CI owns candidate movements; Terraform owns the alias resource.
  lifecycle {
    ignore_changes = [function_version]
  }
}

output "candidate_function" {
  value = aws_lambda_function.app["chat"].function_name
}

output "cognito_client_id" {
  value = aws_cognito_user_pool_client.web.id
}

resource "aws_secretsmanager_secret" "evaluation_users" {
  name        = "${local.name}-evaluation-users"
  description = "Synthetic staging-only evaluation passwords; secure bootstrap owns the value"
}
output "evaluation_users_secret_arn" { value = aws_secretsmanager_secret.evaluation_users.arn }

# The demo employees people sign in as. Kept apart from the pipeline's test accounts above,
# so the CI role, which can read only that secret, never holds a person's password.
resource "aws_secretsmanager_secret" "demo_users" {
  name                    = "${local.name}-demo-users"
  description             = "Shared sign-in password for the demo employees; set by setup, never by CI"
  recovery_window_in_days = 0 # deleted at once on destroy, so the name can be reused
}
output "demo_users_secret_arn" { value = aws_secretsmanager_secret.demo_users.arn }
