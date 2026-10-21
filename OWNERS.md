# Owners

Every file in this repository has one owner. The owner reviews every change to it and answers for it when it breaks. Some files also need a second reviewer, listed in the last column.

In this lab you hold all five roles. In a company, each role is a different person or team.

## The five roles

| Role | Owns | Decides |
|---|---|---|
| **AI engineering** | How the assistant answers: the agent loop, retrieval, the system prompt, the inline judge, the web app | How a question becomes an answer, and which model settings to use |
| **LLMOps (platform)** | How the app is built, tested, deployed and observed: CI/CD, scripts, Terraform, the model gateway, tracing | How a change reaches staging, and what blocks it |
| **Security** | Who can reach what: sign-in, IAM, row-level security, the guardrail, secrets, the firewall | What data each person and service may see |
| **HR policy owner** | What is true: the policy documents and the expected test answers | What a correct answer says |
| **Release owner** | Whether a passing version goes to production | Approving the `hr-production-approval` environment and calling a rollback |

The release owner owns a decision, not files. They read the run summary, the evaluation report and the traces, then approve or refuse the production handoff.

## Who owns which files

| Path | Owner | Also reviews |
|---|---|---|
| `lambda/app/assistant.py`, `retrieval.py`, `inline_judge.py`, `prompt_registry.py` | AI engineering | LLMOps |
| `lambda/app/prompts/` | AI engineering | HR policy owner |
| `lambda/app/chunking.py`, `documents.py`, `embeddings.py`, `search_index.py`, `ingest_handler.py` | AI engineering | |
| `lambda/app/chat_handler.py`, `chat_history.py`, `employees.py`, `database.py`, `config.py` | AI engineering | Security |
| `lambda/app/evaluate_handler.py` | AI engineering | LLMOps |
| `lambda/app/model_gateway.py`, `telemetry.py`, `trace_exporter.py` | LLMOps | Security |
| `lambda/app/guardrails.py`, `secrets.py`, `setup_db_handler.py` | Security | AI engineering |
| `lambda/tests/` | AI engineering | |
| `frontend/` | AI engineering | Security (sign-in settings) |
| `lambda/data/documents/` | HR policy owner | |
| `lambda/data/evaluation_questions.json` | HR policy owner | AI engineering |
| `lambda/db/01_schema.sql`, `03_seed_data.sql`, `04_test_accounts.sql`, `generate_employees.py` | AI engineering | Security |
| `lambda/db/02_security.sql` | Security | AI engineering |
| `.github/workflows/` | LLMOps | Security (AWS access) |
| `ci/` and `ci/tests/` | LLMOps | |
| `ci/auth.py` | LLMOps | Security |
| `ci/data/` | LLMOps | |
| `ci/promote_lab.py`, `ci/rollback_lab.py` | LLMOps | Release owner |
| `examples/evaluation/` | LLMOps | AI engineering |
| `scripts/` | LLMOps | |
| `scripts/create-test-users.py`, `prepare-oidc.py`, `open-gateway-ui.py` | LLMOps | Security |
| `infra/terraform/` | LLMOps | |
| `infra/terraform/bedrock.tf`, `iam.tf`, `cognito.tf`, `secrets.tf`, `security_groups.tf`, `waf.tf` | Security | LLMOps |
| `infra/terraform/gateway.tf`, `gateway-bootstrap.sh.tftpl` | LLMOps | Security |
| `docs/`, `README.md`, `OWNERS.md` | LLMOps | |

When two rows match a file, the more specific row wins. For example, `infra/terraform/bedrock.tf` belongs to Security, not to LLMOps.

## What each owner checks in a review

- **AI engineering:** the answer path still uses tools and evidence, and a failed check ends in the safe reply.
- **LLMOps:** the workflow still blocks a failing release, nothing is deployed without its tests, and traces still arrive.
- **Security:** no new access is granted, no secret is printed or stored, and the employee's identity still comes only from the verified token.
- **HR policy owner:** an expected answer changed only because the policy changed. Never change an expected answer to make a failing test pass.
- **Release owner:** the run is green for the exact commit and prompt version being handed off.
