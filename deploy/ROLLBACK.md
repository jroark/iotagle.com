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

## 2. SSH to the box

```bash
ssh ubuntu@iotagle.com    # or the Lightsail static IP
```

## 3. Identify the last known good SHA

```bash
git -C /opt/iotagle/app log --oneline -n 20
sudo journalctl -u iotagle.service --since "1 hour ago" | tail -50
```

The deploy script keeps the box's `HEAD` at whatever last shipped. If the
auto-rollback ran, that already points to the previous SHA — check
`git -C /opt/iotagle/app rev-parse HEAD` against the workflow log.

## 4. Roll back

```bash
cd /opt/iotagle/app
git fetch --quiet origin
git checkout --quiet --detach <good-sha>
/opt/iotagle/venv/bin/pip install --quiet -r requirements.txt
sudo systemctl restart iotagle
sudo systemctl status iotagle | head -10
curl -i http://iotagle.com/healthz
```

`/healthz` returning `ok` confirms the service is back up. If it doesn't:

```bash
sudo journalctl -u iotagle -n 100 --no-pager
```

## 5. Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `gunicorn` won't start, missing module | requirements.txt drift between SHAs | rerun `pip install -r requirements.txt` |
| 502 from nginx, gunicorn alive | Unix socket perms wrong after a fresh boot | `sudo systemctl restart iotagle.socket iotagle.service` |
| 500 on `/read` for everything | DDG endpoint changed or readability error | check `journalctl -u iotagle`; revert `services/ddg.py` or `services/transcoder.py` |
| All routes hang | Worker timeout deadlock | `sudo systemctl restart iotagle` and watch journal |

## 6. After recovery

Once the box is healthy, fix the bad code on a branch, open a PR, let CI
verify, merge to `main`, and re-enable the deploy workflow if it was
disabled.

```bash
gh workflow enable deploy
```
