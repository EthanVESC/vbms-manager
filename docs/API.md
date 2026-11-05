# VBMS Manager API (v1)

The API lets scripts, home automation and other computers read your boards and change them over HTTP.
It's served on the same port as the web page, under `/api/v1`.

## Turning it on

1. On the computer running VBMS Manager, open **Preferences → API**, turn on **Allow API requests** and
   type `allow api requests`. It's off by default and nobody else can turn it on.
2. Under the switch, make a key: give it a name and the scopes it needs. Copy the key straight away, because it's
   only shown once. Only a hash of it is stored.

**API writes don't ask for confirmation.** The Preferences switch is your approval for all of them. They still
go through every check the web page uses:
- the board's safety warning must be accepted;
- the running script must be verified;
- hard limits apply;
- the board is read fresh before the write;
- a backup is saved;
- the board is read back after the write;
- the write is logged in `changes.log` with the key name and client IP.

Restoring backups and installing add-ons are never possible over the API.

## Keys and scopes

Send the key in a header, either `Authorization: Bearer <key>` or `X-API-Key: <key>`. Never put it in the URL.

| Scope | Allows |
|---|---|
| `read` | Every `GET` |
| `control` | Charge limit, pause/allow charging, balancing, storage mode, schedule, timers, connection |
| `settings` | Editing BMS settings (`/config`). Settings marked ⛔ danger also need the key's **danger** tick |

Too many bad keys from one address in a minute gets HTTP 429 for a minute.

## Security: read this if your server is reachable from the internet

- **Clients from outside your networks only get the API.** VBMS Manager treats any address that isn't private,
  loopback, link-local or CGNAT/Tailscale (100.64.0.0/10) as "internet". Those clients get a 403 for the web page
  and can't pair. You can change this in Preferences → Internet, but keep it off unless you need it.
- **It's plain HTTP.** The key and the data cross the internet unencrypted. Anyone on the path, such as someone on
  the same public WiFi as the caller, could read a key and reuse it. So:
  - give each key only the scopes it needs (a dashboard only needs `read`);
  - revoke a key in Preferences if you think it leaked. It stops working straight away, and the key table shows
    when and from which address each key was last used;
  - for more protection, use a VPN (WireGuard, Tailscale) and don't forward the port at all.
- **Reverse proxies.** A request carrying `X-Forwarded-For`, `Forwarded` or `X-Real-IP` is judged by the address
  the proxy reports, and is never treated as "this computer". Make sure your proxy sets one of those headers.
- **Give each script its own key** with only the scopes it needs, and revoke keys you stop using.
- `/api/v1` doesn't check the `Host` header, so a DDNS name works. Browsers never send API keys by themselves,
  so DNS rebinding isn't a risk here.

## Boards

Wherever a path has `{id}`, use the board's MAC (`AA:BB:CC:DD:EE:FF` or `aabbccddeeff`), its IP/host as added,
or its nickname (case doesn't matter).

## Reading (`read` scope)

| GET | Returns |
|---|---|
| `/api/v1/status` | Version, board counts, this key's name and scopes |
| `/api/v1/boards` | Every board in compact form, described after this table |
| `/api/v1/boards/{id}` | Everything the web page knows about the board, including the config, editable fields and recent events |
| `/api/v1/boards/{id}/cells` | Cell voltages, balancing flags, temperatures |
| `/api/v1/boards/{id}/config` | Editable settings with value, range, unit and risk levels |
| `/api/v1/boards/{id}/history?metric=voltage&from=&to=&points=` | Recorded history. `from`/`to` are Unix seconds and default to the last 15 min. Metrics: `power`, `current`, `voltage`, `soc`, `cells`, `spread`, `temps`, `charger` |
| `/api/v1/boards/{id}/charges` | Charge sessions |
| `/api/v1/boards/{id}/analysis` | Capacity, drift and self-discharge analysis |
| `/api/v1/boards/{id}/events` | Recent events |
| `/api/v1/boards/{id}/schedule` | Schedule, timers and what the schedule is doing now |
| `/api/v1/boards/{id}/storage` | Storage mode state and settings |
| `/api/v1/notices` | Safety notices for every board |
| `/api/v1/log/changes?limit=200` | The change log |
| `/api/v1/log/events?limit=200` | The event log |

Each entry in `/api/v1/boards` has these fields:
- `name`, `state` (online, offline, resting, paused, busy…), `storage`, `rest`, `notices`;
- live values: `soc` (%), `pack_v`, `current_a`, `charger_v`, `charging`, `balancing`, `cells`, `cell_min`, `cell_max`, `delta_mv`, `temp_max`, `fault`;
- config: `charge_start`, `charge_end`, `limit_pct`, `balance_min`, `balance_max_current`, `charging_paused`;
- `last_seen`, the last reading when the board is asleep or offline.

## Changing things

Every `POST` takes a JSON body. Add `?dry_run=1` (or `"dry_run": true`) to see exactly what would change without
writing anything. Writes need the board to be online.

| POST | Body | Scope |
|---|---|---|
| `/api/v1/boards/{id}/charge-limit` | `{"pct": 80}` (60–100), or `{"end": 4.05, "start": 3.95}` | control |
| `/api/v1/boards/{id}/charging` | `{"allowed": false}`. A live RAM command: the BMS forgets it when it restarts or wakes | control |
| `/api/v1/boards/{id}/balancing` | `{"allowed": false}` writes Balance Max Current 0 A; `true` writes your value back. `{"force": true}` sends the live force-balance command | control |
| `/api/v1/boards/{id}/storage` | `{"on": true}` / `{"on": false}` | control |
| `/api/v1/boards/{id}/storage/settings` | `{"end_v": 3.75, "gap": 0.1, "balance_min": 3.4, "balance": true, "sleep": true}` (any subset) | control |
| `/api/v1/boards/{id}/config` | `{"changes": {"vc_balance_min": 3.5, "max_charge_current": 6}}`. Field names come from `/config` | settings (+danger) |
| `/api/v1/boards/{id}/schedule` | `{"schedule": {...}}`, the same shape the web page saves (see `GET …/schedule`) | control |
| `/api/v1/boards/{id}/schedule/off` | `{}` | control |
| `/api/v1/boards/{id}/timers` | `{"timer": {"in_s": 3600, "charge": "on", "pct": 90}}`. Use `at` (Unix seconds) instead of `in_s` for a fixed time, within 7 days. `charge`/`balance` take `"on"`/`"off"` | control |
| `/api/v1/boards/{id}/timers/{timer_id}/cancel` | `{}` | control |
| `/api/v1/boards/{id}/connection` | `{"connected": false}` disconnects (frees it for VESC Tool). `true` reconnects and also ends a low-SOC or storage rest | control |
| `/api/v1/all/charge-limit` | `{"pct": 80}`, applied to every board | control |
| `/api/v1/all/charging` | `{"allowed": true}` | control |
| `/api/v1/all/balancing` | `{"allowed": true}` | control |
| `/api/v1/all/storage` | `{"on": true}`. A board that's asleep when you turn storage **off** is switched back as soon as it reconnects | control |

While storage mode is on, the charge limit and balancing belong to storage mode. Turn it off first; the API
answers 422 or 409 otherwise.

## Errors

Errors come back as JSON `{"error": "...", "code": "..."}`:

| HTTP | code | Meaning |
|---|---|---|
| 401 | `no_key`, `bad_key` | Missing, unknown or revoked key |
| 403 | `api_off` | API requests are turned off in Preferences |
| 403 | `scope`, `danger` | The key lacks the scope or the danger tick. `danger` also lists `fields` |
| 404 | `no_board`, `not_found` | Unknown board or endpoint |
| 409 | `offline`, `risk`, `refused`, `storage`, `link` | A safety check refused it (the message says why) or the board isn't reachable. Nothing was written unless the message says so |
| 422 | `invalid` | Bad value (out of range, wrong type) |
| 429 | `rate_limited` | Too many bad keys from this address |

## Examples

```bash
KEY=vbms_...                       # your key
VBMS=http://my-home.example:8642   # your DDNS name or public IP, or http://192.168.1.10:8642 at home

# What's going on?
curl -s -H "Authorization: Bearer $KEY" $VBMS/api/v1/boards

# Every board to 80 % (check first, then do it)
curl -s -H "Authorization: Bearer $KEY" -X POST "$VBMS/api/v1/all/charge-limit?dry_run=1" -d '{"pct": 80}'
curl -s -H "Authorization: Bearer $KEY" -X POST "$VBMS/api/v1/all/charge-limit" -d '{"pct": 80}'

# Pause charging on one board, allow it again
curl -s -H "Authorization: Bearer $KEY" -X POST $VBMS/api/v1/boards/Bailey/charging -d '{"allowed": false}'
curl -s -H "Authorization: Bearer $KEY" -X POST $VBMS/api/v1/boards/Bailey/charging -d '{"allowed": true}'

# Cell voltages
curl -s -H "Authorization: Bearer $KEY" $VBMS/api/v1/boards/Ethan/cells

# Everything into storage for the winter
curl -s -H "Authorization: Bearer $KEY" -X POST $VBMS/api/v1/all/storage -d '{"on": true}'
```

A GitHub Actions step, or any CI job, works the same way: keep the key in a secret and `curl` the endpoint.
