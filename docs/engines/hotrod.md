# Strata-HotRod (low RAM / low VRAM, Linux)

[Strata-HotRod](https://github.com/sangharshadhyeta/Strata-HotRod) is a Strata fork for small machines. It spreads the
experts over three tiers, **card → RAM → NVMe**, and a learned look-ahead reads the next layers' experts from the SSD
before they're needed. Its headline: the 78 GB IQ3_S of Qwen3.8-Flash-Next on **one RTX 3060 12 GB with 30 GB of RAM**.
Pick it when you have ≤ 12–16 GB of VRAM or less than 64 GB of RAM.

> **Linux only, and untested with this kit.** HotRod is tested by its author on one machine: Rocky Linux 9, CUDA 13,
> gcc-toolset-13, Python ≥ 3.10, RTX 3060 12 GB, 30 GB RAM, a PCIe 3.0 NVMe. Its README lists Windows, macOS, other
> Linux distributions, other cards and other model sizes as **not tested**. This kit runs on Windows, so HotRod is
> used as a **remote backend**: it runs on a Linux machine and the kit's gateway forwards to it.

| | |
|---|---|
| GPU | NVIDIA, tested on an RTX 3060 12 GB (sm_86) only |
| OS | Rocky Linux 9 (tested by its author). Not Windows |
| RAM | 30 GB tested (setup "wants a 64 GB PC" for IQ3_S; naming the model is the consent) |
| Disk | NVMe. Setup refuses to start below **~137 GB free** for IQ3_S in low-RAM mode: the 84 GB download, a 50 GB copy of the experts, room for the MTP layer |
| Toolchain | Recent NVIDIA driver and the CUDA 13 toolkit (not installed for you), gcc-toolset-13, git, Python 3.10+ with venv (Rocky 9's `python3` is 3.9: `sudo dnf install -y python3.12 python3.12-pip`) |
| Speed (**their hardware**: RTX 3060 12 GB, 30 GB, Rocky 9) | IQ3_S at 131K: 50–70 tok/s on short answers (code 52–55, reasoning 66–71, a paragraph 50–53). Long prompts at 262K: 8.5K tokens read in 16.9 s then 52–54 tok/s; 45K tokens read in 67–68 s then 38–41 tok/s; the next turn of that conversation 0.4–0.5 s then 34–45 tok/s. 42–44 tok/s at 262K with the flip re-check off (default), 31–34 with it on. UD-Q4_K_XL (111 GB): 15–25 tok/s in short tests, learners still cold |
| Context | 131K tested widely; 262K fits (longest prompt served: 114K) |
| API | OpenAI-compatible, `serve/server.py --engine strata --config <json> --port <port> [--host 0.0.0.0] [--api-key KEY]`; `/v1/chat/completions`, `/v1/models`, `/health`, `/props`. **One request at a time**; others wait |

Other models live on other branches with their own READMEs: **DeepSeek-V4.1-Flash** on
[`upstream-dsv41`](https://github.com/sangharshadhyeta/Strata-HotRod/tree/upstream-dsv41) (~345 GB free NVMe, 7.5–8
tok/s on their 3060), **Edge0-145B** on [`hotrod-edge0`](https://github.com/sangharshadhyeta/Strata-HotRod/tree/hotrod-edge0)
(early support, 16–18 tok/s measured once). Everything below is for Qwen3.8-Flash-Next on `main`.

## 1. Install (on the Linux machine)

From HotRod's README:

```bash
sudo dnf install -y git gcc-toolset-13
git clone https://github.com/sangharshadhyeta/Strata-HotRod.git
cd Strata-HotRod
scl enable gcc-toolset-13 -- ./setup.sh --model IQ3_S --context 131072 --build --yes --no-start
```

- `--build` compiles the fork's engine; without it setup downloads upstream Strata's, which lacks HotRod's changes.
- `--models-dir /path/on/nvme` puts the model files elsewhere. For 262K, add `--max-context 262144` to the config's `args`.
- For Qwen3.8 IQ3_S setup also downloads the trained look-ahead (5.4 MB, SHA-256 checked) into `data/`.
- Update later with `scl enable gcc-toolset-13 -- ./update.sh --build`.
- The first hour of use is slower: the engine measures its VRAM reserve, RAM tier and expert placement on your machine.

## 2. Model

`setup.sh --model IQ3_S` downloads ISTA-DASLab's IQ3_S (78 GB of files, the size HotRod tested). Unsloth's
UD-Q4_K_XL (111 GB) is the other measured one (15–25 tok/s). Swift 1.5, IQ3_XXS, IQ2_XS, Q2_0 and Coder are **not
tested** by HotRod.

## 3. HotRod config

Setup writes `strata-iq3_s.json`. Add the keys in
[`engines/hotrod/config-additions.example.json`](../../engines/hotrod/config-additions.example.json) at its top level:
`"aliases": ["home"]` (so the gateway's `home` model name is accepted), `"reasoning_effort": "none"` (HotRod's thinking is
on by default; this makes requests without an effort answer at once) and optionally `"idle_unload_s"` (frees the
card's VRAM after N idle seconds).

## 4. Run it

[`engines/hotrod/run-hotrod.example.sh`](../../engines/hotrod/run-hotrod.example.sh) is HotRod's documented command
(inside a `systemd-run` scope with `MemoryMax` at 94% of RAM and no swap) with a key file and port as variables:

```bash
systemd-run --scope -p MemoryMax=$(awk '/MemTotal/ {printf "%dK", $2 * 0.94}' /proc/meminfo) -p MemorySwapMax=0 \
  .venv/bin/python serve/server.py --engine strata --config strata-iq3_s.json \
  --port 8095 --host 0.0.0.0 --api-key YOUR_KEY
```

It prints `ready: http://127.0.0.1:8095/v1` when the model is loaded. **Always set an API key with `--host 0.0.0.0`**
(`--api-key`, or the `STRATA_API_KEY` environment variable, which the example script uses so the key stays off the
process list). Make the key with `openssl rand -base64 32` and keep it out of git. Reach the box over your LAN or
tailnet only; don't expose the port to the internet.

## 5. Point the kit's gateway at it

On the Windows PC, `config.json`:

```json
"strata": { "engine": "hotrod" },
"gateway": { "enabled": true, "models": [
  { "id": "swift-hotrod-remote", "name": "Qwen3.8 Flash-Next (HotRod box)", "kind": "chat",
    "url": "http://<linux-box>:8095", "backendKeyFile": "backend-keys\\hotrod.txt",
    "healthCheck": true, "whenDown": "hold", "ctx": 131072 }
] }
```

- `url`: the Linux box's LAN address or tailnet name (e.g. `http://linux-box.<your-tailnet>.ts.net:8095`).
- `backendKeyFile`: a file in the kit folder (`backend-keys\` is git-ignored) whose first line is the key HotRod was
  started with. The gateway sends it as `Authorization: Bearer` on every call to the box and re-reads it each time.
- `healthCheck` true, **no `requireTier`**: the model counts as up whenever the box's `/health` answers.
- `ctx`: HotRod's `--max-context`.
- `backendModel`: only if you don't add the `home` alias to HotRod's config; then set it to the name HotRod reports
  (`qwen3.8-flash-next` by default, or the config's `model_name`).
- `strata.engine` = `"hotrod"` tells `install.ps1` not to register the local `HomeLLM Strata` task, and
  `scripts\strata-serve.ps1` refuses to start anything. **Remove the external (`strata`) tier from `llm.tiers`**: there
  is no local big model to load. The local tiers (Gemma / 4B / Whisper) keep working as before.
- Clients use the gateway as usual ([README > Model gateway](../../README.md#model-gateway-one-key-for-every-model-and-mode)).

## Gaming and unloading

The kit's supervisor only controls this PC's GPU. **It can't unload a remote HotRod box for a game**, and it doesn't
need to: the box's GPU isn't the one you game on. If you also game on the HotRod box, stop the server yourself (or use
`idle_unload_s` / `--idle-unload` so it frees VRAM when idle; its `--min-free-vram-mib` refuses a reload when too little
VRAM is free). While the box is down, the gateway holds or fails requests per `whenDown`.

## Windows host? (untested)

HotRod doesn't support Windows. Two routes, **neither tested by us or by HotRod**:

- **Dual boot** Linux on the same PC and follow the steps above. Simple, but the PC is either a Windows gaming PC or a
  HotRod server at a time.
- **WSL2** (Ubuntu or a RHEL-compatible distro) with the NVIDIA Windows driver's WSL CUDA support and the CUDA 13
  toolkit for WSL. Keep the model files on WSL's own ext4 disk (e.g. under `~/`), **not under `/mnt/c`**: HotRod streams
  experts from the SSD and the Windows-filesystem bridge is far slower. Check WSL's memory limit (`.wslconfig`), since
  HotRod sizes its RAM tier from free memory, and `systemd-run` needs systemd enabled in the distro. The gateway then
  points at `http://127.0.0.1:8095` (WSL2 forwards localhost by default). Caveat: the kit's supervisor sees WSL's GPU
  memory as another program's (like a game) and will step its own tiers down; with HotRod in WSL, keep the local tiers
  small or turn the supervisor's big tiers off.

## Caveats

- One request at a time; a second request waits for the first.
- The first message of a chat is the slow part (it waits on the SSD); follow-ups reuse what was read.
- Learned files in `data/` are per machine: when moving an install, delete all but `predictor-<id>.bin`.
- No `/v1/decision`: keep Jev decisions on a worker or the local tiers.
