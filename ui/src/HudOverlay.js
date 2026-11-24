import {
  Teleport,
  computed,
  defineComponent,
  h,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  shallowRef,
  watch,
} from "vue"
import { useExtension, useOverlay, useWorkContext } from "@kohakuterrarium/ui-sdk"
import { syncSettingsHostLabels, useI18n } from "./i18n.js"
import { getCuStore } from "./store.js"
import {
  MODES,
  clampPopoverPosition,
  formatDurationSeconds,
  formatTimestamp,
  modeBadgeKey,
  statusTone,
} from "./utils.js"

const DOCK_CAPSULE_SELECTOR =
  '[data-test="v2-dock"] > .kt-v2-float:not([data-test="v2-dock-more-menu"])'
const SETTINGS_NAV_SELECTOR = '[data-section="ext:kohaku-computer-use/preferences"]'
const POPOVER_ESTIMATED_HEIGHT = 380
const POPOVER_WIDTH = 344
const VIEWPORT_MARGIN = 12

function renderMonitorCursorSvg() {
  return h(
    "svg",
    {
      class: "kcu-dock-svg",
      viewBox: "0 0 16 16",
      width: "15",
      height: "15",
      fill: "none",
      "aria-hidden": "true",
    },
    [
      h("rect", {
        x: "1.5",
        y: "2",
        width: "13",
        height: "9.5",
        rx: "1.6",
        stroke: "currentColor",
        "stroke-width": "1.4",
      }),
      h("path", {
        d: "M5.5 14h5M8 11.5V14",
        stroke: "currentColor",
        "stroke-width": "1.35",
        "stroke-linecap": "round",
      }),
      h("path", {
        d: "M6.1 4.2l4.3 3.15-1.9.45.95 1.85-1.05.55-.95-1.85-1.35 1.4V4.2z",
        fill: "currentColor",
      }),
    ],
  )
}

export default defineComponent({
  name: "KcuHudOverlay",
  setup() {
    const kt = useExtension()
    const overlay = useOverlay()
    const store = getCuStore(kt)
    const { locale, t } = useI18n()
    const work = useWorkContext({ retainLast: true })

    overlay.ready(true)
    const stopWorkWatch = store.bindWorkContext(work)

    const dockTarget = shallowRef(null)
    const dockBtnRef = ref(null)
    const popoverRef = ref(null)
    const popoverBox = ref({ left: 16, top: 16, width: POPOVER_WIDTH })

    let slotHost = null
    let domObserver = null
    let syncScheduled = false

    const currentStatus = computed(() => store.state.status)
    const modeValue = computed(() =>
      currentStatus.value && currentStatus.value.mode ? currentStatus.value.mode : "locked",
    )
    const trippedValue = computed(() => Boolean(currentStatus.value && currentStatus.value.tripped))
    const tone = computed(() => statusTone(modeValue.value, trippedValue.value))
    const stateLabel = computed(() => t(modeBadgeKey(modeValue.value, trippedValue.value)))
    const showSafetyPill = computed(
      () =>
        currentStatus.value !== null &&
        (currentStatus.value.mode !== "locked" || currentStatus.value.tripped === true),
    )

    function ensureSlotHost() {
      if (slotHost || typeof document === "undefined") return slotHost
      const el = document.createElement("span")
      el.className = "kcu-dock-slot"
      el.setAttribute("data-test", "kcu-dock-slot")
      slotHost = el
      return slotHost
    }

    function updatePopoverPlacement() {
      if (typeof window === "undefined" || !store.state.popoverOpen) return
      const anchorEl = dockBtnRef.value
      const anchorRect =
        anchorEl && typeof anchorEl.getBoundingClientRect === "function"
          ? anchorEl.getBoundingClientRect()
          : null
      const popEl = popoverRef.value
      const measuredHeight =
        popEl && typeof popEl.getBoundingClientRect === "function"
          ? Math.max(200, popEl.getBoundingClientRect().height)
          : POPOVER_ESTIMATED_HEIGHT
      popoverBox.value = clampPopoverPosition(
        anchorRect,
        POPOVER_WIDTH,
        measuredHeight,
        window.innerWidth,
        window.innerHeight,
        VIEWPORT_MARGIN,
      )
    }

    function syncDockAndLabels() {
      if (typeof document === "undefined") return
      if (document.querySelector(SETTINGS_NAV_SELECTOR)) {
        syncSettingsHostLabels(locale.value, document)
      }
      const capsuleEl = document.querySelector(DOCK_CAPSULE_SELECTOR)
      if (!capsuleEl) {
        if (slotHost && slotHost.parentElement) {
          slotHost.remove()
        }
        if (dockTarget.value !== null) {
          dockTarget.value = null
        }
        if (store.state.popoverOpen) {
          store.setPopoverOpen(false)
        }
        return
      }
      const host = ensureSlotHost()
      if (host && host.parentElement !== capsuleEl) {
        capsuleEl.insertBefore(host, capsuleEl.firstChild || null)
      }
      if (dockTarget.value !== host) {
        dockTarget.value = host
      }
      if (store.state.popoverOpen) {
        void nextTick().then(updatePopoverPlacement)
      }
    }

    function scheduleSync() {
      if (syncScheduled) return
      syncScheduled = true
      queueMicrotask(() => {
        syncScheduled = false
        syncDockAndLabels()
      })
    }

    function onDockBtnClick(event) {
      if (event && typeof event.stopPropagation === "function") {
        event.stopPropagation()
      }
      store.togglePopover()
      if (store.state.popoverOpen) {
        void nextTick().then(updatePopoverPlacement)
      }
    }

    function onDocumentPointerDown(event) {
      if (!store.state.popoverOpen) return
      const target = event ? event.target : null
      if (popoverRef.value && target && popoverRef.value.contains(target)) return
      if (dockBtnRef.value && target && dockBtnRef.value.contains(target)) return
      store.setPopoverOpen(false)
    }

    function onWindowKeyDown(event) {
      if (!store.state.popoverOpen || !event || event.key !== "Escape") return
      store.setPopoverOpen(false)
    }

    function onViewportChange() {
      if (store.state.popoverOpen) {
        updatePopoverPlacement()
      }
    }

    function openControlDeck() {
      store.setPopoverOpen(false)
      kt.openTab("deck")
    }

    const stopLocaleWatch = watch(
      locale,
      (nextLocale) => {
        if (typeof document !== "undefined") {
          syncSettingsHostLabels(nextLocale, document)
        }
      },
      { immediate: true },
    )

    const stopPopoverWatch = watch(
      () => [store.state.popoverOpen, modeValue.value, trippedValue.value],
      ([open]) => {
        if (open) {
          void nextTick().then(updatePopoverPlacement)
        }
      },
    )

    onMounted(() => {
      syncDockAndLabels()
      if (typeof MutationObserver !== "undefined" && typeof document !== "undefined" && document.body) {
        domObserver = new MutationObserver((mutations) => {
          if (
            slotHost &&
            mutations.length > 0 &&
            mutations.every((m) => slotHost.contains(m.target))
          ) {
            return
          }
          scheduleSync()
        })
        domObserver.observe(document.body, { childList: true, subtree: true })
      }
      if (typeof document !== "undefined") {
        document.addEventListener("pointerdown", onDocumentPointerDown, true)
      }
      if (typeof window !== "undefined") {
        window.addEventListener("keydown", onWindowKeyDown, true)
        window.addEventListener("resize", onViewportChange)
        window.addEventListener("scroll", onViewportChange, true)
      }
    })

    onBeforeUnmount(() => {
      stopWorkWatch()
      stopLocaleWatch()
      stopPopoverWatch()
      if (domObserver) {
        domObserver.disconnect()
        domObserver = null
      }
      if (typeof document !== "undefined") {
        document.removeEventListener("pointerdown", onDocumentPointerDown, true)
      }
      if (typeof window !== "undefined") {
        window.removeEventListener("keydown", onWindowKeyDown, true)
        window.removeEventListener("resize", onViewportChange)
        window.removeEventListener("scroll", onViewportChange, true)
      }
      store.setPopoverOpen(false)
      dockTarget.value = null
      if (slotHost && slotHost.parentElement) {
        slotHost.remove()
      }
      slotHost = null
    })

    function renderSafetyPill() {
      if (!showSafetyPill.value) return null
      const status = currentStatus.value
      const lease = status.active_lease
      const contextText = lease
        ? t("pill.leasePrefix", { name: lease.agent_name || lease.creature_id })
        : t("pill.hotkeyHint", { hotkey: status.hotkey || "Ctrl+Alt+F12" })
      const idleText =
        !trippedValue.value && status.idle_remaining_s !== null && status.idle_remaining_s !== undefined
          ? formatDurationSeconds(status.idle_remaining_s)
          : ""

      return h(
        "div",
        {
          class: `kcu-safety-pill kcu-state-${tone.value}`,
          "data-test": "kcu-safety-pill",
          role: "status",
          "aria-live": "polite",
        },
        [
          h("span", { class: "kcu-pill-dot", "aria-hidden": "true" }),
          h("span", { class: "kcu-pill-state" }, stateLabel.value),
          h("span", { class: "kcu-pill-meta" }, contextText),
          idleText ? h("span", { class: "kcu-pill-timer" }, idleText) : null,
          h("div", { class: "kcu-pill-actions" }, [
            trippedValue.value
              ? h(
                  "button",
                  {
                    type: "button",
                    class: "kcu-btn kcu-btn-xs kcu-btn-primary",
                    "data-test": "kcu-pill-reset",
                    disabled: store.state.savingPolicy,
                    onClick: () => void store.resetPanic(),
                  },
                  t("pill.resetPanic"),
                )
              : h(
                  "button",
                  {
                    type: "button",
                    class: "kcu-btn kcu-btn-xs kcu-btn-danger",
                    "data-test": "kcu-pill-lock",
                    disabled: store.state.savingPolicy,
                    onClick: () => void store.setPolicy({ mode: "locked" }),
                  },
                  t("pill.lockNow"),
                ),
            h(
              "button",
              {
                type: "button",
                class: "kcu-btn kcu-btn-xs kcu-btn-ghost",
                "data-test": "kcu-pill-deck",
                onClick: openControlDeck,
              },
              t("pill.openDeck"),
            ),
          ]),
        ],
      )
    }

    function renderPopover() {
      if (!store.state.popoverOpen) return null
      const status = currentStatus.value
      const supported = status ? status.supported !== false : true
      const lease = status && status.active_lease ? status.active_lease : null
      const latestAudit = store.state.audit.length > 0 ? store.state.audit[0] : null
      const idleSummary =
        status && status.idle_timeout_s > 0
          ? status.idle_remaining_s !== null && status.idle_remaining_s !== undefined
            ? `${formatDurationSeconds(status.idle_remaining_s)} / ${status.idle_timeout_s}s`
            : `${status.idle_timeout_s}s`
          : t("popover.idleDisabled")

      return h(
        "div",
        {
          ref: popoverRef,
          class: "kcu-dock-popover",
          "data-test": "kcu-dock-popover",
          role: "dialog",
          "aria-label": t("popover.title"),
          style: {
            left: `${popoverBox.value.left}px`,
            top: `${popoverBox.value.top}px`,
            width: `${popoverBox.value.width}px`,
          },
        },
        [
          h("div", { class: "kcu-popover-head" }, [
            h("span", { class: "kcu-popover-title" }, t("popover.title")),
            h(
              "span",
              {
                class: `kcu-badge kcu-state-${tone.value}`,
                "data-test": "kcu-popover-badge",
              },
              stateLabel.value,
            ),
          ]),
          !supported
            ? h(
                "div",
                { class: "kcu-banner kcu-banner-warn" },
                t("popover.unsupported", { platform: status ? status.platform : "unknown" }),
              )
            : null,
          trippedValue.value
            ? h("div", { class: "kcu-banner kcu-banner-danger" }, [
                h(
                  "span",
                  null,
                  t("popover.trippedBanner", {
                    reason: (status && status.trip_reason) || "emergency_lock",
                  }),
                ),
                h(
                  "button",
                  {
                    type: "button",
                    class: "kcu-btn kcu-btn-xs kcu-btn-primary",
                    "data-test": "kcu-popover-reset",
                    disabled: store.state.savingPolicy,
                    onClick: () => void store.resetPanic(),
                  },
                  t("popover.resetPanic"),
                ),
              ])
            : null,
          h("div", { class: "kcu-popover-section" }, [
            h("div", { class: "kcu-popover-label" }, t("popover.modeLabel")),
            trippedValue.value
              ? h(
                  "div",
                  { class: "kcu-mode-panic-hint", "data-test": "kcu-popover-panic-hint" },
                  t("popover.lockedByPanicHint"),
                )
              : null,
            h(
              "div",
              { class: "kcu-mode-segment", role: "group", "aria-label": t("popover.modeLabel") },
              MODES.map((mode) => {
                const active = !trippedValue.value && modeValue.value === mode
                const disabledTitle = trippedValue.value
                  ? t("popover.lockedByPanicHint")
                  : !supported
                    ? t("popover.unsupported", { platform: status ? status.platform : "unknown" })
                    : null
                return h(
                  "button",
                  {
                    key: mode,
                    type: "button",
                    class: active
                      ? `kcu-mode-btn is-active kcu-state-${mode}`
                      : "kcu-mode-btn",
                    "data-test": `kcu-mode-${mode}`,
                    title: disabledTitle,
                    disabled: trippedValue.value || !supported || store.state.savingPolicy,
                    onClick: () => void store.setPolicy({ mode }),
                  },
                  t(`mode.${mode}`),
                )
              }),
            ),
          ]),
          h("div", { class: "kcu-popover-kv" }, [
            h("div", { class: "kcu-kv-row" }, [
              h("span", { class: "kcu-kv-key" }, t("popover.leaseLabel")),
              h(
                "span",
                { class: "kcu-kv-val" },
                lease
                  ? `${lease.agent_name || lease.creature_id}${lease.target_title ? ` · ${lease.target_title}` : ""}`
                  : t("popover.noLease"),
              ),
            ]),
            h("div", { class: "kcu-kv-row" }, [
              h("span", { class: "kcu-kv-key" }, t("popover.idleLabel")),
              h("span", { class: "kcu-kv-val" }, idleSummary),
            ]),
            h("div", { class: "kcu-kv-row" }, [
              h("span", { class: "kcu-kv-key" }, t("popover.hotkeyLabel")),
              h(
                "span",
                { class: "kcu-kv-val" },
                status
                  ? `${status.hotkey} (${status.hotkey_registered ? t("popover.hotkeyReady") : t("popover.hotkeyUnavailable")})`
                  : "Ctrl+Alt+F12",
              ),
            ]),
            h("div", { class: "kcu-kv-row" }, [
              h("span", { class: "kcu-kv-key" }, t("popover.latestActionLabel")),
              h(
                "span",
                { class: "kcu-kv-val" },
                latestAudit
                  ? `${formatTimestamp(latestAudit.timestamp, locale.value)} · ${latestAudit.summary}`
                  : t("popover.noAction"),
              ),
            ]),
          ]),
          h("div", { class: "kcu-popover-actions" }, [
            h(
              "button",
              {
                type: "button",
                class: "kcu-btn kcu-btn-sm kcu-btn-danger",
                "data-test": "kcu-popover-panic",
                disabled: store.state.savingPolicy,
                onClick: () => void store.triggerPanic("ui_emergency_lock"),
              },
              t("popover.emergencyLock"),
            ),
            h(
              "button",
              {
                type: "button",
                class: "kcu-btn kcu-btn-sm kcu-btn-secondary",
                "data-test": "kcu-popover-deck",
                onClick: openControlDeck,
              },
              t("popover.openDeck"),
            ),
          ]),
        ],
      )
    }

    return () => {
      const btnTitle = t("dock.buttonTitle", { state: stateLabel.value })
      return h("div", { class: "kcu-hud-anchor", "data-test": "kcu-hud-root" }, [
        dockTarget.value
          ? h(Teleport, { to: dockTarget.value }, [
              h(
                "button",
                {
                  ref: dockBtnRef,
                  type: "button",
                  class: `kcu-dock-btn kcu-state-${tone.value}`,
                  "data-test": "kcu-dock-btn",
                  "data-mode": modeValue.value,
                  "data-tripped": String(trippedValue.value),
                  "aria-label": btnTitle,
                  "aria-expanded": String(store.state.popoverOpen),
                  title: btnTitle,
                  onClick: onDockBtnClick,
                },
                [renderMonitorCursorSvg()],
              ),
            ])
          : null,
        showSafetyPill.value
          ? h(Teleport, { to: "body" }, [renderSafetyPill()].filter(Boolean))
          : null,
        store.state.popoverOpen
          ? h(Teleport, { to: "body" }, [renderPopover()].filter(Boolean))
          : null,
      ])
    }
  },
})
