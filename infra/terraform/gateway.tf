# LiteLLM runs separately from Lambda; its tables use a separate Aurora database.
data "aws_ssm_parameter" "gateway_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}
resource "random_password" "gateway_master" {
  length  = 40
  special = false
}
resource "random_password" "gateway_db" {
  length  = 32
  special = false
}
resource "aws_secretsmanager_secret" "gateway_settings" {
  name                    = "${local.name}/gateway-settings"
  recovery_window_in_days = 0
}
resource "aws_secretsmanager_secret_version" "gateway_settings" {
  secret_id     = aws_secretsmanager_secret.gateway_settings.id
  secret_string = jsonencode({ master_key = "sk-${random_password.gateway_master.result}", username = "llm_gateway", password = random_password.gateway_db.result })
}
resource "aws_secretsmanager_secret" "gateway_app_key" {
  name                    = "${local.name}/gateway-app-key"
  recovery_window_in_days = 0
}
resource "aws_internet_gateway" "gateway_egress" { vpc_id = aws_vpc.main.id }
resource "aws_subnet" "gateway" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.20.10.0/24"
  availability_zone       = local.azs[0]
  map_public_ip_on_launch = true
}
resource "aws_route_table" "gateway" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.gateway_egress.id
  }
}
resource "aws_route_table_association" "gateway" {
  subnet_id      = aws_subnet.gateway.id
  route_table_id = aws_route_table.gateway.id
}
resource "aws_security_group" "gateway" {
  name   = "${local.name}-gateway"
  vpc_id = aws_vpc.main.id
}
resource "aws_vpc_security_group_ingress_rule" "gateway_lambda" {
  security_group_id            = aws_security_group.gateway.id
  ip_protocol                  = "tcp"
  from_port                    = 4000
  to_port                      = 4000
  referenced_security_group_id = aws_security_group.lambda.id
}
resource "aws_vpc_security_group_egress_rule" "lambda_gateway" {
  security_group_id            = aws_security_group.lambda.id
  ip_protocol                  = "tcp"
  from_port                    = 4000
  to_port                      = 4000
  referenced_security_group_id = aws_security_group.gateway.id
}
resource "aws_vpc_security_group_egress_rule" "gateway_https" {
  security_group_id = aws_security_group.gateway.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}
resource "aws_vpc_security_group_egress_rule" "gateway_postgres" {
  security_group_id            = aws_security_group.gateway.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.aurora.id
}
resource "aws_vpc_security_group_ingress_rule" "aurora_gateway" {
  security_group_id            = aws_security_group.aurora.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.gateway.id
}
resource "aws_iam_role" "gateway" {
  name               = "${local.name}-gateway"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" } }] })
}
resource "aws_iam_role_policy_attachment" "gateway_ssm" {
  role       = aws_iam_role.gateway.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "gateway" {
  role = aws_iam_role.gateway.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], Resource = local.chat_model_arns },
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.gateway_settings.arn },
    # Read as well as write: a rebuilt gateway updates the key an earlier instance saved.
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"], Resource = aws_secretsmanager_secret.gateway_app_key.arn }
  ] })
}
resource "aws_iam_instance_profile" "gateway" { role = aws_iam_role.gateway.name }
resource "aws_instance" "llm_gateway" {
  ami           = data.aws_ssm_parameter.gateway_ami.value
  instance_type = "t3.medium"
  subnet_id     = aws_subnet.gateway.id
  # A fixed address. The API calls a published Lambda version, and each version keeps
  # the gateway address it was published with, so the address must survive replacement.
  private_ip                  = cidrhost(aws_subnet.gateway.cidr_block, 10)
  vpc_security_group_ids      = [aws_security_group.gateway.id]
  iam_instance_profile        = aws_iam_instance_profile.gateway.name
  user_data_replace_on_change = true
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }
  root_block_device {
    volume_size           = 25
    encrypted             = true
    delete_on_termination = true
  }
  user_data = templatefile("${path.module}/gateway-bootstrap.sh.tftpl", {
    region           = var.aws_region, host = aws_rds_cluster.aurora.endpoint,
    settings_secret  = aws_secretsmanager_secret.gateway_settings.arn,
    key_secret       = aws_secretsmanager_secret.gateway_app_key.arn,
    chat_model       = var.chat_model_id,
    fallback_model   = var.fallback_chat_model_id != "" ? var.fallback_chat_model_id : var.chat_model_id
    secondary_region = var.gateway_secondary_region
  })
  depends_on = [aws_secretsmanager_secret_version.gateway_settings, aws_iam_role_policy.gateway, aws_route_table_association.gateway]
  tags       = { Name = "${local.name}-llm-gateway" }
}
output "gateway_instance_id" { value = aws_instance.llm_gateway.id }
output "gateway_settings_secret_arn" { value = aws_secretsmanager_secret.gateway_settings.arn }
output "gateway_app_key_secret_arn" { value = aws_secretsmanager_secret.gateway_app_key.arn }
output "evaluation_function" { value = aws_lambda_function.app["evaluate"].function_name }

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_gateway" {
  security_group_id            = aws_security_group.endpoints.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.gateway.id
}
