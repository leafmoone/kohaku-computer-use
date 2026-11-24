# kohaku-computer-use

`kohaku-computer-use` adds state-scoped Windows UI Automation and guarded desktop control to a local KohakuTerrarium 2.2.3+ runtime. It reads native UI Automation trees and local Chrome DevTools Protocol (CDP) pages, with optional PNG capture and physical input only when needed.

## Install

From the package checkout, run `kt install .` using the same KohakuTerrarium installation that runs the local service. Reload the runtime if the package is not yet visible under **Settings → Extensions**. Enable the `computer_use` plugin on each creature that needs the tools. The operator can inspect windows from the package’s **Computer Use** Control Deck without enabling tools on a creature.

Desktop access requires an interactive Windows session. The REST API is available only on a local Terrarium service. This adapter operates a single physical desktop and is not suitable for sharing that desktop with untrusted Terrarium users.

## Operator controls

| Mode | Creature access |
| --- | --- |
| **Locked** | No agent observation or input. Every runtime starts here. |
| **Read only** | List, observe and query windows; no `cu_act`. An observation can restore a minimized window unless `restore: false` is set. |
| **Confirm** | Ask the operator to approve each action batch once. |
| **Armed** | Run ordinary actions without a prompt. High-risk chords such as `Alt+F4` and `Win+…` and actions on destructive controls still prompt unless the window matches an explicit allowlist pattern. |

The mode, idle timeout, optional foreground input, and case-insensitive denylist and allowlist patterns are configured in **Computer Use** settings. Denylist matches cannot be overridden by the allowlist. Disabling foreground input leaves supported background UI Automation patterns available. Foreground fallback verifies window identity, foreground state, focus when applicable, and occlusion before injecting. Elevated or unverifiable targets cannot receive actions.

**Idle auto-lock is off by default** (`idle_timeout_s: 0`). Turn it on in **Computer Use** settings and choose an inactivity timeout. Turning it off cancels the countdown without changing the current mode.

**Emergency lock:** press **Ctrl+Alt+F12** (configurable Windows global hotkey) or run **Emergency Lock Computer Use** in the app (**Ctrl+Alt+X**). Panic terminates the worker, releases the desktop lease and latches the broker in Locked. Resetting panic leaves it Locked; explicitly select another mode only when it is safe. If the panic hotkey cannot be registered, mutations are blocked. A worker timeout still locks the broker even if idle auto-lock is off. Lock cannot undo input already delivered.

## Fast desktop operator

Install `@kt-biome` if it is not already installed, then select `@kohaku-computer-use/creatures/cu_operator` as a creature configuration. It uses low reasoning effort and a small tool set. From another creature with `computer_use` enabled, use `cu_delegate` for a self-contained GUI task without sending the parent conversation to the sub-agent. Set **Desktop model** and **Reasoning effort** in Computer Use settings. If no desktop model is selected, delegation uses the plugin’s optional `fast_model`, then an isolated fork of the caller’s model.

For direct control, start with `cu_observe(query="Notepad")` and pass its `state_id` to a single `cu_act` with the related click, type, and key steps. A native click or invoke gets a brief settling pause before the next action; use `wait_ms` (0–10,000 per step) for longer waits. Agent tools return compact Markdown-friendly outlines and action diffs with the successor state’s `@eN` references. The Control Deck still receives full JSON.

## Browser pages and Jupyter

Computer Use discovers CDP pages on configured **loopback** ports (default `9222, 9229`; use an empty list to disable CDP). Use `cu_observe(url="http://localhost:8888/lab")` to attach to an existing tab, or pick its `@rN` from `cu_roots`. Existing tabs can be read in Read only mode. Opening a new tab or launching Edge/Chrome requires foreground-input permission and either Confirm approval or Armed mode. Auto-launch uses a separate CDP browser profile under the local KohakuTerrarium app-data directory. Only configure trusted local debugging endpoints: CDP provides broad access to the connected browser profile. Studio and embedded frames are not CDP input targets.

CDP actions do not move the Windows cursor or activate the browser window. For a notebook, batch a cell click, `set_text` with exact multiline code, and `key: "Shift+Enter"` with `wait_ms: 800` to see output in the successor diff. Screenshots are opt-in on CDP pages. A page can still react to DOM focus, scrolling, keystrokes, or clicks as normal.

For native Windows input, multi-character and Unicode typing uses a temporary clipboard paste, then restores the previous Unicode text. This does not preserve other clipboard formats; applications that defer reading the clipboard may need a different input method. A forced worker termination can interrupt restoration.

## Creature tools

| Tool | Purpose |
| --- | --- |
| `cu_roots` | List visible windows and local CDP pages with `@rN` root references and denylist flags. |
| `cu_observe` | Find a window by title or executable substring (`query`), `@rN`, or HWND, or a browser page by `url`; defaults to the foreground window. Restores non-elevated minimized windows by default (`restore: false` opts out). Returns a `state_id` and state-scoped `@eN` references. A PNG is requested by `include_image: true` or captured automatically for sparse UIA trees. Known text-only DeepSeek models only receive structured output. |
| `cu_query` | Read or filter elements within the creature’s current leased state by text, role or `@eN`. |
| `cu_act` | Run a batch against the current `state_id`, using elements from that exact state or explicit desktop coordinates for clicks. When typing or pressing keys without an element reference, input goes to the verified target window’s current focus. Returns individual outcomes and, when available, a fresh `post_state`. |
| `cu_delegate` | Run a bounded GUI task in a fresh sub-agent with the same desktop lease and approval rules. `max_steps` defaults to 6. |

Example actions: `{"action":"invoke","ref":"@e4"}`; `{"action":"set_text","ref":"@e3","text":"Hello"}`; `{"action":"scroll","direction":"down","amount":2}`. Add an `expect` object such as `{"text":"Saved","timeout_ms":2000}` to check the result. The action list stops on a blocked step or failed verified outcome. If a result is `unknown`, re-observe before deciding whether to retry; the input may already have reached the application.

Manual Control Deck observations do not restore minimized windows unless explicitly requested. The Control Deck retains the latest 16 snapshots and 30 audit entries in memory. Screenshot capture may be unavailable for some windows. Deck frames are read through the local REST API; a screenshot explicitly returned as a visual `cu_observe` tool result can be stored as a session artifact. Direct `cu_act` calls return compact text only; `cu_delegate` can use a successor screenshot internally when its model supports images.
