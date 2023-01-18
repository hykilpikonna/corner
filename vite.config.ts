import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import path from "path";

const src = path.resolve(__dirname, 'src')

export default defineConfig({
  plugins: [
    vue()
  ],
  resolve: {
    alias: {
      '@': src,

      // Runtime compilation
      vue: "vue/dist/vue.esm-bundler.js"
    },
    dedupe: ['vue'],
  }
});
