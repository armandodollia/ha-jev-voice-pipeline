# Home LLM GPU kit

Run a local LLM and voice stack for Home Assistant on a Windows gaming PC with an NVIDIA GPU, and use it from
Home Assistant's Assist and from a Pebble watch, at home or away (over Tailscale). The model steps down
automatically as games, streams or other GPU jobs need video memory, so the GPU stays usable. Optionally a second
GPU PC takes over the LLM and Whisper while you game (see [Multiple PCs](#multiple-pcs-worker-takes-over-while-you-game)).

| Piece | What it does | Port |
|---|---|---|
| **llama-server** ([thecodacus/llama.cpp `parallel-decision`](https://github.com/thecodacus/llama.cpp/tree/parallel-decision)) | OpenAI-compatible chat API with tool calling for Assist, plus Jev-mode `POST /v1/decision` (answers enum/boolean questions in one forward pass, with probabilities) | 8080 |
| **VRAM tier supervisor** | Keeps one model loaded under a fixed name (`home`) and steps it down, or unloads it and Whisper, as other programs need GPU memory | – |
| **Whisper** (Wyoming, GPU) | Speech-to-text for Assist, biased toward your Home Assistant device names | 10300 |
| **Piper** (Wyoming, CPU) | Text-to-speech for Assist | 10200 |
| **Speech API** | OpenAI-style `/v1/audio/transcriptions` + `/v1/audio/speech` in front of Whisper/Piper, for the watch | 10310 |
| **Router** (multi-PC only) | On the main PC, keeps the public LLM and Whisper ports and forwards each connection to the main PC or a worker PC; wakes workers with Wake-on-LAN | 8080, 10300 (public), 8079 (workers) |
| **Assist relay** | Runs Home Assistant `/api/conversation/process` calls and OpenAI `/v1/chat/completions` calls through your **Assist pipeline**, for apps that can't choose an agent (Wristotle's HA commands and Ask Agent) | 10320 (localhost) |

Default VRAM tiers (edit `llm.tiers` in `config.json`). "Other programs" means everything on the GPU except
the kit's own llama-server and Whisper: games, the stream encoder, the desktop, training jobs.

| Tier | Other programs use | Runs | VRAM (measured, RTX 4090) |
|---|---|---|---|
| `full` | under 10 GB | Gemma 4 12B UD-Q4_K_XL + Whisper | ~10 GB + ~1.4 GB |
| `small` | 10–14.5 GB | Qwen3.5 4B UD-Q4_K_XL + Whisper | ~5 GB + ~1.4 GB |
| `voice` | 14.5–19 GB | Whisper only (no LLM) | ~1.4 GB |
| `none` | 19 GB or more | nothing | 0 |

The supervisor reads each process's GPU memory from Windows' performance counters every 5 s. It steps down at
once, and steps up only after other programs have stayed 1.5 GB under the tier's limit for 90 s and the bigger
tier fits. A swap interrupts the LLM for ~2–6 s. Whisper runs as `int8_float16` (`voice.computeType`).

**No model loads while a game or stream runs.** During a game the supervisor only unloads. The smaller tier's
model, upgrades and crash restarts of llama-server or Whisper all wait until the game or stream ends. On an
RTX 4090, loading a model mid-game caused GPU driver resets (TDR) 9–16 s after the load started. Outside games, a
downgrade unloads at once and loads the smaller model 60 s later (`llm.downgradeLoadDelaySec`). Turn this off with
`llm.noLoadsDuringActivity: false`.

**Picking the limits.** A tier's limit = card VRAM − what that tier uses − a safety margin. The margin is free memory
a game can grow into in the seconds before the supervisor notices and unloads. Running out doesn't crash the game:
Windows pages the overflow to system RAM, which shows up as stutter and low-detail textures. The defaults leave
about 3 GB at each limit on a 24 GB card (full: 24 − 11.4 − 10 ≈ 2.6 GB; small: 24 − 6.4 − 14.5 ≈ 3 GB;
voice: 24 − 1.4 − 19 ≈ 3.6 GB). Scale them down for smaller cards.

**Game start.** Many games size their texture pool from the VRAM free *at launch* and don't grow it later. So when
a game or stream starts, the supervisor drops at once to `llm.activityMaxTier` (default `small`). With
`noLoadsDuringActivity` on, that means Gemma unloads and nothing loads until the game ends, so the game launches
with the memory free. While it runs, VRAM is checked every `llm.activityPollSec` (2 s) instead of every 5 s.

**Bigger setups.** The same supervisor scales up to the setup we run now (November 2026):

| Machine | Runs | Used by |
|---|---|---|
| Main PC: RTX 4090 24 GB, 64 GB RAM | Swift 1.5 Flash-Next 125B MoE at 256K context while idle ([Big MoE tier](#big-moe-tier-strata-default-or-basalt-opt-in)); the 4B fallback and local Whisper while gaming | coding agents (OpenCode / T3 Code), Hermes, Assist questions |
| Worker PC: RTX 3080 10 GB | Gemma 4 12B with Jev decisions, Whisper and Piper ([Multiple PCs](#multiple-pcs-worker-takes-over-while-you-game)) | Assist's fast path, voice, small chats while the 4090 games |
| Model gateway on the main PC | one API key per machine, holds requests while the 4090 games ([Model gateway](#model-gateway-one-key-for-every-model-and-mode)) | laptops, Hermes, OpenCode ([Client templates](#client-templates-opencode-hermes-t3-code)) |

Inspired by Codacus' video [*Can You Run Any LLM in Jev Mode Using llama.cpp?*](https://www.youtube.com/watch?v=bcGO7xre46o).

---

## What you need

**On the GPU PC (Windows 10/11):**

| | |
|---|---|
| NVIDIA GPU + driver | default build targets RTX 40 (`cudaArch` `89`); use `86` for RTX 30, `120` for RTX 50 |
| [Visual Studio 2022](https://visualstudio.microsoft.com/downloads/) | workload *Desktop development with C++* (to build llama-server) |
| [CUDA Toolkit 12.x](https://developer.nvidia.com/cuda-downloads) | |
| [Python 3.12](https://www.python.org/downloads/) (3.10–3.13 work) | for the voice services; found via `py`, pyenv-win or a standard install |
| [Tailscale](https://tailscale.com/download/windows) | optional: remote access and HTTPS for the watch (`winget install Tailscale.Tailscale`) |
| [Apollo](https://github.com/ClassicOldSong/Apollo) or [Sunshine](https://github.com/LizardByte/Sunshine) | optional: stream detection |
| ~25 GB free disk | models, build, Python environments |

**Home Assistant:** 2025.x or newer with [HACS](https://hacs.xyz/).

**For the Pebble watch (Android phone):**

| App | Where | Why |
|---|---|---|
| **Pebble app** (Core Devices) or **microPebble** | Google Play / their site | pairs the watch with the phone |
| **Wristotle Companion** | [F-Droid](https://f-droid.org/en/packages/com.lazydevs.wristotle/) or [Codeberg releases](https://codeberg.org/wristotle/wristotle-companion/releases) | turns the watch's dictation button into voice commands |
| **Wristotle watch app** | [rePebble store](https://apps.repebble.com/wristotle_6a0e71faced0bb000943bc90) or [Rebble store](https://apps.rebble.io/en_US/application/6a0e71faced0bb000943bc90) | the watch side of Wristotle |
| **Tailscale** | Google Play | reach the PC from anywhere |

Spoken replies on the watch need a watch with a speaker (Pebble Time 2, Pebble Round 2).

---

## 1. Install on the PC

```powershell
# in an elevated PowerShell, inside this folder
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1 -CheckOnly      # creates config.json, checks prerequisites, changes nothing
notepad config.json           # set homeAssistant.url; review the tailscale section
.\install.ps1                 # build (15-30 min), models, voice, startup tasks, firewall, Tailscale
```

Safe to re-run. Switches: `-SkipVoice`, `-SkipBuild`, `-Rebuild`, `-SkipModels`, `-SkipApollo`, `-SkipTailscale`,
`-SkipTasks`, `-NoTokenPrompt`.

The installer asks (optionally) for a Home Assistant **long-lived access token**; create it as in step 2.1 below.
It's saved to `hass_token.txt`, readable only by you, Administrators and SYSTEM, and lets Whisper learn your
entity/area names, which fixes most misheard device names.

Day to day:

```powershell
.\status.ps1              # tier, VRAM, service health, tailnet URLs, recent switches
.\status.ps1 -Mode full   # pin a tier (full|small|voice|none); -Mode auto returns to automatic tiers
.\uninstall.ps1           # remove tasks, firewall rules, hooks, tailnet publishing (keeps files)
```

Give the PC a **DHCP reservation** in your router so its LAN address doesn't change.

### Main settings (`config.json`)

| Setting | Default | Notes |
|---|---|---|
| `homeAssistant.url` | `http://homeassistant.local:8123` | LAN address of HA (used by Whisper name hints and the Assist relay) |
| `llm.tiers[]` | full / small / voice / none | best first: `name`, `maxOthersMiB` (tier allowed while other programs use less), `whisper`, optional `model` |
| `llm.tiers[].model.repo/file` | Gemma 4 12B / Qwen3.5 4B | any GGUF on Hugging Face; `ctx`, `decisionSeqs`, `chatTemplateKwargs`, `extraArgs` per model |
| `llm.tiers[].model.chatTemplateKwargs` | `{"enable_thinking": false}` | keep thinking off for voice (thinking makes replies ~4x slower) |
| `llm.tiers[].remoteStt` | false | tier has no room for local Whisper: allowed only while a worker's Whisper is healthy (see [Bigger model with a worker doing speech](#bigger-model-with-a-worker-doing-speech)) |
| `llm.tiers[].upgradeMarginMiB` | `vram.upgradeMarginMiB` | per-tier upgrade margin (a tight top tier needs a smaller one) |
| `llm.upgradeDelaySec` | 90 | how long other programs' usage must stay low before a bigger tier loads |
| `llm.downgradeLoadDelaySec` | 60 | outside games: wait this long after a downgrade's unload before loading the smaller model |
| `llm.noLoadsDuringActivity` | true | while a game/stream runs, only unload; loads, upgrades and crash restarts wait until it ends |
| `llm.activityMaxTier` | `small` | best tier allowed while a game/stream runs, applied as it starts (`""` = no cap) |
| `llm.activityPollSec` | 2 | VRAM check interval while a game/stream runs |
| `vram.upgradeMarginMiB` / `headroomMiB` | 1536 / 1024 | upgrade only this far under a tier's limit / keep this much free |
| `voice.computeType` | `int8_float16` | Whisper precision (`float16` uses about twice the VRAM) |
| `llm.cudaArch` | 89 | GPU generation for the build |
| `voice.whisperModel` / `language` | `large-v3-turbo` / `en` | |
| `voice.piperVoice` | `en_US-hfc_female-medium` | any [Piper voice](https://huggingface.co/rhasspy/piper-voices) |
| `voice.endpointingSec` | 0.8 | send the transcript after this much silence |
| `tailscale.*` | see below | |

---

## 2. Tailscale (remote access)

With Tailscale the watch (and any of your devices) reaches the PC from anywhere, and the phone gets real HTTPS
certificates, which Android apps need. Nothing is exposed to the public internet: the kit uses
`tailscale serve` (tailnet only), never `funnel`.

```json
"tailscale": {
  "enabled": true,              // false = LAN only; everything below is ignored
  "login": true,                // log in during install if logged out (prints a login link)
  "authKeyFile": "",            // or: file containing a Tailscale auth key, for unattended/headless installs
  "hostname": "",               // optional machine name on the tailnet (default: Windows computer name)
  "unattended": true,           // stay connected when nobody is signed in to Windows (reboots, headless streaming)
  "allowTailnetFirewall": true, // allow tailnet addresses (100.64.0.0/10) through the Windows firewall
  "serve": {                    // HTTPS ports published inside the tailnet (0 = don't publish)
    "speechApi": 443,           //   https://<pc>.<tailnet>.ts.net/v1        -> watch speech-to-text / text-to-speech
    "haRelay": 8443,            //   https://<pc>.<tailnet>.ts.net:8443      -> watch Home Assistant commands
    "llm": 0                    //   e.g. 8444 -> OpenAI-compatible LLM API for other apps
  },
  "advertiseLanRoute": false    // true = make this PC a subnet router for your home LAN (see below)
}
```

What the installer does with it:

1. Logs the PC in (`tailscale up --unattended`, or with your auth key), sets unattended mode and the hostname.
2. Adds the tailnet ranges to the kit's firewall rules (`allowTailnetFirewall`).
3. Publishes the `serve` ports with `tailscale serve --bg --https=<port>`. **HTTPS certificates must be enabled** in
   the Tailscale admin console (DNS page → *HTTPS Certificates*), with MagicDNS on.
4. With `advertiseLanRoute`, advertises your LAN (e.g. `192.168.1.0/24`) so tailnet devices can reach Home
   Assistant and anything else at home. Approve the route in the admin console (Machines → this PC → *Edit route
   settings*). You don't need this for the watch: the Assist relay already reaches Home Assistant on the LAN.

`.\status.ps1` prints the tailnet URLs.

**Phone side:** install Tailscale, sign in to the same tailnet, and set it as **Always-on VPN**
(Android Settings → Network/Connections → VPN → Tailscale ⚙ → *Always-on VPN*), so the watch works without
opening the app. If the phone can't resolve `*.ts.net`, set Android **Private DNS** to *Off/Automatic*, or enable
*Use Tailscale DNS* in the Tailscale app.

**Shared tailnet?** Everyone on the tailnet can reach the published services by default. Limit it with a grant in
the Tailscale policy file, for example (tag the PC `tag:homellm` first; adapt to your policy):

```json
"grants": [
  { "src": ["you@example.com"], "dst": ["tag:homellm"], "ip": ["443", "8443", "8080", "10200", "10300", "10310"] }
]
```

---

## 3. Home Assistant setup

### 3.1 A user and token for the kit
1. **Settings → People → Users → Add user**, e.g. `homellm`, *not* an administrator.
2. Log in as that user → **Profile → Security → Long-lived access tokens → Create token**. Copy it.
3. Use it for the installer prompt (Whisper name hints) and in Wristotle (step 4.4). One token for both is fine.

### 3.2 Conversation agent (the LLM)
1. **HACS → ⋮ → Custom repositories** → `https://github.com/skye-harris/hass_local_openai_llm`, type *Integration*.
   Install **Local OpenAI LLM**, restart Home Assistant.
2. **Settings → Devices & services → Add integration → Local OpenAI LLM**:
   server type **llama.cpp**, URL `http://<PC-LAN-IP>:8080/v1`, API key empty.
3. **Add conversation agent**: model `home`, LLM API **Assist**, *Enable thinking* **off**, *Use loaded model* off,
   temperature **0.1–0.3**, date/time injection on (keeps the prompt cacheable), history ~5 turns.
4. Instructions (prompt), a good start for voice with 4–12B models:

   ```
   You are a voice assistant for Home Assistant. Replies are spoken aloud.
   Answer in one short sentence. Confirm actions directly without explaining how.
   Plain text only: no markdown, lists, asterisks or emoji.
   Only say an action happened if the tool call succeeded; if it failed, say "Sorry, I couldn't do that."
   Never guess a device state.
   The request comes from speech recognition and may contain misheard words; use the closest matching device or area name.
   Don't end with a question unless the request is truly ambiguous.
   Never control: <locks, alarm, garage door>
   ```
   HA already adds the tool rules, entity list and live-state instructions; don't repeat them.

> Don't use *Local LLMs* (acon96/home-llm) v0.4.11 on HA 2026.9+: it crashes with
> `module 'probatio._vol_shim.validators' has no attribute '_WithSubValidators'` before reaching the server.

### 3.3 Speech-to-text and text-to-speech
1. **Add integration → Wyoming Protocol** → host `<PC-LAN-IP>`, port `10300` (Whisper).
2. **Add integration → Wyoming Protocol** again → port `10200` (Piper).
   Don't run the HA Whisper/Piper add-ons at the same time.

### 3.4 The voice assistant (pipeline)
**Settings → Voice assistants → Add assistant** (or edit yours):
- Conversation agent: the Local OpenAI LLM agent
- *Prefer handling commands locally*: **on** (simple commands run instantly; the rest go to the LLM)
- Speech-to-text: faster-whisper, English
- Text-to-speech: piper, voice `hfc_female` (or any installed)
- Make it the **preferred** assistant: the watch's commands go to the preferred pipeline.

### 3.5 Exposed entities
**Settings → Voice assistants → Expose**: expose only what you control by voice (**under ~30** entities) and give
tricky ones **aliases** (entity → settings → Aliases). This matters more than the prompt: every exposed entity is
sent to the model, and Whisper's name hints come from the same list.

### 3.6 Jev-mode decisions (optional)
Copy `homeassistant\homellm.yaml` (generated by the installer with your PC's address) to `/config/packages/`, and
enable packages in `configuration.yaml`:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

It adds `script.homellm_decide` (ask multiple-choice questions, get answers + probabilities) and a disabled
example automation.

---

### 3.7 Decision-first Assist agent (optional): no hallucinated devices
`homeassistant\custom_components\homellm_assist` is a conversation agent for your Assist pipeline. For every request
it first asks the local model a Jev-mode decision with two fixed-choice questions:
- **action:** turn_on, turn_off, open, close, set_brightness, brighten, dim, set_color, set_temperature, warmer,
  cooler or other
- **device:** one of the devices exposed to Assist, or none. Each device is offered once, by its main name; its
  aliases are listed in the instructions ("Other names: Car = Driveway Car Lock"), so the model's confidence isn't
  split between names of the same device (that split once left "lock the car" at 0.72, under the 0.9 lock threshold)
- **values:** colour (red, orange, yellow, green, cyan, blue, purple, magenta, pink, white), white temperature
  (warm/soft 2700 K, neutral 4000, cool 5000, daylight 6500, or 2000–6500 K), and brightness

**Brightness:**
- **A spoken percentage is read straight from the text** ("5%", "5 percent", "twenty five percent"), so any value
  from 0 to 100 is exact. Scoring digit strings confuses 5 with 50 and 1 with 100. The model's own guess (1% steps to
  20, then steps of 5) is only used when no number was said.
- **brighten / dim** change brightness by the amount said:
  - "by 30 percent": 30 points
  - "a little": 10
  - "a lot": 40
  - nothing: 20 (in practice the model usually picks "a little")
- **Limits:** dimming stops at 1%, never off. Brightening an off light turns it on at that amount. "Dim ... to 12
  percent" sets 12%.
- With a spoken percentage, the action threshold drops to 0.5, because set, dim and brighten all reach the same level.

The model can only answer with those values, so it can't invent a device. The agent runs the action and replies (e.g.
"Set Office Lights to 40%.") in 0.1–0.6 s when all of these hold:
- the action and device are confident (defaults 0.7 / 0.6), and so is the value (0.5);
- they fit together: lights, switches, fans and scripts turn on/off; covers open/close;
- the light supports the setting (brightness, colour, or colour temperature, clamped to the light's range).

**Warmer / cooler:** "make the office lights warmer" lowers the colour temperature by the configured step (Configure
→ warmer/cooler step, default 500 K); "a little" is 0.6× the step, "a lot" 2×, clamped to the light's range.

Everything else goes unchanged to a fallback agent (your LLM agent): questions, several devices, unknown devices and low-confidence answers.

**Doors and locks** (locks, garage/gate/door covers):
- **Allowed actions:** open/close or lock/unlock, one device at a time.
- **Stricter confidence:** the action and the device must both be at least 0.9 sure (Configure → security
  threshold).
- **No-op check:** a command is only sent when it would change the state ("Main Garage Door is already closed"), because
  many garage openers use one toggle pulse for both open and close.
- **Excluded from room and house commands.** Alarms are never on the fast path.

**Rooms:** the same decision call also asks for a **scope**, a **device kind** and a **room**:

| Scope | Example | Targets |
|---|---|---|
| one device | "close the master bedroom shades" | that device, as before |
| this room | "close the shades", "turn off the lights" | every exposed device of that kind in the area of the voice device that heard it |
| named room | "turn off the TV room lights" | that kind in the named area |
| whole house | "close all the shades" | that kind everywhere |

- **Device kinds:** lights, shades, switches and fans.
- **Brightness, colour and temperature work too.** Lights that can't do the requested setting are skipped.
- **"This room" needs a voice device assigned to an area.** Requests from phones, the watch or the web UI have no room,
  so they go to the fallback agent, which can ask which room.
- **Group or individuals:** the prompt lists your groups with their members.
  - Naming a group controls it as one device: "close the TV room shades" when TV Room Shades is a group.
  - "Each", "every", "all", "both" or "individually" mean every device of that kind in the room: "close each of the
    TV room shades".
- **Groups in room or house commands:** a group is used when some of its members aren't exposed themselves; its
  members are then skipped. Groups whose members are all exposed are skipped.
- **Entity areas** come from the entity, else its device. A YAML entity needs a `unique_id` before it can get an area.

Setup:
1. Copy the folder to `/config/custom_components/` and restart Home Assistant.
2. Add **Home LLM Assist** under Settings → Devices & services. Set the decision URL
   (`http://<pc>:8080/v1/decision`, or the router's decision port, see *Jev decisions on a worker*) and the fallback
   agent.
3. Set it as the conversation agent of your Assist pipeline.

Decisions are logged at INFO level under `custom_components.homellm_assist`.

## 4. Pebble watch (Wristotle)

The watch records your voice → the phone sends it to **Whisper on the PC** → the transcript goes through your
**Assist pipeline** via the relay → the reply is spoken by **Piper on the PC** through the watch speaker.
Everything travels over Tailscale; Home Assistant doesn't need to be reachable from the internet.

### 4.1 Pair the watch
Install the **Pebble app** (Core Devices) or **microPebble** on the phone and pair the watch.

### 4.2 Install Wristotle
1. Install **Wristotle Companion** from F-Droid (or the APK from Codeberg).
2. Install the **Wristotle watch app** from the rePebble or Rebble store (links above) to the watch.
3. Open Wristotle Companion and grant the permissions its setup screen asks for.
4. In Wristotle **Settings**, turn on **Show advanced settings** (Home Assistant and Speech are hidden otherwise).

### 4.3 Speech recognition on the PC's GPU
**Settings → Voice & AI → Models & learning → Speech-to-text**

| Field | Value |
|---|---|
| Mode | **Cloud primary, local fallback** |
| Base URL | `https://<pc>.<tailnet>.ts.net/v1` (the *Speech API* URL from `.\status.ps1`) |
| API key | empty |
| Model | `whisper` (any value works) |

Tap **Test connection**. A silent test clip may come back as "empty transcript"; that's expected (silence is
filtered). Keep the phone's local Whisper model downloaded as the offline fallback.

### 4.4 Commands through your Assist pipeline
**Settings → Voice & AI → Home Assistant**

| Field | Value |
|---|---|
| Base URL | `https://<pc>.<tailnet>.ts.net:8443` (the *Assist relay* URL; no trailing path) |
| Long-lived access token | the token from step 3.1 |
| Response timeout | **20** seconds (LLM replies, and model swaps when a game starts) |
| Custom trigger words | optional, e.g. `jarvis` (built-in: "hey home assistant", "home assistant", "hass") |

Wristotle calls Home Assistant's conversation API without choosing an agent, which would skip your pipeline and use
HA's built-in agent. The relay runs the command through your **preferred Assist pipeline** instead (LLM agent,
local handling), passes your token through to Home Assistant, and keeps follow-ups in the same conversation for
5 minutes.

### 4.5 Spoken replies from Piper
**Settings → Voice & AI → Speech** (marked *Experimental*)

1. **Speak on watch**: on.
2. **Speak which replies?**: select **Home Assistant** (and anything else you want read aloud).
3. **Mode**: **Cloud primary — HTTP first, Android fallback**.
4. **HTTP endpoint**: Base URL `https://<pc>.<tailnet>.ts.net/v1`, Model `tts-1` (ignored), Voice `alloy`
   (= the PC's default Piper voice; or a Piper voice name such as `en_US-amy-medium`).
5. Under **Test**, tap **Speak on watch (primary only)**.

### 4.6 Ask Agent through the same pipeline (optional)
The Assist relay also speaks the OpenAI chat API, so Wristotle's **Ask Agent** can use your Assist pipeline too:
device control, questions about the house and general questions, with no MCP setup.
**Settings → Voice & AI → Ask Agent**

| Field | Value |
|---|---|
| Provider | **OpenAI-compatible** |
| Endpoint URL (full /chat/completions) | `https://<pc>.<tailnet>.ts.net:8443/v1/chat/completions` |
| API key | the Home Assistant token from step 3.1 |
| Model | `assist` (any value works) |
| Routing | **Send unrecognised speech to Ask Agent**: phone commands (calls, texts, timers) stay on the phone, everything else goes to Assist without a wake word. Or **Ask Agent only**. |
| Response timeout | **20** seconds |
| Turns to remember | **0**: Home Assistant keeps the conversation itself for 5 minutes |

Leave the MCP servers empty. The relay ignores Wristotle's system prompt, history and tools: the newest request
is run through your preferred Assist pipeline, and its spoken reply comes back as the agent's answer. Follow-ups
("what about the near one?") keep their context.

**Which routing?** Wristotle first checks speech against its own on-phone commands (calls, texts, reminders,
alarms/timers, music, notes, tasks, weather, calculator); Home Assistant commands ("home assistant, …") go to the
relay; Ask Agent gets the rest:

| Routing | Behaviour | Calls / texts |
|---|---|---|
| Off (default) | Ask Agent only with its trigger words ("ask agent …", custom words) | unaffected |
| **Send unrecognised speech to Ask Agent** | phone commands first, anything unmatched goes to Assist | unaffected (recommended) |
| Ask Agent only | every command goes to Assist, skipping phone commands | **lost**: Assist can't place calls or send texts |

Phrasings Wristotle's recognizer misses go to Assist, which will say it can't do them; the optional sentence model
under *Models & learning* helps it match loosely phrased phone commands.

### 4.7 Weather questions
"What's the weather" is one of Wristotle's **own** commands, so it never reaches Ask Agent, and:
- it only knows **current** conditions ("tomorrow" is ignored);
- without a city it needs a recent phone location, but Wristotle only has *while in use* location access
  (no background access, no default-city setting), so from the watch it usually answers *"No recent location"*.

Instead say **"weather in <city>"**, or ask Home Assistant, which knows your home location:
*"home assistant, what's the weather?"* / *"ask agent, what's the weather tomorrow?"*. Expose a weather entity to
Assist for current conditions; forecasts need a small script around `weather.get_forecasts` exposed as a tool,
because HA doesn't give forecasts to the model by default.

### 4.8 Try it
Press the watch's dictation button and say *"Home assistant, turn on the kitchen light."*
On the PC, `logs\speechApi.err.log` shows what Whisper heard and `logs\haRelay.err.log` shows the command and
Home Assistant's reply.

---

## Long GPU jobs (training, teacher models)

The tiers treat **any** other GPU program as a game, including your own training runs. The supervisor never stops
those; it only steps its own model down. That matters when a job uses the LLM itself, for example as a **teacher**
that writes training data through `home`:

| The job uses | What happens to `home` |
|---|---|
| over ~10 GB | Gemma is swapped for the 4B model. Requests keep working but get 4B answers, which can silently lower the quality of generated data |
| over ~16 GB | no LLM: teacher requests fail |
| over ~19 GB | Whisper stops too |

Pin the tier while such a job runs, and unpin it afterwards:

```powershell
.\status.ps1 -Mode full    # pin Gemma + Whisper; VRAM is ignored until you unpin
.\status.ps1 -Mode auto    # back to automatic tiers
```

While pinned, fitting everything in VRAM is up to you. On a 24 GB card: Gemma ~10 GB + Whisper ~1.4 GB +
desktop/encoder ~1–2 GB leaves **~11 GB for the job**. A job that needs more runs out of memory instead of the
LLM stepping aside; generate the data first, then train. Data generators can also watch `state\status.json` and
pause while `tier` isn't `full` (for example before each batch), so they never mix answers from a smaller model
into a dataset.

## Multiple PCs (worker takes over while you game)

A second PC with an NVIDIA GPU can serve the LLM and Whisper while the main PC is gaming or streaming. Home
Assistant and the apps keep pointing at the main PC (the **orchestrator**) and never notice the switch.

- **Router:** on the orchestrator, `scripts\router.py` (task `HomeRouter`) owns the public ports (LLM 8080, Whisper
  10300). The kit's own llama-server and Whisper move to localhost-only `cluster.localPorts` (8081, 10301). The router
  forwards each new connection over TCP, so streaming, Jev-mode `/v1/decision` and Wyoming all pass through unchanged.
- **Routing:** while a game or stream runs on the orchestrator and a worker is healthy, the worker serves (inference
  then doesn't compete with the game for the GPU). The worker also serves while the orchestrator is about to swap
  its model, and for 30 s after, so nobody hits a server mid-swap. Otherwise the orchestrator serves if it can,
  else a worker. While nothing is up yet (a worker still loading right after a game starts), new connections wait up
  to 30 s (`cluster.waitForBackendSec`) instead of failing, but only if a worker has checked in recently.
- **Measured** (4090 orchestrator, 3080 worker, request every 2 s through a stream start, the stream end and the
  Gemma swap): 103 requests, 0 failed; the one request at the moment the stream started waited 14 s.
- **Asking workers to load:** workers poll `GET http://<orchestrator>:8079/assignment`. The orchestrator asks as soon
  as a game/stream starts, or once its own backend has been down for 15 s (`cluster.requestAfterSec`). It releases
  them once nothing runs and its own backend has been healthy for 60 s (`releaseAfterSec`).
- **Worker rules:** the worker runs the same supervisor with its own tiers, sized for its VRAM. Its own games cap it
  (`llm.activityMaxTier`), it never loads a model while its own game runs, and it unloads after 60 s without the
  orchestrator. Whisper only runs while asked (`voice.services: ["whisper"]` skips Piper and the relays).
- **Wake-on-LAN:** give a worker its `mac` and the orchestrator wakes it when it's needed (a game starts) but hasn't
  checked in for 20 s, resending every minute up to 5 times. The worker needs Wake-on-LAN enabled in its BIOS and on
  its network adapter. It can't revive a PC that has frozen.
- **Check it:** `.\status.ps1` on either PC shows the routing and worker state; `http://<orchestrator>:8079/status` shows routes, backend health, what's wanted, and the workers'
  last poll; `logs\router.log` logs every switch.

### Bigger model with a worker doing speech

With a worker serving Whisper and Piper (`cluster.preferWorkers: ["stt", "tts"]`), the main GPU can hold a 27B model
with a context long enough for coding agents. Put a `remoteStt` tier first; it is allowed only while a worker's
Whisper is healthy (the router's `state\router.json`). If the worker's Whisper is gone for 8 s, the supervisor drops
to the next tier, starts local Whisper at once and loads that tier's model 20 s later, so voice keeps working.

Ran on our RTX 4090 (24 GB) from 2026-11-11 until the [Big MoE tier](#big-moe-tier-strata-default-or-basalt-opt-in)
replaced it (2026-11-18), and still the simplest big tier when you don't have ~64 GB of RAM for the MoE:
[Swift 1.5](https://huggingface.co/ukisai/Swift-1.5-Qwen3.8-27b)
(UkisAI's reasoning-efficient Qwen3.8 27B: fewer thinking tokens, better agentic coding scores) at a 96K context:

```json
{ "name": "max", "maxOthersMiB": 2304, "upgradeMarginMiB": 768, "whisper": false, "remoteStt": true,
  "model": { "repo": "ukisai/Swift-1.5-Qwen3.8-27B-GGUF", "file": "Swift-1.5-Qwen3.8-27B-Q4_K_M.gguf",
    "ctx": 98304, "decisionSeqs": 12, "chatTemplateKwargs": { "enable_thinking": false } } },
```

- **Context size.** Coding agents need 64K at the very least (system prompt and tool definitions alone are 15–25K
  tokens); 96K is the largest that fits beside a normal desktop. Qwen3.8 27B only has full attention on every 4th
  layer, so the q8_0 KV cache costs ~33 KB per token: 64K ≈ 20.7 GB in total, 96K ≈ 21.7 GB, 128K ≈ 22.9 GB
  (too tight; `-ctk q4_0 -ctv q4_0` would fit it at some cost to long-context recall).
- **Limits.** 24.5 GB − 21.7 GB leaves ~2.8 GB, so the tier is allowed while other programs use under 2.3 GB, and
  loads only below 1.5 GB (idle desktop: 0.2–0.9 GB). A browser video or a game drops it to the next tier.
- **Thinking per caller.** Keep `enable_thinking: false` as the server default so Home Assistant voice and Jev
  decisions stay fast. An agent that wants reasoning turns it on per request with
  `"chat_template_kwargs": {"enable_thinking": true, "reasoning_effort": "medium"}` (the Qwen3.8 template accepts
  `low`, `medium` and `xhigh`); llama-server merges it over the default for that request only.
- **License.** Swift 1.5 is under the Swift Open License v1.0: free for personal use and for organisations under
  US$1M revenue; read it before using it commercially.

Example worker tiers for a 10 GB card: `small` (Qwen3.5 4B + Whisper) while other programs use < 2.5 GB, `voice`
(Whisper only) < 7.5 GB, then `none`; `activityMaxTier: "voice"`; margins 512 MiB. Gemma 12B doesn't fit in 10 GB.

**Setup:**
1. **Orchestrator:** in `config.json` set `cluster.role` to `"orchestrator"` and list the workers
   (`name`, `host`). Re-run `install.ps1`; it registers `HomeRouter` and opens the control port to the workers only.
2. **Worker:** copy a folder to it with `config.json` (start from `config.worker.example.json`: `cluster.role: "worker"`, `cluster.name`,
   `cluster.orchestrator: "http://<orchestrator-ip>:8079"`, the worker's tiers), `scripts\`,
   `llama.cpp\build\bin\` (llama-server built for the worker's GPU, e.g. `cudaArch` 86 for an RTX 30-series card, plus
   `cudart64_12.dll`, `cublas64_12.dll`, `cublasLt64_12.dll`), `models\` and `requirements-stt.txt`
   (`pip freeze` of the orchestrator's `voice\stt` venv). Copy `voice\data\models--*` too to skip the Whisper download.
3. On the worker, run `scripts\install-worker.ps1` as administrator. It creates the Whisper venv, allows the LLM and
   Whisper ports from the orchestrator only, and registers `HomeLLM` and `HomeVoice`.

### Jev decisions on a worker

Decisions don't need a big model: on our 41-command test a Gemma 4 12B on an RTX 3080 scored 41/41, the same as the
27B. Giving decisions their own router port lets them prefer the worker while chat stays on the main GPU, and frees
the main GPU for a model that has no `/v1/decision` at all (Strata or Basalt, below).

**Where Assist requests go** (with the worker and the big MoE tier):
- **Fast path:** `homellm_assist` asks the decision port, so simple device commands are answered by Gemma on the
  worker in 0.2–0.6 s. They never reach the big model.
- **Everything else** (questions, low-confidence or multi-device requests) goes to the fallback LLM agent → router
  (:8080) → the big model on the main GPU, about 1–1.5 s for a short answer. While the main PC games, the router
  sends it to Gemma on the worker instead.

**Not done: Jev decisions on Basalt.** A Basalt `/v1/decision` exists in a contributor branch
([jesdga95/basalt#206](https://github.com/jesdga95/basalt/pull/206), closed unmerged: upstream doesn't want a decision
API). We didn't port it. On a hybrid MoE every decision branch needs its own recurrent state (~112 MiB per lane) and
pulls different experts from RAM, so it would likely be 2–5× slower than the worker's 0.19 s, decisions would queue
behind long agent replies, and the 12B already scores 41/41.

**Assist through Hermes?** Possible (Hermes' API is OpenAI-compatible), but measured at 3–12 s per answer against
1–1.5 s for the HA agent: every request carries ~16K tokens of Hermes system prompt and tools. Hermes also holds
requests while the main PC games, keeps everything said to it in its memory (bad for a shared house speaker) and its
denylist refuses locks and garage doors. If you want it, point only your own watch's Ask Agent at Hermes and keep the
house pipeline as is.

- **Orchestrator:** `cluster.decisionPort` (e.g. 8082) opens a second public LLM port for decisions; add
  `"decision"` to `cluster.preferWorkers`. Decisions then go to a healthy worker first and to the local server
  otherwise; workers are kept asked to load their model. Re-run `install.ps1` (it opens the port to the LAN and
  tailnet). Point `homellm_assist`'s decision URL at `http://<pc>:<decisionPort>/v1/decision`.
- **Main GPU tiers** with `"remoteDecision": true` are allowed only while a worker answers decisions; if it's gone for
  8 s the supervisor drops to the next tier.
- **Worker tier tuned for decisions** (`config.worker.example.json`, `_exampleJevTier`): Gemma 4 12B UD-Q4_K_XL,
  4K context, q8_0 KV, 24 decision sequences, `estMiB` 8200. Measured on the 3080 next to Whisper: 41/41, 0.19 s per
  decision, ~9.4 GB. A/B notes: 24 sequences were faster than 12 (0.23 s) and as fast as 32; Google's QAT Q4_0 also
  scored 41/41 but with lower confidence on the right answers (min 0.93 vs 0.97); q8_0 KV at 8K overflowed into shared
  memory and doubled latency. Chat on such a tier is limited to short prompts.
- **Prompt cache:** a cached Home Assistant decision takes ~0.2 s, an uncached one 0.6–0.9 s. Other callers'
  decisions with different instructions evict the cache; the model gateway re-warms it (below).

### Big MoE tier: Strata (default) or Basalt (opt-in)

Swift 1.5 Flash-Next (the Swift 1.5 fine-tune of Qwen3.8-Flash-Next, a 125B mixture-of-experts model) runs on one 24 GB card
plus ~45 GB of RAM: the GPU keeps the attention layers and the most-used experts, the rest are read from RAM. Two
engines can serve it. The tier, the supervisor, the router and the gateway are the same for both; only
`scripts\strata-serve.ps1` changes what it starts.

| | **Strata** (default) | **Basalt, Ada fork** (opt-in) |
|---|---|---|
| Works on | RTX 40 / 30 and other consumer cards, out of the box | RTX 40 (sm_89) only, and you build it yourself |
| Model file | Swift 1.5 IQ3_XXS (Strata pack) | Swift 1.5 IQ3_S (`.basalt`, converted from the GGUF): about 40% less error than IQ3_XXS (KLD 0.145 vs 0.240, from the model card) |
| Decode (RTX 4090) | 105–140 tok/s | **146 tok/s** near an empty context; 133 / 128 / 125 at 125K / 195K / 244K |
| Prompt reading | ~4.5K tok/s at 57–98K | ~6.6K tok/s at 57–98K; 244K cold in 38 s |
| Context on 64 GB RAM | 128K | **256K** (needs the direct-parking patch below) |
| Switching back to a long conversation | re-read from scratch | parked on the NVMe: a 59K chat returns in 0.9 s (vs 8.9 s), a 244K chat in 1.2–1.4 s (vs 38 s) |
| Free RAM at the lowest | ~7 GB | 5.9–6.9 GB at 256K |

Both have no `/v1/decision`, so use them with [Jev decisions on a worker](#jev-decisions-on-a-worker).

**Switching engines:** set `strata.engine` in `config.json` to `"strata"` or `"basalt"` (or pass `-Engine` to
`scripts\strata-serve.ps1`), then let the supervisor restart the tier (`.\status.ps1 -Mode none`, then
`-Mode auto`). The other engine's files stay installed, so going back is the same one-line change.

#### Strata (default)

1. Install the Strata Terminal and the model as yourself, without starting it:
   `strata install --family swift --model IQ3_XXS --context 131072 --vision gpu --port 8081 --no-browser --yes --setup --no-start`,
   then `strata update` once (it downloads the engine and test-starts it; free the GPU first). Add
   `"reasoning_effort": "none"` and `"aliases": ["home"]` to the generated `strata-<model>.json` so callers that don't
   ask for thinking (Home Assistant, mail) answer at once. Our measured settings (RTX 4090 + 64 GB): `--max-context
   131072 --kv int8 --kv-resident 32768 --vram-reserve-mib 700 --spec 4 --spec-min-p 0.5 --prompt-cache-spread`, GPU
   image reader: load 23 s, 23.7 GB VRAM, 115–140 tok/s from 7K to 109K tokens of context.
2. In `config.json`: `strata.engine` = `"strata"`, `strata.runConfig` = that file, and put a tier like
   `llm._exampleStrataTier` first in `llm.tiers` (set `external.pathPrefix` to your Strata folder; it's matched
   against process paths). Its `upgradeDelaySec` 300 loads it only after 5 idle minutes, because loading pins RAM and
   can stall the PC briefly.
3. Re-run `install.ps1`. It registers the `HomeLLM Strata` task for your user (Strata lives in your profile); the
   supervisor starts and stops it. Without a session, or if it crashes, the tier is skipped for
   `external.blockMinutes` and the next tier loads.

#### Basalt (opt-in, faster, 256K)

[Basalt](https://github.com/jesdga95/basalt) is a Flash-Next engine for **NVIDIA Blackwell (sm_120) only** upstream.
[Ex3c4Def/basalt_ada](https://github.com/Ex3c4Def/basalt_ada) (branch `fork_4090`) ports it to the RTX 4090 (sm_89)
with fallback kernels for what Ada lacks (TMA, clusters, FP4 MMAs). Expect to build it, and to re-check after every
upstream merge. Requirements: Windows, Visual Studio 2022 C++ tools, a CUDA 13 toolkit (the no-admin redistributable
tree works), CMake + Ninja, Python 3.10+ with Basalt's `requirements.txt`, ~85 GB of disk for the IQ3_S pack plus room
for parked conversations, and an NVMe for them.

1. **Get the fork with the direct-parking patch.** It's one commit on top of `fork_4090`:
   [armandodollia/basalt_ada `conv-cache-disk-direct`](https://github.com/armandodollia/basalt_ada/tree/conv-cache-disk-direct)
   (commit `b20ab0b9`). The PR to Ex3c4Def/basalt_ada couldn't be opened: that repo only accepts PRs from
   collaborators. Compare view: `Ex3c4Def/basalt_ada/compare/fork_4090...armandodollia:basalt_ada:conv-cache-disk-direct`.
   ```powershell
   git clone https://github.com/Ex3c4Def/basalt_ada; cd basalt_ada
   git remote add fork https://github.com/armandodollia/basalt_ada; git fetch fork
   git checkout -b home fork/conv-cache-disk-direct
   git apply <kit>\basalt\patches\0001-any-model-name.patch   # any model name reaches the one model
   ```
2. **Build** for sm_89: copy `basalt\build_ada_local.bat` into the checkout, set its CUDA / venv / vcvars paths, run
   it. It ends with `BUILD OK` in `build_local.log`; the executables land in `bin\`.
3. **Model:** download the Swift 1.5 Flash-Next IQ3_S GGUF (both shards) and its mmproj, then convert:
   `python tools/convert/convert.py --gguf <IQ3_S-00001-of-0000N.gguf> --mmproj <mmproj.gguf> --out Swift-FN-IQ3_S.basalt`
   (see the fork's `docs/model-file.md`). Converted packs are slower than official `.basalt` files where those exist.
4. **Config:** start from `basalt\basalt_home.example.json` (alias `home`, int8 KV, `kv-resident` 32768,
   `reasoning-effort` none, 262144 context, resident experts, direct parking to a 50 GB NVMe cache). Its
   `server.port` must be the tier's LLM port (8081 on an orchestrator).
5. **Kit:** `strata.engine` = `"basalt"` and fill `strata.basalt` (`root`, `config`, `cudaPath`, `venvScripts`). The
   example tier's `processMatch` already recognises Basalt's processes, so the supervisor counts its VRAM as its own
   instead of treating it as a game and unloading it.

The learned expert profile (`expert-profile-save`) improves which experts stay on the GPU over the first sessions.
Images: first one read in 1.1 s, then 0.11 s.

**4-bit (NVFP4) rejected on 64 GB:** more accurate (KLD ~0.07) but 18% of it has to stay on the SSD, which drops
decode to 47 tok/s. Worth revisiting with 96–128 GB of RAM.

#### 256K context

Settings (Basalt): `"max-context": 262144`, `"kv": "int8"`, `"kv-resident": 32768`, and in `engine`:
`"conversation-cache-disk-direct": true`, `"conversation-cache-disk-mib": 51200`, `"conversation-cache-dir"` on an
NVMe. Leave `conversation-cache-min-free-mib` at its default **2560 (2.5 GB)**.

- **Why direct parking is needed.** Basalt parks idle conversations on disk so switching back doesn't re-read them.
  Unpatched, it first copies the whole conversation state into RAM (about 17.8 MB per 1K tokens: 4.3 GB at 244K),
  and only does so if that much RAM is free above the 2.5 GB floor. At 256K on 64 GB only 5–5.6 GB is free, so
  nothing above ~110K tokens could park ("skip parking (snapshot 4345 MiB exceeds available budget)") and every switch
  back to a long conversation cost a full re-read (38 s at 244K). `--conversation-cache-disk-direct` streams the KV
  between the session and the files through one 8 MiB buffer (`conversation-cache-disk-chunk-mib`), so the admission
  only needs ~118 MB plus the buffer. The files are byte for byte the same as the old path's. A write between requests
  yields to an arriving request. Off by default.
- **Measured** (RTX 4090, 64 GB, PCIe 4.0 NVMe): a 244K conversation parks in 2.1 s and comes back in 1.2–1.4 s; recall
  5/5 (code words at 10/50/90/98% plus a two-fact question) after restores; lowest free RAM 6.9 GB in the test, 5.9 GB
  in daily use; decode unchanged (128 / 132 / 129 tok/s at 1.7K / 125K / 244K). Follow-ups in the same conversation
  take 0.2 s.
- **Keep `conversation-cache-min-free-mib` ≥ 2560.** Lowering it would also let parking work without the patch, but
  it eats the margin that keeps Windows from running out of physical RAM while Basalt maps the model file. This PC has
  had BSODs before, so we kept the floor and fixed parking instead.
- **Rejected: a smaller expert RAM budget** (`resident-budget-gib`, 39.5 → 36.7 → 34.9 GiB). It raises idle free RAM
  (5.6 → 7.3 → 9.3 GB) but no decode experts moved to the SSD; long prompts then read experts through a memory-mapped
  view of the model file that grows back into the saved RAM, so free RAM under a long prompt went *down* (to
  2.6–2.9 GB, where Basalt's guard fires), parking still failed, and decode at 244K fell from 125 to 118 tok/s.
- **Without the patch**, stay at 128K on 64 GB, or add RAM (96 GB+ removes the limit).

#### Gaming

- **A game or stream start unloads the big model within seconds** (tested: 23.7 → 0.95 GB in 6 s with Strata,
  Basalt gone within 12 s, 4090 at 1.1 GB used). Chat falls back to the worker through the router.
- **No large model loads during a game.** With `llm.noLoadsDuringActivity` nothing at all loads on the main GPU while
  the game runs; the 4B fallback tier loads only after the game ends, for the ~5 idle minutes (`upgradeDelaySec` 300)
  before the big model comes back.
- **Local Whisper resumes on the main GPU mid-game** (~1.4 GB) when the worker's Whisper is gone, so voice keeps
  working.
- **Requests for the big model during a game** are handled by the gateway: small ones go to the worker's Gemma, the
  rest wait (below). Nothing is loaded on the 4090 for them.

**Thinking:** the engines default to `reasoning_effort` none (set in their configs) so Home Assistant and mail answer
at once; callers ask for none/low/medium/high per request. Community 4090 reports and our tests suggest `low`
(optionally with `reasoning_budget_tokens` 4096) for agent work and `none` for voice. A request's
`chat_template_kwargs.reasoning_effort` beats its top-level `reasoning_effort`, so a client that hard-codes the former
overrides any effort selector.

## Model gateway (one key for every model and mode)

`scripts\gateway.py` (task `HomeLLM Gateway`, `gateway.enabled`) is one OpenAI-compatible endpoint for coding agents
(OpenCode, T3 Code), Hermes and other machines. Each entry in `gateway.models` is a selectable model id that pins a
backend and, optionally, a mode:

| Field | Meaning |
|---|---|
| `url`, `kind` | backend (`chat`), or `jev`: the last user message becomes a decision (choices from `options: a \| b \| c`, a `[a\|b]` bracket or `- a` bullets, else yes/no; a pasted JSON decision body is sent as is) |
| `effort` / `think` | thinking forced per model: `reasoning_effort` none/low/medium/high (Strata/Basalt/Qwen) or `enable_thinking` (Gemma). **Leave both out** to pass the client's own `reasoning_effort` / `chat_template_kwargs` through |
| `reasoningBudget` | hard cap on thinking tokens (`reasoning_budget_tokens`) |
| `chatOnly`, `promptBudget`, `maxReply` | for small-context models: drop tools, replace a long agent system prompt, trim old turns |
| `requireTier`, `requireModel`, `healthCheck` | the model is "up" only while that tier (`state\status.json`) / model (`/props`) is loaded and, with `healthCheck`, `<url>/health` answers |
| `fallback` | while down: send requests **without tools** whose estimated prompt fits the fallback model's `promptBudget` (default 2600 tokens) to that model, e.g. Gemma on the worker |
| `whenDown` | `"fail"` (default: 503 with a clear message) or `"hold"`: wait for the model (below) |

- **Routing:** the model id picks the backend (`<provider>/<model>` ids are accepted); the gateway renames the model
  to the backend's alias (`llm.alias`). Recommended layout: **one** big-model id with no `effort`
  (`swift-1.5-125b-strata`), so clients choose the level with their own effort selector, plus the worker's Gemma
  (chat-only, 4K) and `jev-decision`. Fixed-effort aliases (`strata-swift-none`, `strata-swift-low`) still work for
  tools that can't send an effort.
- **Hold queue (`whenDown: "hold"`):** while the big model is down (a game or stream runs, or the supervisor is
  between tiers), a request that the fallback can't take is **held, not failed**. The connection stays open with a
  keepalive every `hold.keepaliveSec` (15 s): an SSE comment line (`: queued, waiting for …`) for streaming requests,
  a leading space before the JSON body otherwise; clients ignore both. Every `hold.pollSec` it checks the model again;
  once it's back the request runs as if it had just arrived. After `hold.maxHours` (4 h) it gives up with a 503 error
  in the same response. Nothing is loaded on the main GPU for a held request: the supervisor brings the tier back on
  its own schedule. Tested with a fake game: a 20K-token request and a Hermes request with 21 tools were each held
  ~7 minutes and finished correctly. Clients need a request timeout above 4 h (templates below use 15,300 s).
- **Fallback while gaming:** a request with no tools and under ~2,600 estimated tokens goes to Gemma on the worker
  (tested: ~1 s). It loses the requested thinking level and gets a short system prompt if its own is long.
- **Keys, one per machine:** every request needs `Authorization: Bearer <key>`. `gateway.keyFile` (created by
  `install.ps1`) is the `default` key; each `gateway.keyDir\<label>.txt` adds a key for one machine (e.g.
  `work-laptop.txt`), and `gateway.log` names the label on every line. Keys are re-read on every request, so
  deleting a file cuts that machine off at once. Make a key with
  `[Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))` (PowerShell 7) (or any long random string) and copy it to
  the machine out of band (we used Taildrop), never in a chat or a prompt. Publish the gateway tailnet-only with
  `tailscale.serve.gateway` (e.g. 8446).
- **`POST /v1/decision`** passes any decision through to `gateway.decisionUrl` (default: the router's decision port).
- **Re-warm:** after a decision from anyone but Home Assistant, the gateway re-sends Home Assistant's exact payload
  (`scripts\ha_payload.py` rebuilds it from live HA every 15 min) so the next voice command is a cache hit. Measured:
  a voice command right after an unrelated decision took 1.0 s without it, 0.3 s with it. Your own decisions are
  unaffected, apart from occupying the decision GPU for ~0.7 s afterwards.

## Client templates (OpenCode, Hermes, T3 Code)

Templates in `clients\`. Replace `<pc>.<your-tailnet>.ts.net` with your PC's MagicDNS name and keep keys in files.

**OpenCode** (`clients\opencode.example.json` → `~/.config/opencode/opencode.json`; also what T3 Code's OpenCode
provider reads):
- One provider `home` (`@ai-sdk/openai-compatible`), `baseURL` = the gateway, `apiKey` =
  `{file:~/.config/opencode/home-gateway.key}`, `options.timeout` **15300000** ms (longer than the 4 h hold).
- **One** big model, `swift-1.5-125b-strata`: `limit.context` **262144** (131072 with Strata), output 32768,
  `tool_call`/`reasoning`/`attachment` true, `options.reasoningEffort` medium (used when no variant is picked) and
  `variants` none / low / medium / high. `low` also sends `reasoning_budget_tokens: 4096`, the 4K thinking cap.
  T3 Code builds its **Reasoning** selector from these variants.
- Gemma (chat only, `tool_call` false) and `jev-decision` at `limit.context` **4096**.

**Hermes** (`clients\hermes-config.example.yaml`, the relevant parts of `config.yaml`):
- Provider `home-4090` → the gateway (`http://127.0.0.1:8090/v1` on the PC itself, the tailnet URL elsewhere),
  model `swift-1.5-125b-strata` with `context_length` **262144**, `request_timeout_seconds` and
  `stale_timeout_seconds` **15300**, and `agent.gateway_timeout` **15300**, so held requests survive a long game.
- Thinking: `agent.reasoning_effort: medium` for everything, `agent.reasoning_overrides` sets **low** as the
  default for the home model; T3 Code's effort selector on a Hermes thread overrides it per session.
- **Don't** add `extra_body.chat_template_kwargs` with a `reasoning_effort` to the provider: the engine ranks it above
  the request's top-level `reasoning_effort`, so it silently overrides the effort selector (that was our bug).

**MacBook / other machines: Hermes in T3 Code over ACP** (`clients\hermes_acp_bridge.py`, stdlib Python 3.9+):
T3 Code runs it as an ACP agent over stdio; it talks HTTPS to the Hermes API server on the PC (tailnet only). Each
ACP session is a Hermes API session; prompts stream through `/api/sessions/{id}/chat/stream`; dangerous-command
approvals become ACP permission prompts. It offers a **model** option and a **reasoning_effort** option (none,
minimal, low, medium, high, xhigh, max, ultra; shown as low for the home model until picked) and forwards the chosen
level as `"model_options": {"reasoning_effort": …}`. Setup on the Mac:
1. Copy it to `~/.local/share/hermes-remote/`, put the Hermes API key in `~/.config/hermes-remote/key` (`chmod 600`),
   set `HERMES_URL=https://<pc>.<your-tailnet>.ts.net:8445` (or edit the default in the file). `--check` tests it.
2. T3 settings → provider instance `hermes`, driver ACP registry, command `/usr/bin/python3` with the bridge path.
   `--profile <name>` reaches a separate Hermes profile (`/p/<name>`, key `~/.config/hermes-remote/<name>.key`).
3. Known limitation: Hermes' session chat stream sends no thinking events, so the Mac shows answers but not the
   thinking (the level still applies).

**T3 Code notes:**
- T3 caches provider model lists. After changing `opencode.json`, toggle that provider instance off and on (Settings,
  or `enabled` in `~/.t3/userdata/settings.json`, which T3 watches) instead of restarting T3. A thread running on
  that provider at that moment is interrupted. Hermes' list refreshes when a new Hermes session starts.
- Threads created on a removed model id complain the model is missing: switch them to the new one.
- If T3's remote access is served at `https://<pc>.<tailnet>.ts.net/` (port 443) it takes over the speech API's
  address. Give the speech paths their own route, e.g. `tailscale serve --bg --https=443 --set-path=/v1/audio http://127.0.0.1:10310/v1/audio`,
  and re-add it if T3 resets the serve config (we run a small scheduled check every 10 minutes and at logon).

## Measuring decision accuracy (eval)

```
voice\stt\Scripts\python.exe scripts\ha_payload.py
voice\stt\Scripts\python.exe eval\run_eval.py http://<server>:<port> <label> [eval\cases.json]
```

`ha_payload.py` rebuilds the exact payload `homellm_assist` sends (needs an admin token in `homeAssistant.tokenFile`
to read the registries). `run_eval.py` scores a llama-server on your commands the way the integration would act:
aliases count as their device, a spoken percentage overrides the scored number, room/house commands need confident
scope/kind/room, locks and garage doors need 0.9. Start `eval\cases.json` from `eval\cases.example.json` with your own
device and room names; results go to `eval\results_<label>.json`.

## Activity detection

Games and streams no longer pick the model (VRAM does). They're detected to hold back model loads while they run
(see above), and they're shown in `status.ps1` and `supervisor.log`. A game counts as running when Steam reports a running app, or when a process runs from a Steam
library, `C:\Program Files\Epic Games`, `C:\Program Files\EA Games`, Ubisoft's `games` folder or `C:\XboxGames`.
Add other games (e.g. Battle.net) by exe name to `games.txt`; exclude false positives (Wallpaper Engine is already
excluded) in `ignore.txt`. Streams come from an Apollo/Sunshine prep command the installer adds (existing prep
commands are kept; a backup is written next to the config).

## Security notes

- Ports are open to the **local subnet** and, with `tailscale.allowTailnetFirewall`, the **tailnet**, never the
  internet. Anyone on those networks can use the LLM and voice services; restrict tailnet access with a grant
  (see section 2) if you share your tailnet.
- The gateway is published tailnet-only and every request needs a key. Give each machine its own key file in
  `gateway.keyDir` so you can revoke one machine by deleting its file. Keep key files out of the repo and out of
  chats and prompts.
- The token in `hass_token.txt` acts as that Home Assistant user; use a non-admin user. The Assist relay
  doesn't store tokens; it forwards the caller's token to Home Assistant.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| HA's Wyoming setup spins, or Assist hangs at speech-to-text | Windows created **Block** firewall rules for Python (an unanswered prompt). Re-run `install.ps1 -SkipBuild -SkipModels`; it removes them and adds an allow rule. |
| Every LLM request fails with "API server returned an error" after ~16 s, nothing in `server-*.err.log` | Home Assistant can't connect: Windows created **Block** rules for `llama-server.exe` (e.g. after it was started by hand and a firewall prompt went unanswered). Re-run `install.ps1 -SkipBuild -SkipModels`. The integration shows this same message for connection failures. |
| "Unexpected error during intent recognition" | Check HA's log. With *Local LLMs* on HA 2026.9+, switch to *Local OpenAI LLM* (3.2). |
| Chat replies take >1 s | Thinking is on. Keep `enable_thinking=false` in config and *Enable thinking* off in the agent. |
| Whisper hears "We'll be right back." from silence | Old config; the kit runs Whisper with `--vad-filter`. |
| Device names misheard | Set a token (3.1) so Whisper gets name hints; add aliases; the first command after a restart may miss the hints. |
| Piper exits right after "Ready" | An upgrade undid the Windows patch: `voice\tts\Scripts\python.exe scripts\patch_piper.py`. |
| Stuck on a small tier | `.\status.ps1` shows how much VRAM other programs use; close what holds it, or pin with `-Mode full`. A leftover `state\override.txt` also pins a tier. |
| A training job's teacher got worse or failed | The job's VRAM pushed the tier down; pin it during the job (see *Long GPU jobs*). |
| Wristotle "network failure" | Use the `https://….ts.net` URLs, check Tailscale is connected on the phone, turn off Private DNS. |
| Wristotle "access token rejected" | Wrong token, or created by a different user. |
| `tailscale serve` failed | Enable MagicDNS and HTTPS certificates in the Tailscale admin console, then re-run the installer. |
| Supervisor stopped logging while you watched the log | Fixed: a reader holding `supervisor.log` open made a log write fail inside the error handler. Log writes now retry and never stop the supervisor. |
| Strata/Basalt tier never loads / "skipped for 15 min" | The `HomeLLM Strata` task couldn't start (you weren't logged on) or the engine exited; see `logs\strata-serve.log` (it names the engine). The tier also needs a worker's Whisper and decisions (`remoteStt`, `remoteDecision`). |
| Basalt starts, then the supervisor unloads it again and again | The supervisor counts Basalt's VRAM as another program: make the tier's `external.processMatch` match Basalt's processes (the example matches `basalt_home.json` and `\basalt_ada\bin\basalt`). |
| Basalt: "unknown model" for a client name | Apply `basalt\patches\0001-any-model-name.patch` and keep `strata.basalt.anyModelName` on; re-apply after every `git pull` of the fork. |
| Basalt at 256K: switching back to a long chat is slow (~38 s), log says "skip parking" | Direct parking is off or the build lacks it: use the `conv-cache-disk-direct` branch and `"conversation-cache-disk-direct": true`. Don't lower `conversation-cache-min-free-mib` instead (see 256K context). |
| Gateway answers 503 "isn't loaded" | The model's `requireTier`/`requireModel`/`healthCheck` isn't met (PC gaming, worker off) and it has no `whenDown: "hold"`. Pick another model. |
| An agent request hangs for minutes while you game | Expected with `whenDown: "hold"`: `gateway.log` shows `HOLD`. It runs once the big model is back (at most `hold.maxHours`). Small requests without tools go to the fallback instead. |
| A client times out during a hold | Raise its request timeout above `hold.maxHours`: OpenCode `options.timeout`, Hermes `request_timeout_seconds` / `stale_timeout_seconds` / `agent.gateway_timeout` (Client templates). |
| T3 Code's effort selector has no effect on Hermes | Something forces the level: remove `extra_body.chat_template_kwargs` from Hermes' provider; on a Mac, update the ACP bridge (`clients\hermes_acp_bridge.py`). |
| T3 Code still lists old model ids | Toggle the provider instance off and on (Client templates > T3 Code notes). |
| Watch speech gets 404 after enabling T3 remote access | T3's `tailscale serve` took over port 443: re-add the `/v1/audio` route (Client templates > T3 Code notes). |

Logs (`logs\`): `supervisor.log` (tier switches, activity), `server-<tier>.err.log` (llama-server), `voice.log`,
`whisper.err.log`, `piper.err.log`, `speechApi.err.log`, `haRelay.err.log`, `gateway.log`, `strata-serve.log`.

## Layout

```
install.ps1 / uninstall.ps1 / status.ps1
config.example.json        -> config.json (your settings)
config.worker.example.json -> config.json on a worker PC (multi-PC)
games.txt, ignore.txt      game detection lists
scripts\homellm.ps1        VRAM tier supervisor (task HomeLLM)
scripts\voice.ps1          voice supervisor (task HomeVoice)
scripts\router.py          multi-PC router + Wake-on-LAN + decision port (task HomeRouter, orchestrator only)
scripts\gateway.py         model gateway: every model/mode behind one API key (task HomeLLM Gateway)
scripts\ha_payload.py      rebuilds homellm_assist's decision payload from live HA (gateway re-warm, eval)
scripts\strata-serve.ps1   serves the big MoE model with Strata or Basalt (strata.engine) for an external tier
                           (task HomeLLM Strata, runs as you)
basalt\                    Basalt (opt-in engine): basalt_home.example.json, build_ada_local.bat,
                           patches\0001-any-model-name.patch
clients\                   opencode.example.json, hermes-config.example.yaml, hermes_acp_bridge.py (T3 Code on
                           other machines -> Hermes over ACP)
eval\run_eval.py           decision accuracy/latency test; cases.example.json to start from
scripts\install-worker.ps1 sets up a worker PC
scripts\speech_api.py      OpenAI-style speech API (watch STT/TTS)
scripts\ha_relay.py        Assist pipeline relay (watch commands)
scripts\patch_piper.py     Windows fix for wyoming-piper
scripts\apollo-stream.ps1  stream start/stop flag
scripts\common.ps1         shared helpers
homeassistant\             HA package template (Jev-mode decisions)
```
