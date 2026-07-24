$host.UI.RawUI.WindowTitle = "TensorCash — import key"
$key = Read-Host "Paste WIF private key"
$body = (@{jsonrpc="1.0";id="1";method="importprivkey";params=@($key)} | ConvertTo-Json -Compress)
try {
  $r = Invoke-RestMethod http://127.0.0.1:39240 -Body $body -ContentType "text/plain" -Headers @{Authorization="Basic dGM6dGM="} -TimeoutSec 300
  if ($r.error) { Write-Host "ERROR: $($r.error.message)" -Foreground Red }
  else { Write-Host "DONE — key imported!" -Foreground Green }
} catch { Write-Host "ERROR: $_" -Foreground Red }
Read-Host "`nPress Enter"
