#!/usr/bin/env python3
"""Evaluasi Auto-remediation NETRA lewat API: rusakkan, tunggu insiden, buat rencana, approve, ukur waktu pulih.

Prasyarat (di VM):
- Backend jalan dengan NETDRIVER_MODE=real (terminal A).
- Resource uji dari Netra-backend: `KEEP=1 bash scripts/try_workload.sh` (VPC client1 10.30.0.0/16, web, db).
- Token di env T1 (client1) dan T2 (client2), dibuat dengan blok eval di progress/AI.md.
- `sudo -v` baru saja dijalankan (perintah break dan cek memakai `sudo -n`).

Pakai:
  python3 faults/eval.py --mode manual --scenarios 1-6 --repeat 5
  python3 faults/eval.py --mode manual --scenarios 7-9 --repeat 5
  python3 faults/eval.py --selftest          # cek mandiri tanpa jaringan

Skenario 1-6 = drift host (30 percobaan resmi). 7-9 = salah konfigurasi lewat API (tambahan).
Hasil per percobaan ditulis langsung ke CSV, jadi tetap tersimpan kalau evaluasi berhenti di tengah."""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from types import SimpleNamespace

API = os.environ.get("API", "http://127.0.0.1:8000/api/v1")
TARGET_S = 120          # target pulih (approved_at sampai finished_at)
TARGET_PCT = 90
WAIT_S = 60             # batas tunggu insiden muncul, eksekusi selesai, dan kembali sehat

SCENARIOS = {  # nomor: (kategori, resource_type, keterangan)
    1: ("routing", "workload", "default route workload dihapus di host"),
    2: ("ip_forwarding", "vpc", "net.ipv4.ip_forward=0"),
    3: ("igw", "internet_gateway", "chain postrouting (masquerade) di-flush"),
    4: ("nacl", "nacl", "rule NACL tcp dport 80 dihapus di host"),
    5: ("nacl", "nacl", "rule drop dari subnet private disisipkan di host"),
    6: ("dns", "subnet", "dnsmasq subnet public dimatikan"),
    7: ("routing", "subnet", "route 0.0.0.0/0 -> igw dihapus lewat API"),
    8: ("nacl", "subnet", "rule outbound 50 deny ICMP ditambah lewat API"),
    9: ("nacl", "subnet", "semua rule outbound ICMP dihapus lewat API"),
}
FIELDS = ["no", "skenario", "ulangan", "mode", "insiden", "aksi", "status", "deteksi_s", "pulih_s",
          "sehat_setelah", "berhasil", "catatan"]
_IP = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$", re.M)


class Stop(Exception):
    """Evaluasi dihentikan: keadaan tidak sesuai harapan, lanjut berisiko menumpuk kerusakan."""


# ---------- akses API dan host ----------
def api(method, path, token, body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


def ok(method, path, token, body=None):
    code, data = api(method, path, token, body)
    if code >= 300:
        raise Stop(f"{method} {path} -> {code} {data}")
    return data


def sudo(*args, check=True):
    r = subprocess.run(["sudo", "-n", *map(str, args)], capture_output=True, text=True, timeout=20)
    if check and r.returncode != 0:
        raise Stop(f"sudo {' '.join(map(str, args))} gagal: {r.stderr.strip() or r.returncode}")
    return r


def wait(fn, timeout=WAIT_S, every=1.0):
    """Ulangi fn sampai hasilnya truthy, kembalikan hasil itu. None kalau habis waktu."""
    deadline = time.monotonic() + timeout
    while True:
        got = fn()
        if got or time.monotonic() >= deadline:
            return got or None
        time.sleep(every)


def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def find_handle(nft_text, rule):
    """Handle rule di output `nft -a list chain`. None kalau rule tidak ada."""
    m = re.search(rf"^\s*{re.escape(rule)} # handle (\d+)$", nft_text, re.M)
    return m.group(1) if m else None


# ---------- lingkungan uji ----------
def discover(t1):
    vpc = next((v for v in ok("GET", "/vpcs", t1) if v["cidr"] == "10.30.0.0/16"), None)
    if vpc is None:
        raise Stop("VPC 10.30.0.0/16 milik client1 tidak ada. Jalankan `KEEP=1 bash scripts/try_workload.sh` dulu.")
    subs = {s["id"]: s for s in ok("GET", f"/subnets?vpc_id={vpc['id']}", t1)}
    wls = {w["name"]: w for w in ok("GET", "/workloads", t1) if w["subnet_id"] in subs}
    if "web" not in wls or "db" not in wls:
        raise Stop("workload web atau db tidak ter-attach di VPC 10.30.0.0/16")
    web, db = wls["web"], wls["db"]
    pub = subs[web["subnet_id"]]
    rt = next(r for r in ok("GET", f"/route-tables?vpc_id={vpc['id']}", t1) if r["subnet_id"] == pub["id"])
    nacl = next(n for n in ok("GET", f"/nacls?vpc_id={vpc['id']}", t1) if n["subnet_id"] == pub["id"])
    return SimpleNamespace(ns=web["netns_name"], sid=pub["bridge_name"][len("br-"):], prv=subs[db["subnet_id"]]["cidr"],
                           db_ip=db["ip"], rt=rt["id"], nacl=nacl["id"])


def healthy(e):
    """Cek aktif dari workload web: internet, /healthz db (antarsubnet), dan DNS."""
    def x(*a):
        return sudo("ip", "netns", "exec", e.ns, *a, check=False)
    return (x("ping", "-c1", "-W2", "1.1.1.1").returncode == 0
            and x("curl", "-fs", "--max-time", "3", f"http://{e.db_ip}/healthz").returncode == 0
            and bool(_IP.search(x("dig", "+short", "+time=2", "+tries=1", "example.com").stdout)))


def open_incidents(t1, t2):
    return [i for t in (t1, t2) for i in ok("GET", "/incidents", t)
            if i["status"] in ("open", "remediating")]


# ---------- break per skenario ----------
def _rules(e, t1):
    return ok("GET", f"/nacls/{e.nacl}/rules", t1)


def break_(n, e, t1):
    chain = f"in_{e.sid}"
    if n == 1:
        sudo("ip", "netns", "exec", e.ns, "ip", "route", "del", "default")
    elif n == 2:
        sudo("sysctl", "-qw", "net.ipv4.ip_forward=0")
    elif n == 3:
        sudo("nft", "flush", "chain", "ip", "netra", "postrouting")
    elif n == 4:
        h = find_handle(sudo("nft", "-a", "list", "chain", "ip", "netra", chain).stdout, "tcp dport 80 return")
        if h is None:
            raise Stop(f"rule tcp dport 80 tidak ada di {chain}")
        sudo("nft", "delete", "rule", "ip", "netra", chain, "handle", h)
    elif n == 5:
        sudo("nft", "insert", "rule", "ip", "netra", chain, "ip", "saddr", e.prv, "drop")
    elif n == 6:
        pid = sudo("cat", f"/run/netra/dnsmasq-{e.sid}.pid").stdout.strip()
        sudo("kill", pid)
    elif n == 7:
        d = next((r for r in ok("GET", f"/route-tables/{e.rt}/routes", t1) if r["destination"] == "0.0.0.0/0"), None)
        if d is None:
            raise Stop("route 0.0.0.0/0 tidak ada di route table subnet public")
        ok("DELETE", f"/route-tables/{e.rt}/routes/{d['id']}", t1)
    elif n == 8:
        ok("POST", f"/nacls/{e.nacl}/rules", t1, {"rule_no": 50, "direction": "outbound", "protocol": "icmp",
                                                  "cidr": "0.0.0.0/0", "action": "deny"})
    elif n == 9:
        for r in _rules(e, t1):
            if r["direction"] == "outbound" and r["protocol"] == "icmp":
                ok("DELETE", f"/nacls/{e.nacl}/rules/{r['id']}", t1)


def prepare(n, e, t1):
    """Persiapan yang tidak dihitung: skenario 8 butuh rule_no 50 outbound kosong (sisa perbaikan percobaan lalu)."""
    if n == 8:
        for r in _rules(e, t1):
            if r["direction"] == "outbound" and r["rule_no"] == 50:
                ok("DELETE", f"/nacls/{e.nacl}/rules/{r['id']}", t1)


# ---------- satu percobaan ----------
def remediate(inc_id, mode, t1):
    """Buat rencana lalu approve. Kembalikan (rencana, remediation akhir). Remediation None kalau rencana ditolak."""
    code, rem = api("POST", f"/incidents/{inc_id}/remediations", t1, {"mode": mode})
    if code != 201 or rem.get("status") != "pending_approval":
        return rem, None
    ok("POST", f"/remediations/{rem['id']}/approve", t1)
    done = wait(lambda: (r := ok("GET", f"/remediations/{rem['id']}", t1))["status"] != "executing" and r)
    if done is None:
        raise Stop(f"remediation {rem['id']} masih executing setelah {WAIT_S} detik")
    return rem, done


def trial(n, k, mode, e, t1, t2):
    cat, rtype, _ = SCENARIOS[n]
    row = dict(skenario=n, ulangan=k, mode=mode, insiden=f"{cat}/{rtype}", aksi="", status="", deteksi_s="",
               pulih_s="", sehat_setelah="", berhasil=False, catatan="")
    prepare(n, e, t1)
    if not wait(lambda: not open_incidents(t1, t2) and healthy(e)):
        raise Stop("keadaan belum sehat sebelum percobaan (ada insiden open atau cek aktif gagal)")
    t0 = time.monotonic()
    break_(n, e, t1)
    inc = wait(lambda: next((i for i in ok("GET", "/incidents?status=open", t1)
                             if (i["category"], i["resource_type"]) == (cat, rtype)), None))
    if inc is None:
        raise Stop(f"insiden {cat}/{rtype} tidak muncul dalam {WAIT_S} detik setelah break skenario {n}")
    row["deteksi_s"] = round(time.monotonic() - t0, 1)

    plan, done = remediate(inc["id"], mode, t1)
    if isinstance(plan, dict) and plan.get("plan"):
        row["aksi"] = "+".join(str(s.get("action")) for s in plan["plan"])
    if done is None:
        row["status"] = plan.get("status") if isinstance(plan, dict) else "error"
        row["catatan"] = str(plan.get("detail") or plan.get("diagnosis") or "")[:200] if isinstance(plan, dict) else str(plan)
    else:
        row["status"] = done["status"]
        row["pulih_s"] = round((iso(done["finished_at"]) - iso(done["approved_at"])).total_seconds(), 1)

    if row["status"] != "succeeded":  # pulihkan dengan manual agar percobaan berikutnya mulai dari keadaan sehat
        if mode == "manual":
            raise Stop(f"remediation manual skenario {n} berstatus {row['status']}")
        _, fix = remediate(inc["id"], "manual", t1)
        row["catatan"] = (row["catatan"] + f" | dipulihkan manual: {fix and fix['status']}").strip(" |")
        if fix is None or fix["status"] != "succeeded":
            raise Stop(f"pemulihan manual skenario {n} gagal")

    row["sehat_setelah"] = bool(wait(lambda: not open_incidents(t1, t2) and healthy(e)))
    row["berhasil"] = (row["status"] == "succeeded" and row["sehat_setelah"]
                       and row["pulih_s"] != "" and row["pulih_s"] < TARGET_S)
    return row


# ---------- ringkasan ----------
def summary(rows):
    if not rows:
        return "Belum ada percobaan."
    good = sum(1 for r in rows if r["berhasil"])
    pct = 100 * good / len(rows)
    pulih = [r["pulih_s"] for r in rows if r["pulih_s"] != ""]
    deteksi = [r["deteksi_s"] for r in rows if r["deteksi_s"] != ""]
    out = [f"Berhasil pulih < {TARGET_S} detik: {good}/{len(rows)} ({pct:.0f}%). "
           f"Target {TARGET_PCT}%: {'TERCAPAI' if pct >= TARGET_PCT else 'BELUM'}"]
    if pulih:
        out.append(f"Waktu pulih: rata-rata {sum(pulih) / len(pulih):.1f} detik, maksimum {max(pulih):.1f} detik")
    if deteksi:
        out.append(f"Waktu deteksi: rata-rata {sum(deteksi) / len(deteksi):.1f} detik, maksimum {max(deteksi):.1f} detik")
    for n in sorted({r["skenario"] for r in rows}):
        rs = [r for r in rows if r["skenario"] == n]
        aksi = sorted({r["aksi"] for r in rs if r["aksi"]})
        out.append(f"  Skenario {n} ({SCENARIOS[n][2]}): {sum(r['berhasil'] for r in rs)}/{len(rs)} berhasil, "
                   f"aksi {', '.join(aksi) or '-'}")
    return "\n".join(out)


def parse_scenarios(s):
    out = []
    for part in s.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    if not out or any(n not in SCENARIOS for n in out):
        raise SystemExit(f"skenario harus di antara 1-9, dapat {s}")
    return out


def selftest():
    nft = ("table ip netra {\n\tchain in_abc {\n\t\tip protocol icmp return # handle 7\n"
           "\t\ttcp dport 80 return # handle 8\n\t\ttcp dport 1024-65535 return # handle 9\n\t\tdrop # handle 12\n\t}\n}\n")
    assert find_handle(nft, "tcp dport 80 return") == "8"
    assert find_handle(nft, "ip saddr 10.30.2.0/24 drop") is None
    assert parse_scenarios("1-6") == [1, 2, 3, 4, 5, 6] and parse_scenarios("7-9,2") == [7, 8, 9, 2]
    assert (iso("2026-11-27T10:00:02.500000+00:00") - iso("2026-11-27T10:00:00+00:00")).total_seconds() == 2.5
    rows = [dict(skenario=1, berhasil=True, pulih_s=0.1, deteksi_s=12.0, aksi="replace_default_route")] * 9 + \
           [dict(skenario=2, berhasil=False, pulih_s="", deteksi_s=20.0, aksi="")]
    s = summary(rows)
    assert "9/10 (90%)" in s and "TERCAPAI" in s and "Skenario 2" in s, s
    assert "BELUM" in summary(rows[:1] + rows[-1:])
    print("selftest OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["manual", "ai"], default="manual")
    ap.add_argument("--scenarios", default="1-6", help="misalnya 1-6, 7-9, atau 1,4")
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--out", help="file CSV (default /tmp/netra-eval-<mode>-<waktu>.csv)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    scenarios = parse_scenarios(a.scenarios)
    t1, t2 = os.environ.get("T1"), os.environ.get("T2")
    if not (t1 and t2):
        raise SystemExit("Isi env T1 dan T2 dengan token client1 dan client2 dulu.")
    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0:
        raise SystemExit("Jalankan `sudo -v` dulu, lalu ulangi.")
    out = a.out or f"/tmp/netra-eval-{a.mode}-{datetime.now():%Y%m%d-%H%M}.csv"
    rows = []
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        try:
            e = discover(t1)
            print(f"Lingkungan: netns {e.ns}, subnet public {e.sid}, mode {a.mode}, CSV {out}")
            for n in scenarios:
                for k in range(1, a.repeat + 1):
                    row = trial(n, k, a.mode, e, t1, t2)
                    row["no"] = len(rows) + 1
                    rows.append(row)
                    w.writerow(row)
                    f.flush()
                    print(f"[{row['no']:2}] skenario {n} #{k}: {row['status']:<9} aksi={row['aksi'] or '-'} "
                          f"deteksi={row['deteksi_s']}s pulih={row['pulih_s']}s sehat={row['sehat_setelah']} "
                          f"{'OK' if row['berhasil'] else 'GAGAL'} {row['catatan']}")
        except (Stop, KeyboardInterrupt) as ex:
            print(f"\nEVALUASI BERHENTI: {ex or 'Ctrl+C'}")
            print("Cek terminal A. Insiden yang masih open bisa diperbaiki dengan `bash scripts/try_fix.sh` di Netra-backend.")
            print("\n" + summary(rows))
            print(f"CSV: {out}")
            sys.exit(1)
    print("\n" + summary(rows))
    print(f"CSV: {out}")


if __name__ == "__main__":
    main()
