# lenv vs Docker — Benchmark

An honest measurement of where lenv beats Docker Desktop on Windows, where it loses, and
where the two are indistinguishable.

**Short version:** lenv loses on command latency and loses badly on true throwaway
environments. It wins decisively on idle footprint and disk cost. Speed is not the reason
to use lenv.

---

## Test setup

| | |
|---|---|
| **Machine** | Windows 11, Intel Raptor Lake (Family 6 Model 186), 16 GB RAM |
| **WSL** | WSL2 (Store version) |
| **Docker** | Docker Desktop 29.6.2, per-user install |
| **Python** | 3.13.4 |
| **lenv** | 1.2.0 |
| **Date** | August 2026 |

**Method.** Latency is measured interleaved — each iteration times lenv, then Docker — in a
single session, so clock drift and background load hit both sides equally. Exit codes are
asserted `== 0` before any timing is recorded, since a command that fails is not a command
that is fast. Warm-up runs are discarded. Medians and p95 (nearest-rank) are reported
alongside means. Footprint uses three samples per state after a 60-second settle.

**Single-machine caveat.** Everything here is one Windows laptop. Docker Desktop on Windows
routes every CLI call through a named pipe into a WSL2 VM; on native Linux the latency
picture is different and this benchmark says nothing about it. Numbers will vary by machine.
Reproduction steps are at the end — please run them and open an issue if your results
disagree.

---

## Results at a glance

| Test | lenv | Docker | Winner |
|---|---|---|---|
| Warm command (`run` / `exec`) | 167 ms | **121 ms** | **Docker**, by ~46 ms |
| Throwaway env (create → run → destroy) | ~5 s | **624 ms** | **Docker**, by ~8x |
| Kept env vs create-per-command | **167 ms** | 624 ms | lenv — but see note |
| Cold restart of a stopped environment | 362 ms | 364 ms | Tie |
| Idle RAM (marginal, WSL2 already up) | **~0.1 GB** | ~1.6 GB | **lenv** |
| Install + data on disk | **~100 MB/env** | ~5.4 GB | **lenv** |

The third row is a workflow observation, not a speed win: it compares a kept lenv
environment against Docker's create-per-command pattern. Docker's own answer to that
workflow is `docker exec`, which is row one, which lenv loses.

---

## 1. Command latency — Docker wins

```
lenv run (warm)                  n=15  mean=169.6  median=167.1  p95=188.3  min=159.5  max=188.3
docker exec (warm container)     n=15  mean=121.5  median=121.1  p95=150.5  min=103.6  max=150.5
python -c pass (floor)           n=10  median= 29.9
python -c 'import lenv.cli'      n=10  median= 70.0
```

`docker exec` into an already-running container beats `lenv run` by roughly 46 ms. The
distributions barely touch, so this is not a measurement artifact.

Where lenv's 167 ms goes:

| Component | Cost |
|---|---|
| CPython interpreter startup | ~30 ms |
| lenv module imports | ~40 ms |
| `wsl.exe` spawn + shell + run logic | ~97 ms |

Profiling with `-X importtime` during this work found `urllib.request`, `tarfile`, and
`tempfile` costing ~75 ms of import time on a code path that never downloads anything.
Lazy-importing them inside the functions that need them cut warm `lenv run` from 214 ms to
167 ms. The remaining gap to Docker is `wsl.exe` process spawn, not Python, and more import
work will not close it — Docker's CLI is a compiled Go binary and lenv's is Python. Treat
this gap as structural.

### The throwaway case is worse

A genuinely like-for-like throwaway comparison — `lenv init` + run + `lenv destroy` against
a single `docker run --rm alpine true` — is roughly **5 seconds vs 624 ms. lenv loses by
about 8x.** `lenv init` alone is ~2.8 s.

If your workflow is "run one command in a disposable Alpine and forget it," use Docker.
lenv is built for environments you keep.

---

## 2. Cold restart — tie

Reviving a *stopped* environment and running one command in it:

```
lenv   (wsl --terminate -> first run)   n=10  mean=363.4  median=362.2  p95=394.7  min=326.8  max=394.7
docker (stop -> start + exec)           n=10  mean=377.7  median=363.7  p95=521.7  min=333.3  max=521.7
```

**Tie on median (362 vs 364 ms).** lenv's tail is tighter — p95 of 395 ms vs 522 ms — so
restart time is more *consistent*, not faster. At n=10 the median difference is well inside
noise; do not read it as a win for either side.

---

## 3. Idle footprint — lenv wins

Three samples per state, 60-second settle. Per-sample variance was negligible (State B:
780 / 780 / 781 MB).

| State | Process working set | Committed (system-wide) |
|---|---|---|
| A. Nothing (Docker Desktop closed, `wsl --shutdown`) | 19 MB | 26.67 GB |
| B. lenv environment resident | 780 MB | 27.60 GB |
| C. Docker Desktop idle, no containers | 2,377 MB | 29.96 GB |
| D. C + one idle alpine container | 2,370 MB | 30.42 GB |
| E. D + lenv environment resident | 2,501 MB | 30.16 GB |

Read the working-set column as the reliable signal. System-wide committed bytes drift with
unrelated processes at this granularity — State D reports higher committed bytes than
State E while having the lower working set — so treat that column as approximate.

**Two findings:**

1. **Docker Desktop idle costs ~2.4 GB of working set. lenv idle costs ~0.8 GB — and that
   0.8 GB is the shared WSL2 utility VM, which Docker Desktop also requires.**

2. **The costs overlap rather than add.** State E is the one that matters: adding a resident
   lenv environment to a machine already running Docker Desktop raised the working set by
   only ~131 MB (2,370 → 2,501 MB), not by State B's full 780 MB. Both ride the same WSL2
   VM. So on a machine that already runs WSL2, **Docker's marginal cost is ~1.6 GB and
   lenv's is ~0.1 GB.**

An idle container adds essentially nothing on top of the Docker daemon (State C → D). The
cost is the daemon, not the containers.

> A note on metrics: "free RAM" is a misleading way to report this. The test machine had
> ~13 GB consumed by unrelated processes before either tool started, which makes any
> free-RAM delta look more dramatic than the tool's actual cost. Working set and committed
> bytes are reported instead.

### Disk

| | Size on disk |
|---|---|
| lenv environment, fresh Alpine VHDX | 76–104 MB |
| `~/.lenv` total, 8 environments incl. rootfs cache | ~1.2 GB |
| Docker Desktop install + data dirs (one 13 MB alpine image pulled) | ~5.4 GB |

---

## 4. VHDX growth, and `lenv compact`

**The problem:** a WSL2 VHDX grows as you use it and never shrinks by itself. One heavily
used test environment reached 16.9 GB of virtual size. Docker has `docker system prune`;
lenv needed an equivalent before the disk numbers above could be claimed honestly.

What was tried, in order:

- **Zero-filling** (`dd if=/dev/zero`) — no effect. WSL2 VHDX files on this machine are
  NTFS-sparse, so writing zeros never allocates. Writing real data (`dd if=/dev/urandom`,
  400 MB) grew the file 105 → 507 MB as expected.
- **`rm` + `sync` + `fstrim` inside the environment** — no reclaim. Still 507 MB.
- **`wsl --manage <inst> --set-sparse true`** — refused by WSL: *"Sparse VHD support is
  currently disabled due to potential data corruption… `--allow-unsafe`."* lenv does not
  pass `--allow-unsafe`.
- **`diskpart compact vdisk`** (elevated) — reclaimed **0 MB**. Blocks freed inside ext4
  still hold stale data, and Windows cannot read ext4's allocation bitmap. No host-side tool
  can fix this.

**What works: `wsl --export` → unregister → import.** The export archive contains only live
files, so the rebuilt disk starts at minimum size. This is what `lenv compact` does. It
prompts for confirmation (`--yes` to skip), verifies the export archive is non-trivial
before touching the old disk, and preserves the archive with restore instructions if the
import step fails.

Measured on the bloated test environment:

```
virtual size: 484.00 MB
on disk:      484.00 MB  ->  76.00 MB
verified:     environment still boots, Alpine 3.19.0
```

**Note:** because compact works by export/unregister/import, it re-registers the WSL
instance. Verify your default user and any per-instance settings after running it.

---

## 5. What lenv does and doesn't do

**Not claimed:**

- **"Faster than Docker."** Warm `docker exec` beats `lenv run` by ~46 ms, and true
  throwaway environments favor Docker by ~8x. See §1.
- **Security sandboxing.** WSL2 distros share the host VM kernel. lenv isolates your
  filesystem and host processes — it is not a security boundary for untrusted code.
- **Container-engine features.** No image layers, no registry, no `docker build`, no
  cgroup/network isolation, no orchestration. lenv is lighter because it does less.

**What lenv is:**

1. **No daemon.** Nothing runs when nothing is running. No Docker Desktop to install,
   start, license, or babysit.
2. **~0.1 GB marginal RAM** on a machine that already runs WSL2, against Docker Desktop's
   ~1.6 GB.
3. **~100 MB per environment on disk**, removed with `lenv destroy`, shrinkable with
   `lenv compact` — against ~5.4 GB of Docker Desktop install and data.
4. **venv semantics.** The environment belongs to the project directory. One command in,
   one command out, config in `.lenv/`. No Dockerfile, no registry, no image names.

### Which should you use?

| If you… | Use |
|---|---|
| Run one-off disposable commands in Linux | **Docker** |
| Need image layers, registries, reproducible builds, or CI parity | **Docker** |
| Need cgroup/network isolation or a security boundary | **Docker** (or a microVM) |
| Keep a long-lived Linux environment per project | **lenv** |
| Are RAM- or disk-constrained on a Windows laptop | **lenv** |
| Want a Linux shell tied to a directory, with nothing resident | **lenv** |

---

## 6. Not measured

Recorded as open rather than answered:

- **I/O throughput** — ext4-in-VHDX vs overlayfs, with `conv=fdatasync`, n=10.
- **Multi-environment scaling** — N resident lenv environments vs N idle containers.
  Docker's idle container cost measured at ≈ 0 (State C → D), so Docker may well win here.

---

## 7. Verifying commands run where they claim to

`lenv run` has no fallback path: with no `.lenv/config.json` it prints
`No LENV environment found` and exits 1 without touching WSL. A silently-succeeding host
shell is impossible by construction, but the check is worth including anyway, since it is
the easiest way for a benchmark like this to be quietly wrong:

```powershell
PS> python -m lenv run "cat /etc/os-release"     # first line:
NAME="Alpine Linux"

PS> wsl -d <instance> -- ash -c "cat /etc/alpine-release; readlink /proc/1/exe"
3.19.0
/init
```

`/etc/alpine-release` exists only on Alpine. Note that WSL2 instances inherit the Windows
host's hostname by design, so `hostname` output is not evidence either way.

---

## 8. Bugs this benchmark uncovered

Both found while measuring, both fixed:

- **`wsl --import` writes errors as UTF-16.** lenv decoded them as locale-encoded text, so
  the graceful "instance already exists" path never matched and surfaced as an empty error.
- **`wsl --manage` reports failures on stdout, not stderr.** Found when the sparse-VHD
  refusal in §4 appeared as "unknown error."

---

## 9. Reproducing this

```powershell
# Setup
mkdir bench-proj; cd bench-proj
lenv init --distro alpine
docker pull alpine
docker run -d --name lenvbench alpine sleep 3600

# Latency: interleave the two, n>=15, assert exit code 0 before recording
#   lenv run true   vs   docker exec lenvbench true

# Footprint: per state, 3 samples after a 60s settle
Get-Process vmmem*, wslservice, 'Docker Desktop', 'com.docker.*' -ErrorAction SilentlyContinue |
  Measure-Object WorkingSet64 -Sum
(Get-Counter '\Memory\Committed Bytes').CounterSamples.CookedValue

# Cold restart, n=10
#   wsl --terminate <instance>  -> first lenv run
#   docker stop lenvbench       -> docker start + docker exec

# Import profile
python -X importtime -m lenv run "true"

# Cleanup
lenv destroy --yes
docker rm -f lenvbench
```

All benchmark state was removed afterward. Docker Desktop was left running, as found.

If your numbers differ, please open an issue with your machine specs and raw output.