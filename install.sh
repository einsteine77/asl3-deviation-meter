#!/bin/bash
# ASL3 Deviation Meter 2.0 installer - run on the AllStarLink 3 node as root.
#
#   bash install.sh                       install / update (asks the questions it needs)
#   bash install.sh --node 12345          node number (auto-detected from rpt.conf if only one)
#   bash install.sh --title "My Meter"    page heading (default "ASL3 Deviation Meter")
#   bash install.sh --port 8090           web port (default 8090)
#   bash install.sh --yes                 no questions; restart Asterisk without asking
#   bash install.sh --uninstall           remove it and put rpt.conf back
set -euo pipefail

NODE=""
TITLE=""
PORT=8090
YES=0
ACTION=install
HERE="$(cd "$(dirname "$0")" && pwd)"
LIB=/usr/local/lib/m444-meter
CONF=/etc/m444-meter.json
RPT=/etc/asterisk/rpt.conf
TAP=/usr/local/bin/m444-tap
UNIT=m444-meter.service
OLD_KNOWN="asl3-deviation-meter.service"   # older meter this one can replace

while [ $# -gt 0 ]; do
  case "$1" in
    --node)  NODE="$2"; shift 2 ;;
    --title) TITLE="$2"; shift 2 ;;
    --port)  PORT="$2"; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    --uninstall) ACTION=uninstall; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (try --help)"; exit 1 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo bash install.sh)"; exit 1; }

ask() {  # ask "prompt" default -> echoes answer
  local a
  if [ "$YES" = 1 ] || [ ! -t 0 ]; then echo "$2"; return; fi
  read -r -p "$1 [$2]: " a || true
  echo "${a:-$2}"
}

restart_asterisk() {
  echo
  echo "Asterisk has to restart to load the audio tap (this drops any active QSO)."
  local ans=n
  if [ "$YES" = 1 ]; then ans=y; elif [ -t 0 ]; then read -r -p "Restart Asterisk now? [y/N] " ans || ans=n; fi
  if [[ "$ans" =~ ^[Yy] ]]; then
    systemctl restart asterisk && echo "   Asterisk restarted."
  else
    echo "   Skipped. Run 'systemctl restart asterisk' at a quiet moment; until then the meter shows 'No audio'."
  fi
}

# rpt.conf editor: only touches outstreamcmd inside [NODE]
edit_rpt() {  # $1 = add | remove | duplex
  python3 - "$RPT" "$NODE" "$1" "$TAP" <<'PY'
import re, sys
path, node, mode, TAP = sys.argv[1:5]
lines = open(path).read().split("\n")
# settings by section, and which template the node section inherits from
vals, tmpl, cur = {}, None, None
for ln in lines:
    m = re.match(r"\s*\[([^\]]+)\]\s*(?:\(([^)]*)\))?", ln)
    if m:
        cur = m.group(1).strip()
        if cur == node and m.group(2):
            tmpl = m.group(2).replace("!", "").split(",")[0].strip() or None
        continue
    m = re.match(r"\s*([A-Za-z_]+)\s*=\s*(.*?)\s*(?:;.*)?$", ln)
    if m and cur:
        vals.setdefault(cur, {})[m.group(1).lower()] = m.group(2)
def get(k):
    v = vals.get(node, {}).get(k)
    return v if v is not None else (vals.get(tmpl, {}).get(k) if tmpl else None)
if mode == "duplex":
    print(get("duplex") or ""); sys.exit(0)
inherited = None if "outstreamcmd" in vals.get(node, {}) else (vals.get(tmpl, {}).get("outstreamcmd") if tmpl else None)
if inherited and TAP not in inherited:
    v = inherited[len("/bin/sh,-c,"):] if inherited.startswith("/bin/sh,-c,") else inherited.replace(",", " ")
    NEW = f"outstreamcmd = /bin/sh,-c,{TAP} | {v}      ; ASL3 Deviation Meter tap (copied from [{tmpl}])"
else:
    NEW = f"outstreamcmd = {TAP},--sink      ; ASL3 Deviation Meter tap"
out, sec, changed, seen, insert_at = [], None, False, False, None
def close_section():
    global changed, seen
    if sec == node and mode == "add" and not seen and insert_at is not None:
        out.insert(insert_at, NEW); changed = True; seen = True
for ln in lines:
    m = re.match(r"\s*\[([^\]]+)\]", ln)
    if m:
        close_section()
        sec = m.group(1).strip()
        out.append(ln)
        if sec == node:
            insert_at = len(out)          # default: right after the header
        continue
    if sec == node:
        m2 = re.match(r"(\s*outstreamcmd\s*=\s*)(.*)$", ln)
        if m2 and not ln.lstrip().startswith(";"):
            seen = True
            pre, val = m2.groups()
            if mode == "check":
                print("existing:" + val.strip())
            elif mode == "add" and TAP not in val:
                if val.startswith("/bin/sh,-c,"):
                    ln = pre + "/bin/sh,-c," + TAP + " | " + val[len("/bin/sh,-c,"):]
                else:
                    ln = pre + "/bin/sh,-c," + TAP + " | " + val.replace(",", " ")
                changed = True
            elif mode == "remove" and TAP in val:
                changed = True
                if "--sink" in val or "ASL3 Deviation Meter tap (copied" in ln:
                    continue
                ln = ln.replace(TAP + " | ", "")
        if re.match(r"\s*rxchannel\s*=", ln):
            insert_at = len(out) + 1
    out.append(ln)
close_section()
if mode == "check":
    sys.exit(0)
if changed:
    open(path, "w").write("\n".join(out))
print("changed" if changed else "unchanged")
PY
}

# ---------------------------------------------------------------- uninstall
if [ "$ACTION" = uninstall ]; then
  [ -z "$NODE" ] && [ -f "$CONF" ] && NODE=$(python3 -c "import json;print(json.load(open('$CONF')).get('node',''))" 2>/dev/null || true)
  [ -n "$NODE" ] || { echo "Can't tell which node it was installed on; run with --node <number>"; exit 1; }
  systemctl disable --now "$UNIT" 2>/dev/null || true
  rm -f "/etc/systemd/system/$UNIT"
  systemctl daemon-reload
  cp "$RPT" "$RPT.m444bak.$(date +%Y%m%d%H%M%S)"
  r=$(edit_rpt remove); echo "==> rpt.conf [$NODE]: $r"
  if [ -s "$LIB/old-unit" ]; then
    u=$(cat "$LIB/old-unit"); echo "==> Re-enabling the previous meter ($u)"
    systemctl enable --now "$u" || true
  fi
  rm -f "$TAP"
  echo "Removed. $LIB and $CONF were left in place (delete them by hand if you like)."
  [ "$r" = changed ] && restart_asterisk
  exit 0
fi

# ---------------------------------------------------------------- checks
command -v python3 >/dev/null || { echo "python3 is required (apt install python3)"; exit 1; }
[ -f "$RPT" ] || { echo "$RPT not found - is this an AllStarLink 3 node?"; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 7))' || { echo "Python 3.7 or newer is required"; exit 1; }

nodes=$(grep -oP '^\s*\[\K[0-9]{3,7}(?=\])' "$RPT" | sort -u || true)
if [ -z "$NODE" ]; then
  n=$(echo "$nodes" | grep -c . || true)
  if [ "$n" = 1 ]; then NODE=$nodes; echo "==> Found node $NODE in rpt.conf"
  elif [ "$YES" = 1 ] || [ ! -t 0 ]; then echo "Several (or no) nodes in rpt.conf: $nodes - pass --node <number>"; exit 1
  else
    echo "Nodes in rpt.conf: $(echo $nodes)"
    NODE=$(ask "Which node should the meter watch" "$(echo "$nodes" | head -1)")
  fi
fi
echo "$nodes" | grep -qx "$NODE" || { echo "Node [$NODE] isn't a section in $RPT"; exit 1; }

# duplex check (the meter needs duplex = 0 to see both RX and TX audio)
dup=$(edit_rpt duplex)
if [ "${dup:-}" != "0" ]; then
  echo
  echo "!! Node $NODE has duplex = ${dup:-<not set>}."
  echo "   The meter is designed for duplex = 0 (simplex node / repeater controller)."
  echo "   With other duplex modes the outstream only carries one direction, so RX/TX"
  echo "   readings will be incomplete. Nothing in your duplex setting will be changed."
  a=$(ask "Continue anyway? (y/n)" "n"); [[ "$a" =~ ^[Yy] ]] || [ "$YES" = 1 ] || exit 1
fi

[ -n "$TITLE" ] || { [ -f "$CONF" ] && TITLE=$(python3 -c "import json;print(json.load(open('$CONF')).get('title',''))" 2>/dev/null || true); }
[ -n "$TITLE" ] || TITLE=$(ask "Page title" "ASL3 Deviation Meter")

# ---------------------------------------------------------------- port
if [ -f "$CONF" ]; then
  PORT=$(python3 -c "import json;print(json.load(open('$CONF')).get('http_port',$PORT))" 2>/dev/null || echo "$PORT")
fi
pid=$(ss -ltnpH "( sport = :$PORT )" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)
if [ -n "${pid:-}" ]; then
  unit=$(ps -o unit= -p "$pid" 2>/dev/null | tr -d ' ' || true)
  if [ "$unit" = "$UNIT" ]; then
    :   # our own meter: update in place
  elif [[ " $OLD_KNOWN " == *" $unit "* ]]; then
    echo "==> Replacing the older meter ($unit) on port $PORT"
    install -d -m 0755 "$LIB"; echo "$unit" > "$LIB/old-unit"
    bk=/root/m444-old-meter-$(date +%Y%m%d%H%M%S).tgz
    keep=$(systemctl show -p FragmentPath --value "$unit")
    for w in $(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null); do
      [ -f "$w" ] || continue
      f=$(readlink -f "$w")
      case "$f" in /usr/bin/*|/bin/*|/usr/sbin/*|/usr/lib/*) continue ;; esac
      keep="$keep $f"
      d=$(dirname "$f")
      case "$d" in /opt/?*|/srv/?*|/home/?*|/root/?*|/usr/local/lib/?*|/usr/local/share/?*) keep="$keep $d" ;; esac
    done
    timeout 60 tar czf "$bk" --exclude='*/node_modules' --exclude='*/.venv' --exclude='*/venv' $keep 2>/dev/null \
      && echo "   backed up old meter to $bk" || { rm -f "$bk"; echo "   (backup skipped)"; }
    systemctl disable --now "$unit" && echo "   stopped and disabled $unit"
  else
    echo "Port $PORT is already used by $(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null | cut -c1-80) (${unit:-no unit})."
    echo "Nothing was changed. Pick another port: bash install.sh --port 8091"
    exit 1
  fi
fi

# ---------------------------------------------------------------- install
echo "==> Installing files"
install -d -m 0755 "$LIB/www"
install -m 0755 "$HERE/meter.py" "$LIB/meter.py"
install -m 0644 "$HERE"/www/* "$LIB/www/"
install -m 0755 "$HERE/m444-tap" "$TAP"
python3 - "$CONF" "$NODE" "$PORT" "$TITLE" <<'PY'
import json, os, sys
path, node, port, title = sys.argv[1:5]
c = {}
if os.path.exists(path):
    try: c = json.load(open(path))
    except Exception: pass
c.update({"node": node, "http_port": int(port), "title": title})
json.dump(c, open(path, "w"), indent=2)
PY
echo "   $CONF: node $NODE, port $PORT, title \"$TITLE\""

[ -f /etc/allmon3/allmon3.ini ] || grep -q secret /etc/asterisk/manager.conf 2>/dev/null \
  || echo "   note: no Allmon3 or AMI login found - keying will be guessed from audio level (see README)"

echo "==> Service"
install -m 0644 "$HERE/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable -q "$UNIT"
systemctl restart "$UNIT"
sleep 1
systemctl is-active --quiet "$UNIT" && echo "   meter is running" || { journalctl -u m444-meter -n 20 --no-pager; exit 1; }

echo "==> Audio tap in $RPT [$NODE]"
cp "$RPT" "$RPT.m444bak.$(date +%Y%m%d%H%M%S)"
r=$(edit_rpt add)
awk -v n="$NODE" '/^[[:space:]]*\[/{s=$0;sub(/^[[:space:]]*\[/,"",s);sub(/\].*/,"",s);in_n=(s==n)} in_n && /^[[:space:]]*outstreamcmd/' "$RPT" | sed 's/^/   /'
grep -q "$TAP" "$RPT" || { echo "!! Could not add the tap to [$NODE]; see 'Manual rpt.conf edit' in the README."; exit 1; }
if [ "$r" = changed ]; then restart_asterisk; else echo "   tap already in place"; fi

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  echo "   note: ufw is active - allow the page with: ufw allow $PORT/tcp"
fi
if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  echo "   note: firewalld is active - allow the page with: firewall-cmd --permanent --add-port=$PORT/tcp && firewall-cmd --reload"
fi

echo
echo "Done: http://$(hostname -I | awk '{print $1}'):$PORT/   (logs: journalctl -u m444-meter -f)"
