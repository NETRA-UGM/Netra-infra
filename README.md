# Netra-infra

Skrip infrastruktur NETRA: lab VPC manual di satu VM Ubuntu dan fault injection untuk pengujian Auto-remediation AI.
Perintah di sini menjadi acuan modul `netdriver` di Netra-backend.

## Prasyarat

- Ubuntu 24.04 di VMware (4 vCPU, 6 GB RAM, 60 GB disk), NIC VMnet8 untuk internet
- Paket:
  ```bash
  sudo apt install -y iproute2 nftables dnsmasq-base dnsutils curl
  echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/99-netra.conf && sudo sysctl --system
  ```
- Cek interface keluar dengan `ip route get 1.1.1.1`. Default skrip `ens33`, ganti lewat `EXT_IF=<nama>` kalau berbeda.
- `lsmod | grep br_netfilter` sebaiknya kosong.

## Lab VPC (`lab/netra-lab.sh`)

```bash
sudo lab/netra-lab.sh up     # bangun lab (aman diulang)
sudo lab/netra-lab.sh test   # target: Lulus 8, gagal 0
sudo lab/netra-lab.sh down   # bersihkan semua
```

Topologi:

| Subnet | CIDR | Bridge | Workload (netns) | IP workload | Internet |
|---|---|---|---|---|---|
| public | 10.0.1.0/24 | br-pub (10.0.1.1) | ws-pub | 10.0.1.10 | Ya, lewat IGW |
| private | 10.0.2.0/24 | br-prv (10.0.2.1) | ws-prv | 10.0.2.10 | Tidak |

- Internet Gateway: nftables masquerade untuk CIDR subnet public keluar lewat `ens33`.
- NACL: chain `out_<subnet>` dan `in_<subnet>` di tabel `ip netra`, stateless, rule eksplisit dua arah (ICMP, TCP 80, ephemeral 1024-65535, DNS), diakhiri `drop`.
- DNS: satu dnsmasq per bridge, upstream 1.1.1.1. `resolv.conf` workload di `/etc/netns/ws-<id>/`.
- `/healthz`: `python3 -m http.server 80` di dalam netns.
- Penamaan: netns `ws-<id>`, bridge `br-<id>`, veth `vh-<id>`/`vn-<id>`, tabel nftables `netra`. Nama interface maksimal 15 karakter.

## Fault injection (`faults/fault.sh`)

```bash
sudo faults/fault.sh all            # uji 6 skenario: break, check, fix
sudo faults/fault.sh <1-6> break    # rusakkan
sudo faults/fault.sh <1-6> check    # SEHAT atau RUSAK
sudo faults/fault.sh <1-6> fix      # kembalikan ke kondisi bersih
```

| No | Kategori | Break | Check |
|---|---|---|---|
| 1 | Routing | Hapus default route di ws-pub | ping 1.1.1.1 dari ws-pub |
| 2 | IP Forwarding | `ip_forward=0` | ping ws-pub ke ws-prv |
| 3 | Internet Gateway | Flush chain postrouting | ping 1.1.1.1 dari ws-pub |
| 4 | NACL | Hapus rule allow TCP 80 di in_pub | curl /healthz dari ws-prv |
| 5 | NACL | Insert rule drop dari 10.0.2.0/24 | ping dan curl dari ws-prv |
| 6 | DNS | Matikan dnsmasq br-pub | dig dari ws-pub harus berisi IP |

Saat pengujian 30 percobaan, perbaikan dilakukan NETRA (manual atau AI). `fix` hanya untuk mereset lab.

## Evaluasi lewat API (`faults/eval.py`)

Untuk 30 percobaan resmi terhadap backend NETRA (bukan lab bash). Resource uji dibuat Netra-backend (`KEEP=1 bash scripts/try_workload.sh`), nama netns dan chain dicari otomatis lewat API.

```bash
python3 faults/eval.py --selftest                                   # cek mandiri tanpa jaringan
sudo -v
python3 faults/eval.py --mode manual --scenarios 1-6 --repeat 5     # 30 percobaan resmi (drift)
python3 faults/eval.py --mode manual --scenarios 7-9 --repeat 5     # tambahan: salah konfigurasi
```

- Butuh env `T1`, `T2` (token client1, client2) dan backend jalan dengan `NETDRIVER_MODE=real`.
- Setiap percobaan: pastikan sehat, break, tunggu insiden, buat rencana, approve otomatis, ukur `approved_at` sampai `finished_at`, tunggu sehat lagi.
- Hasil per percobaan ke CSV (default `/tmp/netra-eval-<mode>-<waktu>.csv`) dan ringkasan (target 90% pulih < 120 detik).
- Evaluasi berhenti kalau keadaan tidak sesuai harapan, agar kerusakan tidak menumpuk.

## Jebakan yang sudah ditemukan

1. **Dua dnsmasq bentrok di 127.0.0.1:53.** Tanpa `--except-interface=lo`, dnsmasq kedua gagal start dan `set -e` menghentikan skrip sebelum nftables terpasang.
2. **`dig +short` mencetak error ke stdout.** Cek DNS harus mencocokkan alamat IP di output, bukan sekadar output tidak kosong.
3. **`kill` tidak langsung menghentikan proses.** Tunggu dengan `kill -0` sebelum mengecek.
4. Ping ke gateway dan query DNS tidak melewati NACL karena masuk ke hook `input`, bukan `forward`.

## Deploy production backend (`setup/netra-backend.service`)

Backend production di `/opt/netra/backend` (branch `main`), dijalankan systemd sebagai root, hanya listen `127.0.0.1:8000`. Nginx (`nginx/netra.conf`) meneruskan `/api/v1/`, `/api/docs`, dan `/api/openapi.json` ke sana. Path lain, termasuk `/api/auth/*` dan `/api/console/*` milik frontend, ke Next.js `127.0.0.1:3000`.

```bash
sudo mkdir -p /opt/netra && sudo chown "$USER":"$USER" /opt/netra
git clone -b main git@github.com:NETRA-UGM/Netra-backend.git /opt/netra/backend
cd /opt/netra/backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env    # isi: DATABASE_URL ke DB netra, NETDRIVER_MODE=real, JWT_SECRET baru, QWEN_API_KEY
.venv/bin/python scripts/seed_users.py    # akun client1 dan client2 production (jalankan sendirian)
sudo cp ~/netra-infra/setup/netra-backend.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now netra-backend
journalctl -u netra-backend -n 30 --no-pager
curl -s http://127.0.0.1/api/v1/health    # {"status":"ok","env":"production","netdriver":"real"}
```

- Update: `cd /opt/netra/backend && git pull --ff-only && .venv/bin/pip install -r requirements.txt && sudo systemctl restart netra-backend`.
- **Dev dan production tidak boleh jalan bersamaan** (tabel nftables `netra` dan port 8000 sama). Sebelum uji dev: `sudo systemctl stop netra-backend`. Sebelum production menyala lagi: bersihkan resource dev (`CLEAN=1 bash scripts/try_workload.sh` di repo dev).
- Stop service ikut menghentikan dnsmasq dan `/healthz` workload. Saat start, `reconcile` membangun ulang semua dari DB.
- Funnel hanya saat demo: `sudo tailscale funnel --bg 80`, matikan dengan `sudo tailscale funnel reset`.

## Status

Diuji di VM srv-cloud (Ubuntu 24.04, nftables 1.0.9, dnsmasq 2.91): lab 8/8 PASS, fault 6/6 skenario.
