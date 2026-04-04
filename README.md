# kevin.hsu — Personal Website

A self-hosted personal portfolio site running on Nginx Alpine.

## Quick Start

```bash
docker compose up -d
```

Site will be available at `http://localhost:3080`.

## Deploy Behind Nginx Proxy Manager

1. Point a domain (e.g. `kevin.example.com`) to your Proxmox host IP
2. In Nginx Proxy Manager, add a proxy host:
   - **Domain:** `kevin.example.com`
   - **Forward Hostname:** `kevin-website` (or host IP)
   - **Forward Port:** `3080`
   - **SSL:** Request a new Let's Encrypt certificate

## Development

To live-edit without rebuilding, uncomment the volume mount in `docker-compose.yml`:

```yaml
volumes:
  - ./index.html:/usr/share/nginx/html/index.html:ro
```

Then just edit `index.html` and refresh the browser.

## Rebuild

```bash
docker compose up -d --build
```

## Stack

- **Image:** `nginx:1.27-alpine` (~7MB)
- **Security:** X-Frame-Options, CSP, XSS protection headers
- **Performance:** Gzip compression, 30-day static asset caching
- **Health check:** `GET /health` endpoint
