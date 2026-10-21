#!/usr/bin/env bash
# One setup command: runs Terraform, then prepares the application.
#
#   bash scripts/setup-lab.sh          asks before changing AWS infrastructure
#   bash scripts/setup-lab.sh --yes    applies infrastructure changes without asking
#
# Safe to rerun. Steps whose inputs have not changed are skipped, and every other step
# leaves an existing deployment as it is. Each step's full output goes to build/setup.log.
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p build
LOG=build/setup.log
: > "$LOG"
AUTO_APPROVE=false
[ "${1:-}" = "--yes" ] && AUTO_APPROVE=true

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  GREEN=$'\033[32m' RED=$'\033[31m' YELLOW=$'\033[33m' BOLD=$'\033[1m' RESET=$'\033[0m' CLEAR=$'\r\033[K'
else
  GREEN='' RED='' YELLOW='' BOLD='' RESET='' CLEAR=''
fi

elapsed() { local s=$1; if [ "$s" -ge 60 ]; then echo "$((s / 60))m $((s % 60))s"; else echo "${s}s"; fi; }
ok() { printf '%s  %s✓%s %s %s(%s)%s\n' "$CLEAR" "$GREEN" "$RESET" "$1" "$YELLOW" "$2" "$RESET"; }

# step "Checklist label" "What to do if it fails" command [args...]
# Runs the command with its output in the log, shows a live timer, then a green check or a clear failure.
step() {
  local label=$1 hint=$2 start=$SECONDS from
  shift 2
  from=$(wc -l < "$LOG")
  echo "===== $label" >> "$LOG"
  ( "$@" ) >> "$LOG" 2>&1 &
  local pid=$!
  if [ -n "$CLEAR" ]; then
    while kill -0 "$pid" 2>/dev/null; do
      printf '%s  … %s %s' "$CLEAR" "$label" "$(elapsed $((SECONDS - start)))"
      sleep 1
    done
  fi
  if wait "$pid"; then
    ok "$label" "$(elapsed $((SECONDS - start)))"
  else
    printf '%s  %s✗ %s%s\n\n' "$CLEAR" "$RED" "$label" "$RESET"
    printf '  %s\n\n' "$hint"
    local output
    output=$(tail -n +"$((from + 2))" "$LOG" | grep -v '^[[:space:]]*$')
    if grep -q '^Traceback' <<< "$output"; then
      # A Python error: its last line names the problem; the rest is internal detail.
      printf '  Error: %s\n' "$(tail -n 1 <<< "$output")"
    else
      tail -n 20 <<< "$output" | sed 's/^/  | /'
    fi
    printf '\n  Full output: %s\n' "$LOG"
    printf '  When it is fixed, run %sbash scripts/setup-lab.sh%s again. Finished steps are skipped or repeated safely.\n' "$BOLD" "$RESET"
    exit 1
  fi
}

skip() { printf '  %s✓%s %s %s(%s)%s\n' "$GREEN" "$RESET" "$1" "$YELLOW" "$2" "$RESET"; }
trap 'printf "\n\n  Stopped. Run bash scripts/setup-lab.sh again to continue.\n"; exit 130' INT

# Windows has a python3 placeholder that only opens the Microsoft Store, so test that it runs.
if python3 -c '' 2>/dev/null; then PYTHON=python3
elif python -c '' 2>/dev/null; then PYTHON=python
else printf '  %s✗ Python not found.%s Install Python 3.12, then rerun.\n' "$RED" "$RESET"; exit 1; fi

# A fingerprint of some input files, used to skip a step when they have not changed.
fingerprint() { "$PYTHON" -c "import hashlib,sys;h=hashlib.sha256();[h.update(open(f,'rb').read()) for f in sys.argv[1:]];print(h.hexdigest())" "$@"; }
unchanged() { [ -f "$1" ] && [ -f "$2" ] && [ "$(cat "$2")" = "$3" ]; }

printf '\n%sHR assistant lab setup%s\n\n' "$BOLD" "$RESET"

step "Tools, configuration files and AWS account" \
  "Setup has not changed anything yet. Fix each item listed below, then rerun." \
  "$PYTHON" scripts/check-setup.py

# Every AWS call below uses the region from terraform.tfvars, whatever the CLI default is.
AWS_REGION=$(sed -n 's/^ *aws_region *= *"\([^"]*\)".*/\1/p' infra/terraform/terraform.tfvars)
export AWS_REGION=${AWS_REGION:-us-east-1} AWS_DEFAULT_REGION=${AWS_REGION:-us-east-1}
# Terraform reads the Langfuse endpoint from .env.langfuse; check-setup.py confirmed it matches terraform.tfvars.
export TF_VAR_langfuse_base_url
TF_VAR_langfuse_base_url=$(sed -n 's/^ *LANGFUSE_BASE_URL *= *//p' .env.langfuse | tr -d "\"'\r" | sed 's:/*$::')

python_environment() {
  "$PYTHON" -m venv .venv
  # macOS and Linux put the activate script in bin/, Windows in Scripts/.
  if [ -f .venv/Scripts/activate ]; then source .venv/Scripts/activate; else source .venv/bin/activate; fi
  python -m pip install --disable-pip-version-check -r ci/requirements.txt
}
step "Python environment (.venv)" \
  "Python could not create .venv or install its packages. Check your internet connection. If .venv is damaged, delete the .venv folder." \
  python_environment
if [ -f .venv/Scripts/activate ]; then source .venv/Scripts/activate; else source .venv/bin/activate; fi

LAYER_INPUTS=$(fingerprint lambda/requirements.txt scripts/build-layer.sh)
if unchanged build/layer.zip build/.layer-inputs "$LAYER_INPUTS"; then
  skip "Lambda libraries (build/layer.zip)" "unchanged, skipped"
else
  build_layer() { bash scripts/build-layer.sh && echo "$LAYER_INPUTS" > build/.layer-inputs; }
  step "Lambda libraries (build/layer.zip)" \
    "Downloading the Linux libraries for Lambda failed. Check your internet connection." \
    build_layer
fi

step "Application package (build/function.zip)" \
  "Packaging the application failed. A prompt or data file may contain a mistake; the error names it." \
  python ci/release.py

step "Terraform providers" \
  "Terraform could not download its providers. Check your internet connection." \
  terraform -chdir=infra/terraform init -input=false -no-color

plan_infrastructure() {
  rm -f build/tfplan build/.tfplan-has-changes
  terraform -chdir=infra/terraform plan -input=false -no-color -detailed-exitcode -out=../../build/tfplan
  local code=$?
  [ "$code" -eq 2 ] && touch build/.tfplan-has-changes
  [ "$code" -ne 1 ]
}
step "Infrastructure plan" \
  "Terraform could not plan the infrastructure. The message names the setting or AWS permission at fault." \
  plan_infrastructure

if [ -f build/.tfplan-has-changes ]; then
  SUMMARY=$(grep -E '^Plan: ' "$LOG" | tail -n 1)
  printf '\n  Terraform will change your AWS infrastructure. %s%s%s\n' "$BOLD" "$SUMMARY" "$RESET"
  # Name anything Terraform deletes or recreates, so a database replacement is never a surprise.
  REMOVED=$(sed -n 's/^  # \(.*\) \(will be destroyed\|must be replaced\)$/\1 (\2)/p' "$LOG")
  if [ -n "$REMOVED" ]; then
    printf '  %sThese will be deleted or recreated:%s\n' "$YELLOW" "$RESET"
    sed 's/^/    /' <<< "$REMOVED"
  fi
  if [ "$AUTO_APPROVE" = false ]; then
    read -r -p "  Apply these changes? [y/N] " ANSWER
    # Windows terminals can send a carriage return with Enter; ignore it and any spaces.
    ANSWER=$(printf '%s' "$ANSWER" | tr -d '\r[:space:]')
    case "$ANSWER" in
      [yY]|[yY][eE][sS]) ;;
      *) printf '\n  Nothing was changed. Rerun when you are ready.\n'; exit 0 ;;
    esac
  fi
  echo
  step "AWS infrastructure (the longest step; progress is in $LOG)" \
    "Terraform stopped partway. Rerunning continues from where it stopped. If the same error repeats, it names the resource at fault." \
    terraform -chdir=infra/terraform apply -input=false -no-color ../../build/tfplan
else
  skip "AWS infrastructure" "already up to date"
fi
rm -f build/tfplan build/.tfplan-has-changes
terraform -chdir=infra/terraform output -json > build/terraform-outputs.json
output() { terraform -chdir=infra/terraform output -raw "$1"; }

# Invoke the setup-db function with the JSON event in $1 and fail if the function reports an error.
invoke_setup_function() {
  local function_name
  function_name=$(python -c "import json;print(json.load(open('build/terraform-outputs.json'))['function_names']['value']['setup-db'])")
  printf '%s' "$1" > build/setup-event.json
  aws lambda invoke --function-name "$function_name" --payload fileb://build/setup-event.json \
    --cli-read-timeout 300 --output json build/setup-result.json > build/setup-meta.json
  cat build/setup-result.json
  python -c "import json; m=json.load(open('build/setup-meta.json')); r=json.load(open('build/setup-result.json')); failed='FunctionError' in m or (isinstance(r,dict) and 'errorMessage' in r); exit('The setup-db function failed: '+str(r.get('errorMessage', r) if isinstance(r,dict) else r)) if failed else None"
}
step "Database tables and demo data" \
  "The database setup function failed. The database may still have been starting, so rerun once. If it repeats, open the setup-db function's CloudWatch logs." \
  invoke_setup_function '{}'

step "Model gateway" \
  "The model gateway did not finish starting. Rerun to wait again. If it repeats, check the gateway instance in the EC2 console." \
  python scripts/initialize-gateway.py

upload_documents() {
  local bucket uploads
  bucket=$(output documents_bucket)
  # sync sends only new or changed files. The manifest goes last because uploading it starts ingestion.
  uploads=$(aws s3 sync lambda/data/documents/ "s3://$bucket/" --exclude manifest.json --no-progress)
  uploads+=$(aws s3 sync lambda/data/documents/ "s3://$bucket/" --exclude '*' --include manifest.json --no-progress)
  echo "$uploads"
  if [ -n "$uploads" ]; then touch build/.documents-uploaded; else rm -f build/.documents-uploaded; fi
}
step "Policy documents" \
  "Uploading the policy documents to S3 failed. Check that your AWS credentials allow S3 uploads." \
  upload_documents

FRONTEND_INPUTS=$(fingerprint frontend/package-lock.json)
build_frontend() {
  output frontend_env > frontend/.env.production
  if ! unchanged frontend/node_modules/.package-lock.json build/.npm-inputs "$FRONTEND_INPUTS"; then
    npm --prefix frontend ci --no-audit --no-fund && echo "$FRONTEND_INPUTS" > build/.npm-inputs
  fi
  npm --prefix frontend run build
}
step "Web frontend build" \
  "Building the web frontend failed. npm downloads packages, so check your internet connection." \
  build_frontend

publish_frontend() {
  local bucket distribution
  bucket=$(output frontend_bucket)
  distribution=$(output cloudfront_distribution_id)
  aws s3 sync frontend/dist/ "s3://$bucket/" --exclude index.html --cache-control 'public,max-age=31536000,immutable' --no-progress
  aws s3 cp frontend/dist/index.html "s3://$bucket/index.html" --cache-control 'no-cache' --no-progress
  # Git Bash on Windows rewrites arguments that start with / into Windows paths; this turns that off.
  MSYS_NO_PATHCONV=1 aws cloudfront create-invalidation --distribution-id "$distribution" --paths '/index.html' '/' --output json
}
step "Web frontend published" \
  "Uploading the web frontend failed. Rerun; only changed files are sent again." \
  publish_frontend

step "Sign-in accounts: demo employees and pipeline test accounts" \
  "Creating the sign-in accounts in Cognito failed. Rerun; existing accounts keep their passwords." \
  python scripts/create-test-users.py

step "Langfuse keys stored in Secrets Manager" \
  "Storing the Langfuse keys failed. Check the keys in .env.langfuse." \
  python scripts/configure-langfuse.py

# Wait for ingestion only when documents were uploaded; otherwise the index is already built.
READINESS_FLAG=--skip-ingestion-wait
[ -f build/.documents-uploaded ] && READINESS_FLAG=--wait-for-ingestion
step "Readiness check: document search and a signed-in chat request" \
  "The app is deployed but did not answer yet. Wait a minute and rerun; the earlier steps will be skipped or repeated safely." \
  python scripts/verify-readiness.py "$READINESS_FLAG"

# The readiness check chats as a pipeline test account. Delete that chat, and any left by failed requests.
remove_test_chats() {
  local ids='[]'
  [ -f build/test-conversations.json ] && ids=$(cat build/test-conversations.json)
  invoke_setup_function "{\"delete_conversations\": $ids}" && rm -f build/test-conversations.json
}
step "Test chat history removed" \
  "Deleting the readiness check's test chat failed. The app works; rerun to try the cleanup again." \
  remove_test_chats

python scripts/show-demo-users.py

# Start the one file that collects every GitHub setting for document 06b. Document 05 adds the role ARN.
python scripts/github-settings.py || true
