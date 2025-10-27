import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import UnoCSS from 'unocss/vite';
import path from "path";

const src = path.resolve(__dirname, 'src')

export default defineConfig({
  plugins: [
    vue(), UnoCSS()
  ],
  resolve: {
    alias: {
      '@': src,

      // Runtime compilation
      vue: "vue/dist/vue.esm-bundler.js"
    },
    dedupe: ['vue'],
  },
  server: {
    watch: {
      usePolling: true
    }
  }
});
