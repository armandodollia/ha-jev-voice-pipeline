# Home LLM GPU kit

Run a local LLM and voice stack for Home Assistant on a Windows gaming PC with an NVIDIA GPU, and use it from
Home Assistant's Assist and from a Pebble watch, at home or away (over Tailscale). The model shrinks
automatically while you game or stream, so the GPU stays usable.

| Piece | What it does | Port |
|---|---|---|
| **llama-server** ([thecodacus/llama.cpp `parallel-decision`](https://github.com/thecodacus/llama.cpp/tree/parallel-decision)) | OpenAI-compatible chat API with tool calling for Assist, plus Jev-mode `POST /v1/decision` (answers enum/boolean questions in one forward pass, with probabilities) | 8080 |
| **Model switching supervisor** | Keeps one model loaded under a fixed name (`home`) and swaps it by what the PC is doing | – |
| **Whisper** (Wyoming, GPU) | Speech-to-text for Assist, biased toward your Home Assistant device names | 10300 |
| **Piper** (Wyoming, CPU) | Text-to-speech for Assist | 10200 |
| **Speech API** | OpenAI-style `/v1/audio/transcriptions` + `/v1/audio/speech` in front of Whisper/Piper, for the watch | 10310 |
| **Assist relay** | Runs Home Assistant `/api/conversation/process` calls and OpenAI `/v1/chat/completions` calls through your **Assist pipeline**, for apps that can't choose an agent (Wristotle's HA commands and Ask Agent) | 10320 (localhost) |

Default modes (edit in `config.json`):

| Mode | When | Model | VRAM (measured, RTX 4090) |
|---|---|---|---|
| `full` | idle | Gemma 4 12B UD-Q4_K_XL | ~10 GB |
| `stream` | Apollo/Sunshine stream running | Gemma 4 12B (same model, no reload) | ~10 GB |
| `game` | a game is running locally | Qwen3.5 4B UD-Q4_K_XL | ~4.2 GB |

Whisper large-v3-turbo adds ~2.5 GB and stays loaded. Smaller models load immediately; bigger ones wait until
the game/stream has been gone for 90 s. A swap interrupts the LLM for ~2–6 s.

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
.\status.ps1              # mode, service health, tailnet URLs, recent switches
.\status.ps1 -Mode game   # force a mode; -Mode auto returns to automatic switching
.\uninstall.ps1           # remove tasks, firewall rules, hooks, tailnet publishing (keeps files)
```

Give the PC a **DHCP reservation** in your router so its LAN address doesn't change.

### Main settings (`config.json`)

| Setting | Default | Notes |
|---|---|---|
| `homeAssistant.url` | `http://homeassistant.local:8123` | LAN address of HA (used by Whisper name hints and the Assist relay) |
| `llm.modes.<mode>.repo/file` | Gemma 4 12B / Qwen3.5 4B | any GGUF on Hugging Face; `ctx`, `decisionSeqs`, `chatTemplateKwargs`, `extraArgs` per mode |
| `llm.modes.<mode>.chatTemplateKwargs` | `{"enable_thinking": false}` | keep thinking off for voice (thinking makes replies ~4x slower) |
| `llm.upgradeDelaySec` | 90 | wait before loading a bigger model again |
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

### 4.7 Try it
Press the watch's dictation button and say *"Home assistant, turn on the kitchen light."*
On the PC, `logs\speechApi.err.log` shows what Whisper heard and `logs\haRelay.err.log` shows the command and
Home Assistant's reply.

---

## Game detection

A game counts as running when Steam reports a running app, or when a process runs from a Steam library,
`C:\Program Files\Epic Games`, `C:\Program Files\EA Games`, Ubisoft's `games` folder or `C:\XboxGames`.
Add other games (e.g. Battle.net) by exe name to `games.txt`; exclude false positives (Wallpaper Engine is already
excluded) in `ignore.txt`. Stream mode comes from an Apollo/Sunshine prep command the installer adds (existing
prep commands are kept; a backup is written next to the config).

## Security notes

- Ports are open to the **local subnet** and, with `tailscale.allowTailnetFirewall`, the **tailnet**, never the
  internet. Anyone on those networks can use the LLM and voice services; restrict tailnet access with a grant
  (see section 2) if you share your tailnet.
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
| Stuck in game mode | `.\status.ps1` shows what triggered it; add the exe to `ignore.txt`. |
| Wristotle "network failure" | Use the `https://….ts.net` URLs, check Tailscale is connected on the phone, turn off Private DNS. |
| Wristotle "access token rejected" | Wrong token, or created by a different user. |
| `tailscale serve` failed | Enable MagicDNS and HTTPS certificates in the Tailscale admin console, then re-run the installer. |

Logs (`logs\`): `supervisor.log` (mode switches), `server-<mode>.err.log` (llama-server), `voice.log`,
`whisper.err.log`, `piper.err.log`, `speechApi.err.log`, `haRelay.err.log`.

## Layout

```
install.ps1 / uninstall.ps1 / status.ps1
config.example.json        -> config.json (your settings)
games.txt, ignore.txt      game detection lists
scripts\homellm.ps1        model-switching supervisor (task HomeLLM)
scripts\voice.ps1          voice supervisor (task HomeVoice)
scripts\speech_api.py      OpenAI-style speech API (watch STT/TTS)
scripts\ha_relay.py        Assist pipeline relay (watch commands)
scripts\patch_piper.py     Windows fix for wyoming-piper
scripts\apollo-stream.ps1  stream start/stop flag
scripts\common.ps1         shared helpers
homeassistant\             HA package template (Jev-mode decisions)
```
