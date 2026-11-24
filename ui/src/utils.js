export const MODES = Object.freeze(["locked", "read_only", "confirm", "armed"])

export const DEFAULT_DENYLIST_PATTERNS = Object.freeze([
  "1Password",
  "Bitwarden",
  "KeePass",
  "CredentialUIBroker",
  "User Account Control",
  "Windows Security",
  "Taskmgr",
])

export function clampNumber(value, min, max, fallback) {
  const num = Number(value)
  if (!Number.isFinite(num)) return fallback
  if (num < min) return min
  if (num > max) return max
  return num
}

export function clampRect(rect, maxWidth, maxHeight) {
  if (!rect || typeof rect !== "object") return null
  const boundW = Number(maxWidth)
  const boundH = Number(maxHeight)
  if (!Number.isFinite(boundW) || !Number.isFinite(boundH) || boundW <= 0 || boundH <= 0) {
    return null
  }
  const rawX = Number(rect.x)
  const rawY = Number(rect.y)
  const rawW = Number(rect.width)
  const rawH = Number(rect.height)
  if (
    !Number.isFinite(rawX) ||
    !Number.isFinite(rawY) ||
    !Number.isFinite(rawW) ||
    !Number.isFinite(rawH)
  ) {
    return null
  }
  const x1 = clampNumber(rawX, 0, boundW, 0)
  const y1 = clampNumber(rawY, 0, boundH, 0)
  const x2 = clampNumber(rawX + rawW, 0, boundW, 0)
  const y2 = clampNumber(rawY + rawH, 0, boundH, 0)
  const width = Math.max(0, Math.round(x2 - x1))
  const height = Math.max(0, Math.round(y2 - y1))
  if (width <= 0 || height <= 0) return null
  return {
    x: Math.round(x1),
    y: Math.round(y1),
    width,
    height,
  }
}

export function isValidMode(mode) {
  return MODES.includes(mode)
}

export function statusTone(mode, tripped) {
  if (tripped === true) return "tripped"
  if (mode === "read_only" || mode === "confirm" || mode === "armed") return mode
  return "locked"
}

export function modeBadgeKey(mode, tripped) {
  if (tripped === true) return "mode.tripped"
  if (mode === "read_only") return "mode.read_only"
  if (mode === "confirm") return "mode.confirm"
  if (mode === "armed") return "mode.armed"
  return "mode.locked"
}

export function outcomeTone(outcome) {
  if (outcome === "worked") return "success"
  if (outcome === "didnt") return "warning"
  if (outcome === "blocked") return "danger"
  if (outcome === "info") return "info"
  return "neutral"
}

export function formatTimestamp(epochSeconds, locale) {
  const num = Number(epochSeconds)
  if (!Number.isFinite(num) || num <= 0) return "--:--:--"
  const date = new Date(num * 1000)
  if (Number.isNaN(date.getTime())) return "--:--:--"
  try {
    return new Intl.DateTimeFormat(locale || "en", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date)
  } catch (_err) {
    return date.toISOString().slice(11, 19)
  }
}

export function formatDurationSeconds(seconds) {
  if (seconds === null || seconds === undefined) return "--"
  const num = Number(seconds)
  if (!Number.isFinite(num) || num < 0) return "--"
  const total = Math.ceil(num)
  const mins = Math.floor(total / 60)
  const secs = total % 60
  if (mins <= 0) return `${secs}s`
  return `${mins}m ${String(secs).padStart(2, "0")}s`
}

export function formatCoordRect(rect) {
  if (!rect || typeof rect !== "object") return "--"
  const x = Number(rect.x)
  const y = Number(rect.y)
  const w = Number(rect.width)
  const h = Number(rect.height)
  if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(w) || !Number.isFinite(h)) {
    return "--"
  }
  return `${Math.round(x)}, ${Math.round(y)} · ${Math.round(w)}x${Math.round(h)}`
}

export function splitPatternLines(text) {
  return String(text || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
}

export function joinPatternLines(patterns) {
  if (!Array.isArray(patterns)) return ""
  return patterns.map((p) => String(p)).join("\n")
}

export function validateRegexPatterns(patterns) {
  const list = Array.isArray(patterns) ? patterns : []
  const invalid = []
  for (let i = 0; i < list.length; i++) {
    const pattern = String(list[i])
    try {
      new RegExp(pattern, "i")
    } catch (err) {
      invalid.push({
        line: i + 1,
        pattern,
        message: err && err.message ? err.message : String(err),
      })
    }
  }
  return { valid: invalid.length === 0, invalid }
}

export function extractApiError(err, actionContext) {
  const context = actionContext ? String(actionContext) : "API request"
  const status = err && err.response ? err.response.status : null
  const data = err && err.response ? err.response.data : null
  let detailText = ""
  if (typeof data === "string") {
    detailText = data.trim()
  } else if (data && typeof data === "object") {
    if (typeof data.detail === "string") {
      detailText = data.detail.trim()
    } else if (data.detail && typeof data.detail === "object" && !Array.isArray(data.detail)) {
      detailText = String(data.detail.message || data.detail.error || JSON.stringify(data.detail))
    } else if (Array.isArray(data.detail)) {
      detailText = data.detail
        .map((item) => {
          const loc = Array.isArray(item && item.loc) ? item.loc.join(".") : ""
          const msg = item && item.msg ? item.msg : JSON.stringify(item)
          return loc ? `${loc}: ${msg}` : msg
        })
        .join("; ")
    } else if (typeof data.message === "string" || typeof data.error === "string") {
      detailText = [data.error, data.message].filter(Boolean).join(": ")
    }
  }
  if (!detailText) {
    detailText = err && err.message ? String(err.message) : String(err || "Unknown error")
  }
  const statusPart = status ? ` (HTTP ${status})` : ""
  return `${context}${statusPart}: ${detailText}`
}

export function clampPopoverPosition(
  anchorRect,
  popoverWidth,
  popoverHeight,
  viewportWidth,
  viewportHeight,
  margin,
) {
  const safeMargin = clampNumber(margin, 4, 64, 12)
  const vpW = clampNumber(viewportWidth, 240, 10000, 1280)
  const vpH = clampNumber(viewportHeight, 240, 10000, 800)
  const width = clampNumber(popoverWidth, 160, Math.max(160, vpW - safeMargin * 2), 340)
  const height = clampNumber(popoverHeight, 120, Math.max(120, vpH - safeMargin * 2), 360)

  if (!anchorRect || typeof anchorRect !== "object") {
    return {
      left: Math.round(clampNumber((vpW - width) / 2, safeMargin, vpW - width - safeMargin, safeMargin)),
      top: Math.round(clampNumber((vpH - height) / 2, safeMargin, vpH - height - safeMargin, safeMargin)),
      width: Math.round(width),
    }
  }

  const preferredLeft = Number(anchorRect.right) - width
  const preferredTop = Number(anchorRect.top) - height - 10
  const fallbackBelowTop = Number(anchorRect.bottom) + 10
  const chosenTop =
    preferredTop >= safeMargin
      ? preferredTop
      : fallbackBelowTop + height <= vpH - safeMargin
        ? fallbackBelowTop
        : safeMargin

  return {
    left: Math.round(
      clampNumber(preferredLeft, safeMargin, Math.max(safeMargin, vpW - width - safeMargin), safeMargin),
    ),
    top: Math.round(
      clampNumber(chosenTop, safeMargin, Math.max(safeMargin, vpH - height - safeMargin), safeMargin),
    ),
    width: Math.round(width),
  }
}

export function buildElementViewBoxPoints(elements, imageWidth, imageHeight) {
  if (!Array.isArray(elements)) return []
  const w = Number(imageWidth)
  const h = Number(imageHeight)
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return []
  const out = []
  for (const el of elements) {
    if (!el || !el.image_rect) continue
    const boxed = clampRect(el.image_rect, w, h)
    if (!boxed) continue
    out.push({
      ref: String(el.ref || ""),
      role: String(el.role || ""),
      name: String(el.name || ""),
      enabled: el.enabled !== false,
      focused: el.focused === true,
      capabilities: Array.isArray(el.capabilities) ? el.capabilities : [],
      rect: el.rect || null,
      x: boxed.x,
      y: boxed.y,
      width: boxed.width,
      height: boxed.height,
    })
  }
  return out
}

export function buildActionPointMarker(actionPoint, imageWidth, imageHeight) {
  if (!actionPoint || typeof actionPoint !== "object") return null
  const w = Number(imageWidth)
  const h = Number(imageHeight)
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return null
  const ix = Number(actionPoint.image_x)
  const iy = Number(actionPoint.image_y)
  if (!Number.isFinite(ix) || !Number.isFinite(iy)) return null
  return {
    x: Math.round(clampNumber(ix, 0, w, 0)),
    y: Math.round(clampNumber(iy, 0, h, 0)),
    action: String(actionPoint.action || "act"),
    ref: actionPoint.ref ? String(actionPoint.ref) : null,
    outcome: String(actionPoint.outcome || "unknown"),
  }
}

export function filterElements(elements, queryText) {
  if (!Array.isArray(elements)) return []
  const needle = String(queryText || "").trim().toLowerCase()
  if (!needle) return elements
  return elements.filter((el) => {
    if (!el || typeof el !== "object") return false
    const caps = Array.isArray(el.capabilities) ? el.capabilities.join(" ") : ""
    const haystack = [
      el.ref,
      el.role,
      el.name,
      el.automation_id,
      el.value_preview,
      caps,
    ]
      .filter((part) => part !== null && part !== undefined)
      .join(" ")
      .toLowerCase()
    return haystack.includes(needle)
  })
}
