#!/usr/bin/env bash
# Pemakaian: sudo lab/netra-lab.sh up | down | test
set -euo pipefail

EXT_IF="${EXT_IF:-ens33}"
VPC_CIDR="10.0.0.0/16"
RUN=/run/netra-lab
WWW=/tmp/netra-lab/www
# nama  prefix  tipe
SUBNETS=("pub 10.0.1 public" "prv 10.0.2 private")

[[ $EUID -eq 0 ]] || { echo "Jalankan dengan sudo"; exit 1; }

down() {
  for s in "${SUBNETS[@]}"; do
    read -r n p kind <<<"$s"
    for f in "$RUN/dnsmasq-$n.pid" "$RUN/http-$n.pid"; do
      if [[ -f $f ]]; then kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; fi
    done
    ip netns del "ws-$n" 2>/dev/null || true
    ip link del "br-$n" 2>/dev/null || true
    rm -rf "/etc/netns/ws-$n"
  done
  nft delete table ip netra 2>/dev/null || true
}

up() {
  down
  mkdir -p "$RUN" "$WWW"
  echo ok > "$WWW/healthz"
  sysctl -qw net.ipv4.ip_forward=1

  for s in "${SUBNETS[@]}"; do
    read -r n p kind <<<"$s"
    # bridge subnet + gateway
    ip link add "br-$n" type bridge
    ip addr add "$p.1/24" dev "br-$n"
    ip link set "br-$n" up
    # workload (netns) + veth
    ip netns add "ws-$n"
    ip link add "vh-$n" type veth peer name "vn-$n"
    ip link set "vh-$n" master "br-$n" up
    ip link set "vn-$n" netns "ws-$n"
    ip netns exec "ws-$n" ip link set lo up
    ip netns exec "ws-$n" ip link set "vn-$n" name eth0
    ip netns exec "ws-$n" ip addr add "$p.10/24" dev eth0
    ip netns exec "ws-$n" ip link set eth0 up
    # route table: public punya 0.0.0.0/0 ke IGW, private hanya rute lokal VPC
    if [[ $kind == public ]]; then
      ip netns exec "ws-$n" ip route add default via "$p.1"
    else
      ip netns exec "ws-$n" ip route add "$VPC_CIDR" via "$p.1"
    fi
    # DNS per subnet
    mkdir -p "/etc/netns/ws-$n"
    echo "nameserver $p.1" > "/etc/netns/ws-$n/resolv.conf"
    dnsmasq --conf-file=/dev/null --interface="br-$n" --listen-address="$p.1" \
      --bind-interfaces --except-interface=lo --no-resolv --no-hosts --server=1.1.1.1 \
      --pid-file="$RUN/dnsmasq-$n.pid"
    # /healthz di dalam workload
    nohup ip netns exec "ws-$n" python3 -m http.server 80 --bind "$p.10" \
      --directory "$WWW" >/dev/null 2>&1 &
    echo $! > "$RUN/http-$n.pid"
  done

  # nftables: NACL stateless per subnet + IGW (masquerade subnet public)
  local rs="table ip netra {
  chain forward {
    type filter hook forward priority 0; policy accept;"
  for s in "${SUBNETS[@]}"; do read -r n p kind <<<"$s"
    rs+="
    iifname \"br-$n\" jump out_$n"; done
  for s in "${SUBNETS[@]}"; do read -r n p kind <<<"$s"
    rs+="
    oifname \"br-$n\" jump in_$n"; done
  rs+="
  }"
  for s in "${SUBNETS[@]}"; do read -r n p kind <<<"$s"
    rs+="
  chain out_$n {
    ip protocol icmp return
    tcp dport { 80, 443 } return
    udp dport 53 return
    tcp sport 80 tcp dport 1024-65535 return
    drop
  }
  chain in_$n {
    ip protocol icmp return
    tcp dport 80 return
    tcp sport { 80, 443 } tcp dport 1024-65535 return
    udp sport 53 udp dport 1024-65535 return
    drop
  }"; done
  rs+="
  chain postrouting {
    type nat hook postrouting priority 100; policy accept;"
  for s in "${SUBNETS[@]}"; do read -r n p kind <<<"$s"
    [[ $kind == public ]] && rs+="
    oifname \"$EXT_IF\" ip saddr $p.0/24 masquerade"; done
  rs+="
  }
}"
  nft -f - <<<"$rs"
  echo "Lab aktif. Jalankan: sudo $0 test"
}

test_lab() {
  local ok=0 fail=0
  X() { ip netns exec "$@"; }
  t() { # deskripsi harapan(pass|fail) perintah...
    local d=$1 want=$2 got=fail; shift 2
    if "$@" >/dev/null 2>&1; then got=pass; fi
    if [[ $got == "$want" ]]; then echo "PASS  $d"; ok=$((ok+1))
    else echo "FAIL  $d"; fail=$((fail+1)); fi
  }
  t "pub -> gateway"                  pass X ws-pub ping -c1 -W2 10.0.1.1
  t "pub -> internet 1.1.1.1"         pass X ws-pub ping -c1 -W2 1.1.1.1
  t "pub -> DNS example.com"          pass X ws-pub dig +short +time=2 +tries=1 example.com
  t "pub -> prv"                      pass X ws-pub ping -c1 -W2 10.0.2.10
  t "prv -> gateway"                  pass X ws-prv ping -c1 -W2 10.0.2.1
  t "prv -> internet (harus gagal)"   fail X ws-prv ping -c1 -W2 1.1.1.1
  t "prv -> pub /healthz"             pass X ws-prv curl -fs --max-time 3 http://10.0.1.10/healthz
  t "host -> pub /healthz"            pass curl -fs --max-time 3 http://10.0.1.10/healthz
  echo "Lulus: $ok, gagal: $fail"
  [[ $fail -eq 0 ]]
}

case "${1:-}" in
  up) up ;; down) down ;; test) test_lab ;;
  *) echo "Pemakaian: sudo $0 up|down|test"; exit 1 ;;
esac
