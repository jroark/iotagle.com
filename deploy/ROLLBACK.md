# Rollback

When the GitHub Actions deploy auto-rollback also fails, or when a bad commit
needs to be reverted manually after deploy, follow this checklist.

## 1. Stop further auto-deploys

If the bad commit is on `main`, **disable the deploy workflow first** so a
later push doesn't redeploy the same bad code:

```bash
gh workflow disable deploy
```

Re-enable later with `gh workflow enable deploy`.

## 2. SSH to the box as the admin

```bash
ssh ubuntu@iotagle.com    # your personal key; the CI `deploy` key can't open a shell
```

If SSH itself is broken, use the browser SSH button in the Lightsail console.
It authenticates through Lightsail's own CA, not `authorized_keys`.

## 3. Identify the last known good SHA

```bash
git -C /opt/iotagle/app log --oneline -n 20 origin/main
sudo journalctl -u iotagle.service --since "1 hour ago" | tail -50
```

The box's `HEAD` is whatever last shipped. If the auto-rollback ran, it
already points to the previous SHA. Check
`git -C /opt/iotagle/app rev-parse HEAD` against the workflow log.

## 4. Roll back

Use the same entry point CI uses. It verifies the SHA is on `origin/main`,
installs hash-checked deps, restarts, probes `/healthz`, and auto-reverts if
the probe fails:

```bash
sudo -u deploy /usr/local/sbin/iotagle-deploy <good-40-char-sha>
curl -i http://iotagle.com/healthz
```

`/healthz` returning `ok` confirms the service is back up. If it doesn't:

```bash
sudo journalctl -u iotagle -n 100 --no-pager
sudo systemctl status iotagle --no-pager | head -20
```

## 5. Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `gunicorn` won't start, missing module | venv out of sync with `requirements.txt` | `sudo -u deploy /opt/iotagle/venv/bin/pip install --require-hashes --only-binary=:all: -r /opt/iotagle/app/requirements.txt` |
| Service exits immediately with `status=31/SYS` | A dependency hit the systemd syscall filter | `journalctl -k \| grep audit`; relax `SystemCallFilter=` in `deploy/systemd/iotagle.service`, re-run bootstrap |
| Service dies at import with a memory or `mprotect` error | A dependency needs W+X memory | Remove `MemoryDenyWriteExecute=yes` from the unit, re-run bootstrap |
| `/read` fails for every site, `/healthz` ok | DNS isn't going through `127.0.0.53` (blocked by `IPAddressDeny=`) | `cat /etc/resolv.conf`; restore the systemd-resolved stub |
| 502 from nginx, gunicorn alive | gunicorn not listening on 127.0.0.1:8000 | `sudo ss -ltnp \| grep 8000`; `sudo systemctl restart iotagle` |
| 429s for real users | nginx rate limits too tight | adjust `limit_req_zone` rates in `deploy/nginx/iotagle.conf`, re-run bootstrap |
| 500 on `/read` for everything | DDG endpoint changed or readability error | check `journalctl -u iotagle`; revert `services/ddg.py` or `services/transcoder.py` |
| Deploy says `refusing: <sha> is not on origin/main` | Deploying a branch or unpushed commit | By design. Merge to `main` first |
| Deploy says `another deploy is in progress` | Concurrent deploy holds `/run/lock/iotagle-deploy.lock` | Wait, or check for a stuck `iotagle-deploy` process |

## 6. Whole-box failure: restore a snapshot

Snapshots are taken before every infrastructure change
(`aws lightsail create-instance-snapshot`). To restore one:

```bash
aws lightsail get-instance-snapshots --query 'instanceSnapshots[].[name,createdAt]' --output table
aws lightsail create-instances-from-snapshot --instance-snapshot-name <snap> \
    --instance-names iotagle-restore --availability-zone <az> --bundle-id <bundle>
# then move the static IP to the new instance:
aws lightsail attach-static-ip --static-ip-name <ip-name> --instance-name iotagle-restore
```

The restored instance has a **new SSH host key**. Update the
`LIGHTSAIL_KNOWN_HOSTS` secret, checking the fingerprint with
`aws lightsail get-instance-access-details`, before the next deploy.

## 7. After recovery

Once the box is healthy, fix the bad code on a branch, open a PR, let CI
verify, merge to `main`, and re-enable the deploy workflow if it was
disabled.

```bash
gh workflow enable deploy
```
