# Strata (default engine)

[Strata](https://github.com/Niko1221/Strata) is the kit's default big-model engine and the one to pick when nothing in
the [engine chooser](../../README.md#choose-an-engine) says otherwise. It keeps the experts the GPU can't hold in system
RAM and, when RAM runs short, reads the rest from the SSD, which its docs call much slower.

| | |
|---|---|
| GPUs (Strata's README) | NVIDIA RTX 20 / 30 / 40 / 50; AMD Radeon RX 7000 / 9000 and AI PRO R9700; RX 6800/6900 (experimental); Intel Arc (Linux); Apple Silicon; Strix Halo; DGX Spark |
| OS | Windows 10/11 and Linux x86-64 (desktop app or Terminal), macOS 15+ on Apple Silicon. **This kit's scripts are Windows-only.** |
| RAM + VRAM for the weights (Strata's `docs/MODELS.md`) | Q2_0 37.6 GB, IQ2_XS 39.2 GB, IQ3_XXS 47.0 GB, IQ3_S 54.8 GB, plus room for the OS and the context. Swift 1.5 "matches the same size of the original" |
| Measured here | RTX 4090 + 64 GB, Swift 1.5 IQ3_XXS at 128K context: 105–140 tok/s decode, ~4.5K tok/s prompt reading at 57–98K, load 23 s, 23.7 GB VRAM |
| API | OpenAI- and Anthropic-compatible; `strata serve --profile <json> --host <ip> --port <port>` |

## 1. Install

Install the Strata **Terminal** as yourself (it lives in your profile), from Strata's README:

```powershell
irm -TimeoutSec 120 https://github.com/Niko1221/Strata/releases/latest/download/install.ps1 | iex
```

Open a new terminal afterwards. Strata's own guides: [install](https://github.com/Niko1221/Strata/blob/main/docs/INSTALL.md),
[models](https://github.com/Niko1221/Strata/blob/main/docs/MODELS.md),
[terminal](https://github.com/Niko1221/Strata/blob/main/docs/TERMINAL_LAUNCH.md).

## 2. Model

Let Strata download the model and write its run profile, without starting it (the kit's supervisor starts it later).
The command we use for Swift 1.5 (a shorter-thinking fine-tune of Qwen3.8-Flash-Next) on a 24 GB card with 64 GB RAM:

```powershell
strata install --family swift --model IQ3_XXS --context 131072 --vision gpu --port 8081 --no-browser --yes --setup --no-start
strata update    # downloads the engine and test-starts it once; free the GPU first
```

Pick a smaller size on less RAM (`--model IQ2_XS` or `Q2_0`, see the table above), or `--family qwen` for the original
model. `strata launch --plan --family qwen --model iq2_xs` prints what a choice would need without downloading.

Then edit the generated `strata-<model>.json` (in `%LOCALAPPDATA%\Strata\install\`) and add:

```json
"reasoning_effort": "none",
"aliases": ["home"]
```

so callers that don't ask for thinking (Home Assistant, mail) answer at once, and the gateway's `home` model name reaches
the model. Our measured engine arguments on the RTX 4090: `--max-context 131072 --kv int8 --kv-resident 32768
--vram-reserve-mib 700 --spec 4 --spec-min-p 0.5 --prompt-cache-spread`, GPU image reader.

## 3. Point the kit at it

`config.json`:

```json
"strata": {
  "engine": "strata",
  "runConfig": "%LOCALAPPDATA%\\Strata\\install\\strata-swift-iq3_xxs.json",
  "strataCmd": ""
}
```

- Put a tier like `llm._exampleStrataTier` **first** in `llm.tiers`. Set `external.pathPrefix` to your Strata folder
  (`C:\Users\<you>\AppData\Local\Strata\`); it's matched against process paths so the supervisor counts Strata's VRAM as
  its own. `upgradeDelaySec` 300 loads it only after 5 idle minutes, because loading pins RAM and can stall the PC.
- The engine listens on `cluster.localPorts.llm` (8081) on an orchestrator, else on `llm.port`.
- Re-run `install.ps1`. It registers the `HomeLLM Strata` task for your user; the supervisor starts and stops it.

**Gateway** (`gateway.models`): the example `swift-1.5-125b-strata` entry already fits: `url`
`http://127.0.0.1:8081`, `requireTier` `strata`, `healthCheck` true, `whenDown` `hold`, `ctx` 131072 (match your
`--context`).

## Gaming

The supervisor unloads Strata within seconds when a game or stream starts (tested: 23.7 → 0.95 GB in 6 s) and brings it
back after the game plus `upgradeDelaySec`. Requests in between are held or sent to the fallback by the gateway
([README > Gaming](../../README.md#gaming)).

## Caveats

- Without a logged-on session the task can't start, and the tier is skipped for `external.blockMinutes`.
- Strata's cache fills whatever VRAM is free, so the tier plans with `external.estMiB`, not the measured value.
- No `/v1/decision`: keep Jev decisions on a worker ([README > Jev decisions on a worker](../../README.md#jev-decisions-on-a-worker)).
- Only the Windows Terminal install on an RTX 4090 is tested with this kit.
