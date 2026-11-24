import { defineExtension } from "@kohakuterrarium/ui-sdk"
import ControlDeckTab from "./src/ControlDeckTab.js"
import HudOverlay from "./src/HudOverlay.js"
import SettingsView from "./src/SettingsView.js"
import { disposeCuStore, initCuStore } from "./src/store.js"

export default defineExtension((kt) => {
  const store = initCuStore(kt)
  kt.registerOverlay("hud", HudOverlay)
  kt.registerTab("deck", ControlDeckTab)
  kt.registerSettingsSection("preferences", SettingsView)
  kt.registerCommand("panic-lock", () => store.triggerPanic("shortcut_command"))
  kt.onDispose(() => {
    disposeCuStore(kt)
  })
})
