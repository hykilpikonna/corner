import { defineConfig, presetWind3, presetAttributify, presetIcons, presetTypography } from "unocss";

export default defineConfig({
  presets: [
    presetWind3(),
    presetAttributify(),
    presetIcons(),
    presetTypography(),
  ]
})
