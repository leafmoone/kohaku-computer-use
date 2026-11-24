# kohaku-computer-use

`kohaku-computer-use` adds state-scoped Windows UI Automation and guarded desktop control to a local KohakuTerrarium 2.2.3+ runtime. It reads the semantic accessibility tree first, can capture a window PNG on demand, and uses UI Automation patterns before considering physical input.

## Install

From the package checkout, run `kt install .` using the same KohakuTerrarium installation that runs the local service. Reload the runtime if the package is not yet visible under **Settings → Extensions**. Enable the `computer_use` plugin on each creature that needs the tools. The operator can inspect windows from the package’s **Computer Use** Control Deck without enabling tools on a creature.

Desktop access requires an interactive Windows session. The REST API is available only on a local Terrarium service. This adapter operates a single physical desktop and is not suitable for sharing that desktop with untrusted Terrarium users.

## Operator controls

| Mode | Creature access |
| --- | --- |
| **Locked** | No agent observation or input. Every runtime starts here. |
| **Read only** | List, observe and query windows; no actions. |
| **Confirm** | Ask the operator to approve each action batch once. |
| **Armed** | Run ordinary actions without a prompt. High-risk chords such as `Alt+F4` and `Win+…` and actions on destructive controls still prompt unless the window matches an explicit allowlist pattern. |

The mode, idle timeout, optional foreground input, and case-insensitive denylist and allowlist patterns are configured in **Computer Use** settings. Denylist matches cannot be overridden by the allowlist. Disabling foreground input leaves supported background UI Automation patterns available. Foreground fallback verifies window identity, foreground state, focus when applicable, and occlusion before injecting. Elevated or unverifiable targets cannot receive actions.

**Idle auto-lock is off by default** (`idle_timeout_s: 0`). Turn it on in **Computer Use** settings and choose an inactivity timeout. Turning it off cancels the countdown without changing the current mode.

**Emergency lock:** press **Ctrl+Alt+F12** (configurable Windows global hotkey) or run **Emergency Lock Computer Use** in the app (**Ctrl+Alt+X**). Panic terminates the worker, releases the desktop lease and latches the broker in Locked. Resetting panic leaves it Locked; explicitly select another mode only when it is safe. If the panic hotkey cannot be registered, mutations are blocked. A worker timeout still locks the broker even if idle auto-lock is off. Lock cannot undo input already delivered.

## Creature tools

| Tool | Purpose |
| --- | --- |
| `cu_roots` | List visible top-level windows with `@rN` root references and denylist flags. |
| `cu_observe` | Inspect an `@rN` or HWND (foreground by default). Returns `state_id` and state-scoped `@eN` element references. Set `include_image: true` to request a PNG; known text-only DeepSeek models still get only structured tool output. |
| `cu_query` | Read or filter elements within the creature’s current leased state by text, role or `@eN`. |
| `cu_act` | Run a batch against the current `state_id`, using elements from that exact state or explicit desktop coordinates for clicks. Returns individual outcomes and, when available, a fresh `post_state`. |

Example actions: `{"action":"invoke","ref":"@e4"}`; `{"action":"set_text","ref":"@e3","text":"Hello"}`; `{"action":"scroll","direction":"down","amount":2}`. Add an `expect` object such as `{"text":"Saved","timeout_ms":2000}` to check the result. The action list stops on a blocked step or failed verified outcome. If a result is `unknown`, re-observe before deciding whether to retry; the input may already have reached the application.

The Control Deck retains the latest 16 snapshots and 30 audit entries in memory. Screenshot capture may be unavailable for some windows. Deck frames are read through the local REST API; a screenshot explicitly returned as a visual `cu_observe` tool result can be stored as a session artifact. `cu_act` does not return a visual tool attachment.
