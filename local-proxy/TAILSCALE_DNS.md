# .lab Domains over Tailscale

By default, `.lab` domains only resolve on your Mac (via `/etc/hosts`). This guide sets up dnsmasq so every device on your Tailscale network can use `http://cybercache.lab`, `http://rivendell.lab`, etc. with no per-device config.

**How it works:** dnsmasq runs on your Mac and answers `*.lab` DNS queries with your Mac's Tailscale IP. Tailscale is told to route all `.lab` queries to your Mac. Caddy (already running) handles the reverse proxy as normal.

---

## Step 1 — Install dnsmasq

```bash
brew install dnsmasq
```

---

## Step 2 — Get your Mac's Tailscale IP

```bash
tailscale ip -4
```

Note it down — you'll use it in the next two steps. It looks like `100.x.x.x` and doesn't change.

---

## Step 3 — Configure dnsmasq

Find the config file:

- **Apple Silicon:** `/opt/homebrew/etc/dnsmasq.conf`
- **Intel Mac:** `/usr/local/etc/dnsmasq.conf`

Add these lines at the bottom, replacing both occurrences of the IP with yours:

```
# Resolve all *.lab to this Mac's Tailscale IP
address=/.lab/100.x.x.x

# Only bind to loopback + Tailscale IP — avoids port 53 conflict with macOS
listen-address=127.0.0.1,100.x.x.x
bind-interfaces

# Don't forward other queries — Tailscale only sends .lab here anyway
no-resolv
```

---

## Step 4 — Start dnsmasq

Needs sudo so it can bind to port 53:

```bash
sudo brew services start dnsmasq
```

To restart after config changes:

```bash
sudo brew services restart dnsmasq
```

---

## Step 5 — Verify dnsmasq is working

```bash
dig @127.0.0.1 cybercache.lab +short
```

Should return your Tailscale IP. If it doesn't, check for errors:

```bash
sudo brew services list
```

---

## Step 6 — Configure Tailscale split DNS

1. Go to **https://login.tailscale.com/admin/dns**
2. Scroll to **Nameservers** → click **Add nameserver** → choose **Custom**
3. Enter your Mac's Tailscale IP as the nameserver
4. Tick **Restrict to domain** and type: `lab`
5. Click **Save**

This tells every device on your Tailscale network: for anything ending in `.lab`, ask this Mac.

---

## Step 7 — Test from another device

On a phone, iPad, or another machine connected to Tailscale, open a browser and visit:

```
http://cybercache.lab
```

If it doesn't load, check that Tailscale is active on that device, then run:

```bash
nslookup cybercache.lab
```

It should resolve to your Mac's Tailscale IP. If it resolves to something else or times out, double-check the Tailscale admin DNS settings.

---

## Adding new apps

When you add an app to `apps.conf` and rerun `local_apps.sh`, dnsmasq picks it up automatically — no restart needed, because the `address=/.lab/...` wildcard covers all `.lab` subdomains.

---

## Notes

- The Mac itself uses `/etc/hosts` for `.lab` resolution (already set up). dnsmasq is only queried by other Tailscale devices.
- If you ever remove the `/etc/hosts` entries and want the Mac to also go through dnsmasq, create `/etc/resolver/lab` containing `nameserver 127.0.0.1`.
- Tailscale IPs are stable per device but if yours ever changes, update both IP addresses in `dnsmasq.conf` and restart the service.
