import { defineConfig } from 'astro/config';
import vue from '@astrojs/vue';
import UnoCSS from 'unocss/astro';
import path from 'path';

const src = path.resolve(import.meta.dirname, 'src');

// https://astro.build/config
export default defineConfig({
  integrations: [
    vue({
      jsx: true,
      // Astro's Vue integration handles the bundler/runtime choice usually.
      // But if we need the full compiler for some components:
      // appEntrypoint: 'src/_app.ts' // We might need this for global setups
    }),
    UnoCSS({
      injectReset: true,
    })
  ],
  vite: {
    resolve: {
      alias: {
        '@': src,
      },
      dedupe: ['vue']
    },
    optimizeDeps: {
      include: ['tg-blog']
    },
    ssr: {
      noExternal: ['tg-blog']
    },
    css: {
      preprocessorOptions: {
        sass: {
          api: 'modern-compiler'
        }
      }
    }
  }
});
