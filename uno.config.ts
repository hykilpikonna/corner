import { defineConfig, presetWind3, presetAttributify, presetIcons, presetTypography, presetWebFonts, transformerDirectives } from "unocss";

export default defineConfig({
  presets: [
    presetWind3(),
    presetAttributify(),
    presetIcons(),
    presetTypography(),
  ]
})