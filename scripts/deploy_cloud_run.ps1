param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,

    [Parameter(Mandatory = $true)]
    [string]$AuthToken,

    [string]$Region = "us-central1",
    [string]$Service = "pharmacy-mcp"
)

$ErrorActionPreference = "Stop"

gcloud config set project $ProjectId
gcloud run deploy $Service `
    --source . `
    --region $Region `
    --allow-unauthenticated `
    --concurrency 1 `
    --set-env-vars "MCP_AUTH_TOKEN=$AuthToken,PHARMACY_DB=/tmp/pharmacy.db"

$serviceUrl = gcloud run services describe $Service `
    --region $Region `
    --format "value(status.url)"

Write-Host "Health: $serviceUrl/health"
Write-Host "MCP endpoint: $serviceUrl/mcp"
Write-Host "Configure PHARMACY_REMOTE_URL=$serviceUrl/mcp in the chatbot .env file."

