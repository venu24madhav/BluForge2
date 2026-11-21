# BluForge access control

**There is no login.** Anyone who can reach the BluForge port can use the page and the whole API. The server runs as root and
can start jobs, claim radios and (after the licence notice is accepted) install BlueZ, so use it only on a trusted lab network.

History: the server had no login until 2026-10-27, then had a shared access token and a login page, and on 2026-11-19 the token
and login were removed again at the owner's request (a LANforge manager has to reach the BluForge of every resource, and a
token per unit was not wanted yet). Old bookmarks of the form `/?token=...` and `/login` still open the page.

## What is still checked
1. **Listens on every interface by default** (`--host 0.0.0.0`), so the page is reachable at the unit's IP
   (`http://<unit-ip>:5480/`) and a LANforge manager can reach the BluForge on each resource. For this-machine-only access use
   `--host 127.0.0.1` (installer: `sudo GUI_HOST=127.0.0.1 ./install.sh`); a manager then cannot see that unit's radios.
2. **Cross-site protection** for every request that changes something: the `Origin` header must match the server, a
   cross-site `Sec-Fetch-Site` is refused, and the body must be `application/json` (a web page on another site cannot send that
   without a permission check it will fail). Requests that only read are refused when the browser says they are cross-site.
3. **DNS-rebinding guard:** when listening on loopback, the `Host` header must be `localhost`, `127.0.0.1` or `[::1]`.

## Not covered
- No login of any kind, no accounts, no TLS (plain HTTP). Anyone on the network can start and stop jobs.
- A program (not a browser) on the same network can call the API directly; the cross-site checks only stop *web pages*.

## If you need more protection later
- Limit the port to the manager and your own machines with the firewall, for example on a resource:
  `sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="<manager-ip>" port port="5480" protocol="tcp" accept'`
  (and remove the general `--add-port=5480/tcp` rule), then `sudo firewall-cmd --reload`.
- Or listen on `127.0.0.1` and reach the page through an SSH tunnel (`ssh -L 5480:localhost:5480 user@unit`); a cluster then
  needs the manager and resources to reach each other some other way.
- A login can be put back in `server.py` (see `docs/INSTALL_LOG.md`, 2026-10-27, for how it worked).
