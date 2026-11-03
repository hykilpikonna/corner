import { execSync } from 'node:child_process'
import type { Plugin } from 'vite'

const buildTime = new Date().toISOString()
let gitCommit = process.env.GITHUB_SHA?.slice(0, 7)
if (!gitCommit) {
  try {
    gitCommit = execSync('git rev-parse --short HEAD').toString().trim()
  } catch {
    gitCommit = 'dev'
  }
}

function patchTgBlog(): Plugin {
  return {
    name: 'patch-tg-blog',
    enforce: 'pre',
    transform(code, id) {
      if (id.includes('tg-blog') && (id.endsWith('.js') || id.endsWith('.mjs'))) {
        return {
          code: code
            .replace(/console\.log\(`Calculating dimensions for \${[^}]+}\.\.\.`\)/g, 'void 0')
            .replace(/console\.log\("Spoilers initialized\."\)/g, 'void 0')
            .replace(/console\.log\(`Spoiler clicked: \${[^}]+}`\)/g, 'void 0')
            .replace(/console\.log\(v\.value\)/g, 'void 0')
            .replace(/console\.log\(y\.value\)/g, 'void 0')
            .replace(/console\.log\("Hash change",[^)]+\)/g, 'void 0')
            .replace(/console\.log\(e\), te\.value = e;/g, 'te.value = e;')
            .replace(/console\.log\(`Downloading file: \${[^}]+}`\)/g, 'void 0')
            .replace(/loading:\s*"lazy"/g, 'loading: "eager"'),
          map: null,
        }
      }
    },
  }
}

export default defineNuxtConfig({
  srcDir: 'src/',
  ssr: true,

  modules: ['@unocss/nuxt'],
  css: ['@fortawesome/fontawesome-free/css/all.min.css'],

  app: {
    head: {
      htmlAttrs: {lang: 'zh-CN'},
      meta: [
        {charset: 'utf-8'},
        {name: 'viewport', content: 'width=device-width, initial-scale=1'},
        {name: 'description', content: "Aza's Corner"},
      ],
      link: [
        {rel: 'apple-touch-icon', sizes: '180x180', href: '/icons/apple-touch-icon.png'},
        {rel: 'icon', type: 'image/png', sizes: '32x32', href: '/icons/favicon-32x32.png'},
        {rel: 'icon', type: 'image/png', sizes: '16x16', href: '/icons/favicon-16x16.png'},
      ],
    },
  },

  build: {
    transpile: ['vue-i18n'],
  },

  vite: {
    plugins: [patchTgBlog()],
    define: {
      __VUE_PROD_DEVTOOLS__: 'false',
      __BUILD_TIME__: JSON.stringify(buildTime),
      __GIT_COMMIT__: JSON.stringify(gitCommit),
    },
  },

  nitro: {
    replace: {
      __VUE_PROD_DEVTOOLS__: 'false',
      __BUILD_TIME__: JSON.stringify(buildTime),
      __GIT_COMMIT__: JSON.stringify(gitCommit),
    },
    prerender: {
      crawlLinks: true,
      routes: [
        '/',
        '/api/photos.json',
        '/about',
        '/life',
        '/blog',
        '/photo',
        '/others',
        '/kitchen-menu',
        '/friends',
      ],
    },
    hooks: {
      'prerender:generate'(route) {
        if (route.contents && route.fileName?.endsWith('.html')) {
          route.contents = route.contents.replace(
            /<link rel="preload" as="fetch"[^>]*_payload\.json[^>]*>\s*/g,
            '',
          )
        }
      },
    },
  },

  hooks: {
    // Discover /photo/<id> routes from the live manifest so every photo gets
    // prerendered. Failure is fatal: silently shipping a gallery whose new
    // photos all 404 is worse than a red build.
    async 'nitro:config'(nitroConfig) {
      if (nitroConfig.dev) return

      const response = await fetch('https://p.aza.moe/photos')
      if (!response.ok) {
        throw new Error(`Fetching photo manifest for prerendering failed: ${response.status} ${response.statusText}`)
      }
      const photos = await response.json() as Array<{id: string}>
      nitroConfig.prerender ??= {}
      nitroConfig.prerender.routes ??= []
      nitroConfig.prerender.routes.push(...photos.map(photo => `/photo/${photo.id}`))
    },
  },

  compatibilityDate: '2026-08-04',
})
