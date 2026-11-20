# basalt_ada (RTX 40-series, Ada / sm_89)

[Basalt](https://github.com/jesdga95/basalt) is Blackwell-only upstream. [Ex3c4Def/basalt_ada](https://github.com/Ex3c4Def/basalt_ada)
(branch `fork_4090`) ports it to the RTX 4090 (sm_89) with fallback kernels for what Ada lacks (TMA, clusters, FP4 MMAs).
[armandodollia/basalt_ada](https://github.com/armandodollia/basalt_ada), branch
[`conv-cache-disk-direct`](https://github.com/armandodollia/basalt_ada/tree/conv-cache-disk-direct), is one commit
(`b20ab0b9`) on top of `fork_4090` that adds **direct-to-NVMe conversation parking**, which is what makes 256K context
work on 64 GB of RAM. It's the fastest option this kit has measured on a 4090. Expect to build it yourself, and re-check
after every upstream merge.

| | |
|---|---|
| GPU | RTX 40 (sm_89). Tested on an RTX 4090 24 GB only |
| OS | Windows (tested here). Upstream Basalt also builds on Linux; the Ada fork on Linux is untested by us |
| RAM | 64 GB tested (256K with direct parking; 128K without it) |
| Disk | ~85 GB for the IQ3_S pack, plus room for parked conversations (50 GB in our config), on an NVMe |
| Toolchain | Visual Studio 2022 C++ tools, a CUDA 13 toolkit (the no-admin redistributable tree works), CMake 3.24+ and Ninja, Python 3.10+ with Basalt's `requirements.txt` |
| Measured here (RTX 4090, 64 GB, Swift 1.5 IQ3_S) | **146 tok/s** decode near an empty context; 133 / 128 / 125 at 125K / 195K / 244K. ~6.6K tok/s prompt reading at 57–98K; 244K cold in 38 s. 256K context |
| API | OpenAI and Anthropic; `bin\basalt.exe serve --config <json>`, host/port from the config's `server` block |

## 1. Get the fork with the direct-parking patch

```powershell
git clone https://github.com/Ex3c4Def/basalt_ada; cd basalt_ada
git remote add fork https://github.com/armandodollia/basalt_ada; git fetch fork
git checkout -b home fork/conv-cache-disk-direct
git apply <kit>\basalt\patches\0001-any-model-name.patch   # optional: any model name reaches the one model
```

The patch was not sent upstream: Ex3c4Def/basalt_ada only accepts PRs from collaborators. Compare view:
`Ex3c4Def/basalt_ada/compare/fork_4090...armandodollia:basalt_ada:conv-cache-disk-direct`.

## 2. Build for sm_89

Copy `basalt\build_ada_local.bat` into the checkout, set its `CUDA`, `VENV` and `VCVARS` paths, and run it. It configures
with `-DCMAKE_CUDA_ARCHITECTURES=89 -DBASALT_MODELS=qwen4exp` into `build-ada` and ends with `BUILD OK` in
`build_local.log`; the executables land in `bin\`.

## 3. Model

Download the Swift 1.5 Flash-Next IQ3_S GGUF (both shards) and its mmproj from
[ukisai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-GGUF](https://huggingface.co/ukisai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-GGUF),
then convert (the fork's `docs/model-file.md`):

```powershell
python tools/convert/convert.py --gguf <IQ3_S-00001-of-0000N.gguf> --mmproj <mmproj.gguf> --out Swift-FN-IQ3_S.basalt
```

IQ3_S has about 40% less error than IQ3_XXS (KLD 0.145 vs 0.240, from the model card). Converted packs are slower than
official `.basalt` files where those exist. **4-bit (NVFP4) was rejected on 64 GB:** more accurate (KLD ~0.07) but 18% of
it has to stay on the SSD, which drops decode to 47 tok/s. Worth revisiting with 96–128 GB of RAM.

## 4. Basalt config

Start from [`basalt/basalt_home.example.json`](../../basalt/basalt_home.example.json): alias `home`, int8 KV,
`kv-resident` 32768, `reasoning-effort` none, 262144 context, resident experts, direct parking to a 50 GB NVMe cache.
Replace the `<you>` paths. `server.port` must be the tier's LLM port (8081 on an orchestrator).

## 5. Point the kit at it

```json
"strata": {
  "engine": "basalt",
  "basalt": {
    "root": "C:\\Users\\<you>\\llm\\basalt_ada",
    "config": "C:\\Users\\<you>\\llm\\basalt-models\\basalt_home.json",
    "exe": "",
    "args": [],
    "cudaPath": "C:\\Users\\<you>\\llm\\cuda-redist\\cuda-13.x",
    "venvScripts": "C:\\Users\\<you>\\llm\\basalt_venv\\Scripts",
    "anyModelName": true
  }
}
```

- Use the same tier as Strata (`llm._exampleStrataTier`, first in `llm.tiers`). Its `processMatch` already recognises
  Basalt's processes, so the supervisor counts its VRAM as its own instead of treating it as a game.
- Re-run `install.ps1` (registers the `HomeLLM Strata` task for your user).
- **Gateway:** the `swift-1.5-125b-strata` entry works as is (`http://127.0.0.1:8081`); raise `ctx` to 262144.
- Switching back to Strata is `strata.engine` = `"strata"`; both stay installed. Let the supervisor restart the tier
  (`.\status.ps1 -Mode none`, then `-Mode auto`).

The learned expert profile (`expert-profile-save`) improves which experts stay on the GPU over the first sessions.
Images: first one read in 1.1 s, then 0.11 s.

## 256K context on 64 GB

Settings: `"max-context": 262144`, `"kv": "int8"`, `"kv-resident": 32768`, and in `engine`:
`"conversation-cache-disk-direct": true`, `"conversation-cache-disk-mib": 51200`, `"conversation-cache-dir"` on an
NVMe. Leave `conversation-cache-min-free-mib` at its default **2560 (2.5 GB)**. `conversation-cache-disk-chunk-mib`
(default 8, 1..1024) sizes the direct write's staging buffer.

- **Why direct parking is needed.** Basalt parks idle conversations on disk so switching back doesn't re-read them.
  Unpatched, it first copies the whole conversation state into RAM (about 17.8 MB per 1K tokens: 4.3 GB at 244K),
  and only does so if that much RAM is free above the 2.5 GB floor. At 256K on 64 GB only 5–5.6 GB is free, so
  nothing above ~110K tokens could park ("skip parking (snapshot 4345 MiB exceeds available budget)") and every switch
  back to a long conversation cost a full re-read (38 s at 244K). `conversation-cache-disk-direct` streams the KV
  between the session and the files through one 8 MiB buffer, so the admission only needs ~118 MB plus the buffer. The
  files are byte for byte the same as the old path's. A write between requests yields to an arriving request. Off by
  default.
- **Measured** (RTX 4090, 64 GB, PCIe 4.0 NVMe): a 244K conversation parks in 2.1 s and comes back in 1.2–1.4 s (vs
  38 s); a 59K chat in 0.9 s (vs 8.9 s). Recall 5/5 (code words at 10/50/90/98% plus a two-fact question) after
  restores; lowest free RAM 6.9 GB in the test, 5.9 GB in daily use; decode unchanged (128 / 132 / 129 tok/s at 1.7K /
  125K / 244K). Follow-ups in the same conversation take 0.2 s.
- **Keep `conversation-cache-min-free-mib` ≥ 2560.** Lowering it would also let parking work without the patch, but
  it eats the margin that keeps Windows from running out of physical RAM while Basalt maps the model file.
- **Rejected: a smaller expert RAM budget** (`resident-budget-gib`, 39.5 → 36.7 → 34.9 GiB). It raises idle free RAM
  (5.6 → 7.3 → 9.3 GB) but long prompts then read experts through a memory-mapped view of the model file that grows
  back into the saved RAM, so free RAM under a long prompt went *down* (to 2.6–2.9 GB, where Basalt's guard fires),
  parking still failed, and decode at 244K fell from 125 to 118 tok/s.
- **Without the patch**, stay at 128K on 64 GB, or add RAM (96 GB+ removes the limit).

## Gaming

Same as Strata: a game or stream start unloads it (tested: Basalt gone within 12 s, the 4090 back at 1.1 GB used), and
it reloads after the game plus `upgradeDelaySec` ([README > Gaming](../../README.md#gaming)).

## Caveats

- A fork of a fork: every upstream merge can break the Ada fallbacks. Rebuild and re-test after `git pull`, and
  re-apply the any-model-name patch.
- Basalt's Windows notes apply: a display-attached GPU can hit the driver's TDR timeout (raise `TdrDelay`), and large
  pages need "Lock pages in memory".
- No `/v1/decision`: keep Jev decisions on a worker.
- Free RAM sits at 5.9–6.9 GB at 256K on 64 GB. Keep other big RAM users off this PC while it's loaded.
