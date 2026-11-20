# Basalt (RTX 50-series, Blackwell / sm_120)

[Basalt](https://github.com/jesdga95/basalt) is a Strata fork for **NVIDIA Blackwell (compute capability 12.0) only**:
RTX 50-series and RTX PRO Blackwell cards. It keeps what doesn't fit in VRAM (the experts) in system RAM, can use more
cards, and serves the OpenAI and Anthropic APIs. On a 50-series card it's the engine to pick. For an RTX 40 card use the
[Ada fork](basalt-ada.md); for anything else, [Strata](strata.md).

> **Untested with this kit.** We have no Blackwell card. The kit's side (`strata.engine` = `basalt`) is the same
> launcher the Ada fork uses, so it should work, but nobody has run this page end to end. Please report back.

| | |
|---|---|
| GPU | NVIDIA Blackwell, sm_120, with CUDA 13 |
| OS (Basalt's README) | Linux x86-64 with AVX2, or Windows 10 1903+ x64. Basalt tests and benchmarks on Linux; "quality is not measured on Windows". **This kit's scripts are Windows-only** |
| RAM | Enough for the experts that don't fit in VRAM. Basalt's community bench refuses a row if any expert is read from the SSD |
| Toolchain (build from source) | CMake 3.24+, Ninja and an x64 VS 2022 prompt on Windows (GCC 13+ on Linux), CUDA 13, Python 3.10+ with `requirements.txt`. Or skip the build: release archives |
| Speed (**their hardware**, single card, from Basalt's community runs) | IQ3_S: RTX 5060 Ti 16 GB 137–159 tok/s, RTX 5080 16 GB 138–187, RTX 5090 32 GB ~375. NVFP4 on an RTX 5090: 305–386 ([basalt.jgulfo.com/community](https://basalt.jgulfo.com/community/)) |
| Context | Flash-Next up to 262,144 (longer with YaRN) |
| API | `bin\basalt serve --config <json>`; listens on 127.0.0.1 by default, host/port from the config's `server` block |

## 1. Install

**Release archive (simplest).** Download `basalt-vX.Y.Z-windows-x64.zip` from
[releases](https://github.com/jesdga95/basalt/releases) and unzip it. It needs a current NVIDIA driver (R580 branch or
newer), the CUDA 13 runtime libraries (cudart, cuBLAS) on `PATH`, and a Python venv at the archive's top folder:

```powershell
cd basalt-vX.Y.Z-windows-x64
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

`bin\basalt serve` uses that `.venv` when it's there.

**Or build from source** (Basalt's `docs/platforms.md`), from an x64 VS 2022 developer prompt:

```bat
git clone https://github.com/jesdga95/basalt && cd basalt
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DBASALT_MODELS=qwen4exp
cmake --build build -j
pip install -r requirements.txt
```

The executables land in `bin\` either way. `-DBASALT_MODELS=qwen4exp` builds Flash-Next only (leave it out for every model).

## 2. Model

Official packs are on [jesdga/Qwen3.8-Flash-Next-Basalt](https://huggingface.co/jesdga/Qwen3.8-Flash-Next-Basalt): the
default `Qwen3.8-FN-NVFP4-DQ6_K`, `NVFP4-DQ8`, and Unsloth's types (`UD-IQ1_S` … `Q8_0`).

```powershell
hf download jesdga/Qwen3.8-Flash-Next-Basalt Qwen3.8-FN-NVFP4-DQ6_K.basalt --local-dir models
```

For Swift 1.5 or the GSQ-RCO IQ3_S / IQ3_XXS files there's no official pack: convert the GGUF with
`tools/convert/convert.py` as in [basalt-ada.md § Model](basalt-ada.md#3-model) (Basalt's `docs/model-file.md`).
Choose the size by RAM + VRAM (Strata's table in [strata.md](strata.md) is a fair guide; NVFP4 is larger than IQ3_S).

## 3. Basalt config

Start from [`basalt/basalt_blackwell.example.json`](../../basalt/basalt_blackwell.example.json) and replace the `<you>`
paths and the pack name. It sets alias `home`, int8 KV, 262144 context with 32768 cells resident, `reasoning-effort`
none, resident experts, a learned expert profile and a 50 GB conversation cache on disk. `server.port` must be the
tier's LLM port (8081 on an orchestrator). Every key: Basalt's
[configuration](https://github.com/jesdga95/basalt/blob/main/docs/configuration/index.md).

- Upstream Basalt doesn't have the Ada fork's `conversation-cache-disk-direct`. Parking a conversation needs its whole
  state free in RAM first, so on 64 GB long conversations may not park (see [basalt-ada.md § 256K](basalt-ada.md#256k-context-on-64-gb)).
  With 96 GB+ this doesn't matter.
- Don't set `host` to `0.0.0.0` without an API key (`--api-key-file <path>`, command line only: put it in
  `strata.basalt.args`). The kit's router and gateway reach it on 127.0.0.1, so you don't need to.

## 4. Point the kit at it

```json
"strata": {
  "engine": "basalt",
  "basalt": {
    "root": "C:\\Users\\<you>\\llm\\basalt-vX.Y.Z-windows-x64",
    "config": "C:\\Users\\<you>\\llm\\basalt-models\\basalt_blackwell.json",
    "exe": "",
    "args": [],
    "cudaPath": "",
    "venvScripts": "",
    "anyModelName": false
  }
}
```

- `root` is the unzipped archive or your checkout (`bin\basalt.exe` under it; `exe` overrides that). `cudaPath` /
  `venvScripts` only when CUDA 13 isn't on `PATH` or you use a venv outside the archive. `anyModelName` does nothing
  without the Ada fork's patch; the gateway sends the `home` alias anyway.
- Use the same tier as Strata (`llm._exampleStrataTier`, first in `llm.tiers`) and re-run `install.ps1`. The tier's
  `processMatch` recognises `\basalt...\bin\basalt` paths and `basalt_home.json` / `basalt_blackwell.json`; if your
  folder or config has another name, add it, or the supervisor will count Basalt's VRAM as a game's and unload it.
- Size the tier for your card: `external.estMiB` (21000 is a 24 GB card's) and `maxOthersMiB`.
- **Gateway:** the `swift-1.5-125b-strata` entry works as is (`http://127.0.0.1:8081`); set `ctx` to your `max-context`.

## Gaming

The kit's supervisor unloads it when a game or stream starts and reloads it afterwards, as for Strata
([README > Gaming](../../README.md#gaming)). Basalt also has `idle-unload` (frees VRAM after N idle seconds, ends the
process); the kit doesn't need it.

## Caveats

- Blackwell only upstream (sm_120 kernels). RTX 40 → [basalt_ada](basalt-ada.md).
- Windows limits (Basalt's `docs/platforms.md`): a display-attached GPU can hit the driver's TDR timeout (raise
  `TdrDelay`), large pages need "Lock pages in memory", `idle-sleep` is Linux-only, and greedy output can differ run to
  run on a card that drives the desktop.
- No `/v1/decision`: keep Jev decisions on a worker.
