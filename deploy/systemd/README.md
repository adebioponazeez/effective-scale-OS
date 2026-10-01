# Running the kernel as a service

The kernel is designed to be **restarted without fear**: the SQLite store is WAL-backed and
`tests/test_chaos.py` proves that an abrupt close replays without double-committing, and
`tests/test_soak.py` proves sustained load keeps loops alive with bounded state. That is why
the unit uses `Restart=always` rather than trying to nurse a wedged process.

## Linux (systemd)

```bash
sudo install -m 0644 deploy/systemd/effective-scale.service /etc/systemd/system/
sudo install -d -o effective-scale -g effective-scale /var/lib/effective-scale /etc/effective-scale
sudo install -o root -g root -m 0600 deploy/systemd/effective-scale.env.example \
    /etc/effective-scale/effective-scale.env
sudo editor /etc/effective-scale/effective-scale.env
sudo systemctl daemon-reload && sudo systemctl enable --now effective-scale
```

Health, from the operator's side:

```bash
curl -sf http://127.0.0.1:8080/v1/health/live   # process is up, loops alive
curl -sf http://127.0.0.1:8080/v1/health/ready  # the kernel can actually commit
curl -s  http://127.0.0.1:8080/v1/status        # leader, counts, loop errors
journalctl -u effective-scale -f
```

`ready` is deliberately end-to-end: it asks the writer thread to run the store's own probe.
If the store dies or the writer wedges, it returns 503 while `live` stays 200 — so a
supervisor or load balancer stops sending work without killing a live, recoverable process.
That distinction is asserted in `tests/test_cli.py::HealthSemanticsTest`.

## Windows (Service Control Manager)

The SAF subproject ships PowerShell bootstrap scripts; for the kernel use the same pattern:

```powershell
# once, from an elevated shell (adjust paths)
New-Service -Name effective-scale `
  -BinaryPathName '"C:\Python311\python.exe" -m effective_scale --store C:\ProgramData\effective-scale\es.db --listen 0.0.0.0:8080' `
  -DisplayName "effective-scale-OS kernel" -StartupType Automatic
# restart twice, then always, with a short delay - the same policy as the systemd unit
sc.exe failure effective-scale reset= 86400 actions= restart/2000/restart/2000/restart/5000
Start-Service effective-scale
```

Set `PYTHONPATH` to the checkout's `src` (or install the package) for the service account,
and keep the store on a local disk with an `ES_AUTH_SECRET` set in the machine environment.

## Containers / Kubernetes

- `docker-compose.yml` already runs the kernel with a `/v1/health/ready` healthcheck and
  `restart: unless-stopped`.
- `deploy/k8s/03-deployment.yaml` wires `startupProbe`/`readinessProbe` to `/v1/health/live`
  and `/v1/health/ready`, keeps `replicas: 1` (single writer) and gives SIGTERM 30s to drain.
