# Running the kernel as a Windows service

`install-service.ps1` registers the kernel with the Service Control Manager using the same
policy as the systemd unit: restart twice quickly, then every 5s, indefinitely. The kernel is
single-writer and WAL-backed, so an abrupt exit is recoverable by design — the supervisor's
job is to bring it straight back.

```powershell
# from an elevated PowerShell, in the checkout
powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1 `
    -PythonExe C:\Python311\python.exe -RepoRoot C:\opt\effective-scale
Get-Service effective-scale
Invoke-RestMethod http://127.0.0.1:8080/v1/health/ready
```

The script generates `ES_AUTH_SECRET` if you do not pass one, sets machine-wide
`PYTHONPATH`/`ES_LISTEN`, and keeps the store under `%ProgramData%\effective-scale`.

To remove: `Stop-Service effective-scale; sc.exe delete effective-scale`.
