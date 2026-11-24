import { computed, defineComponent, h, nextTick, onBeforeUnmount, onMounted, reactive, watch } from "vue"
import {
  ActionButton,
  SettingsGroup,
  SettingsRow,
  notify,
  useExtension,
} from "@kohakuterrarium/ui-sdk"
import { syncSettingsHostLabels, useI18n } from "./i18n.js"
import { getCuStore } from "./store.js"
import {
  DEFAULT_DENYLIST_PATTERNS,
  MODES,
  joinPatternLines,
  modeBadgeKey,
  splitPatternLines,
  statusTone,
  validateRegexPatterns,
} from "./utils.js"

function buildDraftFromStatus(status, fallbackSeconds) {
  const rawTimeout = status ? Number(status.idle_timeout_s) : 0
  const idleEnabled = Boolean(Number.isFinite(rawTimeout) && rawTimeout > 0)
  const duration = idleEnabled
    ? Math.round(rawTimeout)
    : Number.isFinite(Number(fallbackSeconds)) && Number(fallbackSeconds) > 0
      ? Math.round(Number(fallbackSeconds))
      : 300
  return {
    idleEnabled,
    idleTimeoutText: String(duration),
    allowForegroundInput: status ? status.allow_foreground_input !== false : true,
    hotkey: status && status.hotkey ? String(status.hotkey) : "Ctrl+Alt+F12",
    denylistText: joinPatternLines(
      status && Array.isArray(status.denylist_patterns)
        ? status.denylist_patterns
        : DEFAULT_DENYLIST_PATTERNS,
    ),
    allowlistText: joinPatternLines(
      status && Array.isArray(status.allowlist_patterns) ? status.allowlist_patterns : [],
    ),
  }
}

export default defineComponent({
  name: "KcuSettingsView",
  setup() {
    const kt = useExtension()
    const store = getCuStore(kt)
    const { locale, t } = useI18n()

    const draft = reactive(buildDraftFromStatus(store.state.status, store.state.lastIdleSeconds))
    const dirty = reactive({ value: false })

    function applyStatusToDraft(status) {
      const next = buildDraftFromStatus(status, store.state.lastIdleSeconds)
      draft.idleEnabled = next.idleEnabled
      draft.idleTimeoutText = next.idleTimeoutText
      draft.allowForegroundInput = next.allowForegroundInput
      draft.hotkey = next.hotkey
      draft.denylistText = next.denylistText
      draft.allowlistText = next.allowlistText
      dirty.value = false
    }

    const currentStatus = computed(() => store.state.status)
    const modeValue = computed(() =>
      currentStatus.value && currentStatus.value.mode ? currentStatus.value.mode : "locked",
    )
    const trippedValue = computed(() => Boolean(currentStatus.value && currentStatus.value.tripped))
    const supportedValue = computed(() =>
      currentStatus.value ? currentStatus.value.supported !== false : true,
    )
    const stateLabel = computed(() => t(modeBadgeKey(modeValue.value, trippedValue.value)))
    const tone = computed(() => statusTone(modeValue.value, trippedValue.value))

    const denylistCheck = computed(() =>
      validateRegexPatterns(splitPatternLines(draft.denylistText)),
    )
    const allowlistCheck = computed(() =>
      validateRegexPatterns(splitPatternLines(draft.allowlistText)),
    )

    const timeoutNumber = computed(() => {
      const trimmed = String(draft.idleTimeoutText || "").trim()
      if (!/^\d+$/.test(trimmed)) return null
      const parsed = Number(trimmed)
      if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > 3600) return null
      return parsed
    })

    const hotkeyTrimmed = computed(() => String(draft.hotkey || "").trim())

    const hasValidationError = computed(
      () =>
        (draft.idleEnabled && timeoutNumber.value === null) ||
        !hotkeyTrimmed.value ||
        !denylistCheck.value.valid ||
        !allowlistCheck.value.valid,
    )

    const stopStatusWatch = watch(
      () => store.state.status,
      (nextStatus) => {
        if (nextStatus && !dirty.value) {
          applyStatusToDraft(nextStatus)
        }
      },
      { immediate: true },
    )

    const stopLocaleWatch = watch(
      locale,
      (nextLocale) => {
        if (typeof document !== "undefined") {
          syncSettingsHostLabels(nextLocale, document)
        }
      },
      { immediate: true },
    )

    onMounted(() => {
      store.enterSettings()
      if (typeof document !== "undefined") {
        syncSettingsHostLabels(locale.value, document)
        void nextTick().then(() => syncSettingsHostLabels(locale.value, document))
      }
    })

    onBeforeUnmount(() => {
      stopStatusWatch()
      stopLocaleWatch()
      store.leaveSettings()
    })

    function onTimeoutInput(event) {
      draft.idleTimeoutText = event && event.target ? String(event.target.value) : ""
      dirty.value = true
    }

    async function onIdleToggleChange(event) {
      const checked = Boolean(event && event.target && event.target.checked)
      draft.idleEnabled = checked
      if (checked) {
        const parsed = timeoutNumber.value
        const seconds =
          parsed !== null
            ? parsed
            : store.state.lastIdleSeconds > 0
              ? store.state.lastIdleSeconds
              : 300
        draft.idleTimeoutText = String(seconds)
        store.state.lastIdleSeconds = seconds
      }
      const res = await store.toggleIdleAutoLock(checked)
      if (res && res.ok && !dirty.value) {
        applyStatusToDraft(res.data)
      }
    }

    function onForegroundChange(event) {
      draft.allowForegroundInput = Boolean(event && event.target && event.target.checked)
      dirty.value = true
    }

    function onHotkeyInput(event) {
      draft.hotkey = event && event.target ? String(event.target.value) : ""
      dirty.value = true
    }

    function onDenylistInput(event) {
      draft.denylistText = event && event.target ? String(event.target.value) : ""
      dirty.value = true
    }

    function onAllowlistInput(event) {
      draft.allowlistText = event && event.target ? String(event.target.value) : ""
      dirty.value = true
    }

    function discardChanges() {
      applyStatusToDraft(store.state.status)
    }

    async function savePolicy() {
      if (draft.idleEnabled && timeoutNumber.value === null) {
        notify(t("settings.invalidTimeout"), "error")
        return
      }
      if (!hotkeyTrimmed.value) {
        notify(t("settings.invalidHotkey"), "error")
        return
      }
      if (!denylistCheck.value.valid) {
        const first = denylistCheck.value.invalid[0]
        notify(t("settings.invalidRegex", { line: first.line, pattern: first.pattern }), "error")
        return
      }
      if (!allowlistCheck.value.valid) {
        const first = allowlistCheck.value.invalid[0]
        notify(t("settings.invalidRegex", { line: first.line, pattern: first.pattern }), "error")
        return
      }

      const res = await store.setPolicy({
        idle_timeout_s: draft.idleEnabled ? timeoutNumber.value : 0,
        allow_foreground_input: Boolean(draft.allowForegroundInput),
        hotkey: hotkeyTrimmed.value,
        denylist_patterns: splitPatternLines(draft.denylistText),
        allowlist_patterns: splitPatternLines(draft.allowlistText),
      })
      if (res && res.ok) {
        applyStatusToDraft(res.data)
        notify(t("settings.saved"), "success")
      }
    }

    async function resetDefaults() {
      const res = await store.setPolicy({
        idle_timeout_s: 0,
        allow_foreground_input: true,
        hotkey: "Ctrl+Alt+F12",
        denylist_patterns: [...DEFAULT_DENYLIST_PATTERNS],
        allowlist_patterns: [],
      })
      if (res && res.ok) {
        applyStatusToDraft(res.data)
        notify(t("settings.resetDone"), "info")
      }
    }

    return () => {
      const status = currentStatus.value
      const hotkeyStatusText = status
        ? status.hotkey_registered
          ? t("settings.hotkeyReady", { hotkey: status.hotkey })
          : modeValue.value === "locked"
            ? t("settings.hotkeyStandby", { hotkey: status.hotkey })
            : t("settings.hotkeyUnavailable", { hotkey: status.hotkey })
        : ""
      const hotkeyTone = status
        ? status.hotkey_registered
          ? "ok"
          : modeValue.value === "locked"
            ? "muted"
            : "warn"
        : "muted"

      const denylistStatus = !denylistCheck.value.valid
        ? t("settings.invalidRegex", {
            line: denylistCheck.value.invalid[0].line,
            pattern: denylistCheck.value.invalid[0].pattern,
          })
        : ""
      const allowlistStatus = !allowlistCheck.value.valid
        ? t("settings.invalidRegex", {
            line: allowlistCheck.value.invalid[0].line,
            pattern: allowlistCheck.value.invalid[0].pattern,
          })
        : ""

      return h("div", { class: "kcu-settings", "data-test": "kcu-settings-root" }, [
        h(
          SettingsGroup,
          {
            title: t("settings.policyTitle"),
            description: t("settings.policyDesc"),
          },
          {
            action: () =>
              dirty.value
                ? h("span", { class: "kcu-badge kcu-state-confirm" }, t("settings.unsaved"))
                : null,
            default: () => [
              h(
                SettingsRow,
                {
                  title: t("settings.statusTitle"),
                  hint: t("settings.statusHint"),
                  status: stateLabel.value,
                  tone: trippedValue.value ? "error" : tone.value === "armed" ? "ok" : "muted",
                },
                {
                  detail: () =>
                    h("div", { class: "kcu-settings-mode-wrap" }, [
                      h(
                        "div",
                        {
                          class: "kcu-mode-segment",
                          role: "group",
                          "aria-label": t("deck.modeLabel"),
                        },
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
                              class: active
                                ? `kcu-mode-btn is-active kcu-state-${mode}`
                                : "kcu-mode-btn",
                              "data-test": `kcu-settings-mode-${mode}`,
                              title: disabledTitle,
                              disabled:
                                trippedValue.value ||
                                !supportedValue.value ||
                                store.state.savingPolicy,
                              onClick: () => void store.setPolicy({ mode }),
                            },
                            t(`mode.${mode}`),
                          )
                        }),
                      ),
                      trippedValue.value
                        ? h(
                            "div",
                            {
                              class: "kcu-mode-panic-hint",
                              "data-test": "kcu-settings-panic-hint",
                            },
                            t("deck.lockedByPanicHint"),
                          )
                        : null,
                    ].filter(Boolean)),
                  default: () =>
                    [
                      trippedValue.value
                        ? h(
                            ActionButton,
                            {
                              variant: "primary",
                              size: "sm",
                              "data-test": "kcu-settings-reset-panic",
                              disabled: store.state.savingPolicy,
                              onClick: () => void store.resetPanic(),
                            },
                            { default: () => t("settings.resetPanic") },
                          )
                        : null,
                      h(
                        ActionButton,
                        {
                          variant: "secondary",
                          size: "sm",
                          "data-test": "kcu-settings-open-deck",
                          onClick: () => kt.openTab("deck"),
                        },
                        { default: () => t("settings.openDeck") },
                      ),
                    ].filter(Boolean),
                },
              ),
              h(
                SettingsRow,
                {
                  title: t("settings.idleTimeoutTitle"),
                  hint: t("settings.idleTimeoutHint"),
                  status:
                    draft.idleEnabled && timeoutNumber.value === null
                      ? t("settings.invalidTimeout")
                      : "",
                  tone: draft.idleEnabled && timeoutNumber.value === null ? "error" : "muted",
                  inline: true,
                },
                {
                  default: () =>
                    h("div", { class: "kcu-input-inline" }, [
                      h("label", { class: "kcu-switch-inline" }, [
                        h("input", {
                          type: "checkbox",
                          class: "kcu-checkbox",
                          "data-test": "kcu-settings-idle-toggle",
                          checked: Boolean(draft.idleEnabled),
                          disabled: store.state.savingPolicy || !supportedValue.value,
                          onChange: (event) => void onIdleToggleChange(event),
                        }),
                        h(
                          "span",
                          null,
                          draft.idleEnabled
                            ? t("settings.idleEnabledLabel")
                            : t("popover.idleDisabled"),
                        ),
                      ]),
                      h("input", {
                        type: "number",
                        min: "1",
                        max: "3600",
                        step: "10",
                        class: "kcu-input kcu-input-number",
                        "data-test": "kcu-settings-idle-timeout",
                        disabled: !draft.idleEnabled,
                        value: draft.idleTimeoutText,
                        onInput: onTimeoutInput,
                      }),
                      h("span", { class: "kcu-unit-label" }, t("settings.idleTimeoutUnit")),
                    ]),
                },
              ),
              h(
                SettingsRow,
                {
                  title: t("settings.foregroundTitle"),
                  hint: t("settings.foregroundHint"),
                  inline: true,
                },
                {
                  default: () =>
                    h("label", { class: "kcu-switch-label" }, [
                      h("input", {
                        type: "checkbox",
                        class: "kcu-checkbox",
                        "data-test": "kcu-settings-fg-toggle",
                        checked: Boolean(draft.allowForegroundInput),
                        onChange: onForegroundChange,
                      }),
                      h(
                        "span",
                        null,
                        draft.allowForegroundInput
                          ? t("settings.foregroundEnabled")
                          : t("settings.foregroundDisabled"),
                      ),
                    ]),
                },
              ),
              h(
                SettingsRow,
                {
                  title: t("settings.hotkeyTitle"),
                  hint: t("settings.hotkeyHint"),
                  status: !hotkeyTrimmed.value ? t("settings.invalidHotkey") : hotkeyStatusText,
                  tone: !hotkeyTrimmed.value ? "error" : hotkeyTone,
                },
                {
                  default: () =>
                    h("input", {
                      type: "text",
                      class: "kcu-input kcu-input-hotkey",
                      "data-test": "kcu-settings-hotkey",
                      value: draft.hotkey,
                      placeholder: "Ctrl+Alt+F12",
                      onInput: onHotkeyInput,
                    }),
                },
              ),
            ],
          },
        ),
        h(
          SettingsGroup,
          {
            title: t("settings.listsTitle"),
            description: t("settings.listsDesc"),
          },
          {
            default: () => [
              h(
                SettingsRow,
                {
                  title: t("settings.denylistTitle"),
                  hint: t("settings.denylistHint"),
                  status: denylistStatus,
                  tone: denylistStatus ? "error" : "muted",
                },
                {
                  detail: () =>
                    h("textarea", {
                      class: "kcu-textarea",
                      rows: "5",
                      "data-test": "kcu-settings-denylist",
                      value: draft.denylistText,
                      onInput: onDenylistInput,
                    }),
                },
              ),
              h(
                SettingsRow,
                {
                  title: t("settings.allowlistTitle"),
                  hint: t("settings.allowlistHint"),
                  status: allowlistStatus,
                  tone: allowlistStatus ? "error" : "muted",
                },
                {
                  detail: () =>
                    h("textarea", {
                      class: "kcu-textarea",
                      rows: "4",
                      "data-test": "kcu-settings-allowlist",
                      value: draft.allowlistText,
                      onInput: onAllowlistInput,
                    }),
                },
              ),
            ],
          },
        ),
        h(
          "div",
          { class: "kcu-settings-actions" },
          [
            h(
              ActionButton,
              {
                variant: "primary",
                "data-test": "kcu-settings-save",
                disabled: store.state.savingPolicy || hasValidationError.value,
                onClick: () => void savePolicy(),
              },
              { default: () => (store.state.savingPolicy ? t("settings.saving") : t("settings.save")) },
            ),
            dirty.value
              ? h(
                  ActionButton,
                  {
                    variant: "ghost",
                    "data-test": "kcu-settings-discard",
                    disabled: store.state.savingPolicy,
                    onClick: discardChanges,
                  },
                  { default: () => t("settings.discard") },
                )
              : null,
            h(
              ActionButton,
              {
                variant: "secondary",
                "data-test": "kcu-settings-reset",
                disabled: store.state.savingPolicy,
                onClick: () => void resetDefaults(),
              },
              { default: () => t("settings.resetDefaults") },
            ),
          ].filter(Boolean),
        ),
      ])
    }
  },
})
