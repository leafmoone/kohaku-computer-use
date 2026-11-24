# kohaku-computer-use

`kohaku-computer-use` adds state-scoped Windows UI Automation and guarded desktop control to a local KohakuTerrarium 2.2.3+ runtime. It reads the semantic accessibility tree first, can capture a window PNG on demand, and uses UI Automation patterns before considering physical input.

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

Install `@kt-biome` if it is not already installed, then select `@kohaku-computer-use/creatures/cu_operator` as a creature configuration. It uses low reasoning effort and a small tool set. From another creature with `computer_use` enabled, use `cu_delegate` for a self-contained GUI task without sending the parent conversation to the sub-agent. Set the plugin’s optional `fast_model` to use a different model profile for delegation.

For direct control, start with `cu_observe(query="Notepad")` and pass its `state_id` to a single `cu_act` with the related click, type, and key steps. A click or invoke gets a brief settling pause before the next action; use `wait_ms` to adjust it when needed.

## Creature tools

| Tool | Purpose |
| --- | --- |
| `cu_roots` | List visible top-level windows with `@rN` root references and denylist flags. |
| `cu_observe` | Find a window by title or executable substring (`query`), `@rN`, or HWND; defaults to the foreground. Restores non-elevated minimized windows by default (`restore: false` opts out). Returns a `state_id` and state-scoped `@eN` references. A PNG is requested by `include_image: true` or captured automatically for sparse UIA trees. Known text-only DeepSeek models only receive structured output. |
| `cu_query` | Read or filter elements within the creature’s current leased state by text, role or `@eN`. |
| `cu_act` | Run a batch against the current `state_id`, using elements from that exact state or explicit desktop coordinates for clicks. When typing or pressing keys without an element reference, input goes to the verified target window’s current focus. Returns individual outcomes and, when available, a fresh `post_state`. |
| `cu_delegate` | Run a bounded GUI task in a fresh sub-agent with the same desktop lease and approval rules. `max_steps` defaults to 6. |

Example actions: `{"action":"invoke","ref":"@e4"}`; `{"action":"set_text","ref":"@e3","text":"Hello"}`; `{"action":"scroll","direction":"down","amount":2}`. Add an `expect` object such as `{"text":"Saved","timeout_ms":2000}` to check the result. The action list stops on a blocked step or failed verified outcome. If a result is `unknown`, re-observe before deciding whether to retry; the input may already have reached the application.

Manual Control Deck observations do not restore minimized windows unless explicitly requested. The Control Deck retains the latest 16 snapshots and 30 audit entries in memory. Screenshot capture may be unavailable for some windows. Deck frames are read through the local REST API; a screenshot explicitly returned as a visual `cu_observe` tool result can be stored as a session artifact. Direct `cu_act` calls return structured output only; `cu_delegate` can use a successor screenshot internally when its model supports images.
