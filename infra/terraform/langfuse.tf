# Application Lambdas stay private. CloudWatch forwards bounded span records to an internet-capable exporter.
variable "langfuse_base_url" {
  type    = string
  default = "https://us.cloud.langfuse.com"
  validation {
    condition     = contains(["https://cloud.langfuse.com", "https://us.cloud.langfuse.com", "https://jp.cloud.langfuse.com"], var.langfuse_base_url)
    error_message = "Use an approved Langfuse Cloud region endpoint."
  }
}
resource "aws_secretsmanager_secret" "langfuse" {
  name        = "${local.name}-langfuse"
  description = "Lab API key pair supplied by secure bootstrap, not Terraform variables or state"
}
resource "aws_iam_role" "trace_exporter" {
  name               = "${local.name}-trace-exporter"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}
resource "aws_iam_role_policy_attachment" "trace_logs" {
  role       = aws_iam_role.trace_exporter.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role_policy" "trace_secret" {
  role   = aws_iam_role.trace_exporter.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.langfuse.arn }] })
}
resource "aws_cloudwatch_log_group" "trace_exporter" {
  name              = "/aws/lambda/${local.name}-trace-exporter"
  retention_in_days = 14
}
resource "aws_lambda_function" "trace_exporter" {
  function_name    = "${local.name}-trace-exporter"
  role             = aws_iam_role.trace_exporter.arn
  handler          = "app.trace_exporter.handler"
  runtime          = "python3.12"
  filename         = local.function_zip
  source_code_hash = filebase64sha256(local.function_zip)
  timeout          = 30
  memory_size      = 256
  environment { variables = { LANGFUSE_SECRET_ARN = aws_secretsmanager_secret.langfuse.arn, LANGFUSE_BASE_URL = var.langfuse_base_url } }
  depends_on = [aws_iam_role_policy_attachment.trace_logs, aws_cloudwatch_log_group.trace_exporter]
}
resource "aws_lambda_permission" "trace_logs" {
  statement_id   = "AllowChatLogs"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.trace_exporter.function_name
  principal      = "logs.${var.aws_region}.amazonaws.com"
  source_arn     = "${aws_cloudwatch_log_group.lambda["chat"].arn}:*"
  source_account = data.aws_caller_identity.current.account_id
}
resource "aws_cloudwatch_log_subscription_filter" "traces" {
  name           = "hr-span-export"
  log_group_name = aws_cloudwatch_log_group.lambda["chat"].name
  # Spans and user-feedback scores both go to the exporter.
  filter_pattern  = "{ ($.trace_record = \"hr_otel\") || ($.trace_record = \"hr_score\") }"
  destination_arn = aws_lambda_function.trace_exporter.arn
  depends_on      = [aws_lambda_permission.trace_logs]
}
output "langfuse_secret_arn" { value = aws_secretsmanager_secret.langfuse.arn }

variable "trace_synthetic_content" {
  type        = bool
  default     = false
  description = "Explicit approval to export question/answer content from synthetic staging data only"
}

