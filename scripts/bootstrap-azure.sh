#!/usr/bin/env bash
# Run in Azure Cloud Shell (Bash) with a signed-in administrator.
# Creates only the resource group, identities, GitHub federation and deployment roles.
# Does not create data services, Function Apps, Graph grants or ingestion code.
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo 'Usage: bash scripts/bootstrap-azure.sh SUBSCRIPTION_ID TENANT_ID LOCATION development|production' >&2
  exit 2
fi
ci_subscription_id="$1"
ci_tenant_id="$2"
ci_location="$3"
ci_environment="$4"
case "$ci_environment" in development) ci_suffix=dev ;; production) ci_suffix=prod ;; *) echo 'Use development or production.' >&2; exit 2 ;; esac

az account set --subscription "$ci_subscription_id"
ci_actual_tenant=$(az account show --query tenantId -o tsv)
if [[ "${ci_actual_tenant,,}" != "${ci_tenant_id,,}" ]]; then
  echo 'Selected subscription belongs to a different tenant. No resources created.' >&2
  exit 1
fi

ci_rg="rg-fs-communication-intelligence-${ci_suffix}"
ci_scope="/subscriptions/${ci_subscription_id}/resourceGroups/${ci_rg}"
ci_issuer='https://token.actions.githubusercontent.com'
ci_subject="repo:Finishes-Solutions/fs-communication-intelligence:environment:${ci_environment}"
ci_deploy_name="id-fs-ci-github-${ci_suffix}"

az group create --name "$ci_rg" --location "$ci_location" \
  --tags application=communication-intelligence environment="$ci_environment" --output none

for ci_identity_name in "$ci_deploy_name" "id-fs-ci-ingestion-${ci_suffix}" "id-fs-ci-search-${ci_suffix}" "id-fs-ci-maintenance-${ci_suffix}"; do
  az identity create --name "$ci_identity_name" --resource-group "$ci_rg" --location "$ci_location" --output none
done

ci_deploy_principal=$(az identity show --name "$ci_deploy_name" --resource-group "$ci_rg" --query principalId -o tsv)
ci_existing_count=$(az identity federated-credential list --identity-name "$ci_deploy_name" --resource-group "$ci_rg" \
  --query "length([?name=='github-environment'])" -o tsv)
if [[ "$ci_existing_count" == 0 ]]; then
  az identity federated-credential create --name github-environment --identity-name "$ci_deploy_name" \
    --resource-group "$ci_rg" --issuer "$ci_issuer" --subject "$ci_subject" \
    --audiences api://AzureADTokenExchange --output none
else
  ci_existing_subject=$(az identity federated-credential show --name github-environment --identity-name "$ci_deploy_name" --resource-group "$ci_rg" --query subject -o tsv)
  ci_existing_issuer=$(az identity federated-credential show --name github-environment --identity-name "$ci_deploy_name" --resource-group "$ci_rg" --query issuer -o tsv)
  ci_audience_count=$(az identity federated-credential show --name github-environment --identity-name "$ci_deploy_name" --resource-group "$ci_rg" --query "length(audiences)" -o tsv)
  ci_existing_audience=$(az identity federated-credential show --name github-environment --identity-name "$ci_deploy_name" --resource-group "$ci_rg" --query 'audiences[0]' -o tsv)
  if [[ "$ci_existing_subject" != "$ci_subject" || "$ci_existing_issuer" != "$ci_issuer" || "$ci_audience_count" != 1 || "$ci_existing_audience" != 'api://AzureADTokenExchange' ]]; then
    echo 'Existing federation differs from the expected GitHub environment. Review it manually.' >&2
    exit 1
  fi
fi

# Contributor deploys resources. RBAC Administrator is needed for infrastructure
# role assignments. Remove the latter after setup if the release process no
# longer needs to manage role assignments; ordinary code release does not need it.
for ci_role in 'Contributor' 'Role Based Access Control Administrator'; do
  ci_role_count=$(az role assignment list --assignee "$ci_deploy_principal" --scope "$ci_scope" \
    --query "length([?roleDefinitionName=='${ci_role}' && scope=='${ci_scope}'])" -o tsv)
  if [[ "$ci_role_count" == 0 ]]; then
    az role assignment create --assignee-object-id "$ci_deploy_principal" \
      --assignee-principal-type ServicePrincipal --role "$ci_role" --scope "$ci_scope" --output none
  fi
done

echo 'Bootstrap complete. No data services, Function Apps or mailbox permissions were created.' >&2
echo 'Record these non-secret identity IDs for the setup runbook:' >&2
az identity list --resource-group "$ci_rg" \
  --query '[].{name:name,clientId:clientId,principalId:principalId,resourceId:id}' --output json
