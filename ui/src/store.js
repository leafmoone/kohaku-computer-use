import { reactive, watch } from "vue"
import { notify, useLocale } from "@kohakuterrarium/ui-sdk"
import { tForLocale } from "./i18n.js"
import {
  DEFAULT_CDP_PORTS,
  extractApiError,
  isValidMode,
  normalizeDesktopEffort,
  parsePortsText,
  readStatusField,
} from "./utils.js"

const POLL_INTERVAL_MS = 3000
const WORK_DEBOUNCE_MS = 250
const MAX_STATE_HISTORY = 16
const MAX_AUDIT_ENTRIES = 30

let activeStore = null
let activeKt = null

function activeLocale() {
  try {
    return useLocale().value
  } catch (_err) {
    return "en"
  }
}

export function createCuStore(kt) {
  if (!kt || !kt.api) {
    throw new Error("createCuStore(kt) requires a valid KohakuTerrarium extension handle with kt.api")
  }

  const state = reactive({
    status: null,
    roots: [],
    states: [],
    audit: [],
    selectedStateId: null,
    selectedState: null,
    selectedElementRef: null,
    hoveredElementRef: null,
    popoverOpen: false,
    deckOpenCount: 0,
    settingsOpenCount: 0,
    loadingStatus: false,
    loadingRoots: false,
    loadingObserve: false,
    loadingStates: false,
    loadingModels: false,
    savingPolicy: false,
    models: [],
    desktopModel: "",
    desktopReasoningEffort: "low",
    cdpPorts: [...DEFAULT_CDP_PORTS],
    lastIdleSeconds: 300,
    restoreOnObserve: false,
    error: "",
    lastSyncedAt: 0,
    disposed: false,
  })

  let pollTimer = null
  let workDebounceTimer = null
  let pollInFlight = false
  let statusRequestSeq = 0
  let policyWriteCount = 0
  let panicEpoch = 0
  let userMutatedPolicy = false
  const stopHandles = []

  function beginPolicyWrite() {
    policyWriteCount += 1
    state.savingPolicy = policyWriteCount > 0
  }

  function endPolicyWrite() {
    policyWriteCount = Math.max(0, policyWriteCount - 1)
    state.savingPolicy = policyWriteCount > 0
  }

  function shouldPoll() {
    if (state.disposed) return false
    if (state.popoverOpen || state.deckOpenCount > 0 || state.settingsOpenCount > 0) return true
    return Boolean(state.status && state.status.mode !== "locked")
  }

  function clearTimers() {
    if (pollTimer !== null) {
      clearInterval(pollTimer)
      pollTimer = null
    }
    if (workDebounceTimer !== null) {
      clearTimeout(workDebounceTimer)
      workDebounceTimer = null
    }
  }

  function reconcilePolling() {
    if (shouldPoll()) {
      if (pollTimer === null) {
        pollTimer = setInterval(() => {
          void pollTick()
        }, POLL_INTERVAL_MS)
      }
      return
    }
    if (pollTimer !== null) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  function applyStatus(payload, options) {
    const opts = options && typeof options === "object" ? options : {}
    if (state.disposed || !payload || typeof payload !== "object") return
    const previousLatestId = state.status ? state.status.latest_state_id : null
    const rawIdle = Number(readStatusField(payload, "idle_timeout_s", 0))
    if (Number.isFinite(rawIdle) && rawIdle > 0) {
      state.lastIdleSeconds = Math.round(rawIdle)
    }
    const rawModel = readStatusField(payload, "desktop_model", undefined)
    if (typeof rawModel === "string") {
      state.desktopModel = rawModel
    }
    const rawEffort = readStatusField(payload, "desktop_reasoning_effort", undefined)
    if (typeof rawEffort === "string") {
      state.desktopReasoningEffort = normalizeDesktopEffort(rawEffort)
    }
    const rawPorts = readStatusField(payload, "cdp_ports", undefined)
    if (Array.isArray(rawPorts)) {
      state.cdpPorts = parsePortsText(rawPorts.join(","))
    }
    state.status = payload
    state.lastSyncedAt = Date.now()
    if (opts.clearError === true) {
      state.error = ""
    }
    reconcilePolling()
    if (
      (state.deckOpenCount > 0 || state.popoverOpen) &&
      !state.loadingObserve &&
      !state.loadingStates &&
      payload.latest_state_id &&
      payload.latest_state_id !== previousLatestId
    ) {
      void fetchStates({ silent: true })
    }
  }

  function clearError() {
    state.error = ""
  }

  async function fetchStatus(options) {
    const opts = options && typeof options === "object" ? options : {}
    if (state.disposed) return { ok: false, error: "Store disposed" }
    const reqSeq = ++statusRequestSeq
    state.loadingStatus = true
    try {
      const resp = await kt.api.get("/status")
      if (state.disposed || reqSeq < statusRequestSeq) {
        return { ok: true, data: resp.data }
      }
      applyStatus(resp.data, { clearError: !opts.silent })
      return { ok: true, data: resp.data }
    } catch (err) {
      const msg = extractApiError(err, "GET /status")
      if (!state.disposed) {
        state.error = msg
        if (!opts.silent) notify(msg, "error")
      }
      return { ok: false, error: msg }
    } finally {
      state.loadingStatus = false
    }
  }

  async function persistPolicyPreferences(payload) {
    if (!kt.preferences || typeof kt.preferences.set !== "function") return
    const src = payload && typeof payload === "object" ? payload : {}
    const record = {
      desktop_model:
        typeof readStatusField(src, "desktop_model", undefined) === "string"
          ? readStatusField(src, "desktop_model", "")
          : state.desktopModel,
      desktop_reasoning_effort: normalizeDesktopEffort(
        readStatusField(src, "desktop_reasoning_effort", state.desktopReasoningEffort),
      ),
      cdp_ports: Array.isArray(readStatusField(src, "cdp_ports", undefined))
        ? parsePortsText(readStatusField(src, "cdp_ports", []).join(","))
        : [...state.cdpPorts],
      idle_timeout_s: Number.isFinite(Number(readStatusField(src, "idle_timeout_s", undefined)))
        ? Math.max(0, Math.min(3600, Math.round(Number(readStatusField(src, "idle_timeout_s", 0)))))
        : 0,
      denylist_patterns: Array.isArray(readStatusField(src, "denylist_patterns", undefined))
        ? [...readStatusField(src, "denylist_patterns", [])]
        : [],
    }
    try {
      await kt.preferences.set("policy", record)
    } catch (_err) {
      // Preference persistence is best-effort when storage is unavailable.
    }
  }

  async function fetchModels(options) {
    const opts = options && typeof options === "object" ? options : {}
    if (state.disposed) return { ok: false, error: "Store disposed" }
    state.loadingModels = true
    try {
      const resp = await kt.api.get("/models")
      if (state.disposed) return { ok: true, data: resp.data }
      const list = Array.isArray(resp.data && resp.data.models) ? resp.data.models : []
      state.models = list
      if (!opts.silent) state.error = ""
      return { ok: true, data: list }
    } catch (err) {
      const msg = extractApiError(err, "GET /models")
      if (!state.disposed) {
        state.error = msg
        if (!opts.silent) notify(msg, "error")
      }
      return { ok: false, error: msg }
    } finally {
      state.loadingModels = false
    }
  }

  async function setPolicy(patch) {
    if (state.disposed) return { ok: false, error: "Store disposed" }
    if (!patch || typeof patch !== "object") {
      const msg = "POST /policy: policy patch object is required"
      state.error = msg
      notify(msg, "error")
      return { ok: false, error: msg }
    }
    if (patch.mode !== undefined && !isValidMode(patch.mode)) {
      const msg = `POST /policy: invalid mode "${String(patch.mode)}"`
      state.error = msg
      notify(msg, "error")
      return { ok: false, error: msg }
    }
    userMutatedPolicy = true
    state.error = ""
    beginPolicyWrite()
    statusRequestSeq += 1
    const startPanicEpoch = panicEpoch
    try {
      const resp = await kt.api.post("/policy", patch)
      statusRequestSeq += 1
      if (state.disposed || panicEpoch !== startPanicEpoch) {
        return { ok: false, error: "Superseded by emergency lock", data: resp.data }
      }
      applyStatus(resp.data, { clearError: true })
      if (
        patch.desktop_model !== undefined ||
        patch.desktop_reasoning_effort !== undefined ||
        patch.cdp_ports !== undefined ||
        patch.idle_timeout_s !== undefined ||
        patch.denylist_patterns !== undefined
      ) {
        void persistPolicyPreferences(resp.data)
      }
      if (patch.mode !== undefined) {
        const loc = activeLocale()
        const actualMode = resp.data && resp.data.mode ? resp.data.mode : "locked"
        const isTripped = Boolean(resp.data && resp.data.tripped)
        if (patch.mode === "locked" && actualMode === "locked") {
          notify(tForLocale(loc, "toast.lockedNow"), "info")
        } else if (actualMode === patch.mode && !isTripped) {
          const modeText = tForLocale(loc, `mode.${actualMode}`)
          notify(tForLocale(loc, "toast.modeUpdated", { mode: modeText }), "success")
        } else {
          const requestedText = tForLocale(loc, `mode.${patch.mode}`)
          const actualKey = isTripped ? "mode.tripped" : `mode.${actualMode}`
          const actualText = tForLocale(loc, actualKey)
          const rejectMsg = tForLocale(loc, "toast.modeSwitchRejected", {
            requested: requestedText,
            actual: actualText,
          })
          state.error = rejectMsg
          notify(rejectMsg, "warning")
          return { ok: false, error: rejectMsg, data: resp.data }
        }
      }
      return { ok: true, data: resp.data }
    } catch (err) {
      const msg = extractApiError(err, "POST /policy")
      state.error = msg
      notify(msg, "error")
      return { ok: false, error: msg }
    } finally {
      endPolicyWrite()
    }
  }

  async function toggleIdleAutoLock(enabled) {
    const wantEnabled = Boolean(enabled)
    const fallbackSeconds = state.lastIdleSeconds > 0 ? state.lastIdleSeconds : 300
    const nextTimeout = wantEnabled ? fallbackSeconds : 0
    const res = await setPolicy({ idle_timeout_s: nextTimeout })
    if (res && res.ok) {
      const loc = activeLocale()
      const msg = wantEnabled
        ? tForLocale(loc, "toast.idleEnabled", { seconds: nextTimeout })
        : tForLocale(loc, "toast.idleDisabled")
      notify(msg, "info")
    }
    return res
  }

  async function triggerPanic(reason) {
    if (state.disposed) return { ok: false, error: "Store disposed" }
    const safeReason = reason ? String(reason) : "ui_panic"
    state.error = ""
    beginPolicyWrite()
    panicEpoch += 1
    statusRequestSeq += 1
    try {
      const resp = await kt.api.post("/panic", { reason: safeReason })
      panicEpoch += 1
      statusRequestSeq += 1
      applyStatus(resp.data, { clearError: true })
      notify(tForLocale(activeLocale(), "toast.panicTriggered"), "warning")
      if (state.deckOpenCount > 0 || state.popoverOpen) {
        void fetchStates({ silent: true })
      }
      return { ok: true, data: resp.data }
    } catch (err) {
      const msg = extractApiError(err, "POST /panic")
      state.error = msg
      notify(msg, "error")
      return { ok: false, error: msg }
    } finally {
      endPolicyWrite()
    }
  }

  async function resetPanic() {
    if (state.disposed) return { ok: false, error: "Store disposed" }
    state.error = ""
    beginPolicyWrite()
    statusRequestSeq += 1
    const startPanicEpoch = panicEpoch
    try {
      const resp = await kt.api.post("/panic/reset", {})
      statusRequestSeq += 1
      if (state.disposed || panicEpoch !== startPanicEpoch) {
        return { ok: false, error: "Superseded by emergency lock", data: resp.data }
      }
      applyStatus(resp.data, { clearError: true })
      notify(tForLocale(activeLocale(), "toast.panicReset"), "info")
      if (state.deckOpenCount > 0 || state.popoverOpen) {
        void fetchStates({ silent: true })
      }
      return { ok: true, data: resp.data }
    } catch (err) {
      const msg = extractApiError(err, "POST /panic/reset")
      state.error = msg
      notify(msg, "error")
      return { ok: false, error: msg }
    } finally {
      endPolicyWrite()
    }
  }

  async function fetchRoots(options) {
    const opts = options && typeof options === "object" ? options : {}
    if (state.disposed) return { ok: false, error: "Store disposed" }
    state.loadingRoots = true
    try {
      const resp = await kt.api.get("/roots")
      if (state.disposed) return { ok: true, data: resp.data }
      const list = Array.isArray(resp.data && resp.data.roots) ? resp.data.roots : []
      state.roots = list
      if (!opts.silent) state.error = ""
      return { ok: true, data: list }
    } catch (err) {
      const msg = extractApiError(err, "GET /roots")
      if (!state.disposed) {
        state.error = msg
        if (!opts.silent) notify(msg, "error")
      }
      return { ok: false, error: msg }
    } finally {
      state.loadingRoots = false
    }
  }

  async function selectState(stateId, options) {
    const opts = options && typeof options === "object" ? options : {}
    if (state.disposed) return { ok: false, error: "Store disposed" }
    if (!stateId) {
      state.selectedStateId = null
      state.selectedState = null
      state.selectedElementRef = null
      return { ok: true, data: null }
    }
    const targetId = String(stateId)
    state.selectedStateId = targetId
    try {
      const resp = await kt.api.get(`/state/${encodeURIComponent(targetId)}`)
      if (state.disposed || state.selectedStateId !== targetId) {
        return { ok: true, data: resp.data }
      }
      state.selectedState = resp.data
      if (state.selectedElementRef) {
        const exists = Array.isArray(resp.data && resp.data.elements)
          ? resp.data.elements.some((el) => el.ref === state.selectedElementRef)
          : false
        if (!exists) state.selectedElementRef = null
      }
      if (!opts.silent) state.error = ""
      return { ok: true, data: resp.data }
    } catch (err) {
      const msg = extractApiError(err, `GET /state/${targetId}`)
      if (!state.disposed) {
        state.error = msg
        if (!opts.silent) notify(msg, "error")
      }
      return { ok: false, error: msg }
    }
  }

  async function fetchStates(options) {
    const opts = options && typeof options === "object" ? options : {}
    if (state.disposed) return { ok: false, error: "Store disposed" }
    state.loadingStates = true
    try {
      const resp = await kt.api.get("/states")
      if (state.disposed) return { ok: true, data: resp.data }
      const statesList = Array.isArray(resp.data && resp.data.states)
        ? resp.data.states.slice(0, MAX_STATE_HISTORY)
        : []
      const auditList = Array.isArray(resp.data && resp.data.audit)
        ? resp.data.audit.slice(0, MAX_AUDIT_ENTRIES)
        : []
      state.states = statesList
      state.audit = auditList
      if (!opts.silent) state.error = ""

      if (statesList.length === 0) {
        state.selectedStateId = null
        state.selectedState = null
        state.selectedElementRef = null
      } else {
        const currentSummary = state.selectedStateId
          ? statesList.find((item) => item.state_id === state.selectedStateId)
          : null
        const targetSummary = currentSummary || statesList[0]
        const needsDetail =
          !state.selectedState ||
          state.selectedState.state_id !== targetSummary.state_id ||
          state.selectedState.valid_for_act !== targetSummary.valid_for_act ||
          state.selectedState.has_image !== targetSummary.has_image ||
          JSON.stringify(state.selectedState.last_action_point || null) !==
            JSON.stringify(targetSummary.last_action_point || null)
        if (needsDetail) {
          await selectState(targetSummary.state_id, { silent: opts.silent })
        }
      }
      return { ok: true, data: resp.data }
    } catch (err) {
      const msg = extractApiError(err, "GET /states")
      if (!state.disposed) {
        state.error = msg
        if (!opts.silent) notify(msg, "error")
      }
      return { ok: false, error: msg }
    } finally {
      state.loadingStates = false
    }
  }

  async function observeTarget(rootRef, includeImage) {
    if (state.disposed) return { ok: false, error: "Store disposed" }
    const trimmedRef = rootRef ? String(rootRef) : null
    const matchedRoot = trimmedRef
      ? state.roots.find((item) => item && item.root_ref === trimmedRef)
      : null
    const payload = {
      root_ref: trimmedRef,
      include_image: Boolean(includeImage),
      restore: Boolean(state.restoreOnObserve),
    }
    if (matchedRoot && matchedRoot.kind !== "cdp" && Number(matchedRoot.hwnd) > 0) {
      payload.hwnd = Number(matchedRoot.hwnd)
    }
    state.loadingObserve = true
    try {
      const resp = await kt.api.post("/observe", payload)
      if (state.disposed) return { ok: true, data: resp.data }
      state.selectedState = resp.data
      state.selectedStateId = resp.data ? resp.data.state_id : null
      state.selectedElementRef = null
      state.error = ""
      await Promise.all([fetchStatus({ silent: true }), fetchStates({ silent: true })])
      return { ok: true, data: resp.data }
    } catch (err) {
      const msg = extractApiError(err, "POST /observe")
      if (!state.disposed) {
        state.error = msg
        notify(msg, "error")
      }
      return { ok: false, error: msg }
    } finally {
      state.loadingObserve = false
    }
  }

  async function pollTick() {
    if (state.disposed || pollInFlight || !shouldPoll()) return
    if (typeof document !== "undefined" && document.hidden) return
    pollInFlight = true
    try {
      await fetchStatus({ silent: true })
      if (!state.disposed && state.deckOpenCount > 0) {
        await fetchStates({ silent: true })
      }
    } finally {
      pollInFlight = false
    }
  }

  function scheduleWorkRefresh() {
    if (state.disposed) return
    if (workDebounceTimer !== null) {
      clearTimeout(workDebounceTimer)
    }
    workDebounceTimer = setTimeout(() => {
      workDebounceTimer = null
      if (state.disposed) return
      void fetchStatus({ silent: true })
      if (state.deckOpenCount > 0) {
        void fetchStates({ silent: true })
      }
    }, WORK_DEBOUNCE_MS)
  }

  function bindWorkContext(workRef) {
    if (!workRef) return () => {}
    const stop = watch(
      () => {
        const val = workRef.value
        if (!val) return "none|0|none|none"
        return `${val.sessionId || ""}|${val.busy ? "1" : "0"}|${val.creatureId || ""}|${val.status || ""}`
      },
      (nextKey, prevKey) => {
        if (state.disposed || !prevKey) return
        const prevParts = String(prevKey).split("|")
        const nextParts = String(nextKey).split("|")
        const busyFinished = prevParts[1] === "1" && nextParts[1] === "0"
        const targetChanged = prevParts[0] !== nextParts[0] || prevParts[2] !== nextParts[2]
        if (busyFinished || targetChanged) {
          scheduleWorkRefresh()
        }
      },
    )
    stopHandles.push(stop)
    return () => {
      stop()
      const idx = stopHandles.indexOf(stop)
      if (idx >= 0) stopHandles.splice(idx, 1)
    }
  }

  function setPopoverOpen(open) {
    const next = Boolean(open)
    if (state.popoverOpen === next) return
    state.popoverOpen = next
    reconcilePolling()
    if (next) {
      void fetchStatus({ silent: true })
      void fetchStates({ silent: true })
    }
  }

  function togglePopover() {
    setPopoverOpen(!state.popoverOpen)
  }

  function enterDeck() {
    state.deckOpenCount += 1
    reconcilePolling()
    void fetchStatus({ silent: true })
    void fetchRoots({ silent: true })
    void fetchStates({ silent: true })
  }

  function leaveDeck() {
    state.deckOpenCount = Math.max(0, state.deckOpenCount - 1)
    reconcilePolling()
  }

  function enterSettings() {
    state.settingsOpenCount += 1
    reconcilePolling()
    void fetchStatus({ silent: true })
  }

  function leaveSettings() {
    state.settingsOpenCount = Math.max(0, state.settingsOpenCount - 1)
    reconcilePolling()
  }

  function setSelectedElement(ref) {
    state.selectedElementRef = ref ? String(ref) : null
  }

  function setHoveredElement(ref) {
    state.hoveredElementRef = ref ? String(ref) : null
  }

  function setRestoreOnObserve(enabled) {
    state.restoreOnObserve = Boolean(enabled)
  }

  function frameUrl(stateId, hostUrl) {
    if (!stateId) return ""
    const pkg = encodeURIComponent(kt.package || "kohaku-computer-use")
    const base = hostUrl ? String(hostUrl).replace(/\/+$/, "") : ""
    return `${base}/api/ext/${pkg}/frame/${encodeURIComponent(String(stateId))}`
  }

  function onFocusWake() {
    if (state.disposed) return
    void fetchStatus({ silent: true })
    if (state.deckOpenCount > 0) {
      void fetchStates({ silent: true })
    }
  }

  function onVisibilityWake() {
    if (state.disposed || (typeof document !== "undefined" && document.hidden)) return
    onFocusWake()
  }

  if (typeof window !== "undefined") {
    window.addEventListener("focus", onFocusWake)
    stopHandles.push(() => window.removeEventListener("focus", onFocusWake))
  }
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", onVisibilityWake)
    stopHandles.push(() => document.removeEventListener("visibilitychange", onVisibilityWake))
  }

  function dispose() {
    if (state.disposed) return
    state.disposed = true
    state.popoverOpen = false
    state.deckOpenCount = 0
    state.settingsOpenCount = 0
    policyWriteCount = 0
    state.savingPolicy = false
    clearTimers()
    while (stopHandles.length > 0) {
      const stop = stopHandles.pop()
      try {
        stop()
      } catch (_err) {
        // Ignore cleanup errors during teardown.
      }
    }
  }

  async function initStore() {
    await fetchStatus({ silent: true })
    if (state.disposed) return
    if (kt.preferences && typeof kt.preferences.get === "function") {
      try {
        const saved = await kt.preferences.get("policy", null)
        if (saved && typeof saved === "object" && !state.disposed && !userMutatedPolicy) {
          const syncPatch = {}
          if (typeof saved.desktop_model === "string" && saved.desktop_model !== state.desktopModel) {
            syncPatch.desktop_model = saved.desktop_model
            state.desktopModel = saved.desktop_model
          }
          if (typeof saved.desktop_reasoning_effort === "string") {
            const effort = normalizeDesktopEffort(saved.desktop_reasoning_effort)
            if (effort !== state.desktopReasoningEffort) {
              syncPatch.desktop_reasoning_effort = effort
              state.desktopReasoningEffort = effort
            }
          }
          if (Array.isArray(saved.cdp_ports)) {
            const ports = parsePortsText(saved.cdp_ports.join(","))
            if (JSON.stringify(ports) !== JSON.stringify(state.cdpPorts)) {
              syncPatch.cdp_ports = ports
              state.cdpPorts = ports
            }
          }
          if (Number.isFinite(Number(saved.idle_timeout_s))) {
            const sec = Math.max(0, Math.min(3600, Math.round(Number(saved.idle_timeout_s))))
            const currentSec = Number(readStatusField(state.status, "idle_timeout_s", 0))
            if (sec !== currentSec) {
              syncPatch.idle_timeout_s = sec
            }
          }
          if (Array.isArray(saved.denylist_patterns)) {
            const currentDeny = readStatusField(state.status, "denylist_patterns", [])
            if (JSON.stringify(saved.denylist_patterns) !== JSON.stringify(currentDeny)) {
              syncPatch.denylist_patterns = saved.denylist_patterns
            }
          }
          if (Object.keys(syncPatch).length > 0 && !state.disposed) {
            await setPolicy(syncPatch)
          }
        }
      } catch (_err) {
        // Ignore preference hydration errors on startup.
      }
    }
    if (!state.disposed) {
      void fetchModels({ silent: true })
    }
  }

  void initStore()

  return {
    kt,
    state,
    clearError,
    fetchStatus,
    fetchModels,
    setPolicy,
    toggleIdleAutoLock,
    triggerPanic,
    resetPanic,
    fetchRoots,
    observeTarget,
    fetchStates,
    selectState,
    frameUrl,
    bindWorkContext,
    setPopoverOpen,
    togglePopover,
    enterDeck,
    leaveDeck,
    enterSettings,
    leaveSettings,
    setSelectedElement,
    setHoveredElement,
    setRestoreOnObserve,
    isPollingActive: () => pollTimer !== null,
    dispose,
  }
}

export function initCuStore(kt) {
  if (activeStore && activeKt === kt && !activeStore.state.disposed) {
    return activeStore
  }
  if (activeStore) {
    activeStore.dispose()
  }
  activeKt = kt
  activeStore = createCuStore(kt)
  return activeStore
}

export function getCuStore(kt) {
  if (activeStore && (!kt || activeKt === kt) && !activeStore.state.disposed) {
    return activeStore
  }
  if (kt) {
    return initCuStore(kt)
  }
  return activeStore
}

export function disposeCuStore(kt) {
  if (!activeStore) return
  if (kt && activeKt !== kt) return
  activeStore.dispose()
  activeStore = null
  activeKt = null
}
