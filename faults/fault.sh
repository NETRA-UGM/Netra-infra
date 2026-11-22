#!/usr/bin/env bash
# Pemakaian: sudo faults/fault.sh <1-6|all> break|check|fix|run
set -uo pipefail
EXT_IF="${EXT_IF:-ens33}"
RUN=/run/netra-lab
[[ $EUID -eq 0 ]] || { echo "Jalankan dengan sudo"; exit 1; }

X() { ip netns exec "$@"; }
pass() { "$@" >/dev/null 2>&1; }
handle() { nft -a list chain ip netra "$1" | grep -E "$2" | head -1 | sed -E 's/.*# handle ([0-9]+)$/\1/'; }
P80='^[[:space:]]*tcp dport 80 return # handle'
PDENY='ip saddr 10.0.2.0/24 drop # handle'

# 1 Routing: hapus default route di workload public
break_1() { X ws-pub ip route del default; }
check_1() { pass X ws-pub ping -c1 -W2 1.1.1.1; }
fix_1()   { X ws-pub ip route replace default via 10.0.1.1; }

# 2 IP Forwarding dimatikan
break_2() { sysctl -qw net.ipv4.ip_forward=0; }
check_2() { pass X ws-pub ping -c1 -W2 10.0.2.10; }
fix_2()   { sysctl -qw net.ipv4.ip_forward=1; }

# 3 Internet Gateway: hapus rule masquerade
break_3() { nft flush chain ip netra postrouting; }
check_3() { pass X ws-pub ping -c1 -W2 1.1.1.1; }
fix_3()   { nft flush chain ip netra postrouting
            nft add rule ip netra postrouting oifname "$EXT_IF" ip saddr 10.0.1.0/24 masquerade; }

# 4 NACL: hapus rule allow port 80 (inbound subnet public)
break_4() { local h; h=$(handle in_pub "$P80"); [[ -n $h ]] && nft delete rule ip netra in_pub handle "$h"; }
check_4() { pass X ws-prv curl -fs --max-time 3 http://10.0.1.10/healthz; }
fix_4()   { if [[ -z $(handle in_pub "$P80") ]]; then nft insert rule ip netra in_pub tcp dport 80 return; fi; }

# 5 NACL: tambah rule deny di urutan lebih awal
break_5() { nft insert rule ip netra in_pub ip saddr 10.0.2.0/24 drop; }
check_5() { pass X ws-prv ping -c1 -W2 10.0.1.10 && pass X ws-prv curl -fs --max-time 3 http://10.0.1.10/healthz; }
fix_5()   { local h; h=$(handle in_pub "$PDENY"); if [[ -n $h ]]; then nft delete rule ip netra in_pub handle "$h"; fi; }

# 6 DNS: hentikan dnsmasq subnet public
break_6() { local p; p=$(cat $RUN/dnsmasq-pub.pid); kill "$p"; while kill -0 "$p" 2>/dev/null; do sleep 0.1; done; }
check_6() { X ws-pub dig +short +time=2 +tries=1 example.com 2>/dev/null | grep -qE '^[0-9]+(\.[0-9]+){3}$'; }
fix_6()   { [[ -f $RUN/dnsmasq-pub.pid ]] && kill "$(cat $RUN/dnsmasq-pub.pid)" 2>/dev/null; sleep 0.5
            dnsmasq --conf-file=/dev/null --interface=br-pub --listen-address=10.0.1.1 \
              --bind-interfaces --except-interface=lo --no-resolv --no-hosts \
              --server=1.1.1.1 --pid-file="$RUN/dnsmasq-pub.pid"; }

usage() { echo "Pemakaian: sudo $0 <1-6|all> break|check|fix|run"; exit 1; }
n=${1:-}; cmd=${2:-}
if [[ $n == all ]]; then
  for i in 1 2 3 4 5 6; do echo "== Skenario $i =="; bash "$0" "$i" run; done; exit 0
fi
[[ $n =~ ^[1-6]$ ]] || usage
case $cmd in
  break|fix) "${cmd}_$n" ;;
  check) if "check_$n"; then echo "SEHAT (skenario $n)"; else echo "RUSAK (skenario $n)"; exit 1; fi ;;
  run)
    check_$n || { echo "Lab belum sehat. Jalankan: sudo lab/netra-lab.sh up"; exit 1; }
    break_$n
    if check_$n; then echo "  setelah break: MASIH SEHAT (break tidak berefek)"; else echo "  setelah break: RUSAK (sesuai harapan)"; fi
    fix_$n; sleep 1
    if check_$n; then echo "  setelah fix  : PULIH"; else echo "  setelah fix  : MASIH RUSAK"; fi ;;
  *) usage ;;
esac
