# iotagle

A FrogFind-style search engine and reading proxy for vintage low-power computers — old Macs, DOS PCs, Amigas, text browsers. **Plain HTTP only, no JavaScript, no CSS, HTML 3.2 output.** Modeled on [FrogFind](https://github.com/ActionRetro/FrogFind).

- Search results scraped from [DuckDuckGo Lite](https://lite.duckduckgo.com/lite/).
- Pages transcoded through [readability-lxml](https://pypi.org/project/readability-lxml/) and served as minimal HTML.
- Images downscaled and optionally Floyd–Steinberg dithered to 1-bit for black-and-white screens.
- `w:foo` query prefix jumps straight to a transcoded Wikipedia article.

## Routes

| Path | Purpose |
|------|---------|
| `/` | Search form |
| `/search?q=…` | Results page (HTML 3.2 table) |
| `/read?url=…` | Reader-mode page proxy |
| `/image?url=…&mode=color\|gray\|1bit&w=512` | Image proxy / downscaler |
| `/about` | About page |
| `/visitors` | Last 100 unique browser User-Agents seen |
| `/robots.txt` | Disallows `/read`, `/image`, `/search` |
| `/healthz` | Plain-text `ok` for liveness checks |
| `/favicon.ico` | 16×16 1-bit ICO |

## Local development

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements-dev.txt
gunicorn -c deploy/gunicorn.conf.py wsgi:app -b 127.0.0.1:8000
```

Then in another terminal:

```bash
curl -sS http://127.0.0.1:8000/                       # home page
curl -sS "http://127.0.0.1:8000/search?q=vintage+mac" # results
lynx http://127.0.0.1:8000/                            # what a vintage user sees
```

Tests, lint, and dependency audit:

```bash
ruff check .
ruff format --check .
pytest -q
pip-audit -r requirements.txt --require-hashes --disable-pip
```

### Dependencies

Direct dependencies live in `requirements.in` / `requirements-dev.in`. The
`.txt` files are hashed lock files generated from them — don't edit by hand:

```bash
pip-compile --generate-hashes --allow-unsafe --strip-extras -o requirements.txt requirements.in
pip-compile --generate-hashes --allow-unsafe --strip-extras -o requirements-dev.txt requirements-dev.in
```

Dependabot opens weekly PRs against the `.in` files and regenerates the locks.

## Deployment

Deploys to a single AWS Lightsail Ubuntu VM via GitHub Actions on push to `main`, once `lint`, `test` and `audit` pass. See [deploy/](deploy/) for nginx, systemd, gunicorn, and bootstrap configuration, and [deploy/ROLLBACK.md](deploy/ROLLBACK.md) for rollback steps.

| Account | Purpose |
|---------|---------|
| `ubuntu` | Admin. Full sudo; personal SSH key only. Never used by CI. |
| `deploy` | CI deploys. Owns `/opt/iotagle/{app,venv}`. Its only SSH key is pinned by a forced command to `/usr/local/sbin/iotagle-deploy` ([source](deploy/iotagle-deploy)), which deploys only SHAs already on `origin/main`. Sudo is limited to restarting/probing `iotagle.service`. |
| `iotagle` | Runs gunicorn inside a strict systemd sandbox. Read-only code; writes only `/var/lib/iotagle`. |

`deploy/bootstrap.sh` is idempotent and provisions everything (packages, users, sshd, ufw, fail2ban, unattended security upgrades with 09:00 UTC auto-reboot, nginx, systemd). Changes to `deploy/iotagle-deploy`, nginx or the systemd unit reach the box only when an admin re-runs it:

```bash
ssh ubuntu@iotagle.com
cd /opt/iotagle/app && sudo ./deploy/bootstrap.sh            # add --deploy-pubkey FILE to rotate the CI key
```

## Design constraints

- **No HTTPS.** Vintage browsers can't negotiate modern TLS. The whole project exists because of that.
- **No JavaScript, no CSS, no `<div>`.** HTML 3.2 doctype on every page. Tables-only layout.
- **One canonical hostname** (`iotagle.com`). No `www`, no apex redirects.
- **SSRF guard on every outbound fetch.** Private IP ranges and AWS IMDS are blocked.
- **Hard caps** on every fetch: 2 MB for `/read`, 5 MB for `/image`, ~13 s total timeout.

## License

To be determined.
