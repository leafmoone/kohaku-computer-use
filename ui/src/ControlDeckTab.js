import { computed, defineComponent, h, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { useExtension, useHost } from "@kohakuterrarium/ui-sdk"
import { useI18n } from "./i18n.js"
import { getCuStore } from "./store.js"
import {
  MODES,
  buildActionPointMarker,
  buildElementViewBoxPoints,
  filterElements,
  formatCoordRect,
  formatDurationSeconds,
  formatTimestamp,
  modeBadgeKey,
  outcomeTone,
  statusTone,
} from "./utils.js"

function formatRootOptionLabel(root, t) {
  const dpiPct = Math.round((Number(root.dpi_scale) || 1) * 100)
  const titleText = root.title ? String(root.title) : "(untitled)"
  const tags = []
  if (root.is_foreground) tags.push(`[${t("deck.tagForeground")}]`)
  if (root.is_minimized) tags.push(`[${t("deck.tagMinimized")}]`)
  if (root.is_elevated) tags.push(`[${t("deck.tagElevated")}]`)
  if (root.denylisted) tags.push(`[${t("deck.tagDenylisted")}]`)
  const tagSuffix = tags.length > 0 ? ` ${tags.join(" ")}` : ""
  return `${root.root_ref} · ${root.exe_name} · ${titleText} · ${dpiPct}%${tagSuffix}`
}

export default defineComponent({
  name: "KcuControlDeckTab",
  setup() {
    const kt = useExtension()
    const host = useHost()
    const store = getCuStore(kt)
    const { locale, t } = useI18n()

    const selectedRootRef = ref("")
    const elementFilter = ref("")
    const imageLoadFailed = ref(false)

    const currentStatus = computed(() => store.state.status)
    const modeValue = computed(() =>
      currentStatus.value && currentStatus.value.mode ? currentStatus.value.mode : "locked",
    )
    const trippedValue = computed(() => Boolean(currentStatus.value && currentStatus.value.tripped))
    const supportedValue = computed(() =>
      currentStatus.value ? currentStatus.value.supported !== false : true,
    )
    const tone = computed(() => statusTone(modeValue.value, trippedValue.value))
    const stateLabel = computed(() => t(modeBadgeKey(modeValue.value, trippedValue.value)))

    const selectedRoot = computed(() => {
      if (!selectedRootRef.value) return null
      return store.state.roots.find((item) => item.root_ref === selectedRootRef.value) || null
    })

    const selectedState = computed(() => store.state.selectedState)
    const allElements = computed(() =>
      selectedState.value && Array.isArray(selectedState.value.elements)
        ? selectedState.value.elements
        : [],
    )
    const filteredElements = computed(() => filterElements(allElements.value, elementFilter.value))

    const selectedElement = computed(() => {
      if (!store.state.selectedElementRef) return null
      return allElements.value.find((el) => el.ref === store.state.selectedElementRef) || null
    })

    const svgBoxes = computed(() => {
      const detail = selectedState.value
      if (!detail || !detail.has_image || !detail.image_width || !detail.image_height) return []
      return buildElementViewBoxPoints(allElements.value, detail.image_width, detail.image_height)
    })

    const actionMarker = computed(() => {
      const detail = selectedState.value
      if (!detail || !detail.has_image || !detail.image_width || !detail.image_height) return null
      return buildActionPointMarker(detail.last_action_point, detail.image_width, detail.image_height)
    })

    const frameUrl = computed(() => {
      const detail = selectedState.value
      if (!detail || !detail.has_image || !detail.state_id) return ""
      const baseHostUrl = host.value && host.value.url ? host.value.url : ""
      return store.frameUrl(detail.state_id, baseHostUrl)
    })

    const stopStateWatch = watch(
      () => (selectedState.value ? selectedState.value.state_id : ""),
      () => {
        imageLoadFailed.value = false
      },
    )

    const stopRootsWatch = watch(
      () => store.state.roots,
      (roots) => {
        if (!selectedRootRef.value) return
        if (!roots.some((item) => item.root_ref === selectedRootRef.value)) {
          selectedRootRef.value = ""
        }
      },
    )

    onMounted(() => {
      store.enterDeck()
    })

    onBeforeUnmount(() => {
      stopStateWatch()
      stopRootsWatch()
      store.leaveDeck()
    })

    function onRootSelectChange(event) {
      selectedRootRef.value = event && event.target ? String(event.target.value) : ""
    }

    function onFilterInput(event) {
      elementFilter.value = event && event.target ? String(event.target.value) : ""
    }

    function observeCurrentTarget(includeImage) {
      const refArg = selectedRootRef.value || null
      void store.observeTarget(refArg, includeImage)
    }

    function captureFrameForSelectedState() {
      const detail = selectedState.value
      const refArg = detail && detail.root_ref ? detail.root_ref : selectedRootRef.value || null
      void store.observeTarget(refArg, true)
    }

    function toggleForegroundInput() {
      if (!currentStatus.value) return
      void store.setPolicy({
        allow_foreground_input: !currentStatus.value.allow_foreground_input,
      })
    }

    function renderPolicyStrip() {
      const status = currentStatus.value
      const stats = status && status.stats ? status.stats : { observations: 0, actions: 0, blocked: 0 }
      const fgAllowed = status ? status.allow_foreground_input !== false : true
      const idleEnabled = Boolean(status && status.idle_timeout_s > 0)
      const idleSummary = idleEnabled
        ? status.idle_remaining_s !== null && status.idle_remaining_s !== undefined
          ? `${formatDurationSeconds(status.idle_remaining_s)} / ${status.idle_timeout_s}s`
          : `${status.idle_timeout_s}s`
        : t("popover.idleDisabled")
      const workerAlive = Boolean(status && status.worker_alive)
      const hotkeyText = status && status.hotkey ? status.hotkey : "Ctrl+Alt+F12"
      const lease = status && status.active_lease ? status.active_lease : null

      return h(
        "section",
        { class: "kcu-card kcu-policy-strip", "data-test": "kcu-deck-policy-strip" },
        [
          h("div", { class: "kcu-strip-top" }, [
            h("div", { class: "kcu-strip-title-group" }, [
              h("h1", { class: "kcu-deck-title" }, t("deck.title")),
              h("p", { class: "kcu-deck-subtitle" }, t("deck.subtitle")),
            ]),
            h("div", { class: "kcu-strip-badges" }, [
              h(
                "span",
                {
                  class: `kcu-badge kcu-state-${tone.value}`,
                  "data-test": "kcu-deck-mode-badge",
                },
                stateLabel.value,
              ),
              h(
                "span",
                { class: workerAlive ? "kcu-kpi kcu-kpi-live" : "kcu-kpi" },
                workerAlive ? t("deck.workerLive") : t("deck.workerIdle"),
              ),
              h(
                "span",
                {
                  class:
                    status && status.hotkey_registered
                      ? "kcu-kpi kcu-kpi-live"
                      : status && modeValue.value !== "locked"
                        ? "kcu-kpi kcu-kpi-warn"
                        : "kcu-kpi",
                  "data-test": "kcu-deck-hotkey-kpi",
                },
                `${hotkeyText} · ${
                  status && status.hotkey_registered
                    ? t("popover.hotkeyReady")
                    : modeValue.value === "locked"
                      ? t("deck.hotkeyStandby")
                      : t("popover.hotkeyUnavailable")
                }`,
              ),
              lease
                ? h(
                    "span",
                    { class: "kcu-kpi kcu-kpi-lease" },
                    t("pill.leasePrefix", { name: lease.agent_name || lease.creature_id }),
                  )
                : null,
              status && status.idle_remaining_s !== null && status.idle_remaining_s !== undefined
                ? h(
                    "span",
                    { class: "kcu-kpi" },
                    formatDurationSeconds(status.idle_remaining_s),
                  )
                : null,
            ]),
          ]),
          h("div", { class: "kcu-strip-controls" }, [
            h("div", { class: "kcu-control-group kcu-control-group-mode" }, [
              h("div", { class: "kcu-control-label-stack" }, [
                h("span", { class: "kcu-control-label" }, t("deck.modeLabel")),
                trippedValue.value
                  ? h(
                      "span",
                      { class: "kcu-mode-panic-hint", "data-test": "kcu-deck-panic-hint" },
                      t("deck.lockedByPanicHint"),
                    )
                  : null,
              ]),
              h(
                "div",
                { class: "kcu-mode-segment", role: "group", "aria-label": t("deck.modeLabel") },
                MODES.map((mode) => {
                  const active = !trippedValue.value && modeValue.value === mode
                  const disabledTitle = trippedValue.value
                    ? t("deck.lockedByPanicHint")
                    : !supportedValue.value
                      ? t("deck.unsupportedBanner", {
                          platform: status ? status.platform : "unknown",
                        })
                      : null
                  return h(
                    "button",
                    {
                      key: mode,
                      type: "button",
                      class: active ? `kcu-mode-btn is-active kcu-state-${mode}` : "kcu-mode-btn",
                      "data-test": `kcu-deck-mode-${mode}`,
                      title: disabledTitle,
                      disabled:
                        trippedValue.value || !supportedValue.value || store.state.savingPolicy,
                      onClick: () => void store.setPolicy({ mode }),
                    },
                    t(`mode.${mode}`),
                  )
                }),
              ),
            ]),
            h("div", { class: "kcu-control-group" }, [
              h("span", { class: "kcu-control-label" }, t("deck.foregroundInput")),
              h(
                "button",
                {
                  type: "button",
                  class: fgAllowed ? "kcu-btn kcu-btn-sm kcu-btn-accent" : "kcu-btn kcu-btn-sm kcu-btn-secondary",
                  "data-test": "kcu-deck-fg-toggle",
                  "aria-pressed": String(fgAllowed),
                  disabled: !supportedValue.value || store.state.savingPolicy,
                  onClick: toggleForegroundInput,
                },
                fgAllowed ? t("deck.foregroundOn") : t("deck.foregroundOff"),
              ),
            ]),
            h("div", { class: "kcu-control-group" }, [
              h("label", { class: "kcu-switch-inline" }, [
                h("input", {
                  type: "checkbox",
                  class: "kcu-checkbox",
                  "data-test": "kcu-deck-idle-toggle",
                  checked: idleEnabled,
                  disabled: store.state.savingPolicy || !supportedValue.value,
                  onChange: (event) =>
                    void store.toggleIdleAutoLock(
                      Boolean(event && event.target && event.target.checked),
                    ),
                }),
                h(
                  "span",
                  { class: "kcu-control-label" },
                  `${t("popover.idleLabel")}: ${idleSummary}`,
                ),
              ]),
            ]),
            h("div", { class: "kcu-stats-row" }, [
              h("span", { class: "kcu-stat-chip" }, t("deck.statsObservations", { count: stats.observations })),
              h("span", { class: "kcu-stat-chip" }, t("deck.statsActions", { count: stats.actions })),
              h("span", { class: "kcu-stat-chip" }, t("deck.statsBlocked", { count: stats.blocked })),
            ]),
            h("div", { class: "kcu-strip-actions" }, [
              trippedValue.value
                ? h(
                    "button",
                    {
                      type: "button",
                      class: "kcu-btn kcu-btn-sm kcu-btn-primary",
                      "data-test": "kcu-deck-reset-panic",
                      disabled: store.state.savingPolicy,
                      onClick: () => void store.resetPanic(),
                    },
                    t("deck.resetPanic"),
                  )
                : null,
              h(
                "button",
                {
                  type: "button",
                  class: "kcu-btn kcu-btn-sm kcu-btn-danger",
                  "data-test": "kcu-deck-panic",
                  onClick: () => void store.triggerPanic("deck_emergency_lock"),
                },
                t("deck.emergencyLock"),
              ),
            ]),
          ]),
          !supportedValue.value
            ? h(
                "div",
                { class: "kcu-banner kcu-banner-warn" },
                t("deck.unsupportedBanner", { platform: status ? status.platform : "unknown" }),
              )
            : null,
          trippedValue.value
            ? h(
                "div",
                { class: "kcu-banner kcu-banner-danger" },
                t("deck.trippedBanner", {
                  reason: (status && status.trip_reason) || "emergency_lock",
                }),
              )
            : null,
          store.state.error
            ? h("div", { class: "kcu-banner kcu-banner-danger", "data-test": "kcu-deck-error" }, [
                h("span", null, store.state.error),
                h(
                  "button",
                  {
                    type: "button",
                    class: "kcu-btn kcu-btn-xs kcu-btn-secondary",
                    onClick: store.clearError,
                  },
                  t("deck.dismissError"),
                ),
              ])
            : null,
        ],
      )
    }

    function renderTargetToolbar() {
      const observeDisabled =
        trippedValue.value || !supportedValue.value || store.state.loadingObserve
      return h(
        "section",
        { class: "kcu-card kcu-target-bar", "data-test": "kcu-deck-target-bar" },
        [
          h("div", { class: "kcu-target-row" }, [
            h("label", { class: "kcu-target-label", for: "kcu-root-picker" }, t("deck.targetWindow")),
            h(
              "select",
              {
                id: "kcu-root-picker",
                class: "kcu-select",
                "data-test": "kcu-deck-root-select",
                value: selectedRootRef.value,
                disabled: !supportedValue.value || store.state.loadingRoots,
                onChange: onRootSelectChange,
              },
              [
                h("option", { value: "" }, t("deck.autoForeground")),
                ...store.state.roots.map((root) =>
                  h(
                    "option",
                    { key: root.root_ref, value: root.root_ref },
                    formatRootOptionLabel(root, t),
                  ),
                ),
              ],
            ),
            h("div", { class: "kcu-target-buttons" }, [
              h(
                "button",
                {
                  type: "button",
                  class: "kcu-btn kcu-btn-sm kcu-btn-secondary",
                  "data-test": "kcu-deck-refresh-roots",
                  disabled: !supportedValue.value || store.state.loadingRoots,
                  onClick: () => void store.fetchRoots(),
                },
                t("deck.refreshWindows"),
              ),
              h(
                "button",
                {
                  type: "button",
                  class: "kcu-btn kcu-btn-sm kcu-btn-secondary",
                  "data-test": "kcu-deck-observe-semantic",
                  disabled: observeDisabled,
                  onClick: () => observeCurrentTarget(false),
                },
                store.state.loadingObserve ? t("deck.observing") : t("deck.observeSemantic"),
              ),
              h(
                "button",
                {
                  type: "button",
                  class: "kcu-btn kcu-btn-sm kcu-btn-primary",
                  "data-test": "kcu-deck-observe-frame",
                  disabled: observeDisabled,
                  onClick: () => observeCurrentTarget(true),
                },
                store.state.loadingObserve ? t("deck.observing") : t("deck.observeFrame"),
              ),
            ]),
          ]),
          selectedRoot.value && selectedRoot.value.denylisted
            ? h("div", { class: "kcu-banner kcu-banner-warn" }, t("deck.denylistedNotice"))
            : null,
        ],
      )
    }

    function renderVisualPane() {
      const detail = selectedState.value
      const el = selectedElement.value

      return h(
        "section",
        { class: "kcu-card kcu-visual-pane", "data-test": "kcu-deck-visual-pane" },
        [
          h("div", { class: "kcu-pane-head" }, [
            h("h2", { class: "kcu-pane-title" }, t("deck.visualTitle")),
            detail
              ? h("div", { class: "kcu-pane-meta" }, [
                  h("span", { class: "kcu-mono-chip" }, detail.state_id),
                  h(
                    "span",
                    {
                      class: detail.valid_for_act
                        ? "kcu-badge kcu-state-armed"
                        : "kcu-badge kcu-state-locked",
                    },
                    detail.valid_for_act ? t("deck.validForAct") : t("deck.staleState"),
                  ),
                ])
              : null,
          ]),
          detail
            ? h("div", { class: "kcu-state-subtitle" }, [
                h("span", { class: "kcu-state-title-text" }, `${detail.exe_name} — ${detail.title || "(untitled)"}`),
                h(
                  "span",
                  { class: "kcu-state-meta-text" },
                  t("deck.stateMeta", {
                    epoch: detail.epoch,
                    count: detail.element_count,
                    dpi: Math.round((Number(detail.dpi_scale) || 1) * 100),
                  }),
                ),
              ])
            : null,
          !detail
            ? h("div", { class: "kcu-empty-card" }, [
                h("div", { class: "kcu-empty-title" }, t("deck.emptyStateTitle")),
                h("p", { class: "kcu-empty-hint" }, t("deck.emptyStateHint")),
              ])
            : detail.has_image && detail.image_width > 0 && detail.image_height > 0
              ? h("div", { class: "kcu-stage-wrap" }, [
                  h(
                    "div",
                    {
                      class: "kcu-stage-canvas",
                      "data-test": "kcu-stage-canvas",
                      style: {
                        aspectRatio: `${detail.image_width} / ${detail.image_height}`,
                      },
                    },
                    [
                      h("img", {
                        class: "kcu-stage-img",
                        "data-test": "kcu-stage-img",
                        src: frameUrl.value,
                        alt: detail.title || detail.state_id,
                        onError: () => {
                          imageLoadFailed.value = true
                        },
                        onLoad: () => {
                          imageLoadFailed.value = false
                        },
                      }),
                      h(
                        "svg",
                        {
                          class: "kcu-stage-svg",
                          "data-test": "kcu-stage-svg",
                          viewBox: `0 0 ${detail.image_width} ${detail.image_height}`,
                          preserveAspectRatio: "xMidYMid meet",
                        },
                        [
                          ...svgBoxes.value.map((box) => {
                            const isSelected = store.state.selectedElementRef === box.ref
                            const isHovered = store.state.hoveredElementRef === box.ref
                            const cls = [
                              "kcu-svg-box",
                              isSelected ? "is-selected" : "",
                              isHovered ? "is-hovered" : "",
                              box.focused ? "is-focused" : "",
                              !box.enabled ? "is-disabled" : "",
                            ]
                              .filter(Boolean)
                              .join(" ")
                            const labelX = Math.min(box.x, Math.max(0, detail.image_width - 120))
                            const labelY = box.y >= 20 ? box.y - 4 : Math.min(detail.image_height - 4, box.y + 16)
                            return h(
                              "g",
                              {
                                key: box.ref,
                                class: "kcu-svg-box-group",
                                "data-ref": box.ref,
                                onMouseenter: () => store.setHoveredElement(box.ref),
                                onMouseleave: () => store.setHoveredElement(null),
                                onClick: (event) => {
                                  if (event && typeof event.stopPropagation === "function") {
                                    event.stopPropagation()
                                  }
                                  store.setSelectedElement(box.ref)
                                },
                              },
                              [
                                h("rect", {
                                  class: cls,
                                  x: box.x,
                                  y: box.y,
                                  width: box.width,
                                  height: box.height,
                                  "vector-effect": "non-scaling-stroke",
                                }),
                                isSelected || isHovered
                                  ? h(
                                      "text",
                                      {
                                        class: "kcu-svg-label",
                                        x: labelX,
                                        y: labelY,
                                      },
                                      `${box.ref} · ${box.role}`,
                                    )
                                  : null,
                              ],
                            )
                          }),
                          actionMarker.value
                            ? h(
                                "g",
                                {
                                  class: `kcu-svg-crosshair kcu-outcome-${outcomeTone(actionMarker.value.outcome)}`,
                                  "data-test": "kcu-stage-crosshair",
                                },
                                [
                                  h("line", {
                                    x1: Math.max(0, actionMarker.value.x - 18),
                                    y1: actionMarker.value.y,
                                    x2: Math.min(detail.image_width, actionMarker.value.x + 18),
                                    y2: actionMarker.value.y,
                                    "vector-effect": "non-scaling-stroke",
                                  }),
                                  h("line", {
                                    x1: actionMarker.value.x,
                                    y1: Math.max(0, actionMarker.value.y - 18),
                                    x2: actionMarker.value.x,
                                    y2: Math.min(detail.image_height, actionMarker.value.y + 18),
                                    "vector-effect": "non-scaling-stroke",
                                  }),
                                  h("circle", {
                                    cx: actionMarker.value.x,
                                    cy: actionMarker.value.y,
                                    r: "11",
                                    fill: "none",
                                    "vector-effect": "non-scaling-stroke",
                                  }),
                                  h("circle", {
                                    class: "kcu-crosshair-dot",
                                    cx: actionMarker.value.x,
                                    cy: actionMarker.value.y,
                                    r: "3.5",
                                  }),
                                  h(
                                    "text",
                                    {
                                      class: "kcu-svg-crosshair-label",
                                      x: Math.min(detail.image_width - 140, actionMarker.value.x + 14),
                                      y: Math.max(14, actionMarker.value.y - 10),
                                    },
                                    `${actionMarker.value.action}${actionMarker.value.ref ? ` ${actionMarker.value.ref}` : ""} (${actionMarker.value.outcome})`,
                                  ),
                                ],
                              )
                            : null,
                        ],
                      ),
                    ],
                  ),
                  imageLoadFailed.value
                    ? h("div", { class: "kcu-banner kcu-banner-warn" }, t("deck.imageLoadError"))
                    : null,
                ])
              : h("div", { class: "kcu-empty-card", "data-test": "kcu-semantic-card" }, [
                  h("div", { class: "kcu-empty-title" }, t("deck.semanticOnlyTitle")),
                  h("p", { class: "kcu-empty-hint" }, t("deck.semanticOnlyHint")),
                  h("div", { class: "kcu-semantic-meta" }, [
                    h("span", { class: "kcu-kpi" }, formatCoordRect(detail.rect)),
                    detail.capture_method
                      ? h("span", { class: "kcu-kpi" }, String(detail.capture_method))
                      : null,
                  ]),
                  h(
                    "button",
                    {
                      type: "button",
                      class: "kcu-btn kcu-btn-sm kcu-btn-primary",
                      "data-test": "kcu-capture-frame-btn",
                      disabled: trippedValue.value || !supportedValue.value || store.state.loadingObserve,
                      onClick: captureFrameForSelectedState,
                    },
                    t("deck.captureFrame"),
                  ),
                ]),
          el
            ? h("div", { class: "kcu-selected-strip", "data-test": "kcu-selected-element" }, [
                h("div", { class: "kcu-selected-head" }, [
                  h(
                    "span",
                    { class: "kcu-selected-title" },
                    `${t("deck.selectedElementTitle")}: ${el.ref} · ${el.role}${el.name ? ` · ${el.name}` : ""}`,
                  ),
                  h(
                    "button",
                    {
                      type: "button",
                      class: "kcu-btn kcu-btn-xs kcu-btn-ghost",
                      onClick: () => store.setSelectedElement(null),
                    },
                    t("deck.clearSelection"),
                  ),
                ]),
                h("div", { class: "kcu-selected-grid" }, [
                  h("span", { class: "kcu-kpi" }, `screen: ${formatCoordRect(el.rect)}`),
                  el.image_rect
                    ? h("span", { class: "kcu-kpi" }, `image: ${formatCoordRect(el.image_rect)}`)
                    : null,
                  el.automation_id ? h("span", { class: "kcu-kpi" }, `id: ${el.automation_id}`) : null,
                  el.capabilities && el.capabilities.length > 0
                    ? h("span", { class: "kcu-kpi" }, el.capabilities.join(", "))
                    : null,
                  el.value_preview
                    ? h("span", { class: "kcu-kpi" }, `val: ${el.value_preview}`)
                    : null,
                ]),
              ])
            : null,
        ],
      )
    }

    function renderInspectorPane() {
      const detail = selectedState.value
      const totalEl = allElements.value.length
      const matchedEl = filteredElements.value.length

      return h(
        "section",
        { class: "kcu-inspector-pane", "data-test": "kcu-deck-inspector-pane" },
        [
          h("div", { class: "kcu-card kcu-history-card", "data-test": "kcu-state-history" }, [
            h("div", { class: "kcu-pane-head" }, [
              h("h2", { class: "kcu-pane-title" }, t("deck.stateHistoryTitle")),
              h(
                "button",
                {
                  type: "button",
                  class: "kcu-btn kcu-btn-xs kcu-btn-secondary",
                  disabled: store.state.loadingStates,
                  onClick: () => void store.fetchStates(),
                },
                t("deck.refreshStates"),
              ),
            ]),
            store.state.states.length === 0
              ? h("p", { class: "kcu-empty-inline" }, t("deck.noStates"))
              : h(
                  "div",
                  { class: "kcu-state-pills" },
                  store.state.states.map((summary) => {
                    const active = store.state.selectedStateId === summary.state_id
                    return h(
                      "button",
                      {
                        key: summary.state_id,
                        type: "button",
                        class: active ? "kcu-state-pill is-active" : "kcu-state-pill",
                        "data-test": `kcu-state-pill-${summary.state_id}`,
                        onClick: () => void store.selectState(summary.state_id),
                      },
                      `${summary.state_id} · e${summary.epoch} · ${summary.element_count}${summary.has_image ? ` · ${t("deck.frameTag")}` : ""}`,
                    )
                  }),
                ),
          ]),
          h("div", { class: "kcu-card kcu-elements-card", "data-test": "kcu-element-inspector" }, [
            h("div", { class: "kcu-pane-head" }, [
              h(
                "h2",
                { class: "kcu-pane-title" },
                t("deck.elementsTitle", { matched: matchedEl, total: totalEl }),
              ),
            ]),
            h("input", {
              type: "search",
              class: "kcu-input",
              "data-test": "kcu-element-filter",
              value: elementFilter.value,
              placeholder: t("deck.filterPlaceholder"),
              disabled: !detail,
              onInput: onFilterInput,
            }),
            h("div", { class: "kcu-table-scroll" }, [
              filteredElements.value.length === 0
                ? h("p", { class: "kcu-empty-inline" }, t("deck.noElements"))
                : h("table", { class: "kcu-table", "data-test": "kcu-element-table" }, [
                    h("thead", null, [
                      h("tr", null, [
                        h("th", null, t("deck.colRef")),
                        h("th", null, t("deck.colRole")),
                        h("th", null, t("deck.colName")),
                        h("th", null, t("deck.colCaps")),
                        h("th", null, t("deck.colRect")),
                      ]),
                    ]),
                    h(
                      "tbody",
                      null,
                      filteredElements.value.map((el) => {
                        const isSelected = store.state.selectedElementRef === el.ref
                        const isHovered = store.state.hoveredElementRef === el.ref
                        const rowCls = [
                          "kcu-el-row",
                          isSelected ? "is-selected" : "",
                          isHovered ? "is-hovered" : "",
                        ]
                          .filter(Boolean)
                          .join(" ")
                        const flags = []
                        if (el.focused) flags.push(t("deck.tagFocused"))
                        if (!el.enabled) flags.push(t("deck.tagDisabled"))
                        if (el.truncated) flags.push(t("deck.tagTruncated"))
                        const indentPx = Math.min(48, Math.max(0, (Number(el.depth) || 0) * 10))
                        return h(
                          "tr",
                          {
                            key: el.ref,
                            class: rowCls,
                            "data-ref": el.ref,
                            onMouseenter: () => store.setHoveredElement(el.ref),
                            onMouseleave: () => store.setHoveredElement(null),
                            onClick: () => store.setSelectedElement(el.ref),
                          },
                          [
                            h(
                              "td",
                              {
                                class: "kcu-mono-cell",
                                style: indentPx > 0 ? { paddingLeft: `${8 + indentPx}px` } : null,
                              },
                              el.ref,
                            ),
                            h("td", null, [
                              h("span", null, el.role || "--"),
                              flags.length > 0
                                ? h("span", { class: "kcu-inline-flags" }, ` [${flags.join(", ")}]`)
                                : null,
                            ]),
                            h("td", { class: "kcu-name-cell" }, [
                              h("div", { class: "kcu-el-name" }, el.name || "(unnamed)"),
                              el.automation_id
                                ? h("div", { class: "kcu-el-sub" }, el.automation_id)
                                : null,
                              el.value_preview
                                ? h("div", { class: "kcu-el-sub" }, el.value_preview)
                                : null,
                            ]),
                            h(
                              "td",
                              { class: "kcu-caps-cell" },
                              Array.isArray(el.capabilities) && el.capabilities.length > 0
                                ? el.capabilities.join(", ")
                                : "--",
                            ),
                            h("td", { class: "kcu-mono-cell" }, formatCoordRect(el.rect)),
                          ],
                        )
                      }),
                    ),
                  ]),
            ]),
          ]),
          h("div", { class: "kcu-card kcu-audit-card", "data-test": "kcu-audit-log" }, [
            h("div", { class: "kcu-pane-head" }, [
              h("h2", { class: "kcu-pane-title" }, t("deck.auditTitle")),
            ]),
            h("div", { class: "kcu-table-scroll kcu-audit-scroll" }, [
              store.state.audit.length === 0
                ? h("p", { class: "kcu-empty-inline" }, t("deck.noAudit"))
                : h("table", { class: "kcu-table" }, [
                    h("thead", null, [
                      h("tr", null, [
                        h("th", null, t("deck.colTime")),
                        h("th", null, t("deck.colActor")),
                        h("th", null, t("deck.colEvent")),
                        h("th", null, t("deck.colTarget")),
                        h("th", null, t("deck.colSummary")),
                        h("th", null, t("deck.colOutcome")),
                      ]),
                    ]),
                    h(
                      "tbody",
                      null,
                      store.state.audit.slice(0, 30).map((entry) =>
                        h("tr", { key: entry.id }, [
                          h("td", { class: "kcu-mono-cell" }, formatTimestamp(entry.timestamp, locale.value)),
                          h("td", null, entry.actor || "--"),
                          h("td", null, t(`event.${entry.event}`)),
                          h("td", { class: "kcu-target-cell" }, entry.target_title || "--"),
                          h("td", { class: "kcu-summary-cell" }, entry.summary || "--"),
                          h("td", null, [
                            h(
                              "span",
                              { class: `kcu-outcome-pill kcu-tone-${outcomeTone(entry.outcome)}` },
                              t(`outcome.${entry.outcome}`),
                            ),
                          ]),
                        ]),
                      ),
                    ),
                  ]),
            ]),
          ]),
        ],
      )
    }

    return () =>
      h("div", { class: "kcu-deck", "data-test": "kcu-deck-root" }, [
        renderPolicyStrip(),
        renderTargetToolbar(),
        h("div", { class: "kcu-deck-stage", "data-test": "kcu-deck-stage" }, [
          renderVisualPane(),
          renderInspectorPane(),
        ]),
      ])
  },
})
