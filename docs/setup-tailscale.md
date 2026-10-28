# Cross-network access with Tailscale

The point of Tailscale is to let your client machines reach the Server Laptop
from **any** network — home Wi-Fi, a coffee shop, a phone hotspot — without
opening ports on your router or exposing the server to the public internet.
Tailscale builds an encrypted private mesh; every machine gets a stable address
that follows it around.

## 1. Install Tailscale on every machine

Install on the **Server Laptop** and on **each client**:

- macOS / Windows: download from <https://tailscale.com/download>
- Linux: `curl -fsSL https://tailscale.com/install.sh | sh`

Sign every machine into the **same Tailscale account** (this is your "tailnet"):

```bash
sudo tailscale up
```

## 2. Enable MagicDNS (recommended)

In the Tailscale admin console (<https://login.tailscale.com/admin/dns>), turn on
**MagicDNS**. Now each machine is reachable by name instead of a raw IP.

Find the Server Laptop's name:

```bash
tailscale status
```

You'll see something like `server-laptop  100.101.102.103  ...`. The name on the
left (`server-laptop`) is what clients use.

## 3. Point the client at the server

In each client's `config.toml`:

```toml
[server]
url = "http://server-laptop:8000"     # the MagicDNS name from step 2
api_key = "the-same-key-as-the-server"
```

If you did not enable MagicDNS, use the Tailscale IP instead:
`url = "http://100.101.102.103:8000"`.

## 4. Verify

From a client, on any network:

```bash
python -m client status
```

You should see `server ... OK`. That's the whole trick — the app itself only
ever speaks plain HTTP to a name; Tailscale carries it, encrypted, across
networks.

## Notes

- **Bind the server to its Tailscale IP.** By default the server listens on
  `127.0.0.1`, which only the Server Laptop itself can reach. Set `host` in
  `server-config.toml` to the Tailscale IP from step 2 (e.g. `100.101.102.103`;
  `./install-service.sh` offers it), so only machines in your tailnet can reach
  it. Avoid `0.0.0.0`: it also exposes the server on every other network the
  laptop joins.
- The API key still matters: it authenticates the *client*, while Tailscale
  authenticates the *network*. Together they mean only your machines, holding
  your key, can touch your files.
- **Optional HTTPS:** run `tailscale serve --bg 127.0.0.1:8000` on the server
  (Tailscale 1.52 or newer) to get an automatic TLS certificate. Clients then
  use `https://server-laptop.<your-tailnet>.ts.net` (port 443, no `:8000`). Not
  required, since Tailscale already encrypts all traffic. `tailscale serve`
  reaches the server on `127.0.0.1`, so with it keep the default
  `host = "127.0.0.1"`.
- **Keep the laptop awake:** disable sleep on the Server Laptop (or set it to
  stay awake on AC) so it's reachable when you need it.
