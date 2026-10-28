# Azure and Microsoft Graph Setup

> **Revision 2026-09-29:** Fireflies is the first release. Sections 1–3 (identities, GitHub OIDC) apply now. Sections 5–7 (mailbox and threat permissions) apply to the email release. The first-release resource list is in [Function Apps §6](FUNCTION_APPS.md#6-pilot-resource-inventory); Azure AI Search, App Configuration, Document Intelligence and Defender for Storage in §4 below are deferred to email. Fireflies needs no Graph permission: store its API key and webhook secret in Key Vault.

Status: setup runbook and executable identity bootstrap. Azure infrastructure templates, Function Apps, ingestion code and deployment workflows are not yet implemented in this repository. No tenant changes were performed by preparing these files.

The first release processes email body text and PDF attachments only, including OCR for scanned PDFs. All other file formats remain disabled; see [Email Layers and PDF Scope](EMAIL_PIPELINE_AND_PDF_SCOPE.md).

Use the existing resource group **`fs-communication-intelligence`**, created by Matthew, and a few approved mailboxes. The bootstrap verifies that group exists in the selected subscription; it does not create another group or alter its tags. The initial GitHub environment remains `development`. The steps below distinguish what can be configured now from what depends on the application build. Never assume that permission to read email means the ingestion/search service exists.

## 1. Information to Collect

Record the Azure subscription ID, Microsoft Entra tenant ID, selected Azure region, two or three test mailbox addresses, one mailbox that must remain excluded, and approved executive user object IDs. Use the tenant that actually hosts the Microsoft 365 mailboxes. Do not post client secrets, certificates, passwords or access tokens into chat or GitHub.

Confirm an Azure administrator can create resources and assign roles in the intended resource group. Exchange permission setup requires the Exchange administrative roles described in Microsoft's RBAC documentation. Microsoft Graph application-role grants require an appropriately authorized Entra administrator; an Azure subscription Owner role alone does not grant that authority.

Choose a region after checking availability and quotas for the selected Function hosting plan, PostgreSQL, Search, OCR, Language, Content Safety and embedding model deployment. Azure resources and Microsoft 365 security entitlements are separate purchases/configuration.

## 2. Create the Deployment and Runtime Identities

Use a user-assigned managed identity for the Azure-hosted ingestion app. It gives the application an Entra service principal without maintaining a client secret. A separate app registration is not required just to let that managed identity call Graph. An app registration is needed later for the executive search API and its delegated OAuth scope.

| Identity | Suggested Development Name | Purpose |
|---|---|---|
| GitHub deployment | `id-fs-ci-github-dev` | Deploy infrastructure/code through GitHub OIDC; no mailbox access |
| Ingestion runtime | `id-fs-ci-ingestion-dev` | Read approved source mailboxes and write eligible evidence |
| Search runtime | `id-fs-ci-search-dev` | Read authorized indexed evidence; no broad mailbox ingestion grant |
| Maintenance runtime | `id-fs-ci-maintenance-dev` | Process lifecycle jobs under narrowly assigned storage/database permissions |
| Search API app registration | `app-fs-ci-search-dev` | Represent the API audience and delegated `Communications.Search` scope |

In Azure Portal, open Cloud Shell in **Bash**. Download `scripts/bootstrap-azure.sh` from this repository and upload it to Cloud Shell, or clone the private repository using your approved GitHub authentication. Do not put a GitHub token into a command URL.

From the repository directory, run after replacing the first three arguments:

```bash
bash scripts/bootstrap-azure.sh \
  YOUR-SUBSCRIPTION-ID YOUR-TENANT-ID YOUR-AZURE-REGION development fs-communication-intelligence
```

The script uses your existing resource group and creates four managed identities, GitHub federation and deployment roles scoped to that group. It does **not** provision PostgreSQL, storage, AI services or Function Apps, grant Graph access, or start ingestion. It uses no client secret. Re-running checks existing federation and reuses role assignments; it stops on a federation mismatch. A failed run can leave already-created resources in place and should be reviewed before retrying.

Save the ingestion identity's **client ID**, **principal/object ID** and **Azure resource ID**. These are different identifiers. Exchange needs the service principal's object ID, not an app-registration object ID.

If an ingestion app registration already exists, inventory its credentials and grants before creating/reusing identities. The same Exchange scope approach can use that app's client ID and enterprise application object ID, but runtime authentication must then be configured explicitly, preferably with a certificate or federation. Do not maintain two ingestion identities accidentally.

## 3. Connect GitHub to Azure

Create the GitHub environment `development` under this repository's Settings. Its name must match the federation subject created by the bootstrap:

```text
repo:Finishes-Solutions/fs-communication-intelligence:environment:development
```

Create these **environment variables**, not secrets:

| GitHub Variable | Value |
|---|---|
| `AZURE_CLIENT_ID` | GitHub deployment managed identity client ID |
| `AZURE_TENANT_ID` | Microsoft 365/Entra tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Selected Azure subscription |
| `AZURE_RESOURCE_GROUP` | `fs-communication-intelligence` |
| `AZURE_LOCATION` | Selected supported region |

These IDs are not credentials. Future workflows need `id-token: write`, must reference the `development` environment, and use `azure/login` with these IDs. Separate infrastructure and code release workflows as planned. No workflow currently exists to run; this step prepares authentication for the build.

The bootstrap grants Contributor and Role Based Access Control Administrator only at the specified existing resource group. The latter allows the deployment to assign runtime roles; remove it when the release workflow no longer needs that operation, or use a separate privileged provisioning identity. Do not grant GitHub mailbox permissions.

## 4. Azure Resource Inventory

Provision these resources through reviewed Bicep/workflows during the infrastructure implementation task. This table is a build checklist, not a statement that resources exist. Reuse suitable approved existing AI resources when their boundaries, quotas and permissions permit it.

| Resource | Suggested Name Pattern | Why It’s Needed / Purpose | Required Setup |
|---|---|---|---|
| Resource group | `fs-communication-intelligence` | Keeps this project's Azure resources together so you can manage access, deployments and costs in one place. | Already created by Matthew; bootstrap verifies existence; configure cost tags and budget/alerts separately |
| Virtual network and subnets | `vnet-fs-ci-dev` | Provides the private network path between the application and PostgreSQL, keeping the database off the public internet. | Separate Function integration and PostgreSQL delegated subnets; private DNS for database |
| Function hosts | `func-fs-ci-ingestion-dev-<suffix>`, `func-fs-ci-search-dev-<suffix>`, `func-fs-ci-maintenance-dev-<suffix>` | Run the application code: collect email, process PDFs, answer authorized search requests and handle retention/deletion jobs. | Supported Python runtime, managed identities, HTTPS, host storage, appropriate timeouts/network integration; do not enable ingestion before acceptance |
| Function host storage | Globally unique account names per isolation boundary | Stores the operational data the Functions runtime needs, such as host coordination and timer state. This serves a different purpose from storing email evidence. | Identity-based host connections with the roles required by the chosen triggers/hosting plan; separate from evidence access |
| Evidence Blob Storage | `stfscieviddev<suffix>` | Stores protected source artifacts, PDF attachments and extracted text so the application can retain evidence and cite its source without putting large files in database rows. | No anonymous blobs; separate quarantine/approved and test/production namespaces; retention rules and malware result handling |
| Defender for Storage | On evidence account | Checks uploaded files for malware before PDF extraction. Unknown or failed scan results must not be treated as clean. | On-upload malware scanning, scan result integration, limits/cost monitoring and failed/unknown scan handling |
| PostgreSQL Flexible Server | `pg-fs-ci-dev-<suffix>` | Acts as the authoritative database for communications, source copies, duplicate checks, permissions, classifications, processing jobs and audit history. | Database `communications`; private connectivity; backup policy; separate runtime/migration principals and versioned schema |
| Azure AI Search | `srch-fs-ci-dev-<suffix>` | Finds approved evidence using both keywords and vector similarity, supplying relevant passages to the executive search API and Copilot agent. | Separate test/production indexes; matching embedding dimensions; authenticated access |
| Document Intelligence | `doc-fs-ci-dev-<suffix>` | Reads text from scanned PDF pages using OCR so image-based PDFs can become searchable. Standalone image attachments remain outside the first release. | OCR endpoint and managed-identity access; supported region/quota |
| Azure AI Language | `lang-fs-ci-dev-<suffix>` | Provides sensitive-information detection signals for the publication policy, helping keep restricted text out of search. These signals still require validation and policy decisions. | Sensitive-information detection and validated policy configuration |
| Azure AI Content Safety | `safe-fs-ci-dev-<suffix>` | Helps identify embedded prompt attacks in source content before that content reaches AI workflows; it does not replace malware scanning or access controls. | Prompt-attack screening endpoint and runtime permissions |
| Azure OpenAI embedding deployment | Approved resource plus embedding deployment | Converts approved text and search questions into numerical vectors so the system can find similar meaning even when the wording differs. | Model/version/dimensions consistent with the search schema; quota verified |
| Key Vault | `kv-fs-ci-dev-<suffix>` | Protects any secrets or certificates the application actually needs. Managed identities should eliminate credentials where possible; this is not a store for email content. | RBAC; store only credentials actually required; runtime can read only its required secrets |
| Azure App Configuration | `appcs-fs-ci-dev-<suffix>` | Gives the separate applications one shared place to read runtime settings, including test/production/paused mode and approved scope configuration. | Shared mode and versioned scope controls; scoped read/operator permissions |
| Application Insights and Log Analytics | `appi-fs-ci-dev`, `log-fs-ci-dev` | Show failures, processing delays and operational health so the team can investigate problems and receive alerts without logging email bodies. | Content-free logs, alerts, retention and operational dashboards |

Add cost monitoring before large backfill. Determine SKU/capacity from the pilot; company-wide history must not be sized from a few synthetic emails. Provisioning these hosts does not deploy application code.

### Runtime Role Allocation

| Service | Ingestion/Processing | Retrieval | Migration/Operations |
|---|---|---|---|
| Evidence Blob Storage | Required read/write containers; avoid broad account-wide grants where practical | Only if authorized full-evidence retrieval needs it; no quarantine access by default | Lifecycle permissions on required namespaces |
| AI Search | Search Index Data Contributor for publication | Search Index Data Reader | Search Service Contributor for schema management where needed |
| Embeddings | Cognitive Services OpenAI User | Same only if query embedding occurs here | Model provisioning belongs to deployment administration |
| OCR/Language/Content Safety | Appropriate service data-plane role, commonly Cognitive Services User; validate each endpoint | None unless deliberately used for query policy | Deployment/control-plane permissions separate |
| App Configuration | App Configuration Data Reader | App Configuration Data Reader | Data Owner for the designated configuration controller/operator only |
| PostgreSQL | Application SQL grants for ingestion/queue/evidence | Read/execute only for approved retrieval operations | Dedicated migration role; no schema-admin credential left in ordinary runtime |
| Key Vault | Key Vault Secrets User only if this app needs stored secrets | Same conditional rule | Secrets administration separated from normal runtime |

Function host storage has its own requirements. Follow the host/trigger documentation rather than assuming an evidence-container role is enough. SQL grants and Graph/Exchange permissions are not conferred by Azure Contributor.

## 5. Grant Read-Only Email Access to the Ingestion Identity

Recommended pilot boundary: a dedicated mail-enabled security group containing only the approved mailbox accounts. Exchange Application RBAC grants `Application Mail.Read` over that scope. Do not also grant unscoped Microsoft Graph `Mail.Read` in Entra; grants are additive. `Mail.Send`, `Mail.ReadWrite`, delegated user permissions and directory-wide enumeration are unnecessary for this ingestion design.

In PowerShell with ExchangeOnlineManagement available, connect as the authorized Exchange administrator:

```powershell
Connect-ExchangeOnline

# Values for the dev ingestion identity, from Azure Cloud Shell:
#   az identity show -g fs-communication-intelligence -n id-fs-ci-ingestion-dev --query "{clientId:clientId, principalId:principalId}"
$CiClientId = 'INGESTION-IDENTITY-CLIENT-ID'
$CiPrincipalId = 'INGESTION-IDENTITY-PRINCIPAL-ID'
# Dev: only the synthetic test mailbox (CLAUDE.md 10.2), then Matthew's, then the pilot group.
$CiTestMailboxes = @('ci-test@YOUR-DOMAIN')

# First setup only. Use a fresh, uniquely named group and add direct members.
New-DistributionGroup -Name 'FS CI Email Pilot Scope' -Alias 'fs-ci-email-pilot' -Type Security
foreach ($CiMailbox in $CiTestMailboxes) {
    Add-DistributionGroupMember -Identity 'FS CI Email Pilot Scope' -Member $CiMailbox
}

$CiGroup = Get-DistributionGroup -Identity 'FS CI Email Pilot Scope'
$CiGroupDn = $CiGroup.DistinguishedName.Replace("'", "''")
New-ServicePrincipal -AppId $CiClientId -ObjectId $CiPrincipalId -DisplayName 'FS CI Email Ingestion Dev'
New-ManagementScope -Name 'FS-CI-Email-Pilot' -RecipientRestrictionFilter "MemberOfGroup -eq '$CiGroupDn'"
New-ManagementRoleAssignment -Name 'FS-CI-Email-Pilot-Read' -App $CiPrincipalId -Role 'Application Mail.Read' -CustomResourceScope 'FS-CI-Email-Pilot'
```

These are first-setup commands, not an idempotent migration script. If names already exist, inspect their members, scope and assignments before changing them. Use direct group membership; nested membership is not the intended scope mechanism here. No email is sent by creating the group or assigning these permissions.

Verify both positive and negative authorization:

```powershell
Test-ServicePrincipalAuthorization -Identity $CiPrincipalId -Resource 'FIRST-TEST-MAILBOX@YOUR-DOMAIN'
Test-ServicePrincipalAuthorization -Identity $CiPrincipalId -Resource 'EXCLUDED-MAILBOX@YOUR-DOMAIN'

# Inspect the actual mailbox population resolved by the management scope.
$CiScope = Get-ManagementScope -Identity 'FS-CI-Email-Pilot'
Get-Recipient -RecipientPreviewFilter $CiScope.RecipientFilter |
    Select-Object PrimarySmtpAddress,RecipientTypeDetails
```

Expect the intended read assignment to show in-scope for the first mailbox and out-of-scope for the excluded mailbox. The simulation does not account for separate Entra grants: inspect those too, then perform a real application-token request. Allow permission propagation before diagnosing an immediate negative result.

### 5a. Every Mailbox (Production Scope), Later

Company-wide read access is granted to the **production** ingestion identity (`id-fs-ci-ingestion-prod`, created by `scripts/bootstrap-azure.sh ... production`), never to `id-fs-ci-ingestion-dev`. The dev stack is designed for synthetic and test mail: it has no private networking and is where unfinished code runs. Do this only after all of these are true:

- the capture code is deployed and the pilot passed the acceptance plan (an access grant alone ingests nothing);
- the production stack exists with private PostgreSQL access (ADR-011);
- the mailbox inventory, the exclusion list and the reader audience are approved by the business owners, including the owners of any mailbox that holds privileged or personal mail.

"Every email domain" needs no separate grant. Exchange RBAC scopes **mailboxes**, and a mailbox receives mail for all of its addresses in every accepted domain. Mail sent to an external domain is captured only through the internal mailbox that sent or received it.

The production scope covers user and shared mailboxes and keeps out any mailbox tagged with `CustomAttribute15 = FS-CI-Exclude`:

```powershell
Connect-ExchangeOnline
$CiProdClientId = 'PROD-INGESTION-IDENTITY-CLIENT-ID'
$CiProdPrincipalId = 'PROD-INGESTION-IDENTITY-PRINCIPAL-ID'

# Tag every mailbox that must stay out BEFORE creating the scope.
Set-Mailbox -Identity 'EXCLUDED-MAILBOX@YOUR-DOMAIN' -CustomAttribute15 'FS-CI-Exclude'

New-ServicePrincipal -AppId $CiProdClientId -ObjectId $CiProdPrincipalId -DisplayName 'FS CI Email Ingestion Prod'
$CiFilter = "(RecipientTypeDetails -eq 'UserMailbox' -or RecipientTypeDetails -eq 'SharedMailbox') -and -not (CustomAttribute15 -eq 'FS-CI-Exclude')"
New-ManagementScope -Name 'FS-CI-Email-Prod' -RecipientRestrictionFilter $CiFilter

# Review the resolved population before assigning anything.
Get-Recipient -RecipientPreviewFilter $CiFilter -ResultSize Unlimited |
    Select-Object PrimarySmtpAddress,RecipientTypeDetails,CustomAttribute15 |
    Export-Csv fs-ci-prod-scope-preview.csv -NoTypeInformation

New-ManagementRoleAssignment -Name 'FS-CI-Email-Prod-Read' -App $CiProdPrincipalId -Role 'Application Mail.Read' -CustomResourceScope 'FS-CI-Email-Prod'
Test-ServicePrincipalAuthorization -Identity $CiProdPrincipalId -Resource 'IN-SCOPE-MAILBOX@YOUR-DOMAIN'
Test-ServicePrincipalAuthorization -Identity $CiProdPrincipalId -Resource 'EXCLUDED-MAILBOX@YOUR-DOMAIN'
```

Keep the preview CSV out of Git: it lists every mailbox in the company. Permission changes can take up to about two hours to apply; repeat the positive/negative test (section 6) with a real runtime token before relying on it. The hard Exchange boundary and the application's own scope (`ci scope ...`, `runtime_config`) both apply. A mailbox tagged later drops out of the Exchange scope, and the application also needs `ci scope exclude` so already-indexed mail is withdrawn. The filter syntax above follows CLAUDE.md section 6; check it against the current Exchange RBAC documentation before running it.

## 6. Resolve Mailbox IDs and Test the Actual Runtime Identity

Export only the approved inventory from Exchange. Do not assume every enabled Entra account has a mailbox or grant `Directory.Read.All` just to discover them.

```powershell
foreach ($CiMailbox in $CiTestMailboxes) {
    Get-EXOMailbox -Identity $CiMailbox -Properties ExternalDirectoryObjectId |
        Select-Object PrimarySmtpAddress,ExternalDirectoryObjectId,RecipientTypeDetails
}
```

Store these object IDs in the test allowlist and mailbox registry when that implementation exists. Shared mailboxes must be deliberately included and tested.

Attach the ingestion user-assigned managed identity to the ingestion Function App during provisioning. Set its managed-identity client selection appropriately. From code running on that Azure host, request a token for `https://graph.microsoft.com/.default` and call:

```http
GET https://graph.microsoft.com/v1.0/users/APPROVED-MAILBOX-OBJECT-ID/messages?$top=1&$select=id
Prefer: IdType="ImmutableId"
Authorization: Bearer APPLICATION-TOKEN
```

Expected: HTTP 200 (an empty mailbox can return an empty list). Repeat with the excluded mailbox and require an access denial. A 401 is not proof of correct scoping; it can mean authentication is broken. A 404 is also not a sufficient negative authorization test by itself. Use a real existing excluded mailbox and investigate unexpected results.

Do not test this with Graph Explorer's delegated user token or a normal Cloud Shell user's `az account get-access-token`; those can exercise the administrator's access rather than the ingestion identity. A user-assigned identity must be attached to the compute performing the test. Never print its bearer token or email body to logs.

`/users/{id}/messages` is a smoke test, not the production ingestion algorithm. The app will use folder-aware capture, exclusions, complete paging and per-folder delta checkpoints.

## 7. Configure Threat Checks Separately

Graph mailbox access and security telemetry are separate grants. The planned threat check uses Microsoft Graph application permission `ThreatHunting.Read.All` for `/v1.0/security/runHuntingQuery`. This permission is broader security-data access and is not restricted by the Exchange mailbox scope. Assign it only to the selected screening runtime identity, with explicit tenant security approval.

Using Microsoft Graph PowerShell under an authorized administrator, this example assigns that one application role to the ingestion/screening service principal. Use a separate screening identity instead if the deployment isolates that responsibility:

```powershell
Connect-MgGraph -TenantId 'YOUR-TENANT-ID' -Scopes 'Application.Read.All','AppRoleAssignment.ReadWrite.All'
$CiPrincipalId = 'INGESTION-OR-SCREENING-IDENTITY-PRINCIPAL-ID'
$CiGraph = Get-MgServicePrincipal -Filter "appId eq '00000003-0000-0000-c000-000000000000'" -Property Id,AppRoles
$CiRole = @($CiGraph.AppRoles | Where-Object {
    $_.Value -eq 'ThreatHunting.Read.All' -and $_.AllowedMemberTypes -contains 'Application' -and $_.IsEnabled
})
if ($CiRole.Count -ne 1) { throw 'Expected one enabled application role.' }
$CiExisting = Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $CiPrincipalId -All |
    Where-Object { $_.ResourceId -eq $CiGraph.Id -and $_.AppRoleId -eq $CiRole[0].Id }
if (-not $CiExisting) {
    New-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $CiPrincipalId -BodyParameter @{
        principalId = $CiPrincipalId
        resourceId = $CiGraph.Id
        appRoleId = $CiRole[0].Id
    }
}
```

The administrator's requested PowerShell scopes authorize setup; they are not additional permissions granted to the runtime. The app-role assignment is the actual application permission grant, so a further generic consent click is not required for that same assignment.

Verify `EmailEvents` is populated in your Defender environment for representative mail. Its availability depends on the relevant Defender service/entitlement. A successful API authorization with no applicable data is not a clean verdict. Historical data may fall outside available coverage; increasing the requested timespan does not recreate absent history. Hold unknown history until a validated strategy is approved.

Defender for Storage scans staged attachments. It does not replace email-level spam/phishing checks, nor does OCR replace either control.

## 8. Register the Executive Search API Later in the Build

Create a single-tenant app registration for the retrieval API. Configure v2 access tokens, expose delegated scope `Communications.Search` under its application ID URI, and use the expected audience in API token validation. Configure a separate OAuth client for the Copilot connector with its exact redirect URI and authorized delegated scope.

Executives authenticate through their own delegated connection. The API checks identity, audience, tenant, scope and approved corpus access. Do not grant executives ingestion credentials or use the GitHub deployment identity as the Copilot connection.

This step enables user-to-API authentication; it does not grant the API access to mailboxes. If retrieval needs fresh source validation, implement the planned narrowly authorized validation path explicitly instead of assigning the search identity the ingestion identity's broad grants.

## 9. Start Small, Then Expand

Before capture is enabled, the actual application must exist, schema/index initialization must pass, and security endpoints must be verified. Start with mode `test`, the approved IDs, email enabled and other sources disabled. Set a fixed backfill boundary. Run the acceptance plan before production scope.

Two boundaries control access: Exchange authorization and application scope. Expanding only the application scope cannot exceed Exchange permissions. If you require a single-setting production launch, preconfigure the approved production permission boundary and registry beforehand, then use the shared mode setting to select production. If the pilot identity must technically access only pilot mailboxes, changing Exchange scope or switching to a production identity is an additional launch step.

Company-wide access means the approved mailbox inventory, including intentionally selected shared mailboxes and explicit exclusions. It does not automatically mean archives, recoverable/purged content or every message format. Validate coverage before promising all historical emails.

## 10. Setup Completion Checklist

- Azure subscription/tenant/region and existing `fs-communication-intelligence` resource group confirmed.
- GitHub deployment federation and environment variables configured.
- Runtime client/principal/resource IDs recorded without credentials.
- Data services, Function hosts, networking, monitoring and roles provisioned through the implementation workflow.
- Approved and excluded mailbox authorization tested with both Exchange simulation and a real runtime token.
- Approved mailbox IDs available for registry/test configuration.
- Security telemetry and attachment scan results verified in this tenant.
- Application code, database migrations, index initialization and shared mode controller deployed.
- Executive API/Copilot authentication and retrieval permissions tested.
- Pilot acceptance, lifecycle cleanup, costs and production coverage approved before expansion.

## Microsoft References

- [Exchange Application RBAC](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac)
- [Managed identities and application role assignment](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/assign-app-role-managed-identity-powershell)
- [GitHub to Azure OIDC](https://learn.microsoft.com/en-us/azure/developer/github/connect-from-azure-openid-connect)
- [Graph list messages](https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0)
- [Graph security hunting API](https://learn.microsoft.com/en-us/graph/api/security-security-runhuntingquery?view=graph-rest-1.0)
- [Defender EmailEvents coverage](https://learn.microsoft.com/en-us/defender-xdr/advanced-hunting-emailevents-table)
- [Azure Functions identity-based connections](https://learn.microsoft.com/en-us/azure/azure-functions/functions-reference)
