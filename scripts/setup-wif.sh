#!/usr/bin/env bash
# Bootstrap Workload Identity Federation so GitHub Actions can call Vertex AI
# with NO long-lived service-account key.
#
# Run once, by someone with resourcemanager/iam admin on the project.
# (The interactive account used to build this, kenpkzken@gmail.com, could call
# Vertex but could NOT run `gcloud projects describe` — so this likely needs a
# project owner/admin to execute.)
#
#   ./setup-wif.sh
#
# It prints the two values to paste into the ai-wiki-app repo:
#   GCP_WIF_PROVIDER   -> repo variable or secret
#   GCP_SERVICE_ACCOUNT-> repo variable or secret

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-zken-genai}"
POOL="${POOL:-github-pool}"
PROVIDER="${PROVIDER:-github-provider}"
SA_NAME="${SA_NAME:-ai-wiki-ci}"
GITHUB_REPO="${GITHUB_REPO:-zken-cloud/ai-wiki-app}"

SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "Project : ${PROJECT_ID}"
echo "Repo    : ${GITHUB_REPO}"
echo

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
echo "Project number: ${PROJECT_NUMBER}"

echo "==> Enabling required APIs"
gcloud services enable \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  aiplatform.googleapis.com \
  --project="${PROJECT_ID}"

echo "==> Service account"
gcloud iam service-accounts create "${SA_NAME}" \
  --project="${PROJECT_ID}" \
  --display-name="ai-wiki CI (Vertex AI inference)" 2>/dev/null \
  || echo "    already exists"

echo "==> Granting Vertex AI user (least privilege: inference only)"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/aiplatform.user" \
  --condition=None >/dev/null

echo "==> Workload identity pool"
gcloud iam workload-identity-pools create "${POOL}" \
  --project="${PROJECT_ID}" --location="global" \
  --display-name="GitHub Actions" 2>/dev/null || echo "    already exists"

echo "==> OIDC provider (locked to repo ${GITHUB_REPO})"
gcloud iam workload-identity-pools providers create-oidc "${PROVIDER}" \
  --project="${PROJECT_ID}" --location="global" \
  --workload-identity-pool="${POOL}" \
  --display-name="GitHub OIDC" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository=='${GITHUB_REPO}'" \
  2>/dev/null || echo "    already exists"

echo "==> Allowing that repo to impersonate the service account"
gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --project="${PROJECT_ID}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_REPO}" \
  >/dev/null

PROVIDER_RESOURCE="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"

cat <<EOF

================================================================
Done. Set these on the ai-wiki-app repo:

  gh variable set GCP_WIF_PROVIDER    -R ${GITHUB_REPO} \\
    --body "${PROVIDER_RESOURCE}"

  gh variable set GCP_SERVICE_ACCOUNT -R ${GITHUB_REPO} \\
    --body "${SA_EMAIL}"

  gh variable set GCP_PROJECT         -R ${GITHUB_REPO} --body "${PROJECT_ID}"

Still required (cross-repo push, no OIDC equivalent) -- SSH deploy key:
  ssh-keygen -t ed25519 -N "" -C "ai-wiki-app CI" -f /tmp/k
  gh api -X POST repos/zken-cloud/ai-wiki/keys -f title="ai-wiki-app CI" \\
    -f key="\$(cat /tmp/k.pub)" -F read_only=false
  gh secret set WIKI_DEPLOY_KEY -R ${GITHUB_REPO} < /tmp/k
  rm /tmp/k /tmp/k.pub
================================================================
EOF
