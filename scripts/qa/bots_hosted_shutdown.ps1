# Cloud-side one-shot pilot guard. Assign this identity only read/instanceView
# and deallocate permissions on the one approved controller VM. No user keys.
param([Parameter(Mandatory=$true)][string]$ResourceId)
$ErrorActionPreference = 'Stop'
if ($ResourceId -notmatch '^/subscriptions/[0-9a-f-]{36}/resourceGroups/[A-Za-z0-9_.()-]+/providers/Microsoft.Compute/virtualMachines/alysis-bots-night-[A-Za-z0-9-]+$') {
    throw 'Invalid pilot controller resource.'
}
try {
    $identityHeaders = @{'X-IDENTITY-HEADER'=$env:IDENTITY_HEADER; 'Metadata'='True'}
    $token = Invoke-RestMethod -Uri ($env:IDENTITY_ENDPOINT + '?resource=https://management.azure.com/') -Method Get -Headers $identityHeaders
    $headers = @{'Authorization'=('Bearer ' + $token.access_token)}
    $uri = 'https://management.azure.com' + $ResourceId
    $null = Invoke-RestMethod -Uri ($uri + '/deallocate?api-version=2024-11-01') -Method Post -Headers $headers
    for ($attempt=0; $attempt -lt 20; $attempt++) {
        $view = Invoke-RestMethod -Uri ($uri + '/instanceView?api-version=2024-11-01') -Method Get -Headers $headers
        if ($view.statuses.code -contains 'PowerState/deallocated') {
            Write-Output 'Confirmed: pilot controller deallocated.'
            return
        }
        Start-Sleep -Seconds 15
    }
    throw 'Pilot controller deallocation was not confirmed.'
} catch {
    # Never emit raw HTTP errors, identity headers or token-bearing responses.
    throw 'Cloud pilot shutdown failed; operator must reconcile VM power state.'
}
