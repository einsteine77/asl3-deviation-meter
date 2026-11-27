# ASL3 Deviation Meter 2.0

A live audio level and deviation meter for an AllStarLink 3 node, in your browser.

- Separate **RX** (local receiver) and **TX** (network to transmitter) level bars with peak hold
- **Estimated deviation** in kHz, once you calibrate it against a service monitor
- **Listen live**: stream the node's audio to your browser
- A key-up timer, a 60-second activity strip, a waveform view, and a history of recent transmissions showing which linked node was talking
- Python 3 standard library only. No pip, no Node.js, no database.

It reads the node's own audio stream (`outstreamcmd` in `rpt.conf`) through a small pass-through "tap". If you already stream to Broadcastify or Icecast through `outstreamcmd`, that keeps working.

---

## Requirements

- An **AllStarLink 3** node (Debian 12 / Raspberry Pi OS Bookworm), with root or sudo access
- **python3** 3.7 or newer (ASL3 already has it)
- The node set to **`duplex = 0`**. In that mode, app_rpt puts local receiver audio on the outstream while COS is up and network audio otherwise, so one feed covers both directions. With other duplex modes you'll only see one side. The installer warns you but doesn't change your duplex setting.
- **Optional but recommended:** Allmon3 installed. The meter reuses Allmon3's Asterisk Manager (AMI) login to see exactly when RX/TX is keyed and which linked node is talking. Without it, the meter falls back to `/etc/asterisk/manager.conf`, and if it can't log in there either, it guesses keying from the audio level.

---

## Install

### Quick install straight from GitHub

On the node, as root:

```
cd /root
git clone https://github.com/einsteine77/asl3-deviation-meter.git
cd asl3-deviation-meter
bash install.sh
```

(No git? Run `apt install -y git`, or use the zip method below. To update later: `cd /root/asl3-deviation-meter && git pull && bash install.sh`.)

### Or: copy a zip to the node

#### 1. Copy the package to the node

From **Windows (PowerShell)**, or from Mac/Linux, replacing the address with your node's:

```
scp asl3-deviation-meter.zip root@192.168.1.50:/root/
```

(If you don't log in as root, use your normal user and put `sudo` in front of the commands below.)

#### 2. Unzip and run the installer

```
ssh root@192.168.1.50
cd /root
apt install -y unzip          # only if unzip isn't installed
unzip asl3-deviation-meter.zip
cd asl3-deviation-meter
bash install.sh
```

Use `bash install.sh`, not `./install.sh`. Some nodes mount `/tmp` and other folders so scripts can't run directly.

The installer:

1. Finds your node number in `rpt.conf`, or asks if you have more than one
2. Checks `duplex` and warns if it isn't 0
3. Asks for a page title, such as `KD2XYZ Repeater Meter`
4. Installs the meter and starts it as a systemd service (`m444-meter`)
5. Backs up `rpt.conf`, then adds the audio tap to your node's section
6. Asks before restarting Asterisk, because the tap only loads on restart and the restart drops any QSO in progress

When it finishes, it prints the address. Open it in a browser:

```
http://<node-ip>:8090/
```

### Installer options

| Option | What it does |
|---|---|
| `--node 12345` | Node to watch. Only needed if `rpt.conf` has more than one. |
| `--title "My Meter"` | Page heading |
| `--port 8091` | Web port (default **8090**) |
| `--yes` | No questions asked. Restarts Asterisk automatically. |
| `--uninstall` | Removes the meter and takes the tap out of `rpt.conf` |

Running `install.sh` again updates an existing install and keeps your calibration.

---

## What it changes on your node

| Path | Purpose |
|---|---|
| `/usr/local/lib/m444-meter/` | Meter program (`meter.py`) and web page (`www/`) |
| `/usr/local/bin/m444-tap` | The audio tap |
| `/etc/m444-meter.json` | Settings and calibration |
| `/etc/systemd/system/m444-meter.service` | systemd service |
| `/etc/asterisk/rpt.conf` | One `outstreamcmd` line in your node's section. A timestamped backup is saved as `rpt.conf.m444bak.*` every time. |

**How the `rpt.conf` change works:**

- **No `outstreamcmd` yet:** it adds `outstreamcmd = /usr/local/bin/m444-tap,--sink`
- **You already stream** (lame/ezstream to Broadcastify, for example): it puts the tap in front of your existing command, so your stream gets identical audio
- **Your `outstreamcmd` lives in a template** such as `[node-main]`: it copies that command into your node's section with the tap in front, and leaves the template alone

Nothing else in Asterisk is touched. No new AMI users, no new ports in Asterisk.

---

## Calibrating deviation

The level bars work immediately. Deviation in kHz needs a one-time calibration against a service monitor or deviation meter. Open **Calibration** at the bottom of the page.

- **RX:** transmit a steady tone (a 1 kHz tone at a known deviation, such as 3 kHz) into the receiver. While it's playing, enter the deviation your monitor reads.
- **TX:** send a steady 1 kHz tone through the network (a parrot node, or another node playing a tone). Read the transmitter's deviation on the monitor and enter it while the tone is still playing.

Calibration can only be changed from your local network. The figures are estimates derived from audio levels, not RF measurements. TX audio is measured before the node's pre-emphasis, so on-air voice deviation rises with pitch.

---

## Settings (`/etc/m444-meter.json`)

The installer writes `node`, `http_port` and `title`. Other keys you can add:

```json
{
  "node": "12345",
  "http_port": 8090,
  "title": "KD2XYZ Repeater Meter",
  "listen_enabled": true,
  "public_url": "https://meter.example.com",
  "ami": { "host": "127.0.0.1", "port": 5038, "user": "admin", "pass": "secret" }
}
```

- `listen_enabled`: set to `false` to turn off the Listen live button
- `public_url`: if you publish the meter through a reverse proxy, the address the Bookmark button should show
- `ami`: only needed if the meter can't find your AMI login in Allmon3 or `manager.conf`

After editing, run `systemctl restart m444-meter`.

---

## Uninstall

```
cd /root/asl3-deviation-meter
bash install.sh --uninstall
```

This stops and removes the service, removes the tap from `rpt.conf` (restoring your original `outstreamcmd` if you had one), and asks before restarting Asterisk. `/usr/local/lib/m444-meter` and `/etc/m444-meter.json` are left in place in case you reinstall. Delete them by hand if you don't.

---

## Troubleshooting

**The page says "No audio from the node".**
Asterisk hasn't restarted since the install. Run `systemctl restart asterisk` at a quiet moment. Then check that the tap is in your node's section with `grep -n outstreamcmd /etc/asterisk/rpt.conf`.

**The page won't load.**
Check `systemctl status m444-meter` and `journalctl -u m444-meter -n 50`. If a firewall is on, open the port: `ufw allow 8090/tcp`.

**"Port 8090 is already used by …"**
Something else is on that port. The installer stops without changing anything. Rerun with `--port 8091`, or any free port.

**Keying shows "Keying: audio level" instead of AMI.**
The meter couldn't log in to the Asterisk Manager. Install Allmon3, or add an `ami` block to `/etc/m444-meter.json` (see Settings).

**Only RX or only TX ever shows.**
Your node probably isn't `duplex = 0`. See Requirements.

**My Broadcastify stream stopped.**
Compare your `rpt.conf` with the newest `rpt.conf.m444bak.*` backup. The tap passes audio straight through, but you can uninstall to put the original line back.

### Manual `rpt.conf` edit

If the installer couldn't add the tap, add it by hand in your node's section (`[12345]`), then restart Asterisk:

```
; no existing stream:
outstreamcmd = /usr/local/bin/m444-tap,--sink

; with an existing stream, put the tap first:
outstreamcmd = /bin/sh,-c,/usr/local/bin/m444-tap | /usr/bin/lame ... | /usr/bin/ezstream ...
```

---

## Web API (for dashboards)

| Endpoint | Returns |
|---|---|
| `/api/status` | JSON snapshot: levels, deviation, keying, history |
| `/api/stream` | Server-Sent Events, about 25 updates/s |
| `/api/audio` | Live raw PCM, signed 16-bit, 8 kHz, mono |

Originally written for N2UGS's WA2HKS 444.000 system in Western New York. Shared freely; use at your own risk.
