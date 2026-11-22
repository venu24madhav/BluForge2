# BluForge on a LANforge cluster

When BluForge runs on a LANforge **manager**, its page also shows and controls the Bluetooth radios of every **resource**
clustered with that manager that runs BluForge too. You open one page, on the manager, and see all the radios; you start an
advertisement, an interference job, a survey or a Classic test on a resource radio from there, and it runs on that resource.

## What you see
- **Radios tab:** a table "LANforge units in this cluster" (the manager, each resource that runs BluForge, and in grey each
  resource that does not), and the radios of every unit grouped under a heading. Radios are named after their unit, the way
  LANforge names ports: `1.1.hci0` to `1.1.hci7` for the manager (resource 1.1) and `1.2.hci0`, `1.2.hci1` for resource 1.2.
  The status bar says `Unit: <manager> (+ 1 resource in the cluster)`.
- **Interference, Advertise, BLE Sniff, Bluetooth Classic:** every radio of every unit is offered wherever a radio can be
  picked. Ticking radios of several units at once starts one job on each, each on its own unit. Jobs, console lines, survey
  devices and saved Classic results of a resource come through the manager's page. **Stop** and **Stop All** reach the resources.
- A single unit (no manager, or no resource with BluForge) looks exactly as it always did: plain `hci0` names, no extra table.

## Install
1. Install BluForge on the manager **and** on every resource that has Bluetooth radios, with the same package and installer
   (`DEPLOYMENT_GUIDE.md`): `sudo ./install.sh`. Use the same port (5480) everywhere.
2. Open `http://<manager-ip>:5480/`. Within about ten seconds the resources that run BluForge appear. Nothing to configure.
3. A resource listed in grey as *no BluForge* either does not run it yet or cannot be reached on that port. On the resource
   check `systemctl status ericsson-bluforge`, that it listens on all interfaces (the default), and the firewall
   (`sudo firewall-cmd --state`; the manager must be able to reach port 5480). A LANforge unit that never runs BluForge
   stays grey for good and is asked about only once a minute. Resources that are not LANforge units are not shown at all:
   the manager's list also holds phones and other interop devices, and only a resource whose hardware version (`hw version`
   in the list) starts with `Linux` counts.

## How it works
- The BluForge on the manager asks the LANforge JSON API (`http://127.0.0.1:8080/resource/all`, the same list the LANforge
  GUI shows) which resources exist and their control addresses, and asks each one whether BluForge answers on port 5480.
  It recognises itself (by address, host name, or an instance id in `/api/health`) and never lists itself twice.
  Only LANforge units count: the list also holds phones and other interop devices (and phantom resources), and a resource
  whose `hw version` does not start with `Linux` is skipped, never listed and never probed. One whose record carries no
  `hw version` at all is kept, so a unit that cannot be classified does not silently vanish.
- Each resource keeps working on its own and keeps its own page. The manager only forwards: a request for a resource radio
  goes to that resource's BluForge with the resource's own radio number, and what comes back is renumbered for the page.
  A resource's console output and job events reach the manager over a live connection to the resource's `/api/stream`.
- Requests one BluForge sends another carry the header `X-BluForge-Local: 1` ("answer for yourself only"), so two servers
  can never forward to each other in a loop.
- A resource that stops answering disappears from the lists, a warning says so, and it comes back by itself. Jobs on a
  resource keep running if the manager's BluForge restarts (they belong to the resource), and show up again afterwards.

## Numbering (only matters if you call the API)
The page identifies a radio by one number. Radios of the unit that serves the page keep their plain hci number (0 to 31). Each
resource that runs BluForge gets a block of 100 the first time it is seen (the first one 100 to 131, the next 200 to 231, ...),
in resource order, and keeps it while the server runs. Job ids from a resource carry its id (`1.2:job3`) and saved result files
are listed as `<block>__<name>` (`100__a2dp-...json`), so nothing collides. `GET /api/units` lists the units and their blocks.

## Options (server.py)
| Option | Meaning |
|---|---|
| `--cluster auto` (default) / `off` | look for resources / this unit only |
| `--manager-api URL` | where the resource list comes from (default `http://127.0.0.1:8080`) |
| `--peer EID=HOST[:PORT]` | add a resource by hand, e.g. `--peer 1.2=192.168.204.24`, if the manager's list cannot be read or does not show it (repeatable) |
| `--peer-port N` | port BluForge listens on at the resources (default: this server's own port) |
| `--scan-interval S` | seconds between looks for resources (default 10) |

A unit that is not a LANforge manager (no JSON API on its own port 8080) simply runs as a single unit, so the same installer and
the same command line are right for managers and resources.

## Limits
- The manager's resource list comes from the LANforge JSON API on port 8080, which is served while the LANforge GUI is running
  on the manager. If it is not reachable the page says so (Radios tab, units note) and shows only this unit; `--peer` is the
  fallback.
- A job runs entirely on one unit. Classic tests that use a second radio of the same host (the internal receiver, `link-local`)
  need both radios on the same unit; the page offers only same-unit receivers and the server refuses anything else.
  The four-controller interference check (`verify_core.py`) needs four controllers on one unit.
- There is no login (`ACCESS_CONTROL.md`): anyone who can reach port 5480 on a resource can drive its radios.
- The manager and the resources should run the same BluForge version.
- Checked with: `tests/test_cluster.py` (fake resources and a fake manager API, no hardware) and `tests/hw_cluster.py`
  (two real servers on loopback and real radios).
